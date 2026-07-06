from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from app.database import get_connection, release_connection
from app.models import UserRegister, UserLogin, TokenResponse
from app.utils import safe_db_error
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
import os
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)


# Update /login — add Request param and decorator


router = APIRouter()

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY is not set in your .env file. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def require_admin(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") not in ("super_admin", "admin", "chairperson", "secretary", "treasurer"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


# ------------------------------------------------------------------ #
#  REGISTER
# ------------------------------------------------------------------ #
@router.post("/register", status_code=201)
@limiter.limit("3/minute")
def register(request: Request, data: UserRegister):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM users WHERE phone_number=%s", (data.phone_number,))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="Phone number already registered")

        if data.id_number:
            cur.execute("SELECT id FROM users WHERE id_number=%s", (data.id_number,))
            if cur.fetchone():
                raise HTTPException(status_code=400, detail="ID number already registered")

        if data.email:
            cur.execute("SELECT id FROM users WHERE email=%s", (data.email,))
            if cur.fetchone():
                raise HTTPException(status_code=400, detail="Email already registered")

        hashed = hash_password(data.password)

        # 1. Insert into users (source of truth for all profile fields)
        cur.execute("""
            INSERT INTO users (
                full_name, phone_number, email, id_number, hashed_password,
                date_of_birth, marital_status, residence, court, house_number,
                spouse_name, next_of_kin_name, next_of_kin_phone,
                next_of_kin_2, nok2_phone
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            data.full_name, data.phone_number, data.email, data.id_number, hashed,
            data.date_of_birth, data.marital_status, data.residence, data.court,
            data.house_number, data.spouse_name, data.next_of_kin_name,
            data.next_of_kin_phone, data.next_of_kin_2, data.nok2_phone
        ))
        new_user_id = cur.fetchone()[0]

        # 2. Insert children if any
        for child in (data.children or []):
            if child.full_name:
                cur.execute("""
                    INSERT INTO member_children
                        (user_id, full_name, date_of_birth, relationship, cert_number)
                    VALUES (%s,%s,%s,%s,%s)
                """, (new_user_id, child.full_name, child.date_of_birth,
                      child.relationship, child.cert_number))

        # 3. Insert parents/parents-in-law if any
        for parent in (data.parents or []):
            if parent.full_name:
                cur.execute("""
                    INSERT INTO member_parents
                        (user_id, full_name, status, id_number, current_residence, contact_phone)
                    VALUES (%s,%s,%s,%s,%s,%s)
                """, (new_user_id, parent.full_name, parent.status,
                      parent.id_number, parent.current_residence, parent.contact_phone))

        # 4. Mirror basic fields into members table
        cur.execute("""
            INSERT INTO members (
                full_name, phone_number, id_number, role, status,
                date_joined, next_of_kin_name, next_of_kin_phone
            )
            VALUES (%s,%s,%s,'member','active',CURRENT_DATE,%s,%s)
            ON CONFLICT (phone_number) DO NOTHING
        """, (
            data.full_name, data.phone_number, data.id_number,
            data.next_of_kin_name, data.next_of_kin_phone,
        ))

        conn.commit()

        from app.routes.audit import log_action
        log_action("Registration Submitted", data.full_name,
                    detail=f"New member registration ({data.phone_number}) awaiting approval",
                    target=data.full_name)

        return {
            "message": "Registration submitted. Await admin approval.",
            "user_id": new_user_id,
            "status": "pending"
        }

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=500, public_msg="Could not complete the request. Please try again.")
    finally:
        cur.close()
        release_connection(conn)


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
def login(request: Request, data: UserLogin):
    conn = get_connection()
    cur = conn.cursor()
    try:
        if data.email:
            cur.execute(
                """SELECT id, full_name, role, hashed_password, is_active, registration_status, phone_number, must_change_password
                   FROM users WHERE email=%s""",
                (data.email,)
            )
        elif data.phone_number:
            cur.execute(
                """SELECT id, full_name, role, hashed_password, is_active, registration_status, phone_number, must_change_password
                   FROM users WHERE phone_number=%s""",
                (data.phone_number,)
            )
        else:
            raise HTTPException(status_code=400, detail="Phone number or email required")
        user = cur.fetchone()
    finally:
        cur.close()
        release_connection(conn)

    if not user or not verify_password(data.password, user[3]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if user[5] == "pending":
        raise HTTPException(status_code=403, detail="Your registration is pending admin approval.")
    if user[5] == "rejected":
        raise HTTPException(status_code=403, detail="Your registration was not approved. Contact the admin.")
    if not user[4]:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    token = create_token({"sub": str(user[0]), "user_id": user[0], "role": user[2]})

    from app.routes.audit import log_action
    log_action("Login", user[1], detail=f"Successful login ({user[2]})", target=user[1])

    return TokenResponse(access_token=token, user_id=user[0], full_name=user[1], role=user[2],
                          phone_number=user[6] or "", must_change_password=bool(user[7]))

# ------------------------------------------------------------------ #
#  SWAGGER /docs token endpoint
# ------------------------------------------------------------------ #
@router.post("/token")
def token(form_data: OAuth2PasswordRequestForm = Depends()):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT id, full_name, role, hashed_password, is_active, registration_status
               FROM users WHERE phone_number=%s""",
            (form_data.username,)
        )
        user = cur.fetchone()
    finally:
        cur.close()
        release_connection(conn)

    if not user or not verify_password(form_data.password, user[3]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if user[5] == "pending":
        raise HTTPException(status_code=403, detail="Registration pending approval")

    token_str = create_token({"sub": form_data.username, "user_id": user[0], "role": user[2]})
    return {"access_token": token_str, "token_type": "bearer"}


@router.post("/logout")
def logout(current_user: dict = Depends(get_current_user)):
    """
    JWTs are stateless so there's nothing to invalidate server-side —
    this endpoint exists purely to record the logout in the audit log.
    The frontend still clears its local token regardless of this call's outcome.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        from app.routes.audit import log_action, get_actor_name
        actor = get_actor_name(cur, current_user)
        log_action("Logout", actor, detail=f"Signed out ({current_user.get('role','')})", target=actor)
    finally:
        cur.close()
        release_connection(conn)
    return {"message": "Logged out"}


# ------------------------------------------------------------------ #
#  CHANGE PASSWORD (self-service, requires current password)
# ------------------------------------------------------------------ #
@router.post("/change-password")
@limiter.limit("5/minute")
def change_password(request: Request, data: dict, current_user: dict = Depends(get_current_user)):
    """
    Lets a logged-in user set a new password themselves.
    Used for the mandatory first-login password change (when an admin
    creates a member with the default password) as well as voluntary
    password changes from an already-logged-in session.
    """
    current_password = (data.get("current_password") or "").strip()
    new_password     = (data.get("new_password") or "").strip()

    if not current_password or not new_password:
        raise HTTPException(status_code=400, detail="Current and new password are required")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
    if new_password == current_password:
        raise HTTPException(status_code=400, detail="New password must be different from the current one")

    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT hashed_password, full_name FROM users WHERE id=%s",
            (current_user["user_id"],)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        if not verify_password(current_password, row[0]):
            raise HTTPException(status_code=401, detail="Current password is incorrect")

        hashed = hash_password(new_password)
        cur.execute(
            "UPDATE users SET hashed_password=%s, must_change_password=false WHERE id=%s",
            (hashed, current_user["user_id"])
        )
        conn.commit()

        from app.routes.audit import log_action
        log_action("Password Changed", row[1], detail="User changed their own password", target=row[1])

        return {"message": "Password updated successfully."}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        release_connection(conn)


# ------------------------------------------------------------------ #
#  ME
# ------------------------------------------------------------------ #
@router.get("/me")
def get_me(current_user: dict = Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
    """SELECT u.id, u.full_name, u.phone_number, u.role, u.is_active,
              u.registration_status, u.created_at, u.email,
              u.date_of_birth, u.marital_status, u.residence,
              u.court, u.house_number, u.spouse_name,
              u.next_of_kin_name, u.next_of_kin_phone,
              u.next_of_kin_2, u.nok2_phone,
              m.id_number, m.status, m.date_joined, m.notes,
              u.must_change_password
       FROM users u
       LEFT JOIN members m ON m.phone_number = u.phone_number
       WHERE u.id=%s""",
    (current_user["user_id"],)
)
        user = cur.fetchone()
        children = []
        parents = []
        if user:
            cur.execute(
                """SELECT full_name, date_of_birth, relationship, cert_number
                   FROM member_children WHERE user_id=%s ORDER BY id""",
                (user[0],)
            )
            children = [
                {
                    "full_name": r[0],
                    "date_of_birth": str(r[1]) if r[1] else None,
                    "relationship": r[2],
                    "cert_number": r[3]
                }
                for r in cur.fetchall()
            ]
            # fetch parents
            cur.execute(
                """SELECT full_name, status, id_number, current_residence, contact_phone
                   FROM member_parents WHERE user_id=%s ORDER BY id""",
                (user[0],)
            )
            parents = [
                {
                    "full_name": r[0],
                    "status": r[1],
                    "id_number": r[2],
                    "current_residence": r[3],
                    "contact_phone": r[4]
                }
                for r in cur.fetchall()
            ]
    finally:
        cur.close()
        release_connection(conn)

    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
    "id": user[0], "full_name": user[1], "phone_number": user[2],
    "role": user[3], "is_active": user[4],
    "registration_status": user[5], "created_at": user[6],
    "email": user[7],
    "date_of_birth": str(user[8]) if user[8] else None,
    "marital_status": user[9],
    "residence": user[10],
    "court": user[11],
    "house_number": user[12],
    "spouse_name": user[13],
    "next_of_kin_name": user[14],
    "next_of_kin_phone": user[15],
    "next_of_kin_2": user[16],
    "nok2_phone": user[17],
    "id_number": user[18],
    "status": user[19],
    "date_joined": str(user[20]) if user[20] else None,
    "notes": user[21],
    "must_change_password": bool(user[22]),
    "children": children,
    "parents": parents
}
# ------------------------------------------------------------------ #
#  ADMIN — list pending, approve, reject, change role
# ------------------------------------------------------------------ #
@router.get("/admin/pending")
def list_pending(current_user=Depends(require_admin)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT u.id, u.full_name, u.phone_number, u.email, u.role, u.created_at,
                   u.id_number, u.date_of_birth, u.marital_status, u.residence,
                   u.court, u.house_number, u.spouse_name,
                   u.next_of_kin_name, u.next_of_kin_phone,
                   u.next_of_kin_2, u.nok2_phone
            FROM users u
            WHERE u.registration_status = 'pending'
            ORDER BY u.created_at ASC
        """)
        rows = cur.fetchall()

        result = []
        for r in rows:
            cur.execute("""
                SELECT full_name, date_of_birth, relationship, cert_number
                FROM member_children WHERE user_id=%s
            """, (r[0],))
            children = [
                {"full_name": c[0], "date_of_birth": str(c[1]) if c[1] else None,
                 "relationship": c[2], "cert_number": c[3]}
                for c in cur.fetchall()
            ]
            cur.execute("""
                SELECT full_name, status, id_number, current_residence, contact_phone
                FROM member_parents WHERE user_id=%s
            """, (r[0],))
            parents = [
                {"full_name": p[0], "status": p[1], "id_number": p[2],
                 "current_residence": p[3], "contact_phone": p[4]}
                for p in cur.fetchall()
            ]
            result.append({
                "id": r[0], "full_name": r[1], "phone_number": r[2],
                "email": r[3], "role": r[4], "applied_at": r[5],
                "id_number": r[6],
                "date_of_birth": str(r[7]) if r[7] else None,
                "marital_status": r[8], "residence": r[9],
                "court": r[10], "house_number": r[11], "spouse_name": r[12],
                "next_of_kin_name": r[13], "next_of_kin_phone": r[14],
                "next_of_kin_2": r[15], "nok2_phone": r[16],
                "children": children,
                "parents": parents
            })
        return result
    finally:
        cur.close()
        release_connection(conn)
@router.get("/admin/rejected")
def list_rejected(current_user=Depends(require_admin)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, full_name, phone_number, email, created_at, rejection_reason
            FROM users
            WHERE registration_status = 'rejected'
            ORDER BY created_at DESC
        """)
        rows = cur.fetchall()
        return [
            {
                "id": r[0], "full_name": r[1], "phone_number": r[2],
                "email": r[3], "applied_at": r[4],
                "rejection_reason": r[5] or "No reason provided"
            }
            for r in rows
        ]
    finally:
        cur.close()
        release_connection(conn)

@router.patch("/admin/{user_id}/approve")
def approve_user(user_id: int, current_user=Depends(require_admin)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE users
            SET registration_status = 'approved', is_active = true
            WHERE id = %s
            RETURNING id, full_name
        """, (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        conn.commit()

        from app.routes.audit import log_action, get_actor_name
        actor = get_actor_name(cur, current_user)
        log_action("Member Approved", actor, detail=f"Approved and activated {row[1]}", target=row[1])

        return {"message": f"{row[1]} approved and activated"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=500, public_msg="Could not complete the request. Please try again.")
    finally:
        cur.close()
        release_connection(conn)


@router.patch("/admin/{user_id}/reject")
def reject_user(user_id: int, body: dict = None, current_user=Depends(require_admin)):
    reason = (body or {}).get("reason", "")
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE users
            SET registration_status = 'rejected', is_active = false,
                rejection_reason = %s
            WHERE id = %s
            RETURNING id, full_name
        """, (reason, user_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        conn.commit()

        from app.routes.audit import log_action, get_actor_name
        actor = get_actor_name(cur, current_user)
        log_action("Member Rejected", actor, detail=reason or "No reason given", target=row[1])

        return {"message": f"{row[1]} rejected"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=500, public_msg="Could not complete the request. Please try again.")
    finally:
        cur.close()
        release_connection(conn)

@router.patch("/admin/{user_id}/role")
def change_role(user_id: int, body: dict, current_user=Depends(require_admin)):
    valid_roles = ("member", "admin", "treasurer", "secretary", "chairperson", "super_admin")
    new_role = body.get("role")
    if new_role not in valid_roles:
        raise HTTPException(status_code=400, detail=f"Role must be one of {valid_roles}")
    if new_role == "super_admin" and current_user.get("role") != "super_admin":
        raise HTTPException(
            status_code=403,
            detail="Only a super_admin can assign the super_admin role"
        )
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE users SET role=%s WHERE id=%s RETURNING full_name",
            (new_role, user_id)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        conn.commit()

        from app.routes.audit import log_action, get_actor_name
        actor = get_actor_name(cur, current_user)
        log_action("Role Changed", actor, detail=f"Role changed to {new_role}", target=row[0])

        return {"message": f"{row[0]}'s role updated to {new_role}"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=500, public_msg="Could not complete the request. Please try again.")
    finally:
        cur.close()
        release_connection(conn)

@router.patch("/admin/{user_id}/reinstate")
def reinstate_user(user_id: int, current_user=Depends(require_admin)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE users
            SET registration_status = 'pending', is_active = false,
                rejection_reason = NULL
            WHERE id = %s
            RETURNING id, full_name
        """, (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        conn.commit()

        from app.routes.audit import log_action, get_actor_name
        actor = get_actor_name(cur, current_user)
        log_action("Member Reinstated", actor, detail=f"{row[1]} moved back to pending", target=row[1])

        return {"message": f"{row[1]} moved back to pending"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        safe_db_error(e, status=500, public_msg="Could not complete the request. Please try again.")
    finally:
        cur.close()
        release_connection(conn)