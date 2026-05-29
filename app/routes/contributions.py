# app/routes/contributions.py
from fastapi import APIRouter, HTTPException, Depends
from app.database import get_connection
from app.models import MonthlyContributionCreate
from app.routes.auth import get_current_user
from app.auth_deps import require_treasurer, require_chairperson

router = APIRouter()


@router.post("/")
def record_contribution(
    data: MonthlyContributionCreate,
    _=Depends(require_treasurer)        # treasurer, chairperson, super_admin
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
        return {"message": "Contribution recorded", "id": new_id, "member": member[1]}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()


@router.get("/")
def list_contributions(
    month: str = "",
    member_id: int = 0,
    _=Depends(get_current_user)         # any logged-in user
):
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
    query += " ORDER BY mc.recorded_at DESC"

    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
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
    conn.close()
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
    conn.close()
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
        cur.execute(
            """SELECT id, member_id, amount, month, payment_date, method, reference, notes
               FROM contributions
               WHERE member_id = (SELECT id FROM members WHERE phone_number=%s)
               ORDER BY payment_date DESC""",
            (current_user["phone_number"],)
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    return [
        {"id": r[0], "member_id": r[1], "amount": float(r[2]),
         "month": r[3], "payment_date": str(r[4]), "method": r[5],
         "reference": r[6], "notes": r[7]}
        for r in rows
    ]

@router.delete("/{contribution_id}")
def delete_contribution(
    contribution_id: int,
    _=Depends(require_chairperson)      # chairperson and super_admin only
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM monthly_contributions WHERE id=%s RETURNING id", (contribution_id,))
    deleted = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="Contribution not found")
    return {"message": "Deleted"}