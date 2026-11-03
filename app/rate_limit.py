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

Raises HTTPException(429) if the limit is exceeded.

Clock note: every timestamp here is UTC, written and compared inside
Postgres. The old version compared Python's datetime.utcnow() against a
column that defaulted to NOW() in the Africa/Nairobi session timezone, so
stored events sat 3 hours "in the future", never aged out of the window,
and a "5 per minute" limit behaved like "5 per 3 hours".

It is one round trip: prune this key's expired rows, count what is left in
the window, and insert the new event only if the key is under its limit.
"""
from fastapi import HTTPException
from app.database import get_connection, release_connection

_SQL = """
WITH cleaned AS (
    DELETE FROM rate_limit_events
    WHERE rl_key = %(key)s
      AND created_at < (NOW() AT TIME ZONE 'UTC') - %(win)s * INTERVAL '1 second'
    RETURNING 1
),
recent AS (
    SELECT COUNT(*) AS n
    FROM rate_limit_events
    WHERE rl_key = %(key)s
      AND created_at >= (NOW() AT TIME ZONE 'UTC') - %(win)s * INTERVAL '1 second'
),
ins AS (
    INSERT INTO rate_limit_events (rl_key, created_at)
    SELECT %(key)s, NOW() AT TIME ZONE 'UTC'
    FROM recent
    WHERE n < %(lim)s
    RETURNING 1
)
SELECT EXISTS (SELECT 1 FROM ins)
"""


def enforce_rate_limit(key: str, limit: int, window_seconds: int):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(_SQL, {"key": key, "win": int(window_seconds), "lim": int(limit)})
        allowed = cur.fetchone()[0]
        conn.commit()           # keeps the cleanup even when rejecting

        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Please wait a moment and try again.",
                headers={"Retry-After": str(int(window_seconds))},
            )
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        # Fail open: a rate-limiter outage should never itself take down
        # login/registration for everyone.
    finally:
        cur.close()
        release_connection(conn)
