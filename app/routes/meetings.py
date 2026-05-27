import os
import requests
from fastapi import APIRouter, HTTPException, Depends
from app.database import get_connection
from app.routes.auth import get_current_user
from app.auth_deps import require_secretary

router = APIRouter()


# ─── Africa's Talking SMS ────────────────────────────────────────────────────

AT_USERNAME  = os.getenv("AT_USERNAME", "")
AT_API_KEY   = os.getenv("AT_API_KEY", "")
AT_SENDER_ID = os.getenv("AT_SENDER_ID", "")   # e.g. "Drumvale" — optional

def _send_sms(phones: list[str], message: str) -> dict:
    """
    Send an SMS to a list of phone numbers via Africa's Talking.
    Returns a dict with status, recipients count, and any error reason.
    """
    if not AT_USERNAME or not AT_API_KEY:
        return {"status": "skipped", "reason": "AT credentials not configured"}
    if not phones:
        return {"status": "skipped", "reason": "No active members with phone numbers"}

    # AT expects E.164 format: +2547XXXXXXXX
    def _fmt(p: str) -> str:
        p = p.strip().replace(" ", "")
        if p.startswith("0"):
            return "+254" + p[1:]
        if p.startswith("254") and not p.startswith("+"):
            return "+" + p
        return p

    formatted = [_fmt(p) for p in phones if p]
    recipients_str = ",".join(formatted)

    payload = {
        "username": AT_USERNAME,
        "to":       recipients_str,
        "message":  message,
    }
    if AT_SENDER_ID:
        payload["from"] = AT_SENDER_ID

    # Use sandbox endpoint during testing; switch to production when ready
    sandbox = AT_USERNAME == "sandbox"
    url = (
        "https://api.sandbox.africastalking.com/version1/messaging"
        if sandbox else
        "https://api.africastalking.com/version1/messaging"
    )

    try:
        resp = requests.post(
            url,
            data=payload,
            headers={
                "apiKey": AT_API_KEY,
                "Accept": "application/json",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        # AT returns SMSMessageData.Recipients list
        sent = data.get("SMSMessageData", {}).get("Recipients", [])
        success = [r for r in sent if r.get("status") == "Success"]
        return {
            "status":     "sent",
            "recipients": len(success),
            "total":      len(formatted),
        }
    except requests.RequestException as e:
        return {"status": "error", "reason": str(e)}


def _get_all_active_phones(cur) -> list[str]:
    cur.execute(
        "SELECT phone_number FROM members "
        "WHERE status='active' AND phone_number IS NOT NULL AND phone_number != ''"
    )
    return [r[0] for r in cur.fetchall()]


# ─── Table setup ─────────────────────────────────────────────────────────────

def _ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS meetings (
            id         SERIAL PRIMARY KEY,
            title      TEXT NOT NULL,
            date       DATE NOT NULL,
            time       TEXT,
            venue      TEXT,
            agenda     TEXT,
            status     TEXT NOT NULL DEFAULT 'scheduled'
                           CHECK (status IN ('scheduled','completed','cancelled')),
            created_by TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS meeting_attendance (
            id         SERIAL PRIMARY KEY,
            meeting_id INTEGER REFERENCES meetings(id) ON DELETE CASCADE,
            member_id  INTEGER REFERENCES members(id)  ON DELETE CASCADE,
            present    BOOLEAN DEFAULT FALSE,
            UNIQUE (meeting_id, member_id)
        )
    """)


# ─── Routes ──────────────────────────────────────────────────────────────────

@router.post("/")
def schedule_meeting(body: dict, current_user=Depends(require_secretary)):
    title  = (body.get("title") or "").strip()
    date   = body.get("date")
    time   = body.get("time", "")
    venue  = (body.get("venue") or "").strip()
    agenda = (body.get("agenda") or "").strip() or None

    if not title: raise HTTPException(400, "title is required")
    if not date:  raise HTTPException(400, "date is required")

    conn = get_connection()
    cur  = conn.cursor()
    try:
        _ensure_table(cur)
        cur.execute("""
            INSERT INTO meetings (title, date, time, venue, agenda, created_by)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
        """, (title, date, time, venue, agenda, current_user.get("sub")))
        meeting_id = cur.fetchone()[0]

        # Post a notice automatically
        try:
            notice_body = (
                f"📅 Meeting Scheduled\n\n"
                f"Title: {title}\n"
                f"Date: {date}{' at ' + time if time else ''}\n"
                f"Venue: {venue or 'TBD'}\n"
                + (f"\nAgenda:\n{agenda}" if agenda else "")
            )
            cur.execute("""
                CREATE TABLE IF NOT EXISTS notices (
                    id         SERIAL PRIMARY KEY,
                    title      TEXT NOT NULL,
                    body       TEXT,
                    posted_by  TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute(
                "INSERT INTO notices (title, body, posted_by) VALUES (%s, %s, %s)",
                (f"Meeting: {title}", notice_body, current_user.get("sub"))
            )
        except Exception:
            pass  # notice failure shouldn't block meeting creation

        conn.commit()

        # ── Send SMS to all active members ──────────────────────────────────
        phones = _get_all_active_phones(cur)
        sms_message = (
            f"📅 DRUMVALE MEETING\n"
            f"{title}\n"
            f"Date: {date}{' at ' + time if time else ''}\n"
            f"Venue: {venue or 'TBD'}\n"
            "Please attend. Drumvale Welfare."
        )
        sms_result = _send_sms(phones, sms_message)

        return {
            "id":      meeting_id,
            "message": "Meeting scheduled",
            "sms":     sms_result,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        cur.close()
        conn.close()


@router.get("/")
def list_meetings(_=Depends(get_current_user)):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        _ensure_table(cur)
        cur.execute("""
            SELECT id, title, date::text, time, venue, status, created_at::text
            FROM meetings ORDER BY date DESC, created_at DESC
        """)
        rows = cur.fetchall()
        return [
            {"id": r[0], "title": r[1], "date": r[2], "time": r[3],
             "venue": r[4], "status": r[5], "created_at": r[6]}
            for r in rows
        ]
    finally:
        cur.close()
        conn.close()


@router.get("/{meeting_id}")
def get_meeting(meeting_id: int, _=Depends(get_current_user)):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT id, title, date::text, time, venue, agenda, status FROM meetings WHERE id=%s", (meeting_id,))
        row = cur.fetchone()
        if not row: raise HTTPException(404, "Meeting not found")
        cur.execute("""
            SELECT ma.member_id, m.full_name, ma.present
            FROM meeting_attendance ma
            JOIN members m ON m.id = ma.member_id
            WHERE ma.meeting_id = %s
        """, (meeting_id,))
        attendance = [{"member_id": r[0], "name": r[1], "present": r[2]} for r in cur.fetchall()]
        return {
            "id": row[0], "title": row[1], "date": row[2], "time": row[3],
            "venue": row[4], "agenda": row[5], "status": row[6],
            "attendance": attendance
        }
    finally:
        cur.close()
        conn.close()


@router.patch("/{meeting_id}/status")
def update_meeting_status(meeting_id: int, body: dict, _=Depends(require_secretary)):
    valid = ("scheduled", "completed", "cancelled")
    status = body.get("status")
    if status not in valid:
        raise HTTPException(400, f"status must be one of {valid}")
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("UPDATE meetings SET status=%s WHERE id=%s RETURNING id", (status, meeting_id))
        if not cur.fetchone(): raise HTTPException(404, "Meeting not found")
        conn.commit()
        return {"message": f"Meeting marked as {status}"}
    finally:
        cur.close()
        conn.close()


@router.post("/{meeting_id}/sms")
def resend_meeting_sms(meeting_id: int, _=Depends(require_secretary)):
    """Manually resend the meeting SMS to all active members."""
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute(
            "SELECT title, date::text, time, venue FROM meetings WHERE id=%s",
            (meeting_id,)
        )
        row = cur.fetchone()
        if not row: raise HTTPException(404, "Meeting not found")
        title, date, time, venue = row
        phones = _get_all_active_phones(cur)
        sms_message = (
            f"📅 DRUMVALE MEETING REMINDER\n"
            f"{title}\n"
            f"Date: {date}{' at ' + time if time else ''}\n"
            f"Venue: {venue or 'TBD'}\n"
            "Drumvale Welfare."
        )
        result = _send_sms(phones, sms_message)
        return result
    finally:
        cur.close()
        conn.close()