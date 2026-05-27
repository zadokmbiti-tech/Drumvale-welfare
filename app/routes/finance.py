import csv
import io
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from app.database import get_connection
from app.routes.auth import get_current_user
from app.auth_deps import require_treasurer

router = APIRouter()


def _ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS finance (
            id          SERIAL PRIMARY KEY,
            type        TEXT NOT NULL CHECK (type IN ('income','expense')),
            category    TEXT NOT NULL,
            amount      NUMERIC(12,2) NOT NULL,
            description TEXT,
            date        DATE NOT NULL DEFAULT CURRENT_DATE,
            recorded_by TEXT,
            created_at  TIMESTAMP DEFAULT NOW()
        )
    """)


@router.post("/")
def record_transaction(body: dict, current_user=Depends(require_treasurer)):
    t    = body.get("type")
    cat  = body.get("category")
    amt  = body.get("amount")
    desc = body.get("description", "")
    date = body.get("date")

    if t not in ("income", "expense"):
        raise HTTPException(400, "type must be 'income' or 'expense'")
    if not amt or float(amt) <= 0:
        raise HTTPException(400, "amount must be positive")

    conn = get_connection()
    cur  = conn.cursor()
    try:
        _ensure_table(cur)
        cur.execute("""
            INSERT INTO finance (type, category, amount, description, date, recorded_by)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
        """, (t, cat, float(amt), desc, date, current_user.get("sub")))
        new_id = cur.fetchone()[0]
        conn.commit()
        return {"id": new_id, "message": "Transaction recorded"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        cur.close()
        conn.close()


@router.get("/")
def list_transactions(_=Depends(get_current_user)):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        _ensure_table(cur)
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
        conn.close()


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
        _ensure_table(cur)
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

        # Summary rows
        income  = sum(float(r[3]) for r in rows if r[1] == "income")
        expense = sum(float(r[3]) for r in rows if r[1] == "expense")
        net     = income - expense
    finally:
        cur.close()
        conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "type", "category", "amount", "description", "date", "recorded_by", "created_at"])
    writer.writerows(rows)
    writer.writerow([])
    writer.writerow(["", "TOTAL INCOME",  "", f"{income:.2f}"])
    writer.writerow(["", "TOTAL EXPENSE", "", f"{expense:.2f}"])
    writer.writerow(["", "NET BALANCE",   "", f"{net:.2f}"])
    buf.seek(0)

    filename = "finance_report.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.delete("/{record_id}")
def delete_transaction(record_id: int, _=Depends(require_treasurer)):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("DELETE FROM finance WHERE id=%s RETURNING id", (record_id,))
        deleted = cur.fetchone()
        conn.commit()
        if not deleted:
            raise HTTPException(404, "Record not found")
        return {"message": "Deleted"}
    finally:
        cur.close()
        conn.close()