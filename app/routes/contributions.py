from fastapi import APIRouter, HTTPException, Depends
from app.database import get_connection, release_connection
from app.models import MonthlyContributionCreate
from app.routes.auth import get_current_user
from app.auth_deps import require_treasurer, require_chairperson, require_member
from pydantic import BaseModel
from typing import Optional
from app.utils import safe_db_error

router = APIRouter()


MONTHLY = "Monthly Contribution"
CONTRIBUTION_TYPES = [MONTHLY, "Welfare Registration Fee", "Mchango", "Project Fund"]


def resolve_type(explicit, notes=None):
    """Pick the contribution type: the explicit value if given (must be valid),
    otherwise recover it from the text the member portal puts at the start of the
    notes (keeps older cached portal pages working), otherwise Monthly."""
    if explicit:
        if explicit not in CONTRIBUTION_TYPES:
            raise HTTPException(status_code=400,
                detail="Contribution type must be one of: " + ", ".join(CONTRIBUTION_TYPES))
        return explicit
    head = (notes or "").strip()
    for t in CONTRIBUTION_TYPES[1:]:
        if head.startswith(t):
            return t
    return MONTHLY


class MemberContributionSubmit(BaseModel):
    contribution_type: Optional[str] = None
    amount: float
    month: str                          # YYYY-MM  e.g. "2026-06"
    payment_method: str = "M-Pesa"
    reference: Optional[str] = None    # M-Pesa code
    notes: Optional[str] = None


@router.post("/")
def record_contribution(
    data: MonthlyContributionCreate,
    current_user=Depends(require_treasurer)        # treasurer, chairperson, super_admin
):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, full_name FROM members WHERE id=%s", (data.member_id,))
        member = cur.fetchone()
        if not member:
            raise HTTPException(status_code=404, detail="Member not found")

        ctype = resolve_type(data.contribution_type, data.notes)
        cur.execute("""
            INSERT INTO monthly_contributions
                (member_id, amount, month, payment_method, reference, notes, contribution_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (data.member_id, data.amount, data.month,
              data.payment_method, data.reference, data.notes, ctype))
        new_id = cur.fetchone()[0]
        conn.commit()

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Contribution Created",
                         detail=f"{ctype} · KES {data.amount} · {data.month}",
                         target=member[1])

        return {"message": "Contribution recorded", "id": new_id, "member": member[1],
                "contribution_type": ctype}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=400)
    finally:
        cur.close()
        release_connection(conn)


@router.get("")
@router.get("/")
def list_contributions(
    month: str = "",
    member_id: int = 0,
    limit: int = 200,
    offset: int = 0,
    _=Depends(get_current_user)         # any logged-in user
):
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    conn = get_connection()
    cur = conn.cursor()
    query = """
        SELECT mc.id, m.full_name, mc.amount, mc.month,
               mc.payment_method, mc.reference, mc.recorded_at, mc.contribution_type
        FROM monthly_contributions mc
        JOIN members m ON mc.member_id = m.id
        WHERE mc.status = 'approved'
    """
    params = []
    if month:
        query += " AND mc.month = %s"
        params.append(month)
    if member_id:
        query += " AND mc.member_id = %s"
        params.append(member_id)
    query += " ORDER BY mc.recorded_at DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    release_connection(conn)
    return [
        {"id": r[0], "member": r[1], "amount": float(r[2]),
         "month": r[3], "payment_method": r[4], "reference": r[5], "recorded_at": r[6],
         "contribution_type": r[7]}
        for r in rows
    ]


@router.get("/summary")
def contributions_summary(_=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT month, SUM(amount) as total,
               COUNT(DISTINCT member_id) FILTER (WHERE contribution_type = 'Monthly Contribution') as count
        FROM monthly_contributions
        WHERE month LIKE %s AND status = 'approved'
        GROUP BY month ORDER BY month
    """, (f"{__import__('datetime').date.today().year}-%",))
    rows = cur.fetchall()

    cur.execute("SELECT COUNT(*) FROM members WHERE status='active'")
    total_members = cur.fetchone()[0]

    cur.execute("SELECT COALESCE(SUM(amount),0) FROM monthly_contributions WHERE status = 'approved'")
    all_time_total = cur.fetchone()[0]

    cur.close()
    release_connection(conn)
    return {
        "monthly_breakdown": [
            {"month": r[0], "total": float(r[1]), "count": r[2]} for r in rows
        ],
        "total_members": total_members,
        "all_time_total": float(all_time_total)
    }


