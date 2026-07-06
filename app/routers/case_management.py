"""
Case Management — Drumvale
---------------------------
Upgrades the plain "Events" table into full welfare Cases:

  Case No. 42: Mzee Charles Nyamosi Mikuro
  - beneficiary (existing member)
  - amount_per_member (constant, e.g. KES 200 for every member)
  - start_date (can be backdated) -> deadline = start_date + 7 days
  - a case auto-closes once "today" passes the deadline (computed on
    every read — no cron needed, so it works fine on serverless)
  - a roster: every registered member, with BOTH their attendance
    (present / absent / apology / new-member) AND their contribution
    (paid?, amount, date) recorded side by side
  - a computed NOTE describing what the beneficiary can expect, per
    the house rules:
      Absent dominates   -> no one will visit, contribution only
      Apology dominates  -> members send apology + contribution
      Present dominates  -> members visit + hand over contribution
      Present == Apology -> members will visit

Money is stored in the existing `contributions` table (event_id,
member_id, amount, ...) so nothing already recorded is lost.
Attendance is stored in a new `case_attendance` table so a member's
physical presence is tracked independently of their payment.

Run migrations/002_case_management.sql once before using this.
"""

from datetime import date, timedelta
from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.database import get_connection, release_connection
from app.auth_deps import require_treasurer
from app.routes.auth import get_current_user

router = APIRouter(prefix="/cases", tags=["Case Management"])

DEADLINE_DAYS = 7


class AttendanceStatus(str, Enum):
    present = "present"
    absent = "absent"
    apology = "apology"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _auto_close_overdue(cur):
    """Lazily flip status='closed' for any case whose deadline has passed.
    Called at the top of every read so the /events list (legacy) and the
    /cases list always agree, with no background worker required."""
    cur.execute("""
        UPDATE events SET status = 'closed', date_closed = CURRENT_DATE
        WHERE deadline IS NOT NULL AND deadline < CURRENT_DATE AND status = 'open'
    """)


def _own_member_id(cur, current_user) -> Optional[int]:
    """Resolve the members.id row that belongs to the logged-in user, via
    users.phone_number -> members.phone_number. Returns None if this user
    (e.g. an office-only admin account) isn't linked to a member record."""
    user_id = current_user.get("user_id")
    if not user_id:
        return None
    cur.execute("SELECT phone_number FROM users WHERE id=%s", (user_id,))
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    cur.execute("SELECT id FROM members WHERE phone_number=%s", (row[0],))
    m = cur.fetchone()
    return m[0] if m else None


def _compute_note(present: int, absent: int, apology: int) -> str:
    if present == 0 and absent == 0 and apology == 0:
        return "No attendance recorded yet."
    if present == apology and present == max(present, absent, apology) and present > 0:
        return "Members will visit you."
    if absent > present and absent > apology:
        return "No member will visit you, but you will receive contribution only."
    if apology > present and apology > absent:
        return "Members will send Apology too, and you will receive contribution."
    if present > absent and present > apology:
        return "Members will visit you and also handover the contribution."
    return "Attendance is mixed — check the roster for details."


def _case_row_to_dict(row, total_collected=None):
    (id_, title, event_type, beneficiary_id, beneficiary_name_raw, description, target_amount,
     status, date_raised, date_closed, case_no, amount_per_member,
     start_date, deadline, member_full_name) = row
    today = date.today()
    is_open = bool(deadline is None or today <= deadline)
    return {
        "id": id_,
        "case_no": case_no,
        "title": title,
        "description": description,
        "beneficiary_id": beneficiary_id,
        "beneficiary_name": member_full_name or beneficiary_name_raw,
        "amount_per_member": float(amount_per_member) if amount_per_member is not None else None,
        "start_date": str(start_date) if start_date else None,
        "deadline": str(deadline) if deadline else None,
        "days_remaining": (deadline - today).days if (deadline and is_open) else 0,
        "is_open": is_open,
        "status": "open" if is_open else "closed",
        "date_raised": str(date_raised) if date_raised else None,
        "total_collected": float(total_collected) if total_collected is not None else None,
    }


