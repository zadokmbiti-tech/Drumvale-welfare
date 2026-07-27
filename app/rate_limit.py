"""
DB-backed rate limiting.

The existing @limiter.limit(...) decorators (slowapi) keep counters in
in-process memory. That's fine on a single long-running server, but this
app deploys to Vercel, where each invocation can be a fresh process with
no shared memory — meaning the in-memory limiter can silently do nothing
across cold starts. This module keeps a small, persistent counter table
in Postgres instead, so the limit holds regardless of how many processes
or cold starts are involved.

Usage:
    from app.rate_limit import enforce_rate_limit
    enforce_rate_limit(f"login:{request.client.host}", limit=5, window_seconds=60)

Raises HTTPException(429) if the limit is exceeded. Cheap to call — one
DELETE (opportunistic cleanup of old rows for this key) + one COUNT + one
INSERT, all indexed on (rl_key, created_at).
"""
from fastapi import HTTPException
from app.database import get_connection, release_connection
from datetime import datetime, timedelta


def enforce_rate_limit(key: str, limit: int, window_seconds: int):
    conn = get_connection()
    cur = conn.cursor()
    try:
        window_start = datetime.utcnow() - timedelta(seconds=window_seconds)

        # Opportunistic cleanup: drop this key's events older than the
        # window so the table doesn't grow forever. Cheap since it's
        # scoped to one key and indexed.
        cur.execute(
            "DELETE FROM rate_limit_events WHERE rl_key=%s AND created_at < %s",
            (key, window_start)
        )

        cur.execute(
            "SELECT COUNT(*) FROM rate_limit_events WHERE rl_key=%s AND created_at >= %s",
            (key, window_start)
        )
        count = cur.fetchone()[0]

        if count >= limit:
            conn.commit()  # keep the cleanup above even though we're rejecting
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Please wait a moment and try again."
            )

        cur.execute("INSERT INTO rate_limit_events (rl_key) VALUES (%s)", (key,))
        conn.commit()
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        # Fail open: a rate-limiter outage should never itself take down
        # login/registration for everyone.
    finally:
        cur.close()
        release_connection(conn)
