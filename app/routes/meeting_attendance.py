"""
Meeting attendance and minutes
  GET  /meeting-attendance/{meeting_id}        — get attendance register
  POST /meeting-attendance/{meeting_id}/mark   — mark attendance (bulk)
  PUT  /meeting-attendance/{meeting_id}/minutes — save minutes
"""
from fastapi import APIRouter, Depends, HTTPException
from app.database import get_connection, release_connection
from app.routes.auth import get_current_user, require_admin
from app.utils import safe_db_error

router = APIRouter(prefix="/meeting-attendance", tags=["Attendance"])


@router.get("/{meeting_id}")
def get_attendance(meeting_id: int, _=Depends(get_current_user)):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        # Get meeting info
        cur.execute("SELECT id, title, date, venue, minutes, status FROM meetings WHERE id=%s", (meeting_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Meeting not found")
        meeting = {"id": row[0], "title": row[1], "date": str(row[2]),
                   "venue": row[3], "minutes": row[4], "status": row[5]}

        # All active members with their attendance status
        cur.execute("""
            SELECT m.id, m.full_name, m.role, m.phone_number,
                   COALESCE(a.present, false) as present
            FROM members m
            LEFT JOIN attendance a ON a.member_id=m.id AND a.meeting_id=%s
            WHERE m.status='active'
            ORDER BY m.full_name
        """, (meeting_id,))
        members = [{"member_id": r[0], "full_name": r[1], "role": r[2],
                    "phone_number": r[3], "present": r[4]} for r in cur.fetchall()]

        present_count = sum(1 for m in members if m["present"])
        return {
            "meeting": meeting,
            "members": members,
            "present_count": present_count,
            "total_members": len(members),
            "quorum_met": present_count >= max(1, len(members) // 2 + 1)
        }
    finally:
        cur.close()
        release_connection(conn)


@router.post("/{meeting_id}/mark")
def mark_attendance(meeting_id: int, data: dict, current_user=Depends(require_admin)):
    """
    data = {"attendance": [{"member_id": 1, "present": true}, ...]}
    """
    records = data.get("attendance", [])
    conn = get_connection()
    cur  = conn.cursor()
    try:
        for rec in records:
            cur.execute("""
                INSERT INTO attendance (meeting_id, member_id, present)
                VALUES (%s, %s, %s)
                ON CONFLICT (meeting_id, member_id)
                DO UPDATE SET present = EXCLUDED.present
            """, (meeting_id, rec["member_id"], rec.get("present", False)))
        conn.commit()

        from app.routes.audit import log_user_action
        present_count = sum(1 for r in records if r.get("present"))
        log_user_action(current_user, "Meeting Attendance Marked",
                         detail=f"{present_count}/{len(records)} present",
                         target=f"meeting #{meeting_id}")

        return {"message": f"Attendance saved for {len(records)} members"}
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=500)
    finally:
        cur.close()
        release_connection(conn)


@router.put("/{meeting_id}/minutes")
def save_minutes(meeting_id: int, data: dict, current_user=Depends(require_admin)):
    minutes = data.get("minutes", "").strip()
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("UPDATE meetings SET minutes=%s WHERE id=%s", (minutes, meeting_id))
        conn.commit()

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Meeting Minutes Updated",
                         detail="Minutes saved", target=f"meeting #{meeting_id}")

        return {"message": "Minutes saved"}
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=500)
    finally:
        cur.close()
        release_connection(conn)