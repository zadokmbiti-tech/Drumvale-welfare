from fastapi import APIRouter, HTTPException, Depends
from app.database import get_connection
from app.models import MeetingCreate, AttendanceUpdate
from app.routes.auth import get_current_user
from app.auth_deps import require_secretary, require_chairperson

router = APIRouter()


@router.post("/")
def create_meeting(
    meeting: MeetingCreate,
    _=Depends(require_secretary)        # secretary and above can schedule meetings
):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO meetings (title, date, time, venue, agenda)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (meeting.title, meeting.date, meeting.time, meeting.venue, meeting.agenda))
        meeting_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM members WHERE status='active'")
        members = cur.fetchall()
        for m in members:
            cur.execute(
                "INSERT INTO attendance (meeting_id, member_id) VALUES (%s, %s)",
                (meeting_id, m[0])
            )
        conn.commit()
        return {"message": "Meeting created", "id": meeting_id}
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
    _=Depends(require_secretary)        # secretary marks attendance
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
    _=Depends(require_secretary)        # secretary records minutes
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE meetings SET minutes=%s WHERE id=%s",
                (minutes.get("minutes"), meeting_id))
    conn.commit()
    cur.close()
    conn.close()
    return {"message": "Minutes saved"}