@router.get("/status/{month}")
def month_payment_status(month: str, _=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT m.id, m.full_name,
               COALESCE(SUM(mc.amount), 0) as amount,
               COUNT(mc.id) > 0 as paid
        FROM members m
        LEFT JOIN monthly_contributions mc
            ON mc.member_id = m.id AND mc.month = %s AND mc.status = 'approved'
               AND mc.contribution_type = 'Monthly Contribution'
        WHERE m.status = 'active'
        GROUP BY m.id, m.full_name
        ORDER BY m.full_name
    """, (month,))
    rows = cur.fetchall()
    cur.close()
    release_connection(conn)
    return [
        {"member_id": r[0], "full_name": r[1],
         "amount": float(r[2]), "paid": r[3]}
        for r in rows
    ]

@router.get("/my")
def my_contributions(current_user: dict = Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        # monthly_contributions is the correct table; join users→members via phone
        cur.execute(
            """SELECT mc.id, mc.member_id, mc.amount, mc.month,
                      mc.payment_method, mc.reference, mc.notes, mc.recorded_at,
                      mc.status, mc.contribution_type
               FROM monthly_contributions mc
               JOIN members m ON mc.member_id = m.id
               JOIN users u   ON u.phone_number = m.phone_number
               WHERE u.id = %s
               ORDER BY mc.recorded_at DESC""",
            (current_user["user_id"],)
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        release_connection(conn)
    return [
        {"id": r[0], "member_id": r[1], "amount": float(r[2]),
         "month": r[3], "payment_method": r[4], "reference": r[5],
         "notes": r[6], "recorded_at": str(r[7]) if r[7] else None,
         "date": str(r[7]) if r[7] else None,
         "contribution_type": r[9] or MONTHLY,
         "status": r[8] if r[8] else "approved"}   # legacy rows have no status
        for r in rows
    ]


# ── Member self-submission (status = pending, awaits admin approval) ──────────
@router.post("/submit")
def member_submit_contribution(
    data: MemberContributionSubmit,
    current_user: dict = Depends(require_member)
):
    """Any logged-in member can submit their own contribution for admin approval."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        # Resolve this user → members row via shared phone number
        cur.execute(
            "SELECT id FROM members WHERE phone_number = "
            "(SELECT phone_number FROM users WHERE id = %s)",
            (current_user["user_id"],)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail="No member record linked to your account. Contact an admin."
            )
        member_id = row[0]

        ctype = resolve_type(data.contribution_type, data.notes)
        cur.execute("""
            INSERT INTO monthly_contributions
                (member_id, amount, month, payment_method, reference, notes,
                 status, submitted_by, contribution_type)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s)
            RETURNING id
        """, (member_id, data.amount, data.month,
              data.payment_method, data.reference, data.notes,
              current_user["user_id"], ctype))
        new_id = cur.fetchone()[0]
        conn.commit()

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Contribution Submitted",
                         detail=f"{ctype} · KES {data.amount} · {data.month} (awaiting approval)",
                         target=f"member #{member_id}")

        return {
            "message": "Contribution submitted and awaiting admin approval",
            "id": new_id,
            "status": "pending"
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=400)
    finally:
        cur.close()
        release_connection(conn)


# ── Admin approve / reject a pending contribution ────────────────────────────
@router.patch("/{contribution_id}/approve")
def approve_contribution(
    contribution_id: int,
    current_user: dict = Depends(require_treasurer)
):
    """Treasurer or above can approve a pending member-submitted contribution."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, status FROM monthly_contributions WHERE id = %s",
            (contribution_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Contribution not found")
        if row[1] == "approved":
            return {"message": "Already approved"}

        cur.execute(
            "UPDATE monthly_contributions SET status = 'approved' WHERE id = %s",
            (contribution_id,)
        )
        conn.commit()

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Contribution Approved",
                         detail=f"Contribution #{contribution_id} approved",
                         target=f"contribution #{contribution_id}")

        return {"message": "Contribution approved", "id": contribution_id}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=400)
    finally:
        cur.close()
        release_connection(conn)


@router.patch("/{contribution_id}/reject")
def reject_contribution(
    contribution_id: int,
    current_user: dict = Depends(require_treasurer)
):
    """Treasurer or above can reject a pending member-submitted contribution."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE monthly_contributions SET status = 'rejected' WHERE id = %s RETURNING id",
            (contribution_id,)
        )
        row = cur.fetchone()
        conn.commit()
        if not row:
            raise HTTPException(status_code=404, detail="Contribution not found")

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Contribution Rejected",
                         detail=f"Contribution #{contribution_id} rejected",
                         target=f"contribution #{contribution_id}")

        return {"message": "Contribution rejected", "id": contribution_id}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=400)
    finally:
        cur.close()
        release_connection(conn)


# ── Admin: list pending submissions for review ───────────────────────────────
@router.get("/pending")
def list_pending_contributions(_=Depends(require_treasurer)):
    """Returns all contributions awaiting admin approval."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT mc.id, m.full_name, mc.amount, mc.month,
                   mc.payment_method, mc.reference, mc.notes,
                   mc.recorded_at, mc.status, mc.contribution_type
            FROM monthly_contributions mc
            JOIN members m ON mc.member_id = m.id
            WHERE mc.status = 'pending'
            ORDER BY mc.recorded_at ASC
        """)
        rows = cur.fetchall()
    finally:
        cur.close()
        release_connection(conn)
    return [
        {
            "id": r[0], "member": r[1], "amount": float(r[2]),
            "month": r[3], "payment_method": r[4], "reference": r[5],
            "notes": r[6], "recorded_at": str(r[7]), "status": r[8],
            "contribution_type": r[9]
        }
        for r in rows
    ]


