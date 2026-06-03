"""
Profile Update Request flow
  POST   /profile-updates/              — member submits a change request
  GET    /profile-updates/my            — member views their own requests
  GET    /profile-updates/              — admin lists all pending requests
  PATCH  /profile-updates/{id}/approve  — admin approves (applies changes)
  PATCH  /profile-updates/{id}/reject   — admin rejects with optional reason
"""
from fastapi import APIRouter, HTTPException, Depends
from app.database import get_connection, release_connection
from app.routes.auth import get_current_user, require_admin
from app.schemas import ProfileUpdateRequest
from app.utils import safe_db_error
import json

router = APIRouter()

# Scalar fields that live directly in profile_update_requests columns
SCALAR_FIELDS = [
    "full_name", "phone_number", "email", "id_number", "date_of_birth",
    "marital_status", "residence", "court", "house_number", "spouse_name",
    "next_of_kin_name", "next_of_kin_phone", "next_of_kin_2", "nok2_phone",
]

# Which scalar fields also exist on the users table
USERS_SCALAR_FIELDS = {
    "full_name", "phone_number", "email", "id_number", "date_of_birth",
    "marital_status", "residence", "court", "house_number", "spouse_name",
    "next_of_kin_name", "next_of_kin_phone", "next_of_kin_2", "nok2_phone",
}

# Which scalar fields are mirrored to the members table
MEMBERS_MIRROR_FIELDS = {
    "full_name", "phone_number", "id_number",
    "next_of_kin_name", "next_of_kin_phone",
}


# ── Member: submit update request ────────────────────────────────────
@router.post("/", status_code=201)
def submit_update_request(
    body: ProfileUpdateRequest,
    current_user: dict = Depends(get_current_user)
):
    data = body.dict(exclude_none=True)
    # Separately grab the list fields before they get treated as scalars
    children_raw = data.pop("children", None)
    parents_raw  = data.pop("parents",  None)
    if not data and not children_raw and not parents_raw:
        raise HTTPException(400, "No fields provided — nothing to update.")

    user_id = current_user.get("user_id")
    conn = get_connection()
    cur  = conn.cursor()
    try:
        # Block duplicate pending requests
        cur.execute(
            "SELECT id FROM profile_update_requests WHERE user_id=%s AND status='pending'",
            (user_id,)
        )
        if cur.fetchone():
            raise HTTPException(
                409,
                "You already have a pending update request. "
                "Please wait for admin review before submitting another."
            )

        # Separate scalar fields
        scalar_data  = {f: data[f] for f in SCALAR_FIELDS if f in data}
        children_val = None
        parents_val  = None

        if children_raw:
            children_val = json.dumps([
                {k: str(v) if v is not None else None for k, v in c.items()}
                for c in children_raw
            ])
        if parents_raw:
            parents_val = json.dumps([
                {k: str(v) if v is not None else None for k, v in p.items()}
                for p in parents_raw
            ])

        # Build INSERT
        cols   = ["user_id"] + list(scalar_data.keys())
        values = [user_id]   + list(scalar_data.values())

        if children_val is not None:
            cols.append("children_json")
            values.append(children_val)
        if parents_val is not None:
            cols.append("parents_json")
            values.append(parents_val)

        placeholders = ", ".join(["%s"] * len(values))
        cur.execute(
            f"INSERT INTO profile_update_requests ({', '.join(cols)}) "
            f"VALUES ({placeholders}) RETURNING id",
            values
        )
        req_id = cur.fetchone()[0]
        conn.commit()
        return {
            "message": "Update request submitted. An admin will review it shortly.",
            "request_id": req_id
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, safe_db_error(e))
    finally:
        cur.close()
        release_connection(conn)


# ── Member: view their own requests ──────────────────────────────────
@router.get("/my")
def my_update_requests(current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("user_id")
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT id, status, requested_at, reviewed_at, reject_reason,
                   full_name, phone_number, email, id_number, date_of_birth, marital_status,
                   residence, court, house_number, spouse_name,
                   next_of_kin_name, next_of_kin_phone, next_of_kin_2, nok2_phone,
                   children_json, parents_json
            FROM profile_update_requests
            WHERE user_id = %s
            ORDER BY requested_at DESC
            LIMIT 10
        """, (user_id,))
        rows = cur.fetchall()
    finally:
        cur.close()
        release_connection(conn)

    return [_row_to_dict(r) for r in rows]


# ── Admin: list all pending requests ─────────────────────────────────
@router.get("/")
def list_update_requests(_=Depends(require_admin)):
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT r.id, r.status, r.requested_at, r.reviewed_at, r.reject_reason,
                   r.full_name, r.phone_number, r.email, r.id_number, r.date_of_birth,
                   r.marital_status, r.residence, r.court, r.house_number, r.spouse_name,
                   r.next_of_kin_name, r.next_of_kin_phone, r.next_of_kin_2, r.nok2_phone,
                   r.children_json, r.parents_json,
                   u.full_name AS current_name, u.phone_number AS current_phone, r.user_id
            FROM profile_update_requests r
            JOIN users u ON r.user_id = u.id
            WHERE r.status = 'pending'
            ORDER BY r.requested_at ASC
        """)
        rows = cur.fetchall()
    finally:
        cur.close()
        release_connection(conn)

    result = []
    for r in rows:
        d = _row_to_dict(r)
        d["current_name"]  = r[21]
        d["phone_number"]  = r[22]
        d["user_id"]       = r[23]
        result.append(d)
    return result


