"""
SMS alerts to admins (Africa's Talking), so nobody has to be sitting on the
dashboard to notice a new registration, password reset request, payment, etc.

    from app.notify import notify_admins
    notify_admins("New registration: Jane Doe awaiting approval.")

Safe by design:
  * Never raises. A failed or unconfigured SMS must not break the request that
    triggered it (the member's action has already been committed by then).
  * Capped at ADMIN_SMS_HOURLY_CAP texts per hour overall. Registration and
    password-reset requests are public endpoints, so without a cap someone
    could script them to burn your SMS credit.
  * Never put secrets (OTPs, passwords) in these messages.

Config (.env):
  AT_USERNAME, AT_API_KEY      already used by meeting SMS
  ADMIN_ALERT_PHONES           optional, comma-separated, e.g. 0712345678,0722333444
                               If set, only these numbers are texted. If empty,
                               active users with an alert role (see below) are.
  ADMIN_SMS_ENABLED            "false" to switch alerts off (default on)
  ADMIN_SMS_HOURLY_CAP         default 30
"""
import logging
import os

import requests
from fastapi import HTTPException

from app.database import get_connection, release_connection
from app.rate_limit import enforce_rate_limit

log = logging.getLogger("notify")

ADMIN_ROLES = ("super_admin", "admin")
FINANCE_ROLES = ("super_admin", "admin", "treasurer")


def _fmt(p: str) -> str:
    p = p.strip().replace(" ", "")
    if p.startswith("0"):
        return "+254" + p[1:]
    if p.startswith("254"):
        return "+" + p
    return p


def _recipients(roles) -> list:
    env = os.getenv("ADMIN_ALERT_PHONES", "").strip()
    if env:
        return [p.strip() for p in env.split(",") if p.strip()]
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT DISTINCT phone_number FROM users
               WHERE role = ANY(%s) AND is_active = true
                 AND phone_number IS NOT NULL AND phone_number != ''""",
            (list(roles),),
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        cur.close()
        release_connection(conn)


def user_name(user_id) -> str:
    """Best-effort display name for an alert; never raises."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT full_name FROM users WHERE id=%s", (user_id,))
            row = cur.fetchone()
            return row[0] if row else "A member"
        finally:
            cur.close()
            release_connection(conn)
    except Exception:
        return "A member"


def notify_admins(message: str, roles=ADMIN_ROLES) -> None:
    try:
        if os.getenv("ADMIN_SMS_ENABLED", "true").lower() == "false":
            return
        username = os.getenv("AT_USERNAME", "")
        api_key = os.getenv("AT_API_KEY", "")
        if not username or not api_key:
            return

        try:
            enforce_rate_limit(
                "admin-sms-alerts",
                limit=int(os.getenv("ADMIN_SMS_HOURLY_CAP", "30")),
                window_seconds=3600,
            )
        except HTTPException:
            log.warning("Admin SMS hourly cap reached; alert dropped: %s", message)
            return

        phones = [_fmt(p) for p in _recipients(roles)]
        if not phones:
            return

        url = (
            "https://api.sandbox.africastalking.com/version1/messaging"
            if username == "sandbox"
            else "https://api.africastalking.com/version1/messaging"
        )
        resp = requests.post(
            url,
            data={"username": username, "to": ",".join(phones), "message": f"Drumvale: {message}"[:300]},
            headers={"apikey": api_key, "Accept": "application/json"},
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001 - alerts must never break the caller
        log.warning("Admin SMS alert failed: %s", e)


def notify_member(phone: str, message: str) -> None:
    """Best-effort SMS to one member (e.g. a security warning). Never raises,
    never carries a secret, and is capped per number."""
    try:
        if os.getenv("ADMIN_SMS_ENABLED", "true").lower() == "false":
            return
        username = os.getenv("AT_USERNAME", "")
        api_key = os.getenv("AT_API_KEY", "")
        if not username or not api_key or not phone:
            return
        try:
            enforce_rate_limit(f"member-sms:{phone}", limit=3, window_seconds=3600)
        except HTTPException:
            return
        url = (
            "https://api.sandbox.africastalking.com/version1/messaging"
            if username == "sandbox"
            else "https://api.africastalking.com/version1/messaging"
        )
        resp = requests.post(
            url,
            data={"username": username, "to": _fmt(phone), "message": f"Drumvale: {message}"[:300]},
            headers={"apikey": api_key, "Accept": "application/json"},
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning("Member SMS failed: %s", e)
