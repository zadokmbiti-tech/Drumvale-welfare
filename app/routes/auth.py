from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from app.database import get_connection, release_connection
from app.models import UserRegister, UserLogin, TokenResponse
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
import os

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
    if current_user.get("role") not in ("super_admin", "chairperson", "secretary"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


# ------------------------------------------------------------------ #
#  REGISTER
# ------------------------------------------------------------------ #
@router.post("/register", status_code=201)
def register(data: UserRegister):
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

        cur.execute("""
            INSERT INTO users (
                full_name, phone_number, email, id_number, role,
                hashed_password, registration_status, is_active,
                date_of_birth, marital_status, residence, court,
                house_number, spouse_name,
                next_of_kin_name, next_of_kin_phone,
                next_of_kin_2, nok2_phone
            )
            VALUES (%s,%s,%s,%s,%s,%s,'pending',false,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            data.full_name, data.phone_number, data.email,
            data.id_number, "member", hashed,
            data.date_of_birth, data.marital_status, data.residence,
            data.court, data.house_number, data.spouse_name,
            data.next_of_kin_name, data.next_of_kin_phone,
            data.next_of_kin_2, data.nok2_phone,
        ))
        new_user_id = cur.fetchone()[0]

        for child in (data.children or []):
            if child.full_name:
                cur.execute("""
                    INSERT INTO member_children
                        (user_id, full_name, date_of_birth, relationship, cert_number)
                    VALUES (%s,%s,%s,%s,%s)
                """, (new_user_id, child.full_name, child.date_of_birth,
                      child.relationship, child.cert_number))

        # Mirror into members table
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
        return {
            "message": "Registration submitted. Await admin approval.",
            "user_id": new_user_id,
            "status": "pending"
        }

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        release_connection(conn)


@router.post("/login", response_model=TokenResponse)
def login(data: UserLogin):
    conn = get_connection()
    cur = conn.cursor()
    try:
        if data.email:
            cur.execute(
                """SELECT id, full_name, role, hashed_password, is_active, registration_status, phone_number
                   FROM users WHERE email=%s""",
                (data.email,)
            )
        elif data.phone_number:
            cur.execute(
                """SELECT id, full_name, role, hashed_password, is_active, registration_status, phone_number
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
    return TokenResponse(access_token=token, user_id=user[0], full_name=user[1], role=user[2], phone_number=user[6] or "")

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


# ------------------------------------------------------------------ #
#  ME
# ------------------------------------------------------------------ #
@router.get("/me")
def get_me(current_user: dict = Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT id, full_name, phone_number, role, is_active, registration_status, created_at
               FROM users WHERE id=%s""",
            (current_user["user_id"],)
        )
        user = cur.fetchone()
    finally:
        cur.close()
        release_connection(conn)

    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "id": user[0], "full_name": user[1], "phone_number": user[2],
        "role": user[3], "is_active": user[4],
        "registration_status": user[5], "created_at": user[6]
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
            SELECT id, full_name, phone_number, email, role, created_at
            FROM users
            WHERE registration_status = 'pending'
            ORDER BY created_at ASC
        """)
        rows = cur.fetchall()
    finally:
        cur.close()
        release_connection(conn)

    return [
        {
            "id": r[0], "full_name": r[1], "phone_number": r[2],
            "email": r[3], "role": r[4], "applied_at": r[5]
        }
        for r in rows
    ]


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
        return {"message": f"{row[1]} approved and activated"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        release_connection(conn)


@router.patch("/admin/{user_id}/reject")
def reject_user(user_id: int, current_user=Depends(require_admin)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE users
            SET registration_status = 'rejected', is_active = false
            WHERE id = %s
            RETURNING id, full_name
        """, (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        conn.commit()
        return {"message": f"{row[1]} rejected"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        release_connection(conn)


@router.patch("/admin/{user_id}/role")
def change_role(user_id: int, body: dict, current_user=Depends(require_admin)):
    valid_roles = ("member", "treasurer", "secretary", "chairperson", "super_admin")
    new_role = body.get("role")
    if new_role not in valid_roles:
        raise HTTPException(status_code=400, detail=f"Role must be one of {valid_roles}")

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
        return {"message": f"{row[0]}'s role updated to {new_role}"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        release_connection(conn)