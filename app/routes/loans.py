from fastapi import APIRouter, HTTPException, Depends
from app.database import get_connection, release_connection
from app.models import LoanCreate, LoanRepayment, LoanStatusUpdate
from app.routes.auth import get_current_user
from app.auth_deps import require_treasurer
from app.utils import safe_db_error          # ← was missing
from datetime import datetime, timedelta

router = APIRouter()


@router.post("/")
def apply_loan(data: LoanCreate, current_user=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, full_name FROM members WHERE id=%s", (data.member_id,))
        member = cur.fetchone()
        if not member:
            raise HTTPException(status_code=404, detail="Member not found")

        if current_user.get("role") == "member":
            cur.execute(
                "SELECT id FROM members WHERE id=%s AND phone_number="
                "(SELECT phone_number FROM users WHERE id=%s)",
                (data.member_id, current_user.get("user_id"))
            )
            if not cur.fetchone():
                raise HTTPException(
                    status_code=403,
                    detail="You can only submit a loan application for yourself"
                )

        total_repayable = round(data.amount * (1 + data.interest_rate / 100), 2)
        due_date = (datetime.utcnow() + timedelta(days=90)).date()

        cur.execute("""
            INSERT INTO loans (member_id, amount, interest_rate, purpose, total_repayable, due_date)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
        """, (data.member_id, data.amount, data.interest_rate,
               data.purpose, total_repayable, due_date))
        loan_id = cur.fetchone()[0]
        conn.commit()

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Loan Applied",
                         detail=f"KES {data.amount} · {data.purpose or 'no purpose given'}",
                         target=member[1])

        return {
            "message": "Loan application submitted",
            "id": loan_id,
            "member": member[1],
            "amount": data.amount,
            "total_repayable": total_repayable,
            "due_date": str(due_date)
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=500, public_msg="Could not complete the request. Please try again.")
    finally:
        cur.close()
        release_connection(conn)


