"""
Password reset flow:
  POST /reset/request   — member gives phone OR email.
                          * Account has an email on file (and SMTP is set up):
                            a 6-digit OTP is emailed straight to them, valid
                            EMAIL_OTP_MINUTES (default 15).
                          * Otherwise (no email / email failed): the admins are
                            alerted and relay an OTP by phone/WhatsApp, as before.
  POST /reset/confirm   — validate OTP + set new password (phone or email)
The response is identical whether or not an account exists.
"""
from fastapi import APIRouter, HTTPException, Request, Depends
from slowapi import Limiter
from slowapi.util import get_remote_address
from app.database import get_connection, release_connection
from app.routes.auth import require_admin
from app.rate_limit import enforce_rate_limit
from app.email_service import send_otp_email, email_configured
from passlib.context import CryptContext
import secrets, string, hashlib, hmac, os, re
from datetime import datetime, timedelta

limiter = Limiter(key_func=get_remote_address)  # Create a Limiter instance

router = APIRouter(prefix="/reset", tags=["Password Reset"])

# How long an OTP stays valid. The admin relays it by phone/WhatsApp, so it
# needs to outlive a slow reply; set OTP_VALIDITY_HOURS in .env to change it.
OTP_VALIDITY_HOURS = max(1, min(int(os.getenv("OTP_VALIDITY_HOURS", "48")), 48))  # hard cap: 48h
# Wrong guesses allowed per OTP before it is burned. Because the code lives
# for hours, this (not just the per-minute rate limit) caps brute-forcing.
OTP_MAX_ATTEMPTS = 5
# Emailed codes are self-service (no admin in the loop), so they are short-lived.
EMAIL_OTP_MINUTES = max(5, min(int(os.getenv("EMAIL_OTP_MINUTES", "15")), 60))
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _resolve_account(identifier: str):
    """Find an account by phone number or email. Returns
    (phone_number, full_name, email, id_number) or None."""
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    conn = get_connection()
    cur = conn.cursor()
    try:
        if "@" in identifier:
            cur.execute(
                "SELECT phone_number, full_name, email, id_number FROM users WHERE lower(email)=lower(%s) LIMIT 1",
                (identifier,))
        else:
            cur.execute(
                "SELECT phone_number, full_name, email, id_number FROM users WHERE phone_number=%s",
                (identifier,))
        return cur.fetchone()
    finally:
        cur.close()
        release_connection(conn)


def _norm_id(v) -> str:
    return "".join(str(v or "").split()).lower()


def _mask_email(e: str) -> str:
    name, _, dom = e.partition("@")
    return (name[:1] + "***@" + dom) if dom else "***"


def _valid_email(e: str) -> bool:
    return bool(re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+", e)) and len(e) <= 200


def _validity_label() -> str:
    h = OTP_VALIDITY_HOURS
    return f"{h} hour{'s' if h != 1 else ''}"


def _hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.encode()).hexdigest()


