import csv
import io
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from app.database import get_connection, release_connection
from app.routes.auth import get_current_user
from app.auth_deps import require_treasurer
from app.schemas import FinanceTransactionCreate   # ← Pydantic model, not raw dict
from app.utils import safe_db_error

router = APIRouter()

# NOTE: _ensure_table() removed — finance table is created once at startup in main.py


@router.post("/")
def record_transaction(body: FinanceTransactionCreate, current_user=Depends(require_treasurer)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO finance (type, category, amount, description, date, recorded_by)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
        """, (body.type, body.category, body.amount,
              body.description, body.date, current_user.get("sub")))
        new_id = cur.fetchone()[0]
        conn.commit()

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Finance Transaction Recorded",
                         detail=f"{body.type} · {body.category} · KES {body.amount}",
                         target=body.description or f"transaction #{new_id}")

        return {"id": new_id, "message": "Transaction recorded"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=500, public_msg="Could not record transaction. Please try again.")
    finally:
        cur.close()
        release_connection(conn)


@router.get("/")
def list_transactions(_=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, type, category, amount, description, date::text, recorded_by
            FROM finance ORDER BY date DESC, created_at DESC
        """)
        rows = cur.fetchall()
        return [
            {"id": r[0], "type": r[1], "category": r[2], "amount": float(r[3]),
             "description": r[4], "date": r[5], "recorded_by": r[6]}
            for r in rows
        ]
    finally:
        cur.close()
        release_connection(conn)


@router.get("/report/csv")
def download_finance_csv(
    type: str = "",
    category: str = "",
    date_from: str = "",
    date_to: str = "",
    _=Depends(require_treasurer)
):
    """
    Download finance records as CSV.
    Optional query params: type (income|expense), category, date_from, date_to (YYYY-MM-DD).
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        clauses, params = [], []
        if type in ("income", "expense"):
            clauses.append("type = %s");     params.append(type)
        if category:
            clauses.append("category = %s"); params.append(category)
        if date_from:
            clauses.append("date >= %s");    params.append(date_from)
        if date_to:
            clauses.append("date <= %s");    params.append(date_to)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cur.execute(f"""
            SELECT id, type, category, amount, description, date::text, recorded_by, created_at::text
            FROM finance {where} ORDER BY date DESC, created_at DESC
        """, params)
        rows = cur.fetchall()

        income  = sum(float(r[3]) for r in rows if r[1] == "income")
        expense = sum(float(r[3]) for r in rows if r[1] == "expense")
        net     = income - expense
    finally:
        cur.close()
        release_connection(conn)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "type", "category", "amount", "description", "date", "recorded_by", "created_at"])
    writer.writerows(rows)
    writer.writerow([])
    writer.writerow(["", "TOTAL INCOME",  "", f"{income:.2f}"])
    writer.writerow(["", "TOTAL EXPENSE", "", f"{expense:.2f}"])
    writer.writerow(["", "NET BALANCE",   "", f"{net:.2f}"])
    buf.seek(0)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=finance_report.csv"}
    )


@router.delete("/{record_id}")
def delete_transaction(record_id: int, current_user=Depends(require_treasurer)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT description, amount FROM finance WHERE id=%s", (record_id,))
        existing = cur.fetchone()
        cur.execute("DELETE FROM finance WHERE id=%s RETURNING id", (record_id,))
        deleted = cur.fetchone()
        conn.commit()
        if not deleted:
            raise HTTPException(404, "Record not found")

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Finance Transaction Deleted",
                         detail=f"Amount: KES {existing[1]}" if existing else "",
                         target=(existing[0] if existing and existing[0] else f"transaction #{record_id}"))

        return {"message": "Deleted"}
    finally:
        cur.close()
        release_connection(conn)