@router.get("/summary")
def loan_summary(_=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE status='pending')   AS pending,
                COUNT(*) FILTER (WHERE status='disbursed') AS active,
                COUNT(*) FILTER (WHERE status='repaid')    AS repaid,
                COALESCE(SUM(amount) FILTER (WHERE status='disbursed'), 0) AS total_outstanding,
                COALESCE(SUM(amount_repaid), 0) AS total_repaid
            FROM loans
        """)
        r = cur.fetchone()
    finally:
        cur.close()
        release_connection(conn)

    return {
        "pending": r[0], "active": r[1], "repaid": r[2],
        "total_outstanding": float(r[3]), "total_repaid": float(r[4])
    }


@router.get("/my")
def my_loans(current_user: dict = Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        # Join users→members via phone_number since members has no user_id FK
        cur.execute(
            """SELECT l.id, l.member_id, l.amount, l.interest_rate, l.status,
                      l.created_at, l.total_repayable, l.due_date, l.amount_repaid
               FROM loans l
               JOIN members m ON l.member_id = m.id
               JOIN users u   ON u.phone_number = m.phone_number
               WHERE u.id = %s
               ORDER BY l.created_at DESC""",
            (current_user["user_id"],)
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        release_connection(conn)

    return [
        {"id": r[0], "member_id": r[1], "amount": float(r[2]),
         "interest_rate": float(r[3]) if r[3] else None, "status": r[4],
         "created_at": str(r[5]), "total_repayable": float(r[6]) if r[6] else None,
         "due_date": str(r[7]) if r[7] else None,
         "amount_repaid": float(r[8]) if r[8] else None}
        for r in rows
    ]


@router.get("")
@router.get("/")
def list_loans(status: str = "", limit: int = 200, offset: int = 0, _=Depends(get_current_user)):
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    conn = get_connection()
    cur = conn.cursor()
    try:
        query = """
            SELECT l.id, m.full_name, l.amount, l.interest_rate, l.total_repayable,
                   l.amount_repaid, l.status, l.purpose, l.created_at, l.due_date
            FROM loans l JOIN members m ON l.member_id = m.id
            WHERE 1=1
        """
        params = []
        if status:
            query += " AND l.status=%s"
            params.append(status)
        query += " ORDER BY l.created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        cur.execute(query, params)
        rows = cur.fetchall()
    finally:
        cur.close()
        release_connection(conn)

    return [
        {
            "id": r[0], "member": r[1], "amount": float(r[2]),
            "interest_rate": float(r[3]), "total_repayable": float(r[4]),
            "amount_repaid": float(r[5]),
            "balance": round(float(r[4]) - float(r[5]), 2),
            "status": r[6], "purpose": r[7],
            "created_at": r[8], "due_date": r[9]
        }
        for r in rows
    ]


@router.get("/{loan_id}")
def get_loan(loan_id: int, _=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT l.*, m.full_name FROM loans l
            JOIN members m ON l.member_id = m.id WHERE l.id=%s
        """, (loan_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Loan not found")

        cur.execute("""
            SELECT amount, payment_method, reference, paid_at
            FROM loan_repayments WHERE loan_id=%s ORDER BY paid_at DESC
        """, (loan_id,))
        repayments = cur.fetchall()
    finally:
        cur.close()
        release_connection(conn)

    return {
        "id": row[0], "member_id": row[1], "member": row[13],
        "amount": float(row[2]), "interest_rate": float(row[3]),
        "purpose": row[4], "status": row[5],
        "total_repayable": float(row[6]) if row[6] else None,
        "amount_repaid": float(row[7]),
        "disbursed_at": row[9], "due_date": row[10], "repaid_at": row[11],
        "repayments": [
            {"amount": float(r[0]), "method": r[1], "ref": r[2], "paid_at": r[3]}
            for r in repayments
        ]
    }


@router.patch("/{loan_id}/status")
def update_loan_status(
    loan_id: int,
    data: LoanStatusUpdate,
    current_user=Depends(require_treasurer)
):
    valid = ("approved", "rejected", "disbursed", "repaid")
    if data.status not in valid:
        raise HTTPException(status_code=400, detail=f"Status must be one of {valid}")

    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT status FROM loans WHERE id=%s", (loan_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Loan not found")

        cur.execute("SELECT m.full_name FROM loans l JOIN members m ON l.member_id=m.id WHERE l.id=%s", (loan_id,))
        member_row = cur.fetchone()

        updates = {"status": data.status}
        if data.status == "disbursed":
            updates["disbursed_at"] = datetime.utcnow()
            updates["approved_by"] = current_user.get("user_id")
        if data.status == "repaid":
            updates["repaid_at"] = datetime.utcnow()

        set_clause = ", ".join(f"{k}=%s" for k in updates)
        cur.execute(
            f"UPDATE loans SET {set_clause} WHERE id=%s",
            (*updates.values(), loan_id)
        )
        conn.commit()

        from app.routes.audit import log_user_action
        action_map = {"approved": "Loan Approved", "rejected": "Loan Rejected",
                      "disbursed": "Loan Disbursed", "repaid": "Loan Marked Repaid"}
        log_user_action(current_user, action_map.get(data.status, f"Loan Status Updated"),
                         detail=f"Status changed from {row[0]} to {data.status}",
                         target=member_row[0] if member_row else f"loan #{loan_id}")

        return {"message": f"Loan status updated to {data.status}"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=500, public_msg="Could not complete the request. Please try again.")
    finally:
        cur.close()
        release_connection(conn)


@router.post("/{loan_id}/repay")
def repay_loan(loan_id: int, data: LoanRepayment, current_user=Depends(require_treasurer)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT status, total_repayable, amount_repaid FROM loans WHERE id=%s",
            (loan_id,)
        )
        loan = cur.fetchone()
        if not loan:
            raise HTTPException(status_code=404, detail="Loan not found")
        if loan[0] != "disbursed":
            raise HTTPException(status_code=400, detail="Loan is not currently active/disbursed")

        cur.execute("SELECT m.full_name FROM loans l JOIN members m ON l.member_id=m.id WHERE l.id=%s", (loan_id,))
        member_row = cur.fetchone()

        cur.execute("""
            INSERT INTO loan_repayments (loan_id, amount, payment_method, reference, notes)
            VALUES (%s, %s, %s, %s, %s)
        """, (loan_id, data.amount, data.payment_method, data.reference, data.notes))

        new_repaid = float(loan[2]) + data.amount
        cur.execute("UPDATE loans SET amount_repaid=%s WHERE id=%s", (new_repaid, loan_id))

        if new_repaid >= float(loan[1]):
            cur.execute(
                "UPDATE loans SET status='repaid', repaid_at=%s WHERE id=%s",
                (datetime.utcnow(), loan_id)
            )

        conn.commit()
        balance = max(0, float(loan[1]) - new_repaid)

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Loan Repayment Recorded",
                         detail=f"KES {data.amount} via {data.payment_method or 'unspecified'}",
                         target=member_row[0] if member_row else f"loan #{loan_id}")

        return {
            "message": "Repayment recorded",
            "amount_paid": data.amount,
            "total_repaid": new_repaid,
            "balance_remaining": balance,
            "fully_repaid": balance == 0
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=500, public_msg="Could not complete the request. Please try again.")
    finally:
        cur.close()
        release_connection(conn)