# ── Admin: approve ────────────────────────────────────────────────────
@router.patch("/{req_id}/approve")
def approve_update(req_id: int, current_user: dict = Depends(require_admin)):
    reviewer_id = current_user.get("user_id")
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("""
            SELECT user_id,
                   full_name, phone_number, email, id_number, date_of_birth,
                   marital_status, residence, court, house_number, spouse_name,
                   next_of_kin_name, next_of_kin_phone, next_of_kin_2, nok2_phone,
                   children_json, parents_json
            FROM profile_update_requests
            WHERE id=%s AND status='pending'
        """, (req_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Request not found or already reviewed.")

        user_id = row[0]
        # Build scalar changes dict (non-null values only)
        proposed = dict(zip(SCALAR_FIELDS, row[1:15]))
        changes  = {k: v for k, v in proposed.items() if v is not None}

        # 1. Apply scalar changes to users table
        if changes:
            set_u = ", ".join(f"{k}=%s" for k in changes if k in USERS_SCALAR_FIELDS)
            vals_u = [v for k, v in changes.items() if k in USERS_SCALAR_FIELDS]
            if set_u:
                cur.execute(f"UPDATE users SET {set_u} WHERE id=%s", (*vals_u, user_id))

            # 2. Mirror subset to members table
            m_changes = {k: v for k, v in changes.items() if k in MEMBERS_MIRROR_FIELDS}
            if m_changes:
                set_m = ", ".join(f"{k}=%s" for k in m_changes)
                cur.execute(
                    f"UPDATE members SET {set_m} "
                    f"WHERE phone_number=(SELECT phone_number FROM users WHERE id=%s)",
                    (*m_changes.values(), user_id)
                )

        # 3. Apply children — replace all existing rows
        children_json = row[15]
        if children_json:
            children = json.loads(children_json)
            cur.execute("DELETE FROM member_children WHERE user_id=%s", (user_id,))
            for child in children:
                if child.get("full_name"):
                    cur.execute("""
                        INSERT INTO member_children
                            (user_id, full_name, date_of_birth, relationship, cert_number)
                        VALUES (%s,%s,%s,%s,%s)
                    """, (
                        user_id,
                        child.get("full_name"),
                        child.get("date_of_birth"),
                        child.get("relationship"),
                        child.get("cert_number"),
                    ))

        # 4. Apply parents — replace all existing rows
        parents_json = row[16]
        if parents_json:
            parents = json.loads(parents_json)
            cur.execute("DELETE FROM member_parents WHERE user_id=%s", (user_id,))
            for parent in parents:
                if parent.get("full_name"):
                    cur.execute("""
                        INSERT INTO member_parents
                            (user_id, full_name, id_number, current_residence, contact_phone)
                        VALUES (%s,%s,%s,%s,%s)
                    """, (
                        user_id,
                        parent.get("full_name"),
                        parent.get("id_number"),
                        parent.get("current_residence"),
                        parent.get("contact_phone"),
                    ))

        # 5. Mark approved
        cur.execute("""
            UPDATE profile_update_requests
            SET status='approved', reviewed_by=%s, reviewed_at=NOW()
            WHERE id=%s
        """, (reviewer_id, req_id))
        conn.commit()
        return {"message": "Profile update approved and applied."}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, safe_db_error(e))
    finally:
        cur.close()
        release_connection(conn)


# ── Admin: reject with optional reason ───────────────────────────────
@router.patch("/{req_id}/reject")
def reject_update(req_id: int, body: dict = None, current_user: dict = Depends(require_admin)):
    reviewer_id = current_user.get("user_id")
    reason = (body or {}).get("reason", "")
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute(
            "SELECT id FROM profile_update_requests WHERE id=%s AND status='pending'",
            (req_id,)
        )
        if not cur.fetchone():
            raise HTTPException(404, "Request not found or already reviewed.")
        cur.execute("""
            UPDATE profile_update_requests
            SET status='rejected', reviewed_by=%s, reviewed_at=NOW(), reject_reason=%s
            WHERE id=%s
        """, (reviewer_id, reason, req_id))
        conn.commit()
        return {"message": "Profile update request rejected."}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, safe_db_error(e))
    finally:
        cur.close()
        release_connection(conn)


# ── Helper ────────────────────────────────────────────────────────────
def _row_to_dict(r):
    scalar_keys = SCALAR_FIELDS
    proposed = {}
    for i, k in enumerate(scalar_keys):
        v = r[5 + i]
        if v is not None:
            proposed[k] = str(v)

    # Parse children/parents JSON
    children_json = r[5 + len(scalar_keys)]      # index 19
    parents_json  = r[5 + len(scalar_keys) + 1]  # index 20

    if children_json:
        try:
            proposed["children"] = json.loads(children_json)
        except Exception:
            proposed["children"] = children_json

    if parents_json:
        try:
            proposed["parents"] = json.loads(parents_json)
        except Exception:
            proposed["parents"] = parents_json

    return {
        "id":            r[0],
        "status":        r[1],
        "requested_at":  str(r[2]) if r[2] else None,
        "reviewed_at":   str(r[3]) if r[3] else None,
        "reject_reason": r[4],
        "proposed":      proposed,
    }
