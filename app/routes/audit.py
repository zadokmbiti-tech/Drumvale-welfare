"""
Audit log — every sensitive action is recorded here.
  GET  /audit                — admin: paginated audit trail
  POST /audit/log            — internal: record an action (called server-side)
"""
from fastapi import APIRouter, Depends, Query
from app.database import get_connection, release_connection
from app.routes.auth import require_admin

router = APIRouter(prefix="/audit", tags=["Audit"])


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
