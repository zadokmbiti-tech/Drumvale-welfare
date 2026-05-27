from fastapi import APIRouter, HTTPException, Depends
from app.database import get_connection
from app.models import MeetingCreate, AttendanceUpdate
from app.routes.auth import get_current_user
from app.auth_deps import require_secretary, require_chairperson
import os, logging

router = APIRouter()
logger = logging.getLogger(__name__)

# ── Africa's Talking SMS helper ───────────────────────────────────────────────
def _normalize_ke_phone(raw: str) -> str | None:
    """Normalize a Kenyan phone number to E.164 (+2547XXXXXXXX)."""
    p = raw.strip().replace(" ", "").replace("-", "")
    if p.startswith("+254") and len(p) == 13:
        return p
    if p.startswith("254") and len(p) == 12:
        return "+" + p
    if p.startswith("07") or p.startswith("01"):
        return "+254" + p[1:]
    return None


def send_sms_broadcast(phones: list[str], message: str) -> dict:
    """
    Send an SMS to a list of Kenyan phone numbers via Africa's Talking.
    Returns a summary dict.  Silently skips if AT credentials are missing.
    """
    at_user = os.getenv("AT_USERNAME")
    at_key  = os.getenv("AT_API_KEY")
    sender  = os.getenv("AT_SENDER_ID", "DRUMVALE")   # registered sender ID

    if not at_user or not at_key:
        logger.warning("Africa's Talking credentials not set — SMS skipped.")
        return {"status": "skipped", "reason": "AT_USERNAME or AT_API_KEY not configured"}

    try:
        import africastalking
        africastalking.initialize(at_user, at_key)
        sms = africastalking.SMS

        # Normalize and deduplicate
        normalized = list({_normalize_ke_phone(p) for p in phones if _normalize_ke_phone(p)})
        if not normalized:
            return {"status": "skipped", "reason": "No valid phone numbers"}

        # AT SDK accepts a list; sends are batched automatically
        response = sms.send(message, normalized, sender_id=sender)
        return {"status": "sent", "recipients": len(normalized), "response": str(response)}
    except ImportError:
        logger.warning("africastalking package not installed — SMS skipped.")
        return {"status": "skipped", "reason": "africastalking not installed"}
    except Exception as e:
        logger.error(f"SMS broadcast failed: {e}")
        return {"status": "error", "reason": str(e)}


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/")
def create_meeting(
    meeting: MeetingCreate,
    current_user=Depends(require_secretary)   # secretary and above can schedule
):
    conn = get_connection()
    cur = conn.cursor()
    try:
        # 1. Save the meeting
        cur.execute("""
            INSERT INTO meetings (title, date, time, venue, agenda)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (meeting.title, meeting.date, meeting.time, meeting.venue, meeting.agenda))
        meeting_id = cur.fetchone()[0]

        # 2. Seed attendance rows for all active members
        cur.execute("SELECT id FROM members WHERE status='active'")
        members = cur.fetchall()
        for m in members:
            cur.execute(
                "INSERT INTO attendance (meeting_id, member_id) VALUES (%s, %s)",
                (meeting_id, m[0])
            )

        # 3. Auto-post a notice for members who are online
        notice_title = f"📅 Meeting Scheduled: {meeting.title}"
        notice_body  = (
            f"A new meeting has been scheduled.\n\n"
            f"📌 Title: {meeting.title}\n"
            f"📅 Date:  {meeting.date}\n"
            f"⏰ Time:  {meeting.time}\n"
            f"📍 Venue: {meeting.venue}\n"
            f"📋 Agenda:\n{meeting.agenda or 'To be announced'}"
        )
        # Ensure notices table exists
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notices (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                priority TEXT DEFAULT 'important',
                created_by TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            INSERT INTO notices (title, body, priority, created_by)
            VALUES (%s, %s, 'important', %s)
        """, (notice_title, notice_body, current_user.get("sub")))

        conn.commit()

        # 4. Fetch all active member phones for SMS (outside transaction — read-only)
        cur.execute(
            "SELECT phone_number FROM members WHERE status='active' AND phone_number IS NOT NULL"
        )
        phones = [r[0] for r in cur.fetchall()]

        # 5. Build SMS (max ~160 chars per segment — keep it tight)
        sms_text = (
            f"[DRUMVALE] Meeting Alert!\n"
            f"Title: {meeting.title}\n"
            f"Date:  {meeting.date}\n"
            f"Time:  {meeting.time}\n"
            f"Venue: {meeting.venue}\n"
            f"Agenda: {(meeting.agenda or 'TBA')[:80]}"
        )
        sms_result = send_sms_broadcast(phones, sms_text)

        return {
            "message": "Meeting created",
            "id": meeting_id,
            "notice_posted": True,
            "sms": sms_result,
        }
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()


@router.get("/")
def list_meetings(_=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, title, date, venue, status FROM meetings ORDER BY date DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {"id": r[0], "title": r[1], "date": r[2], "venue": r[3], "status": r[4]}
        for r in rows
    ]


@router.get("/{meeting_id}")
def get_meeting(meeting_id: int, _=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM meetings WHERE id=%s", (meeting_id,))
    meeting = cur.fetchone()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    cur.execute("""
        SELECT m.id, m.full_name, a.present
        FROM attendance a JOIN members m ON a.member_id = m.id
        WHERE a.meeting_id = %s ORDER BY m.full_name
    """, (meeting_id,))
    attendance = cur.fetchall()
    cur.close()
    conn.close()
    return {
        "id": meeting[0], "title": meeting[1], "date": meeting[2],
        "time": str(meeting[3]), "venue": meeting[4], "agenda": meeting[5],
        "minutes": meeting[6], "status": meeting[7],
        "attendance": [{"id": a[0], "full_name": a[1], "present": a[2]} for a in attendance]
    }


@router.patch("/{meeting_id}/attendance")
def mark_attendance(
    meeting_id: int,
    data: AttendanceUpdate,
    _=Depends(require_secretary)
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE attendance SET present=FALSE WHERE meeting_id=%s", (meeting_id,))
    for member_id in data.member_ids:
        cur.execute(
            "UPDATE attendance SET present=TRUE WHERE meeting_id=%s AND member_id=%s",
            (meeting_id, member_id)
        )
    cur.execute("UPDATE meetings SET status='completed' WHERE id=%s", (meeting_id,))
    conn.commit()
    cur.close()
    conn.close()
    return {"message": "Attendance saved"}


@router.patch("/{meeting_id}/minutes")
def add_minutes(
    meeting_id: int,
    minutes: dict,
    _=Depends(require_secretary)
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE meetings SET minutes=%s WHERE id=%s",
                (minutes.get("minutes"), meeting_id))
    conn.commit()
    cur.close()
    conn.close()
    return {"message": "Minutes saved"}