# ---------------------------------------------------------------------------
# 1. Suggest next case number
# ---------------------------------------------------------------------------

@router.get("/reports/attendance")
def attendance_report(current_user=Depends(require_treasurer)):
    """Admin view: how each member's attendance looks across every case
    that has recorded attendance so far."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT m.id, m.full_name,
                   COUNT(*) FILTER (WHERE ca.status = 'present') AS present,
                   COUNT(*) FILTER (WHERE ca.status = 'absent')  AS absent,
                   COUNT(*) FILTER (WHERE ca.status = 'apology') AS apology,
                   COUNT(ca.id) AS total_recorded
            FROM members m
            LEFT JOIN case_attendance ca ON ca.member_id = m.id
            GROUP BY m.id, m.full_name
            ORDER BY m.full_name
        """)
        members_summary = [
            {
                "member_id": r[0], "member_name": r[1],
                "present": r[2], "absent": r[3], "apology": r[4],
                "total_recorded": r[5],
            }
            for r in cur.fetchall()
        ]

        cur.execute("""
            SELECT e.id, e.case_no, e.title, e.start_date,
                   COUNT(*) FILTER (WHERE ca.status = 'present') AS present,
                   COUNT(*) FILTER (WHERE ca.status = 'absent')  AS absent,
                   COUNT(*) FILTER (WHERE ca.status = 'apology') AS apology
            FROM events e
            JOIN case_attendance ca ON ca.event_id = e.id
            WHERE e.case_no IS NOT NULL
            GROUP BY e.id
            ORDER BY e.start_date DESC NULLS LAST, e.id DESC
        """)
        by_case = [
            {
                "case_id": r[0], "case_no": r[1], "title": r[2], "start_date": str(r[3]) if r[3] else None,
                "present": r[4], "absent": r[5], "apology": r[6],
            }
            for r in cur.fetchall()
        ]

        return {"members": members_summary, "cases": by_case}
    finally:
        cur.close()
        release_connection(conn)