def _generate_and_store_otp(phone: str, valid_for: timedelta = None):
    """Shared logic: create a fresh OTP for a phone number and store it in
    the database (not in-process memory — this app can run as short-lived
    serverless invocations with no shared memory between them, and an
    in-memory store would also just vanish on every restart/redeploy).
    Returns (full_name, otp) or (None, None) if no account has that number.
    Only the OTP's hash is persisted, matching how refresh tokens are stored."""
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT id, full_name FROM users WHERE phone_number=%s", (phone,))
        row = cur.fetchone()
        if not row:
            return None, None
        full_name = row[1]

        # Opportunistic cleanup of this phone's stale codes, then invalidate
        # any still-outstanding OTP for this number before issuing a new one.
        cur.execute("DELETE FROM otp_codes WHERE phone_number=%s AND (used OR expires_at < NOW())", (phone,))

        # Only one live OTP per number: burn any earlier unused code.
        cur.execute("UPDATE otp_codes SET used=true WHERE phone_number=%s AND used=false", (phone,))

        otp = "".join(secrets.choice(string.digits) for _ in range(6))
        expires_at = datetime.utcnow() + (valid_for or timedelta(hours=OTP_VALIDITY_HOURS))
        cur.execute(
            "INSERT INTO otp_codes (phone_number, otp_hash, expires_at) VALUES (%s, %s, %s)",
            (phone, _hash_otp(otp), expires_at)
        )
        conn.commit()
    finally:
        cur.close()
        release_connection(conn)
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
    # Inputs (all optional individually, but we need a phone or an email):
    #   phone_number  the member's account phone (or "identifier")
    #   email         where the member wants the code sent
    #   id_number     National ID, required to send the code to an email that
    #                 is NOT already on the account
    ident = str(data.get("identifier") or "").strip()
    phone_in = str(data.get("phone_number") or (ident if "@" not in ident else "")).strip()
    email_in = str(data.get("email") or (ident if "@" in ident else "")).strip()
    id_in = str(data.get("id_number") or "").strip()
    if not phone_in and not email_in:
        raise HTTPException(status_code=400, detail="Phone number or email required")
    if email_in and not _valid_email(email_in):
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")

    enforce_rate_limit(f"otp-request:{get_remote_address(request)}", limit=5, window_seconds=60)

    # Same answer whether or not the account exists, and whether the code
    # went by email or to the admins — so this can't be used to find out
    # who is a member.
    generic = {"message": "If the details match an account, a reset code has been emailed to you, "
                          "or the admin has been notified to share one with you."}

    acct = _resolve_account(phone_in or email_in)
    if not acct:
        return generic
    phone, acct_name, acct_email, acct_id = acct

    # Where may the code go?
    #   1. the email already on the account  -> only its owner can read it
    #   2. any other email the member typed  -> only if they also prove who
    #      they are with the National ID on file. Without this, anyone who
    #      knows a member's phone number could send the code to their own
    #      inbox and take over the account.
    #   Anything else falls back to the admin-relayed OTP below.
    target = None
    if email_in and acct_email and email_in.lower() == acct_email.lower():
        target = acct_email
    elif not email_in and acct_email:
        target = acct_email                      # phone only: use the email on file
    elif email_in and id_in and acct_id:
        try:
            # Each guess at an ID is also a chance of a code landing in the
            # guesser's inbox, so wrong guesses are capped per account.
            enforce_rate_limit(f"otp-id-check:{phone}", limit=5, window_seconds=3600)
            if hmac.compare_digest(_norm_id(id_in).encode(), _norm_id(acct_id).encode()):
                target = email_in
        except HTTPException:
            target = None

    # ── Preferred path: email the OTP ─────────────────────────────────
    if target and email_configured():
        try:
            # Caps email-bombing of one member's inbox.
            enforce_rate_limit(f"otp-email:{phone}", limit=3, window_seconds=900)
        except HTTPException:
            return generic
        _, email_otp = _generate_and_store_otp(phone, timedelta(minutes=EMAIL_OTP_MINUTES))
        if email_otp and send_otp_email(target, acct_name, email_otp, EMAIL_OTP_MINUTES):
            from app.routes.audit import log_action
            from app.notify import notify_admins, notify_member
            different = not (acct_email and target.lower() == acct_email.lower())
            log_action("Password Reset Requested", acct_name,
                       detail=("Reset code emailed to a NEW address (" + _mask_email(target) + "), verified by ID number"
                               if different else "Reset code emailed to the member's email on file"),
                       target=acct_name)
            if different:
                # Warn the real owner (SMS to the account phone) and the admins.
                # No code in either message.
                notify_member(phone, f"A password reset code was sent to {_mask_email(target)}. "
                                     f"If this wasn't you, contact the admin immediately.")
                notify_admins(f"{acct_name} requested a password reset code to a different email "
                              f"({_mask_email(target)}), verified by ID number.")
            return generic
        # Email failed (SMTP down / bad address): fall through to admin relay.

    # ── Fallback: admin-relayed OTP by phone/WhatsApp ─────────────────
    full_name, otp = _generate_and_store_otp(phone)
    if not full_name:
        return generic

    # Record this so admins see it on their dashboard — this is the actual
    # notification the admin needs, separate from generating the OTP itself.
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO password_reset_requests (phone_number, full_name) VALUES (%s, %s)",
            (phone, full_name)
        )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        cur.close()
        release_connection(conn)

    from app.routes.audit import log_action
    log_action("Password Reset Requested", full_name, detail=f"Reset requested for {phone}", target=full_name)

    from app.notify import notify_admins
    notify_admins(f"{full_name} ({phone}) requested a password reset. Open the dashboard to generate their OTP.")

    return generic


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

    # Generating the OTP here is the admin's response to any outstanding
    # self-service request for this phone, so mark it handled.
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """UPDATE password_reset_requests
               SET resolved=true, resolved_by=%s, resolved_at=NOW()
               WHERE phone_number=%s AND resolved=false""",
            (current_user.get("user_id"), phone)
        )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        cur.close()
        release_connection(conn)

    from app.routes.audit import log_action
    log_action("Password Reset Requested (Admin)", full_name, detail=f"OTP generated for {phone}", target=full_name)

    return {
        "message": f"OTP generated for {full_name}. Share with member via phone/WhatsApp.",
        "otp": otp,  # Admin sees this and calls/WhatsApps the member
        "expires_in": _validity_label(),
    }


