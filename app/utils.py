import logging
import re
import os
from fastapi import HTTPException

logger = logging.getLogger("chamalink")

_DEV = os.getenv("APP_ENV", "development").lower() != "production"


def safe_db_error(e: Exception, *, status: int = 500, public_msg: str = "A server error occurred"):
    """Log the real error; expose detail in dev, hide it in production."""
    logger.error(f"DB error: {e}", exc_info=True)
    detail = str(e) if _DEV else public_msg
    raise HTTPException(status_code=status, detail=detail)


# ── Phone-number uniqueness ──────────────────────────────────────────
# The database UNIQUE constraint only blocks byte-identical strings, so
# 0712345678, +254712345678 and 254712345678 would all slip through as
# "different" numbers. We compare on the last 9 digits instead, across both
# the users and members tables.
PHONE_IN_USE_MSG = "This phone number is already registered to another account."


def phone_key(phone) -> str:
    return re.sub(r"\D", "", phone or "")[-9:]


def phone_in_use(cur, phone, own_current_phone=None) -> bool:
    """True if another users/members record already uses this number (any
    format). Pass own_current_phone when editing an existing record so it
    doesn't count as a clash with itself."""
    key = phone_key(phone)
    if not key:
        return False
    for table in ("users", "members"):
        sql = (f"SELECT 1 FROM {table} "
               "WHERE RIGHT(regexp_replace(phone_number, '[^0-9]', '', 'g'), 9) = %s")
        params = [key]
        if own_current_phone:
            sql += " AND phone_number <> %s"
            params.append(own_current_phone)
        cur.execute(sql + " LIMIT 1", params)
        if cur.fetchone():
            return True
    return False
