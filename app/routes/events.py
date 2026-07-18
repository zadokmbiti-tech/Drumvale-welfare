from fastapi import APIRouter, HTTPException, Depends
from app.database import get_connection, release_connection
from app.models import EventCreate, ContributionCreate
from app.routes.auth import get_current_user
from app.auth_deps import require_secretary, require_chairperson, require_treasurer
from datetime import date
from app.utils import safe_db_error

router = APIRouter()


@router.post("/")
def create_event(
    event: EventCreate,
    current_user=Depends(require_secretary)        # secretary and above can create events
):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO events (title, event_type, beneficiary_id, description, target_amount)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (event.title, event.event_type, event.beneficiary_id,
              event.description, event.target_amount))
        new_id = cur.fetchone()[0]
        conn.commit()

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Event Created",
                         detail=f"{event.event_type} · target KES {event.target_amount}",
                         target=event.title)

        return {"message": "Event raised", "id": new_id}
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=400)
    finally:
        cur.close()
        release_connection(conn)


@router.get("")
@router.get("/")
def list_events(status: str = "", limit: int = 200, offset: int = 0, _=Depends(get_current_user)):
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    conn = get_connection()
    cur = conn.cursor()
    if status:
        cur.execute("""
            SELECT e.id, e.title, e.event_type, m.full_name,
                   e.target_amount, e.status, e.date_raised, e.description
            FROM events e LEFT JOIN members m ON e.beneficiary_id = m.id
            WHERE e.status = %s ORDER BY e.date_raised DESC LIMIT %s OFFSET %s
        """, (status, limit, offset))
    else:
        cur.execute("""
            SELECT e.id, e.title, e.event_type, m.full_name,
                   e.target_amount, e.status, e.date_raised, e.description
            FROM events e LEFT JOIN members m ON e.beneficiary_id = m.id
            ORDER BY e.date_raised DESC LIMIT %s OFFSET %s
        """, (limit, offset))
    rows = cur.fetchall()
    cur.close()
    release_connection(conn)
    return [
        {"id": r[0], "title": r[1], "event_type": r[2], "beneficiary": r[3],
         "target_amount": r[4], "status": r[5],
         "date_raised": str(r[6]) if r[6] else None,
         "date": str(r[6]) if r[6] else None,   # alias for member.html
         "description": r[7]}
        for r in rows
    ]


@router.get("/{event_id}")
def get_event(event_id: int, _=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT e.*, m.full_name,
        COALESCE(SUM(c.amount), 0) as total_raised
        FROM events e
        LEFT JOIN members m ON e.beneficiary_id = m.id
        LEFT JOIN contributions c ON c.event_id = e.id
        WHERE e.id = %s GROUP BY e.id, m.full_name
    """, (event_id,))
    row = cur.fetchone()
    cur.close()
    release_connection(conn)
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    return {
        "id": row[0], "title": row[1], "event_type": row[2],
        "description": row[4], "target_amount": row[5],
        "status": row[6], "date_raised": row[7],
        "beneficiary": row[10], "total_raised": row[11]
    }

@router.get("/my")
def my_events(current_user: dict = Depends(get_current_user)):
    """Return all events — all members can view events."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, title, description, event_date, venue, status FROM events ORDER BY event_date DESC"
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        release_connection(conn)
    return [
        {"id": r[0], "title": r[1], "description": r[2],
         "event_date": str(r[3]), "venue": r[4], "status": r[5]}
        for r in rows
    ]


@router.post("/{event_id}/contribute")
def add_contribution(
    event_id: int,
    contribution: ContributionCreate,
    current_user=Depends(require_treasurer)        # treasurer and above records contributions
):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO contributions (event_id, member_id, amount, payment_method, reference, notes)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
        """, (event_id, contribution.member_id, contribution.amount,
              contribution.payment_method, contribution.reference, contribution.notes))
        new_id = cur.fetchone()[0]
        conn.commit()

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Event Contribution Recorded",
                         detail=f"KES {contribution.amount}",
                         target=f"event #{event_id}")

        return {"message": "Contribution recorded", "id": new_id}
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=400)
    finally:
        cur.close()
        release_connection(conn)


@router.patch("/{event_id}/close")
def close_event(
    event_id: int,
    current_user=Depends(require_chairperson)      # only chairperson/super_admin can close events
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT title FROM events WHERE id=%s", (event_id,))
    existing = cur.fetchone()
    cur.execute("UPDATE events SET status='closed', date_closed=%s WHERE id=%s",
                (date.today(), event_id))
    conn.commit()
    cur.close()
    release_connection(conn)

    from app.routes.audit import log_user_action
    log_user_action(current_user, "Event Closed", detail="Event marked closed",
                     target=existing[0] if existing else f"event #{event_id}")

    return {"message": "Event closed"}