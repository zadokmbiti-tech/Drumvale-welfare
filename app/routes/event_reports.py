"""
Event / Case Report flow
  POST   /event-reports/              — member submits a case report
  GET    /event-reports/my            — member views their own reports
  GET    /event-reports/              — admin lists all pending reports
  PATCH  /event-reports/{id}/approve  — admin approves → publishes as event
  PATCH  /event-reports/{id}/reject   — admin rejects with optional reason

  GET    /event-reports/summary                  — stats (existing)
  GET    /event-reports/by-status                — filter by status (existing)
  GET    /event-reports/contributions-summary/{event_id}
  GET    /event-reports/attendance/{event_id}
  GET    /event-reports/monthly-report
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from app.database import get_connection, release_connection
from app.routes.auth import get_current_user, require_admin
from pydantic import BaseModel
from typing import Optional
from datetime import date, timedelta

router = APIRouter()


# ── Pydantic ──────────────────────────────────────────────────────────
class CaseReportCreate(BaseModel):
    title:                str
    event_type:           str                  # bereavement | medical | welfare | other
    description:          Optional[str] = None
    date:                 Optional[str] = None  # YYYY-MM-DD
    affected_member_name: Optional[str] = None  # person affected (free text)


# ── POST  /event-reports/  — member submits ───────────────────────────
@router.post("/", status_code=201)
def submit_case_report(
    body: CaseReportCreate,
    current_user: dict = Depends(get_current_user)
):
    user_id = current_user.get("user_id")
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO case_reports
                (user_id, title, event_type, description, occurrence_date,
                 affected_member_name, status)
            VALUES (%s,%s,%s,%s,%s,%s,'pending')
            RETURNING id, submitted_at
        """, (
            user_id,
            body.title,
            body.event_type,
            body.description,
            body.date or None,
            body.affected_member_name,
        ))
        row = cur.fetchone()
        conn.commit()
        return {
            "message":      "Case report submitted — awaiting committee approval.",
            "id":           row[0],
            "submitted_at": str(row[1]),
        }
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_connection(conn)


# ── GET  /event-reports/my  — member's own reports ───────────────────
@router.get("/my")
def my_case_reports(current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("user_id")
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT id, title, event_type, description,
                   occurrence_date, affected_member_name,
                   status, reject_reason, submitted_at, reviewed_at
            FROM case_reports
            WHERE user_id = %s
            ORDER BY submitted_at DESC
        """, (user_id,))
        rows = cur.fetchall()
    finally:
        cur.close()
        release_connection(conn)

    return [
        {
            "id":                   r[0],
            "title":                r[1],
            "event_type":           r[2],
            "description":          r[3],
            "date":                 str(r[4]) if r[4] else None,
            "affected_member_name": r[5],
            "status":               r[6],
            "reject_reason":        r[7],
            "submitted_at":         str(r[8]) if r[8] else None,
            "reviewed_at":          str(r[9]) if r[9] else None,
        }
        for r in rows
    ]


# ── GET  /event-reports/  — admin: all pending ────────────────────────
@router.get("/")
def list_pending_reports(_=Depends(require_admin)):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT cr.id, cr.title, cr.event_type, cr.description,
                   cr.occurrence_date, cr.affected_member_name,
                   cr.status, cr.submitted_at,
                   u.full_name AS reporter_name,
                   u.phone_number AS reporter_phone
            FROM case_reports cr
            JOIN users u ON u.id = cr.user_id
            WHERE cr.status = 'pending'
            ORDER BY cr.submitted_at ASC
        """)
        rows = cur.fetchall()
    finally:
        cur.close()
        release_connection(conn)

    return [
        {
            "id":                   r[0],
            "title":                r[1],
            "event_type":           r[2],
            "description":          r[3],
            "date":                 str(r[4]) if r[4] else None,
            "affected_member_name": r[5],
            "status":               r[6],
            "submitted_at":         str(r[7]) if r[7] else None,
            "reporter_name":        r[8],
            "reporter_phone":       r[9],
        }
        for r in rows
    ]


