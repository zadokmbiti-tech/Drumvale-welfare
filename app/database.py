from dotenv import load_dotenv
load_dotenv()

import os
import threading
import time
from urllib.parse import urlparse

import psycopg2
import psycopg2.extensions
from sqlalchemy import create_engine

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    DB_HOST = os.getenv("DB_HOST")
    DB_PORT = os.getenv("DB_PORT", "5432")
    DB_NAME = os.getenv("DB_NAME")
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

engine = create_engine(DATABASE_URL)


# ──────────────────────────────────────────────────────────────────────
#  Connection reuse
#
#  Every request used to open a brand-new TLS connection to Postgres (and
#  most requests opened two: one in get_current_user, one in the route).
#  The handshake is the slow part, so under a burst the database spent its
#  time accepting connections instead of answering queries.
#
#  This keeps a small per-process stack of idle connections and hands them
#  back out. It is deliberately forgiving:
#    * Nothing is capped. If every idle connection is busy, a fresh one is
#      opened exactly as before, so a burst is never *worse* than the old
#      behaviour and nested get_connection() calls can't deadlock.
#    * release_connection() always rolls back any open transaction, which
#      is what closing the connection used to do implicitly.
#    * Connections that are old, closed, or idle for a while are checked or
#      thrown away, because Neon suspends idle computes and serverless
#      processes get frozen between invocations.
#
#  Tuning (env vars, all optional):
#    DB_POOL_SIZE          idle connections kept per process (default 4, 0 = off)
#    DB_IDLE_PING_SECONDS  ping a connection idle longer than this (default 20)
#    DB_MAX_CONN_AGE       retire connections older than this (default 300)
#    DB_SSLMODE            default "require"
# ──────────────────────────────────────────────────────────────────────
_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "4"))
_IDLE_PING_AFTER = float(os.getenv("DB_IDLE_PING_SECONDS", "20"))
_MAX_AGE = float(os.getenv("DB_MAX_CONN_AGE", "300"))
_SSLMODE = os.getenv("DB_SSLMODE", "require")
_TIMEZONE = "Africa/Nairobi"

_idle = []                      # LIFO stack of idle _PooledConnection objects
_idle_lock = threading.Lock()


class _PooledConnection(psycopg2.extensions.connection):
    """A plain psycopg2 connection can't carry extra attributes; this can."""
    born = 0.0
    last_used = 0.0
    leased = False


def _quiet_close(conn):
    try:
        conn.close()
    except Exception:
        pass


def _new_connection():
    u = urlparse(DATABASE_URL)
    kwargs = dict(
        host=u.hostname,
        port=u.port or 5432,
        dbname=u.path.lstrip("/"),
        user=u.username,
        password=u.password,
        sslmode=_SSLMODE,
        connect_timeout=15,
        keepalives=1,
        connection_factory=_PooledConnection,
    )
    # Neon's pooled endpoint ("-pooler" in the hostname) rejects the
    # `options` startup parameter, so there the timezone is set with a
    # statement instead. See migrations/010 for the server-side default
    # that makes this reliable when using the pooled endpoint.
    via_pooler = "-pooler" in (u.hostname or "")
    if not via_pooler:
        kwargs["options"] = f"-c TimeZone={_TIMEZONE}"

    conn = psycopg2.connect(**kwargs)
    if via_pooler:
        cur = conn.cursor()
        try:
            cur.execute(f"SET TIME ZONE '{_TIMEZONE}'")
            conn.commit()
        finally:
            cur.close()
    conn.born = conn.last_used = time.monotonic()
    return conn


def _ping(conn):
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT 1")
        finally:
            cur.close()
        conn.rollback()
        return True
    except Exception:
        return False


def get_connection():
    while True:
        with _idle_lock:
            conn = _idle.pop() if _idle else None
        if conn is None:
            break
        now = time.monotonic()
        if conn.closed or now - conn.born > _MAX_AGE:
            _quiet_close(conn)
            continue
        if now - conn.last_used > _IDLE_PING_AFTER and not _ping(conn):
            _quiet_close(conn)
            continue
        conn.leased = True
        return conn

    conn = _new_connection()
    conn.leased = True
    return conn


def release_connection(conn):
    if conn is None:
        return
    # Releasing twice is a no-op, so a stray second call can never put the
    # same connection on the idle stack twice.
    if getattr(conn, "leased", None) is False:
        return
    try:
        conn.leased = False
    except AttributeError:
        pass                    # not one of ours — just close it below

    try:
        if conn.closed:
            return
        if conn.info.transaction_status != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
            conn.rollback()
    except Exception:
        _quiet_close(conn)
        return

    if _POOL_SIZE > 0 and isinstance(conn, _PooledConnection):
        conn.last_used = time.monotonic()
        with _idle_lock:
            if len(_idle) < _POOL_SIZE:
                _idle.append(conn)
                return
    _quiet_close(conn)


def init_pool():
    # Kept for backwards compatibility with main.py's startup hook. The
    # idle stack fills itself as requests finish, so there is nothing to do.
    pass
