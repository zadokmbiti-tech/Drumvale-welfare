"""
Password reset flow (admin-assisted OTP via phone):
  POST /reset/request   — generate a 6-digit OTP for a phone number
  POST /reset/confirm   — validate OTP + set new password
"""
from fastapi import APIRouter, HTTPException
from app.database import get_connection, release_connection
from passlib.context import CryptContext
import random, string
from datetime import datetime, timedelta

router = APIRouter(prefix="/reset", tags=["Password Reset"])
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

# In-memory OTP store {phone: (otp, expires_at)}  — fine for this scale
_otp_store: dict = {}


@router.post("/request/")
@limiter.limit("5/minute")  # Prevent abuse of OTP generation
def request_reset(data: dict):
    phone = data.get("phone_number", "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Phone number required")

    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT id, full_name FROM users WHERE phone_number=%s", (phone,))
        row = cur.fetchone()
        if not row:
            # Don't reveal if phone exists — just say OK
            return {"message": "If that number is registered, an OTP has been generated."}
        full_name = row[1]
    finally:
        cur.close()
        release_connection(conn)

    otp = "".join(random.choices(string.digits, k=6))
    _otp_store[phone] = (otp, datetime.now() + timedelta(minutes=15))

    # Return OTP in response (admin will relay to member via WhatsApp/phone)
    # In production you'd send SMS via Africa's Talking here
    return {
        "message": f"OTP generated for {full_name}. Share with member via phone/WhatsApp.",
        "otp": otp,  # Admin sees this and calls/WhatsApps the member
        "expires_in": "15 minutes"
    }


@router.post("/confirm/")
def confirm_reset(data: dict):
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
        hashed = pwd_ctx.hash(new_pass)
        cur.execute("UPDATE users SET hashed_password=%s WHERE phone_number=%s", (hashed, phone))
        conn.commit()
        _otp_store.pop(phone, None)
        return {"message": "Password updated successfully. Please log in with your new password."}
    finally:
        cur.close()
        release_connection(conn)
