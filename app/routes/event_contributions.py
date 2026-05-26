# app/routes/event_contributions.py
from fastapi import APIRouter, HTTPException, Depends
from app.database import get_connection, release_connection
from app.models import EventContributionCreate, EventContributionOut, EventContributionSummary
from app.auth_deps import require_treasurer, require_chairperson
from app.routes.auth import get_current_user
from decimal import Decimal

router = APIRouter()


# ── POST /events/{event_id}/contributions ──────────────────────
@router.post("/{event_id}/contributions")
def add_event_contribution(
    event_id: int,
    data: EventContributionCreate,
    _=Depends(require_treasurer)
):
    conn = get_connection()
    cur = conn.cursor()
    try:
        # Check event exists and is open
        cur.execute("SELECT id, title, status FROM events WHERE id=%s", (event_id,))
        event = cur.fetchone()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        if event[2] != "open":
            raise HTTPException(status_code=400, detail="Event is not open for contributions")

        # Check member exists
        cur.execute("SELECT id, full_name FROM members WHERE id=%s", (data.member_id,))
        member = cur.fetchone()
        if not member:
            raise HTTPException(status_code=404, detail="Member not found")

        cur.execute("""
            INSERT INTO event_contributions
                (event_id, member_id, amount, payment_method, reference, notes, recorded_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, recorded_at
        """, (
            event_id,
            data.member_id,
            data.amount,
            "M-Pesa",           # extend schema if needed
            None,
            data.note,
            data.paid_at
        ))
        new_id, recorded_at = cur.fetchone()
        conn.commit()

        return {
            "message": "Contribution recorded",
            "id": new_id,
            "event": event[1],
            "member": member[1],
            "amount": data.amount,
            "recorded_at": recorded_at
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        release_connection(conn)


# ── GET /events/{event_id}/contributions ───────────────────────
@router.get("/{event_id}/contributions")
def get_event_contributions(
    event_id: int,
    _=Depends(get_current_user)
):
    conn = get_connection()
    cur = conn.cursor()
    try:
        # Check event exists
        cur.execute(
            "SELECT id, title, target_amount FROM events WHERE id=%s",
            (event_id,)
        )
        event = cur.fetchone()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")

        # Fetch all contributions with member name
        cur.execute("""
            SELECT ec.id, ec.event_id, ec.member_id, m.full_name,
                   ec.amount, ec.payment_method, ec.reference,
                   ec.notes, ec.recorded_at
            FROM event_contributions ec
            JOIN members m ON ec.member_id = m.id
            WHERE ec.event_id = %s
            ORDER BY ec.recorded_at DESC
        """, (event_id,))
        rows = cur.fetchall()

        # Total raised
        cur.execute("""
            SELECT COALESCE(SUM(amount), 0), COUNT(*)
            FROM event_contributions
            WHERE event_id = %s
        """, (event_id,))
        total_raised, count = cur.fetchone()

        contributions = [
            {
                "id":             r[0],
                "event_id":       r[1],
                "member_id":      r[2],
                "member_name":    r[3],
                "amount":         float(r[4]),
                "payment_method": r[5],
                "reference":      r[6],
                "notes":          r[7],
                "recorded_at":    r[8],
            }
            for r in rows
        ]

        return {
            "event_id":          event[0],
            "event_title":       event[1],
            "target_amount":     float(event[2] or 0),
            "total_raised":      float(total_raised),
            "contributor_count": count,
            "contributions":     contributions
        }
    finally:
        cur.close()
        release_connection(conn)


# ── PUT /events/contributions/{contribution_id} ────────────────
@router.put("/contributions/{contribution_id}")
def update_event_contribution(
    contribution_id: int,
    data: EventContributionCreate,
    _=Depends(require_treasurer)
):
    conn = get_connection()
    cur = conn.cursor()
    try:
        # Check contribution exists
        cur.execute("SELECT id FROM event_contributions WHERE id=%s", (contribution_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Contribution not found")

        # Check member exists
        cur.execute("SELECT id FROM members WHERE id=%s", (data.member_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Member not found")

        cur.execute("""
            UPDATE event_contributions
            SET member_id=%s, amount=%s, notes=%s
            WHERE id=%s RETURNING id
        """, (data.member_id, data.amount, data.note, contribution_id))
        conn.commit()
        return {"message": "Contribution updated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        release_connection(conn)


# ── DELETE /events/contributions/{contribution_id} ─────────────
@router.delete("/contributions/{contribution_id}")
def delete_event_contribution(
    contribution_id: int,
    _=Depends(require_chairperson)
):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM event_contributions WHERE id=%s RETURNING id",
            (contribution_id,)
        )
        deleted = cur.fetchone()
        conn.commit()
        if not deleted:
            raise HTTPException(status_code=404, detail="Contribution not found")
        return {"message": "Contribution deleted"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        release_connection(conn)