import logging
import os
from fastapi import HTTPException

logger = logging.getLogger("chamalink")

_DEV = os.getenv("APP_ENV", "development").lower() != "production"


def safe_db_error(e: Exception, *, status: int = 500, public_msg: str = "A server error occurred"):
    """Log the real error; expose detail in dev, hide it in production."""
    logger.error(f"DB error: {e}", exc_info=True)
    detail = str(e) if _DEV else public_msg
    raise HTTPException(status_code=status, detail=detail)