@router.delete("/{contribution_id}")
def delete_contribution(
    contribution_id: int,
    current_user=Depends(require_chairperson)      # chairperson and super_admin only
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM monthly_contributions WHERE id=%s RETURNING id", (contribution_id,))
    deleted = cur.fetchone()
    conn.commit()
    cur.close()
    release_connection(conn)
    if not deleted:
        raise HTTPException(status_code=404, detail="Contribution not found")

    from app.routes.audit import log_user_action
    log_user_action(current_user, "Contribution Deleted",
                     detail=f"Contribution #{contribution_id} deleted",
                     target=f"contribution #{contribution_id}")

    return {"message": "Deleted"}

# ── Edit a recorded contribution / per-member payment history ────────────────

import re as _re
from pydantic import Field


class MonthlyContributionUpdate(BaseModel):
    amount: float = Field(gt=0)
    month: str                          # YYYY-MM
    payment_method: str = "M-Pesa"
    reference: Optional[str] = None
    notes: Optional[str] = None
    contribution_type: Optional[str] = None   # None = leave unchanged


@router.get("/member/{member_id}")
def member_payment_history(
    member_id: int,
    _=Depends(require_treasurer)        # treasurer, secretary-and-above payment records
):
    """Every monthly contribution recorded for one member, newest month first."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, full_name, membership_no FROM members WHERE id=%s", (member_id,))
        member = cur.fetchone()
        if not member:
            raise HTTPException(status_code=404, detail="Member not found")

        cur.execute("""
            SELECT id, amount, month, payment_method, reference, notes,
                   COALESCE(status, 'approved'), recorded_at, contribution_type
            FROM monthly_contributions
            WHERE member_id = %s
            ORDER BY month DESC, recorded_at DESC
        """, (member_id,))
        rows = cur.fetchall()
    finally:
        cur.close()
        release_connection(conn)

    payments = [
        {"id": r[0], "amount": float(r[1]), "month": r[2], "payment_method": r[3],
         "reference": r[4], "notes": r[5], "status": r[6], "recorded_at": str(r[7]),
         "contribution_type": r[8]}
        for r in rows
    ]
    return {
        "member": {"id": member[0], "full_name": member[1], "membership_no": member[2]},
        "total_approved": sum(p["amount"] for p in payments if p["status"] == "approved"),
        "payments": payments,
    }


@router.put("/{contribution_id}")
def update_contribution(
    contribution_id: int,
    data: MonthlyContributionUpdate,
    current_user=Depends(require_treasurer)        # treasurer, chairperson, super_admin
):
    """Correct the amount, month, method, M-Pesa code or notes of a recorded payment."""
    if not _re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", data.month or ""):
        raise HTTPException(status_code=400, detail="Month must be in YYYY-MM format")

    reference = (data.reference or "").strip() or None
    notes = (data.notes or "").strip() or None

    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT mc.amount, mc.month, mc.payment_method, mc.reference, m.full_name,
                   mc.contribution_type
            FROM monthly_contributions mc
            JOIN members m ON m.id = mc.member_id
            WHERE mc.id = %s
        """, (contribution_id,))
        old = cur.fetchone()
        if not old:
            raise HTTPException(status_code=404, detail="Contribution not found")

        new_type = resolve_type(data.contribution_type) if data.contribution_type else old[5]
        cur.execute("""
            UPDATE monthly_contributions
            SET amount=%s, month=%s, payment_method=%s, reference=%s, notes=%s, contribution_type=%s
            WHERE id=%s
        """, (data.amount, data.month, data.payment_method, reference, notes, new_type, contribution_id))
        conn.commit()

        changes = []
        if float(old[0]) != float(data.amount):
            changes.append(f"amount KES {float(old[0]):g} → {float(data.amount):g}")
        if old[1] != data.month:
            changes.append(f"month {old[1]} → {data.month}")
        if old[2] != data.payment_method:
            changes.append(f"method {old[2]} → {data.payment_method}")
        if (old[3] or None) != reference:
            changes.append(f"ref {old[3] or '—'} → {reference or '—'}")
        if old[5] != new_type:
            changes.append(f"type {old[5]} → {new_type}")

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Contribution Edited",
                         detail=f"Contribution #{contribution_id}: " + (", ".join(changes) or "notes updated"),
                         target=old[4])

        return {"message": "Contribution updated", "id": contribution_id}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=400)
    finally:
        cur.close()
        release_connection(conn)