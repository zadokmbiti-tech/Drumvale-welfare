"""
Password reset flow (admin-assisted OTP via phone):
  POST /reset/request   — generate a 6-digit OTP for a phone number
  POST /reset/confirm   — validate OTP + set new password
"""
from fastapi import APIRouter, HTTPException, Request, Depends
from slowapi import Limiter
from slowapi.util import get_remote_address
from app.database import get_connection, release_connection
from app.routes.auth import require_admin
from passlib.context import CryptContext
import random, string
from datetime import datetime, timedelta

limiter = Limiter(key_func=get_remote_address)  # Create a Limiter instance

router = APIRouter(prefix="/reset", tags=["Password Reset"])
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

# In-memory OTP store {phone: (otp, expires_at)}  — fine for this scale
_otp_store: dict = {}


def _generate_and_store_otp(phone: str):
    """Shared logic: create a fresh OTP for a phone number and store it.
    Returns (full_name, otp) or (None, None) if no account has that number."""
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT id, full_name FROM users WHERE phone_number=%s", (phone,))
        row = cur.fetchone()
        if not row:
            return None, None
        full_name = row[1]
    finally:
        cur.close()
        release_connection(conn)

    otp = "".join(random.choices(string.digits, k=6))
    _otp_store[phone] = (otp, datetime.now() + timedelta(minutes=15))
    return full_name, otp


# Public — called from member.html's "Forgot password" screen when a member
# requests a reset for themselves. This must stay reachable without login
# (that's the whole point of a forgot-password flow), so it deliberately
# never returns the OTP itself. member.html's own UI already says "the
# admin will share this with you via phone or WhatsApp" — the real OTP the
# member types in comes from an admin using /reset/admin-request below, not
# from this endpoint's response.
#
# SECURITY: this endpoint used to return the OTP directly in its response.
# Since it has no login requirement, that meant anyone who knew (or
# guessed) a member's phone number could read the OTP straight from the
# API and immediately use /reset/confirm to take over that account — no
# admin, no phone call, no WhatsApp needed. The fix is simply to never put
# the OTP in this response; generating it here (so it's ready once the
# admin relays it) is fine, exposing it here was the actual hole.
@router.post("/request/")
@limiter.limit("5/minute")  # Prevent abuse of OTP generation
def request_reset(request: Request, data: dict):
    phone = data.get("phone_number", "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Phone number required")

    full_name, otp = _generate_and_store_otp(phone)
    if not full_name:
        # Don't reveal whether the phone is registered — just say OK either way
        return {"message": "If that number is registered, an admin has been notified."}

    from app.routes.audit import log_action
    log_action("Password Reset Requested", full_name, detail=f"Reset requested for {phone}", target=full_name)

    return {
        "message": "Request received. An admin will share your OTP via phone or WhatsApp.",
        "expires_in": "15 minutes",
    }


# Admin-only — used by the dashboard's password-reset tool. This is the one
# place the OTP is actually returned, because only an authenticated admin
# can see it, and they relay it to the member out of band themselves.
@router.post("/admin-request/")
@limiter.limit("20/minute")
def admin_request_reset(request: Request, data: dict, current_user: dict = Depends(require_admin)):
    phone = data.get("phone_number", "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Phone number required")

    full_name, otp = _generate_and_store_otp(phone)
    if not full_name:
        raise HTTPException(status_code=404, detail="No account found with that phone number.")

    from app.routes.audit import log_action
    log_action("Password Reset Requested (Admin)", full_name, detail=f"OTP generated for {phone}", target=full_name)

    return {
        "message": f"OTP generated for {full_name}. Share with member via phone/WhatsApp.",
        "otp": otp,  # Admin sees this and calls/WhatsApps the member
        "expires_in": "15 minutes"
    }


@router.post("/confirm/")
@limiter.limit("10/minute")  # Prevent brute-force OTP attempts
def confirm_reset(request: Request, data: dict):
    phone    = data.get("phone_number", "").strip()
    otp      = data.get("otp", "").strip()
    new_pass = data.get("new_password", "").strip()

    if not phone or not otp or not new_pass:
        raise HTTPException(status_code=400, detail="Phone, OTP, and new password required")
    if len(new_pass) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    stored = _otp_store.get(phone)
    if not stored:
        raise HTTPException(status_code=400, detail="No OTP found for this number. Request a new one.")
    stored_otp, expires_at = stored
    if datetime.now() > expires_at:
        _otp_store.pop(phone, None)
        raise HTTPException(status_code=400, detail="OTP has expired. Request a new one.")
    if otp != stored_otp:
        raise HTTPException(status_code=400, detail="Invalid OTP")

    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT full_name FROM users WHERE phone_number=%s", (phone,))
        name_row = cur.fetchone()
        hashed = pwd_ctx.hash(new_pass)
        cur.execute("UPDATE users SET hashed_password=%s WHERE phone_number=%s", (hashed, phone))
        conn.commit()
        _otp_store.pop(phone, None)

        from app.routes.audit import log_action
        actor = name_row[0] if name_row else phone
        log_action("Password Reset Completed", actor, detail="Password changed via OTP flow", target=actor)

        return {"message": "Password updated successfully. Please log in with your new password."}
    finally:
        cur.close()
        release_connection(conn)