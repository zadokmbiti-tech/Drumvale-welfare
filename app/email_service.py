"""
Outgoing email (SMTP) — used to send password-reset OTPs.

    from app.email_service import send_email, email_configured
    ok = send_email("member@example.com", "Subject", "plain text body")

Never raises: a failed or unconfigured email must not crash the request.
send_email() returns True only if the SMTP server accepted the message.

Config (.env):
  SMTP_HOST       e.g. smtp.gmail.com  (empty = email switched off)
  SMTP_PORT       587 (STARTTLS, default) or 465 (implicit SSL)
  SMTP_USER       login for the mailbox
  SMTP_PASSWORD   for Gmail use an App Password, not the normal password
  SMTP_FROM       optional, e.g. "Drumvale Welfare <noreply@drumvale-welfare.com>"
                  (defaults to SMTP_USER)
"""
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

log = logging.getLogger("email_service")


def email_configured() -> bool:
    return bool(os.getenv("SMTP_HOST", "").strip() and os.getenv("SMTP_USER", "").strip()
                and os.getenv("SMTP_PASSWORD", ""))


def send_email(to: str, subject: str, body: str, html: str = None) -> bool:
    try:
        if not email_configured():
            return False
        host = os.getenv("SMTP_HOST").strip()
        port = int(os.getenv("SMTP_PORT", "587"))
        user = os.getenv("SMTP_USER").strip()
        password = os.getenv("SMTP_PASSWORD")
        sender = os.getenv("SMTP_FROM", "").strip() or user

        # Header-injection guard: no CR/LF in anything that goes in a header.
        for v in (to, subject, sender):
            if "\r" in v or "\n" in v:
                log.warning("Refusing to send email: newline in header value")
                return False
        if "@" not in parseaddr(to)[1]:
            return False

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender if "<" in sender else formataddr(("Drumvale Welfare", sender))
        msg["To"] = to
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")

        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=10, context=ctx) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=10) as s:
                s.starttls(context=ctx)
                s.login(user, password)
                s.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001 - email must never break the caller
        log.warning("Email send failed: %s", e)
        return False


def send_otp_email(to: str, full_name: str, otp: str, minutes: int) -> bool:
    import html as _html
    first = _html.escape((full_name or "Member").split()[0])
    subject = "Your Drumvale password reset code"
    text = (
        f"Hello {first},\n\n"
        f"Your password reset code is: {otp}\n\n"
        f"It is valid for {minutes} minutes and can only be used once.\n"
        f"If you did not ask to reset your password, ignore this email — "
        f"your password has not been changed. Never share this code with anyone, "
        f"including group officials.\n\n"
        f"Drumvale Riverview Welfare Group"
    )
    html = (
        f"<div style='font-family:Arial,sans-serif;max-width:480px;margin:auto;color:#222'>"
        f"<h2 style='margin-bottom:4px'>Drumvale Riverview Welfare Group</h2>"
        f"<p>Hello {first},</p>"
        f"<p>Your password reset code is:</p>"
        f"<p style='font-size:32px;letter-spacing:8px;font-weight:700;margin:16px 0'>{otp}</p>"
        f"<p>It is valid for <strong>{minutes} minutes</strong> and can only be used once.</p>"
        f"<p style='color:#666;font-size:13px'>If you did not ask to reset your password, ignore this "
        f"email — your password has not been changed. Never share this code with anyone, "
        f"including group officials.</p></div>"
    )
    return send_email(to, subject, text, html)