# Admin-only — powers the "Pending Password Reset Requests" panel on the
# dashboard, so an admin can see at a glance that a member is waiting for
# an OTP, without digging through the audit log.
@router.get("/pending")
def list_pending_resets(current_user: dict = Depends(require_admin)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT id, phone_number, full_name, requested_at
               FROM password_reset_requests
               WHERE resolved=false
               ORDER BY requested_at ASC"""
        )
        rows = cur.fetchall()
        return [
            {"id": r[0], "phone_number": r[1], "full_name": r[2], "requested_at": str(r[3])}
            for r in rows
        ]
    except Exception:
        conn.rollback()
        return []
    finally:
        cur.close()
        release_connection(conn)


# Admin-only — lets an admin dismiss a pending request without generating
# an OTP (e.g. a duplicate request, or they handled it another way).
@router.patch("/pending/{request_id}/dismiss")
def dismiss_pending_reset(request_id: int, current_user: dict = Depends(require_admin)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """UPDATE password_reset_requests
               SET resolved=true, resolved_by=%s, resolved_at=NOW()
               WHERE id=%s RETURNING id""",
            (current_user.get("user_id"), request_id)
        )
        row = cur.fetchone()
        conn.commit()
        if not row:
            raise HTTPException(status_code=404, detail="Request not found")
        return {"message": "Dismissed"}
    finally:
        cur.close()
        release_connection(conn)


@router.post("/confirm/")
@limiter.limit("10/minute")  # Prevent brute-force OTP attempts
def confirm_reset(request: Request, data: dict):
    identifier = str(data.get("identifier") or data.get("phone_number") or data.get("email") or "").strip()
    otp        = str(data.get("otp", "")).strip()
    new_pass   = str(data.get("new_password", "")).strip()

    if not identifier or not otp or not new_pass:
        raise HTTPException(status_code=400, detail="Phone or email, OTP, and new password required")
    if len(new_pass) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    # OTPs are stored against the account's phone number; if the member gave
    # an email, translate it. Unknown accounts fall through to "no OTP found".
    acct  = _resolve_account(identifier)
    phone = acct[0] if acct else identifier

    # DB-backed limit, scoped per phone number rather than per IP — this is
    # what actually stops someone brute-forcing a 6-digit OTP by spreading
    # guesses across many IPs; the IP-based limiter above can't see that.
    enforce_rate_limit(f"otp-confirm:{phone}", limit=10, window_seconds=60)

    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute(
            """SELECT id, otp_hash, expires_at, attempts FROM otp_codes
               WHERE phone_number=%s AND used=false
               ORDER BY created_at DESC LIMIT 1""",
            (phone,)
        )
        stored = cur.fetchone()
        if not stored:
            raise HTTPException(status_code=400, detail="No OTP found for this number. Request a new one.")
        otp_id, otp_hash, expires_at, attempts = stored
        if datetime.utcnow() > expires_at:
            raise HTTPException(status_code=400, detail="OTP has expired. Request a new one.")
        if not hmac.compare_digest(_hash_otp(otp), otp_hash):
            attempts += 1
            # Burn the code once the guess limit is hit; commit before raising
            # so the count actually persists.
            cur.execute("UPDATE otp_codes SET attempts=%s, used=%s WHERE id=%s",
                        (attempts, attempts >= OTP_MAX_ATTEMPTS, otp_id))
            conn.commit()
            if attempts >= OTP_MAX_ATTEMPTS:
                raise HTTPException(status_code=400, detail="Too many wrong attempts. Request a new OTP.")
            raise HTTPException(status_code=400, detail="Invalid OTP")

        cur.execute("SELECT full_name FROM users WHERE phone_number=%s", (phone,))
        name_row = cur.fetchone()
        hashed = pwd_ctx.hash(new_pass)
        cur.execute("UPDATE users SET hashed_password=%s WHERE phone_number=%s", (hashed, phone))
        cur.execute("UPDATE otp_codes SET used=true WHERE id=%s", (otp_id,))
        conn.commit()

        from app.routes.audit import log_action
        actor = name_row[0] if name_row else phone
        log_action("Password Reset Completed", actor, detail="Password changed via OTP flow", target=actor)

        return {"message": "Password updated successfully. Please log in with your new password."}
    finally:
        cur.close()
        release_connection(conn)