@router.get("/next-case-no")
def next_case_no(current_user=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT case_no FROM events WHERE case_no IS NOT NULL")
        existing = [r[0] for r in cur.fetchall()]
        numeric = [int(c) for c in existing if c and c.isdigit()]
        return {"next_case_no": str(max(numeric) + 1) if numeric else "1"}
    finally:
        cur.close()
        release_connection(conn)


# ---------------------------------------------------------------------------
# 2. Create a case
# ---------------------------------------------------------------------------

from pydantic import BaseModel, field_validator


class CaseCreate(BaseModel):
    case_no: str
    title: str
    description: Optional[str] = None
    beneficiary_id: Optional[int] = None
    beneficiary_name: Optional[str] = None
    amount_per_member: Optional[float] = None
    start_date: date

    @field_validator("amount_per_member")
    @classmethod
    def positive_amount(cls, v):
        if v is not None and v <= 0:
            raise ValueError("Amount per member must be greater than zero")
        return v

    @field_validator("beneficiary_name")
    @classmethod
    def need_a_beneficiary(cls, v, info):
        if not info.data.get("beneficiary_id") and not (v and v.strip()):
            raise ValueError("Provide a beneficiary_id or a beneficiary_name")
        return v


@router.post("")
@router.post("/")
def create_case(payload: CaseCreate, current_user=Depends(require_treasurer)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        if payload.beneficiary_id:
            cur.execute("SELECT id FROM members WHERE id=%s", (payload.beneficiary_id,))
            if not cur.fetchone():
                raise HTTPException(400, "Beneficiary member not found")

        cur.execute("SELECT id FROM events WHERE case_no=%s", (payload.case_no,))
        if cur.fetchone():
            raise HTTPException(400, f"Case No. {payload.case_no} already exists")

        deadline = payload.start_date + timedelta(days=DEADLINE_DAYS)

        cur.execute("""
            INSERT INTO events (title, event_type, beneficiary_id, beneficiary_name, description,
                                 target_amount, status, date_raised,
                                 case_no, amount_per_member, start_date, deadline)
            VALUES (%s, 'welfare', %s, %s, %s, %s, 'open', %s, %s, %s, %s, %s)
            RETURNING id
        """, (payload.title, payload.beneficiary_id, payload.beneficiary_name, payload.description,
              payload.amount_per_member, payload.start_date,
              payload.case_no, payload.amount_per_member, payload.start_date, deadline))
        new_id = cur.fetchone()[0]
        conn.commit()

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Case Created",
                         detail=f"Case No. {payload.case_no} · KES {payload.amount_per_member}/member",
                         target=payload.title)

        return {"message": "Case created", "id": new_id, "deadline": str(deadline)}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        cur.close()
        release_connection(conn)


# ---------------------------------------------------------------------------
# 3. List cases  (past / present / future — ordered by start_date)
# ---------------------------------------------------------------------------

@router.get("")
@router.get("/")
def list_cases(current_user=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        _auto_close_overdue(cur)
        conn.commit()

        cur.execute("""
            SELECT e.id, e.title, e.event_type, e.beneficiary_id, e.beneficiary_name, e.description,
                   e.target_amount, e.status, e.date_raised, e.date_closed,
                   e.case_no, e.amount_per_member, e.start_date, e.deadline,
                   m.full_name,
                   COALESCE(SUM(c.amount), 0) AS total_collected
            FROM events e
            LEFT JOIN members m ON e.beneficiary_id = m.id
            LEFT JOIN contributions c ON c.event_id = e.id
            WHERE e.case_no IS NOT NULL
            GROUP BY e.id, m.full_name
            ORDER BY e.start_date DESC NULLS LAST, e.id DESC
        """)
        rows = cur.fetchall()
        return [_case_row_to_dict(r[:-1], total_collected=r[-1]) for r in rows]
    finally:
        cur.close()
        release_connection(conn)


# ---------------------------------------------------------------------------
# 4. Case profile — details + full roster + note
# ---------------------------------------------------------------------------

@router.get("/{case_id}")
def get_case_profile(case_id: int, current_user=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        _auto_close_overdue(cur)
        conn.commit()

        cur.execute("""
            SELECT e.id, e.title, e.event_type, e.beneficiary_id, e.beneficiary_name, e.description,
                   e.target_amount, e.status, e.date_raised, e.date_closed,
                   e.case_no, e.amount_per_member, e.start_date, e.deadline,
                   m.full_name
            FROM events e
            LEFT JOIN members m ON e.beneficiary_id = m.id
            WHERE e.id = %s
        """, (case_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Case not found")

        cur.execute("""
            SELECT m.id, m.full_name
            FROM members m
            ORDER BY m.full_name
        """)
        members = cur.fetchall()

        cur.execute("SELECT member_id, amount, date(recorded_at) FROM contributions WHERE event_id=%s", (case_id,))
        contributions = {r[0]: {"amount": r[1], "date_paid": r[2]} for r in cur.fetchall()}

        cur.execute("SELECT member_id, status, is_new_member FROM case_attendance WHERE event_id=%s", (case_id,))
        attendance = {r[0]: {"status": r[1], "is_new_member": r[2]} for r in cur.fetchall()}

        amount_per_member = row[11]

        # Plain members may see who attended and who's paid, but only their
        # own contribution amount — not what anyone else specifically paid.
        restrict_amounts = current_user.get("role") == "member"
        own_id = _own_member_id(cur, current_user) if restrict_amounts else None

        roster = []
        present = absent = apology = 0
        total_collected = 0
        for member_id, full_name in members:
            c = contributions.get(member_id)
            a = attendance.get(member_id)
            status = a["status"] if a else None
            if status == "present":
                present += 1
            elif status == "absent":
                absent += 1
            elif status == "apology":
                apology += 1
            paid = c is not None
            if paid:
                total_collected += float(c["amount"])

            hide_amount = restrict_amounts and member_id != own_id
            amount_val = None if hide_amount else (
                float(c["amount"]) if c else (float(amount_per_member) if amount_per_member else None)
            )
            date_paid_val = None if hide_amount else (str(c["date_paid"]) if c else None)

            roster.append({
                "member_id": member_id,
                "member_name": full_name,
                "attendance_status": status,
                "is_new_member": bool(a["is_new_member"]) if a else False,
                "paid": paid,
                "amount": amount_val,
                "date_paid": date_paid_val,
            })

        case = _case_row_to_dict(row, total_collected=total_collected)
        case["roster"] = roster
        case["attendance_summary"] = {"present": present, "absent": absent, "apology": apology}
        case["note"] = _compute_note(present, absent, apology)
        return case
    finally:
        cur.close()
        release_connection(conn)


# ---------------------------------------------------------------------------
# 5. Save roster — attendance + contribution for one or more members at once
# ---------------------------------------------------------------------------

class RosterEntry(BaseModel):
    member_id: int
    attendance_status: Optional[AttendanceStatus] = None
    is_new_member: bool = False
    paid: bool = False
    amount: Optional[float] = None
    date_paid: Optional[date] = None
    payment_method: str = "M-Pesa"


@router.post("/{case_id}/roster")
def save_roster(case_id: int, entries: list[RosterEntry], current_user=Depends(require_treasurer)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT amount_per_member FROM events WHERE id=%s", (case_id,))
        case_row = cur.fetchone()
        if not case_row:
            raise HTTPException(404, "Case not found")
        default_amount = case_row[0]

        saved = 0
        skipped_no_amount = 0
        for e in entries:
            if e.attendance_status is not None:
                cur.execute("""
                    INSERT INTO case_attendance (event_id, member_id, status, is_new_member)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (event_id, member_id)
                    DO UPDATE SET status = EXCLUDED.status, is_new_member = EXCLUDED.is_new_member
                """, (case_id, e.member_id, e.attendance_status.value, e.is_new_member))

            if e.paid:
                amount = e.amount if e.amount is not None else default_amount
                if amount is None:
                    # This case has no per-member amount (e.g. funded from the
                    # kitty) and no explicit amount was given for this member —
                    # attendance is still recorded above, we just skip logging
                    # a contribution with no figure attached to it.
                    skipped_no_amount += 1
                    saved += 1
                    continue
                cur.execute(
                    "SELECT id FROM contributions WHERE event_id=%s AND member_id=%s LIMIT 1",
                    (case_id, e.member_id)
                )
                existing = cur.fetchone()
                if existing:
                    if e.date_paid:
                        cur.execute("""
                            UPDATE contributions SET amount=%s, payment_method=%s, recorded_at=%s
                            WHERE id=%s
                        """, (amount, e.payment_method, e.date_paid, existing[0]))
                    else:
                        cur.execute("""
                            UPDATE contributions SET amount=%s, payment_method=%s
                            WHERE id=%s
                        """, (amount, e.payment_method, existing[0]))
                else:
                    if e.date_paid:
                        cur.execute("""
                            INSERT INTO contributions (event_id, member_id, amount, payment_method, recorded_at)
                            VALUES (%s, %s, %s, %s, %s)
                        """, (case_id, e.member_id, amount, e.payment_method, e.date_paid))
                    else:
                        cur.execute("""
                            INSERT INTO contributions (event_id, member_id, amount, payment_method)
                            VALUES (%s, %s, %s, %s)
                        """, (case_id, e.member_id, amount, e.payment_method))
            saved += 1

        conn.commit()

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Case Roster Updated",
                         detail=f"{saved} member row(s) saved", target=f"case #{case_id}")

        return {"saved": saved, "skipped_no_amount": skipped_no_amount}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(400, str(e))
    finally:
        cur.close()
        release_connection(conn)