from fastapi import APIRouter, HTTPException, Depends
from app.database import get_connection, release_connection
from app.models import MonthlyContributionCreate
from app.routes.auth import get_current_user
from app.auth_deps import require_treasurer, require_chairperson, require_member
from pydantic import BaseModel
from typing import Optional
from app.utils import safe_db_error

router = APIRouter()


class MemberContributionSubmit(BaseModel):
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

        cur.execute("""
            INSERT INTO monthly_contributions
                (member_id, amount, month, payment_method, reference, notes)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (data.member_id, data.amount, data.month,
              data.payment_method, data.reference, data.notes))
        new_id = cur.fetchone()[0]
        conn.commit()

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Contribution Created",
                         detail=f"KES {data.amount} · {data.month}",
                         target=member[1])

        return {"message": "Contribution recorded", "id": new_id, "member": member[1]}
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
               mc.payment_method, mc.reference, mc.recorded_at
        FROM monthly_contributions mc
        JOIN members m ON mc.member_id = m.id
        WHERE 1=1
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
         "month": r[3], "payment_method": r[4], "reference": r[5], "recorded_at": r[6]}
        for r in rows
    ]


@router.get("/summary")
def contributions_summary(_=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT month, SUM(amount) as total, COUNT(*) as count
        FROM monthly_contributions
        WHERE month LIKE %s
        GROUP BY month ORDER BY month
    """, (f"{__import__('datetime').date.today().year}-%",))
    rows = cur.fetchall()

    cur.execute("SELECT COUNT(*) FROM members WHERE status='active'")
    total_members = cur.fetchone()[0]

    cur.execute("SELECT COALESCE(SUM(amount),0) FROM monthly_contributions")
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
               COALESCE(mc.amount, 0) as amount,
               CASE WHEN mc.id IS NOT NULL THEN true ELSE false END as paid
        FROM members m
        LEFT JOIN monthly_contributions mc
            ON mc.member_id = m.id AND mc.month = %s
        WHERE m.status = 'active'
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
                      mc.status
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
         "contribution_type": "monthly",
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

        cur.execute("""
            INSERT INTO monthly_contributions
                (member_id, amount, month, payment_method, reference, notes,
                 status, submitted_by)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s)
            RETURNING id
        """, (member_id, data.amount, data.month,
              data.payment_method, data.reference, data.notes,
              current_user["user_id"]))
        new_id = cur.fetchone()[0]
        conn.commit()

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Contribution Submitted",
                         detail=f"KES {data.amount} · {data.month} (awaiting approval)",
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
                   mc.recorded_at, mc.status
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
            "notes": r[6], "recorded_at": str(r[7]), "status": r[8]
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