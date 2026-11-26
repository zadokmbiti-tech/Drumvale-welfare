"""
Signed data-protection consent (Data Protection Act, 2019, Kenya).

Every time a member gives consent, one row goes into member_consents
(append-only history) and the latest values are mirrored onto the users row
so the app can check them cheaply.

  method values:
    self_registration       member ticked + signed on the public form
    admin_added_first_login member was added by an admin and signed the
                            consent themselves on their first login
    policy_resign           existing member signed at a later login because
                            they had no signature on the current policy version

Bump POLICY_VERSION whenever the privacy / data-protection text changes
materially, so you can tell which wording each member agreed to.
"""
from fastapi import HTTPException, Request

POLICY_VERSION = "2026-11"

MIN_SIGNATURE_LEN = 3
MAX_SIGNATURE_LEN = 120


def needs_consent(must_accept_flag, signed_version) -> bool:
    """True if this account has to (re)sign at login: either an admin-added
    account that never signed (flag), or anyone whose last signature is not
    for the CURRENT policy version. That second rule is what makes every
    existing member sign once after this feature goes live, and again
    whenever POLICY_VERSION is bumped."""
    return bool(must_accept_flag) or signed_version != POLICY_VERSION


def clean_signature(raw) -> str:
    """Typed signature (the member's full name). Collapses whitespace and
    rejects anything too short/long to be a real name."""
    sig = " ".join((raw or "").split())
    if len(sig) < MIN_SIGNATURE_LEN:
        raise HTTPException(
            status_code=400,
            detail="Please type your full name as your signature to give consent.",
        )
    if len(sig) > MAX_SIGNATURE_LEN:
        raise HTTPException(status_code=400, detail="Signature is too long.")
    return sig


def client_ip(request: Request) -> str:
    """Best-effort client IP. Behind Vercel the real address is the first
    entry of X-Forwarded-For."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def record_consent(cur, user_id: int, signature: str, method: str, request: Request):
    """Insert the consent record and update the user's current-consent
    columns. Runs on the caller's cursor, so it commits/rolls back together
    with the surrounding change."""
    ip = client_ip(request)
    ua = (request.headers.get("user-agent") or "")[:300]
    cur.execute(
        """INSERT INTO member_consents
               (user_id, policy_version, signature_name, method, ip_address, user_agent)
           VALUES (%s,%s,%s,%s,%s,%s)""",
        (user_id, POLICY_VERSION, signature, method, ip, ua),
    )
    cur.execute(
        """UPDATE users
           SET privacy_accepted        = true,
               privacy_accepted_at     = NOW(),
               must_accept_privacy     = false,
               consent_signature       = %s,
               consent_policy_version  = %s,
               consent_method          = %s,
               consent_ip              = %s
           WHERE id = %s""",
        (signature, POLICY_VERSION, method, ip, user_id),
    )
