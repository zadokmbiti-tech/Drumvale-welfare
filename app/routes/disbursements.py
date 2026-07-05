"""
Welfare disbursement tracking
  GET  /disbursements/event/{event_id}  — get disbursement for an event
  POST /disbursements/event/{event_id}  — record a disbursement
  GET  /disbursements                   — admin: all disbursements
"""
from fastapi import APIRouter, Depends, HTTPException
from app.database import get_connection, release_connection
from app.routes.auth import get_current_user, require_admin
from datetime import date

router = APIRouter(prefix="/disbursements", tags=["Disbursements"])


@router.get("")
@router.get("/")
def list_disbursements(_=Depends(require_admin)):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT d.id, e.title, d.amount, d.recipient_name, d.payment_method,
                   d.reference, d.notes, d.disbursed_by, d.disbursed_at
            FROM disbursements d
            JOIN events e ON e.id = d.event_id
            ORDER BY d.disbursed_at DESC
        """)
        rows = cur.fetchall()
        return [
            {"id": r[0], "event_title": r[1], "amount": float(r[2]),
             "recipient_name": r[3], "payment_method": r[4], "reference": r[5],
             "notes": r[6], "disbursed_by": r[7], "disbursed_at": str(r[8])}
            for r in rows
        ]
    finally:
        cur.close()
        release_connection(conn)


@router.get("/event/{event_id}")
def event_disbursements(event_id: int, _=Depends(get_current_user)):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT title FROM events WHERE id=%s", (event_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Event not found")
        event_title = row[0]

        cur.execute("""
            SELECT id, amount, recipient_name, payment_method, reference, notes, disbursed_by, disbursed_at
            FROM disbursements WHERE event_id=%s ORDER BY disbursed_at DESC
        """, (event_id,))
        disbursements = [
            {"id": r[0], "amount": float(r[1]), "recipient_name": r[2],
             "payment_method": r[3], "reference": r[4], "notes": r[5],
             "disbursed_by": r[6], "disbursed_at": str(r[7])}
            for r in cur.fetchall()
        ]

        cur.execute("SELECT COALESCE(SUM(amount),0) FROM contributions WHERE event_id=%s", (event_id,))
        total_collected = float(cur.fetchone()[0])
        total_disbursed = sum(d["amount"] for d in disbursements)

        return {
            "event_id": event_id,
            "event_title": event_title,
            "total_collected": total_collected,
            "total_disbursed": total_disbursed,
            "balance": total_collected - total_disbursed,
            "disbursements": disbursements,
        }
    finally:
        cur.close()
        release_connection(conn)


@router.post("/event/{event_id}")
def record_disbursement(event_id: int, data: dict, current_user: dict = Depends(require_admin)):
    amount = data.get("amount")
    if not amount or float(amount) <= 0:
        raise HTTPException(status_code=400, detail="Valid amount required")

    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO disbursements
                (event_id, amount, recipient_name, payment_method, reference, notes, disbursed_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (
            event_id,
            float(amount),
            data.get("recipient_name", ""),
            data.get("payment_method", "M-Pesa"),
            data.get("reference", ""),
            data.get("notes", ""),
            current_user.get("full_name", current_user.get("phone_number", "admin")),
        ))
        new_id = cur.fetchone()[0]
        conn.commit()

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Disbursement Recorded",
                         detail=f"KES {amount} via {data.get('payment_method','M-Pesa')}",
                         target=data.get("recipient_name") or f"event #{event_id}")

        return {"id": new_id, "message": "Disbursement recorded"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        release_connection(conn)