# ── PATCH  /event-reports/{id}/approve  ──────────────────────────────
@router.patch("/{report_id}/approve")
def approve_report(report_id: int, current_user: dict = Depends(require_admin)):
    reviewer_id = current_user.get("user_id")
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT user_id, title, event_type, description, occurrence_date, affected_member_name
            FROM case_reports
            WHERE id=%s AND status='pending'
        """, (report_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Report not found or already reviewed.")

        user_id, title, event_type, description, occ_date, affected = row

        # Look up the submitting member's member_id for beneficiary linkage (join via phone)
        cur.execute("""
            SELECT m.id FROM members m
            JOIN users u ON u.phone_number = m.phone_number
            WHERE u.id = %s LIMIT 1
        """, (user_id,))
        member_row = cur.fetchone()
        beneficiary_id = member_row[0] if member_row else None

        # Publish as a proper event
        cur.execute("""
            INSERT INTO events (title, event_type, beneficiary_id, description, target_amount, status, date_raised)
            VALUES (%s, %s, %s, %s, 0, 'open', %s)
            RETURNING id
        """, (
            title,
            event_type,
            beneficiary_id,
            description,
            occ_date or date.today(),
        ))
        event_id = cur.fetchone()[0]

        # Mark report as approved
        cur.execute("""
            UPDATE case_reports
            SET status='approved', reviewed_by=%s, reviewed_at=NOW(), published_event_id=%s
            WHERE id=%s
        """, (reviewer_id, event_id, report_id))

        conn.commit()
        return {"message": "Report approved and published as public event.", "event_id": event_id}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_connection(conn)


# ── PATCH  /event-reports/{id}/reject  ───────────────────────────────
@router.patch("/{report_id}/reject")
def reject_report(
    report_id: int,
    body: dict = None,
    current_user: dict = Depends(require_admin)
):
    reviewer_id = current_user.get("user_id")
    reason = (body or {}).get("reason", "")
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute(
            "SELECT id FROM case_reports WHERE id=%s AND status='pending'",
            (report_id,)
        )
        if not cur.fetchone():
            raise HTTPException(404, "Report not found or already reviewed.")
        cur.execute("""
            UPDATE case_reports
            SET status='rejected', reviewed_by=%s, reviewed_at=NOW(), reject_reason=%s
            WHERE id=%s
        """, (reviewer_id, reason, report_id))
        conn.commit()
        return {"message": "Report rejected."}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        cur.close()
        release_connection(conn)


# ══════════════════════════════════════════════════════════════════════
# EXISTING ANALYTICS ENDPOINTS (kept intact)
# ══════════════════════════════════════════════════════════════════════

@router.get("/summary")
def get_event_reports_summary(_=Depends(get_current_user)):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM events")
        total_events = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM events WHERE status='open'")
        active_events = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM events WHERE status='closed'")
        completed_events = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM events WHERE status='cancelled'")
        cancelled_events = cur.fetchone()[0]
        return {
            "total_events":     total_events,
            "active_events":    active_events,
            "completed_events": completed_events,
            "cancelled_events": cancelled_events,
        }
    finally:
        cur.close()
        release_connection(conn)


@router.get("/by-status")
def get_events_by_status(status: str = Query("open"), _=Depends(get_current_user)):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT id, title, date_raised, description, status, date_raised
            FROM events WHERE status = %s ORDER BY date_raised DESC
        """, (status,))
        rows = cur.fetchall()
        return [
            {"id": r[0], "title": r[1], "date_raised": str(r[2]),
             "description": r[3], "status": r[4], "created_at": str(r[5])}
            for r in rows
        ]
    finally:
        cur.close()
        release_connection(conn)


@router.get("/contributions-summary/{event_id}")
def get_event_contributions_summary(event_id: int, _=Depends(get_current_user)):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT title FROM events WHERE id=%s", (event_id,))
        event = cur.fetchone()
        if not event:
            raise HTTPException(404, "Event not found")
        cur.execute("""
            SELECT COUNT(*), COALESCE(SUM(amount),0), COALESCE(AVG(amount),0),
                   COALESCE(MAX(amount),0), COALESCE(MIN(amount),0)
            FROM event_contributions WHERE event_id = %s
        """, (event_id,))
        stats = cur.fetchone()
        return {
            "event_id": event_id, "title": event[0],
            "total_contributors": stats[0], "total_amount": float(stats[1]),
            "avg_contribution": float(stats[2]), "max_contribution": float(stats[3]),
            "min_contribution": float(stats[4]),
        }
    finally:
        cur.close()
        release_connection(conn)


@router.get("/attendance/{event_id}")
def get_event_attendance(event_id: int, _=Depends(get_current_user)):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM event_contributions WHERE event_id=%s", (event_id,))
        attendance = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM members WHERE status='active'")
        total_members = cur.fetchone()[0]
        return {
            "event_id": event_id, "attendance": attendance,
            "total_members": total_members,
            "attendance_percentage": round((attendance / total_members * 100), 2) if total_members > 0 else 0,
        }
    finally:
        cur.close()
        release_connection(conn)


@router.get("/monthly-report")
def get_monthly_event_report(_=Depends(require_admin)):
    from datetime import datetime
    current_month = datetime.now().strftime("%Y-%m")
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT COUNT(*), COALESCE(SUM(ec.amount), 0)
            FROM events e
            LEFT JOIN event_contributions ec ON e.id = ec.event_id
            WHERE DATE_TRUNC('month', e.date_raised) = %s::DATE
        """, (f"{current_month}-01",))
        result = cur.fetchone()
        return {
            "month":               current_month,
            "events_this_month":   result[0],
            "total_contributions": float(result[1]) if result[1] else 0,
        }
    finally:
        cur.close()
        release_connection(conn)