import csv
import io
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from app.database import get_connection, release_connection
from app.routes.auth import require_admin

router = APIRouter(prefix="/audit", tags=["Audit"])


def get_actor_name(cur, current_user: dict) -> str:
    """Resolve a human-readable name for whoever performed an action, from the JWT payload."""
    user_id = current_user.get("user_id") if current_user else None
    if not user_id:
        return "system"
    try:
        cur.execute("SELECT full_name FROM users WHERE id=%s", (user_id,))
        row = cur.fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return f"user #{user_id}"


def log_action(action: str, performed_by: str, detail: str = "", target: str = ""):
    """Record an audit event. Call this from any route that changes data."""
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO audit_log (action, performed_by, detail, target)
            VALUES (%s, %s, %s, %s)
        """, (action, performed_by, detail, target))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        cur.close()
        release_connection(conn)


def log_user_action(current_user: dict, action: str, detail: str = "", target: str = ""):
    """
    Convenience wrapper: resolves the actor's name from the JWT payload and
    records the audit entry, all on its own connection. Use this from routes
    instead of manually calling get_actor_name()+log_action() with the
    request's own cursor — avoids reusing a cursor after its connection
    has already been committed/released.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        actor = get_actor_name(cur, current_user)
        log_action(action, actor, detail=detail, target=target)
    except Exception:
        pass
    finally:
        cur.close()
        release_connection(conn)


@router.get("")
@router.get("/")
def list_audit(
    page: int = Query(1, ge=1),
    limit: int = Query(50, le=200),
    action: str = "",
    _=Depends(require_admin)
):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        offset = (page - 1) * limit
        if action:
            cur.execute("""
                SELECT id, action, performed_by, detail, target, created_at
                FROM audit_log WHERE action ILIKE %s
                ORDER BY created_at DESC LIMIT %s OFFSET %s
            """, (f"%{action}%", limit, offset))
        else:
            cur.execute("""
                SELECT id, action, performed_by, detail, target, created_at
                FROM audit_log ORDER BY created_at DESC LIMIT %s OFFSET %s
            """, (limit, offset))
        rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM audit_log" + (" WHERE action ILIKE %s" if action else ""),
                    (f"%{action}%",) if action else ())
        total = cur.fetchone()[0]
        return {
            "total": total,
            "page": page,
            "limit": limit,
            "logs": [
                {"id": r[0], "action": r[1], "performed_by": r[2],
                 "detail": r[3], "target": r[4], "created_at": str(r[5])}
                for r in rows
            ]
        }
    finally:
        cur.close()
        release_connection(conn)


EXPORT_HEADERS = ["ID", "Time", "Action", "Performed By", "Detail", "Target"]
EXPORT_BATCH = 2000


def _csv_safe(value) -> str:
    """Stop spreadsheet apps from running text such as '=cmd(...)' as a formula."""
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _csv_time(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return "" if value is None else str(value)


@router.get("/export")
def export_audit(
    start: Optional[date] = Query(None, description="First day to include, YYYY-MM-DD"),
    end: Optional[date] = Query(None, description="Last day to include (inclusive), YYYY-MM-DD"),
    action: str = "",
    current_user: dict = Depends(require_admin),
):
    """
    Download the audit log as a CSV file.
    - No dates: every entry.  - start/end: only entries on or between those days.
    - action: optional filter, same as the on-screen list.
    The download itself is recorded in the audit log.
    """
    if start and end and start > end:
        raise HTTPException(status_code=400, detail="The start date must be on or before the end date.")

    filters, params = [], []
    if start:
        filters.append("created_at >= %s")
        params.append(start)
    if end:
        filters.append("created_at < %s")          # end day is inclusive
        params.append(end + timedelta(days=1))
    if action:
        filters.append("action ILIKE %s")
        params.append(f"%{action}%")

    conn = get_connection()
    cur = conn.cursor()
    try:
        where = (" WHERE " + " AND ".join(filters)) if filters else ""
        cur.execute("SELECT COUNT(*) FROM audit_log" + where, tuple(params))
        total = cur.fetchone()[0]
    finally:
        cur.close()
        release_connection(conn)

    if start or end:
        span = f"{start or 'beginning'} to {end or 'today'}"
    else:
        span = "all dates"
    note = f"Exported {total} audit entries ({span})" + (f", action contains '{action}'" if action else "")
    log_user_action(current_user, "Audit Export", detail=note, target="audit_log")

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        buf.write("\ufeff")
        writer.writerow(EXPORT_HEADERS)
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)

        conn = get_connection()
        cur = conn.cursor()
        try:
            last = None
            while True:
                clause, args = list(filters), list(params)
                if last:
                    clause.append("(created_at, id) < (%s, %s)")
                    args.extend(last)
                where = (" WHERE " + " AND ".join(clause)) if clause else ""
                cur.execute(
                    "SELECT id, action, performed_by, detail, target, created_at "
                    "FROM audit_log" + where +
                    " ORDER BY created_at DESC, id DESC LIMIT %s",
                    tuple(args) + (EXPORT_BATCH,),
                )
                rows = cur.fetchall()
                if not rows:
                    break
                for r in rows:
                    writer.writerow([
                        r[0], _csv_time(r[5]), _csv_safe(r[1]),
                        _csv_safe(r[2]), _csv_safe(r[3]), _csv_safe(r[4]),
                    ])
                yield buf.getvalue()
                buf.seek(0); buf.truncate(0)
                last = (rows[-1][5], rows[-1][0])
                if len(rows) < EXPORT_BATCH:
                    break
        finally:
            cur.close()
            try:
                conn.rollback()
            except Exception:
                pass
            release_connection(conn)

    if start or end:
        name = f"audit-log_{start or 'start'}_to_{end or 'today'}.csv"
    else:
        name = f"audit-log_all_{date.today().isoformat()}.csv"

    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Row-Count": str(total),
            "Cache-Control": "no-store",
        },
    )