import csv
import io
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from fastapi.responses import StreamingResponse
from app.database import get_connection, release_connection
from app.models import MemberCreate
from app.schemas import ProfileUpdateRequest
from app.routes.auth import get_current_user, require_admin
from app.auth_deps import require_secretary, require_chairperson, require_super_admin

router = APIRouter()


DEFAULT_MEMBER_PASSWORD = "Drumvale2026"


@router.post("/")
def add_member(
    member: MemberCreate,
    current_user=Depends(require_secretary)        # secretary and above can add members
):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM members WHERE phone_number=%s", (member.phone_number,))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="A member with this phone number already exists")

        if member.email:
            cur.execute("SELECT id FROM users WHERE email=%s", (member.email,))
            if cur.fetchone():
                raise HTTPException(status_code=400, detail="Email already registered")

        if member.member_id:
            cur.execute("SELECT id FROM users WHERE membership_no=%s", (member.member_id,))
            if cur.fetchone():
                raise HTTPException(status_code=400, detail="Member ID is already in use by another member")

        cur.execute("""
            INSERT INTO members (full_name, phone_number, id_number, role, status,
                date_joined, next_of_kin_name, next_of_kin_phone, notes, membership_no)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (
            member.full_name, member.phone_number, member.id_number,
            member.role, member.status, member.date_joined,
            member.next_of_kin_name, member.next_of_kin_phone, member.notes,
            member.member_id
        ))
        new_id = cur.fetchone()[0]

        # Give the member an account they can log in with right away, using
        # a default password they'll be forced to change on first login.
        # ON CONFLICT protects anyone who already self-registered (and thus
        # already set their own password) from being overwritten.
        from app.routes.auth import hash_password
        default_hashed = hash_password(DEFAULT_MEMBER_PASSWORD)
        cur.execute("""
            INSERT INTO users (
                full_name, phone_number, email, id_number, hashed_password,
                role, is_active, registration_status, must_change_password,
                must_accept_privacy,
                date_of_birth, marital_status, residence, court, house_number,
                spouse_name, next_of_kin_name, next_of_kin_phone,
                next_of_kin_2, nok2_phone, privacy_accepted, membership_no
            )
            VALUES (%s,%s,%s,%s,%s,%s,true,'approved',true,true,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (phone_number) DO NOTHING
            RETURNING id
        """, (
            member.full_name, member.phone_number, member.email, member.id_number,
            default_hashed, member.role,
            member.date_of_birth, member.marital_status, member.residence,
            member.court, member.house_number, member.spouse_name,
            member.next_of_kin_name, member.next_of_kin_phone,
            member.next_of_kin_2, member.nok2_phone, member.privacy_accepted,
            member.member_id
        ))
        user_row = cur.fetchone()
        new_user_id = user_row[0] if user_row else None

        # Insert children/parents against the users record, same as self-registration.
        # Skipped if the phone number already had a user row (ON CONFLICT above).
        if new_user_id:
            for child in (member.children or []):
                if child.full_name:
                    cur.execute("""
                        INSERT INTO member_children
                            (user_id, full_name, date_of_birth, relationship, cert_number)
                        VALUES (%s,%s,%s,%s,%s)
                    """, (new_user_id, child.full_name, child.date_of_birth,
                          child.relationship, child.cert_number))

            for parent in (member.parents or []):
                if parent.full_name:
                    cur.execute("""
                        INSERT INTO member_parents
                            (user_id, full_name, status, id_number, current_residence, contact_phone)
                        VALUES (%s,%s,%s,%s,%s,%s)
                    """, (new_user_id, parent.full_name, parent.status,
                          parent.id_number, parent.current_residence, parent.contact_phone))

        conn.commit()

        from app.routes.audit import log_action, get_actor_name
        actor = get_actor_name(cur, current_user)
        log_action("Member Created", actor, detail=f"Added member {member.full_name}", target=member.full_name)

        return {"message": "Member added", "id": new_id, "default_password": DEFAULT_MEMBER_PASSWORD}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        release_connection(conn)


@router.post("/bulk-import")
async def bulk_import_members(
    file: UploadFile = File(...),
    current_user=Depends(require_secretary)
):
    """
    Import members from a CSV file.
    Required columns: full_name, phone_number
    Optional: id_number, role, status, date_joined, next_of_kin_name, next_of_kin_phone, notes
    Returns counts of inserted rows and any row-level errors.
    """
    if not file.filename.endswith(".csv"):
        raise HTTPException(400, "Only CSV files are accepted")

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")   # strip BOM if present
    except UnicodeDecodeError:
        raise HTTPException(400, "File must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(text))
    required = {"full_name", "phone_number"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise HTTPException(400, f"CSV must contain columns: {', '.join(required)}")

    inserted, errors = 0, []
    conn = get_connection()
    cur = conn.cursor()
    try:
        for i, row in enumerate(reader, start=2):   # row 1 = header
            full_name    = (row.get("full_name") or "").strip()
            phone_number = (row.get("phone_number") or "").strip()
            if not full_name or not phone_number:
                errors.append({"row": i, "error": "full_name and phone_number are required"})
                continue
            id_number   = (row.get("id_number") or "").strip() or None
            role        = (row.get("role") or "member").strip()
            status      = (row.get("status") or "active").strip()
            date_joined = (row.get("date_joined") or "").strip() or None
            nok_name    = (row.get("next_of_kin_name") or "").strip() or None
            nok_phone   = (row.get("next_of_kin_phone") or "").strip() or None
            notes       = (row.get("notes") or "").strip() or None
            try:
                cur.execute("""
                    INSERT INTO members
                        (full_name, phone_number, id_number, role, status,
                         date_joined, next_of_kin_name, next_of_kin_phone, notes)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (phone_number) DO NOTHING
                """, (full_name, phone_number, id_number, role, status,
                      date_joined, nok_name, nok_phone, notes))
                inserted += cur.rowcount

                if cur.rowcount:
                    from app.routes.auth import hash_password
                    cur.execute("""
                        INSERT INTO users (full_name, phone_number, id_number, role, hashed_password,
                            is_active, registration_status, must_change_password, must_accept_privacy)
                        VALUES (%s,%s,%s,%s,%s, true, 'approved', true, true)
                        ON CONFLICT (phone_number) DO NOTHING
                    """, (full_name, phone_number, id_number, role, hash_password(DEFAULT_MEMBER_PASSWORD)))
            except Exception as e:
                conn.rollback()
                errors.append({"row": i, "error": str(e)})
                continue
        conn.commit()
    finally:
        cur.close()
        release_connection(conn)

    from app.routes.audit import log_user_action
    log_user_action(current_user, "Members Bulk Imported",
                     detail=f"{inserted} inserted, {len(errors)} error(s) from {file.filename}",
                     target=file.filename)

    return {
        "inserted": inserted,
        "errors": errors,
        "default_password": DEFAULT_MEMBER_PASSWORD,
        "message": f"{inserted} member(s) imported, {len(errors)} error(s). "
                   f"They can log in with the default password '{DEFAULT_MEMBER_PASSWORD}' and will be asked to change it."
    }


# ── Static GET routes MUST come before /{member_id} ──────────────────────────

@router.get("")
@router.get("/")
def list_members(_=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, full_name, phone_number, role, status, date_joined, membership_no FROM members ORDER BY id DESC"
    )
    rows = cur.fetchall()
    cur.close()
    release_connection(conn)
    return [
        {"id": r[0], "full_name": r[1], "phone_number": r[2],
         "role": r[3], "status": r[4], "date_joined": r[5], "membership_no": r[6]}
        for r in rows
    ]


@router.get("/template/csv")
def download_csv_template(_=Depends(require_secretary)):
    """Return a blank CSV template for bulk member import."""
    fields = [
        "full_name", "phone_number", "id_number", "role", "status",
        "date_joined", "next_of_kin_name", "next_of_kin_phone", "notes"
    ]
    example = [
        "Jane Doe", "0712345678", "12345678", "member", "active",
        "2024-01-15", "John Doe", "0798765432", "Founding member"
    ]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(fields)
    writer.writerow(example)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=members_template.csv"}
    )


@router.get("/report/csv")
def download_members_csv(_=Depends(require_secretary)):
    """Download all members as a CSV report."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, full_name, phone_number, id_number, role, status,
                   date_joined::text, next_of_kin_name, next_of_kin_phone, notes
            FROM members ORDER BY full_name
        """)
        rows = cur.fetchall()
    finally:
        cur.close()
        release_connection(conn)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "full_name", "phone_number", "id_number", "role", "status",
        "date_joined", "next_of_kin_name", "next_of_kin_phone", "notes"
    ])
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=members_report.csv"}
    )


@router.get("/phones/all")
def get_all_phones(current_user=Depends(require_super_admin)):
    """Return phone numbers of all active members — used for SMS broadcast."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT phone_number FROM members WHERE status='active' AND phone_number IS NOT NULL"
    )
    rows = cur.fetchall()
    cur.close()
    release_connection(conn)
    return [r[0] for r in rows]


# ── Dynamic route last ────────────────────────────────────────────────────────

@router.get("/{member_id}")
def get_member(member_id: int, _=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT m.id, m.full_name, m.phone_number, m.id_number, m.role, m.status,
                   m.date_joined, m.next_of_kin_name, m.next_of_kin_phone, m.notes,
                   u.email, u.date_of_birth, u.marital_status, u.residence,
                   u.court, u.house_number, u.spouse_name,
                   u.next_of_kin_2, u.nok2_phone, m.membership_no
            FROM members m
            LEFT JOIN users u ON u.phone_number = m.phone_number
            WHERE m.id = %s
        """, (member_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Member not found")

        # Get children
        cur.execute("""
            SELECT full_name, date_of_birth, relationship, cert_number
            FROM member_children
            WHERE user_id = (SELECT id FROM users WHERE phone_number = %s)
            ORDER BY id
        """, (row[2],))
        children = [
            {"full_name": c[0], "date_of_birth": str(c[1]) if c[1] else None,
             "relationship": c[2], "cert_number": c[3]}
            for c in cur.fetchall()
        ]

        # Get parents/parents-in-law
        cur.execute("""
            SELECT full_name, status, id_number, current_residence, contact_phone
            FROM member_parents
            WHERE user_id = (SELECT id FROM users WHERE phone_number = %s)
            ORDER BY id
        """, (row[2],))
        parents = [
            {"full_name": p[0], "status": p[1], "id_number": p[2],
             "current_residence": p[3], "contact_phone": p[4]}
            for p in cur.fetchall()
        ]
    finally:
        cur.close()
        release_connection(conn)

    return {
        "id": row[0], "full_name": row[1], "phone_number": row[2],
        "id_number": row[3], "role": row[4], "status": row[5],
        "date_joined": str(row[6]) if row[6] else None,
        "next_of_kin_name": row[7], "next_of_kin_phone": row[8], "notes": row[9],
        "email": row[10],
        "date_of_birth": str(row[11]) if row[11] else None,
        "marital_status": row[12], "residence": row[13],
        "court": row[14], "house_number": row[15], "spouse_name": row[16],
        "next_of_kin_2": row[17], "nok2_phone": row[18],
        "membership_no": row[19],
        "children": children,
        "parents": parents
    }


@router.put("/{member_id}")
def update_member(
    member_id: int,
    member: MemberCreate,
    current_user=Depends(require_secretary)        # secretary and above can edit members
):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE members SET full_name=%s, phone_number=%s, id_number=%s,
            role=%s, status=%s, date_joined=%s, next_of_kin_name=%s,
            next_of_kin_phone=%s, notes=%s WHERE id=%s
        """, (
            member.full_name, member.phone_number, member.id_number,
            member.role, member.status, member.date_joined,
            member.next_of_kin_name, member.next_of_kin_phone,
            member.notes, member_id
        ))
        conn.commit()

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Member Updated", detail="Basic member fields updated",
                         target=member.full_name or f"member #{member_id}")

        return {"message": "Member updated"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        release_connection(conn)


@router.put("/{member_id}/admin-edit")
def admin_edit_full_profile(
    member_id: int,
    body: ProfileUpdateRequest,
    current_user: dict = Depends(require_admin)
):
    """
    Admin edits a member's full profile directly — no approval step,
    since the admin performing this IS the approver.

    Reuses the exact same field set and semantics as the member
    self-service update flow (see app/routes/profile_updates.py):
    scalar fields go to users (+ mirrored subset to members),
    children/parents lists REPLACE all existing rows for that user.
    """
    SCALAR_FIELDS = [
        "full_name", "phone_number", "email", "id_number", "date_of_birth",
        "marital_status", "residence", "court", "house_number", "spouse_name",
        "next_of_kin_name", "next_of_kin_phone", "next_of_kin_2", "nok2_phone",
        "membership_no",
    ]
    MEMBERS_MIRROR_FIELDS = {
        "full_name", "phone_number", "id_number",
        "next_of_kin_name", "next_of_kin_phone", "membership_no",
    }

    data = body.dict(exclude_none=True)
    children = data.pop("children", None)
    parents  = data.pop("parents", None)
    changes  = {f: data[f] for f in SCALAR_FIELDS if f in data}

    if not changes and children is None and parents is None:
        raise HTTPException(400, "No fields provided — nothing to update.")

    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT phone_number FROM members WHERE id=%s", (member_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Member not found")
        old_phone = row[0]

        cur.execute("SELECT id FROM users WHERE phone_number=%s", (old_phone,))
        user_row = cur.fetchone()
        if not user_row:
            raise HTTPException(
                400,
                "This member has no linked users record (never registered/logged in), "
                "so full-profile fields, children, and parents can't be set. "
                "Only basic member fields can be edited via the standard member update."
            )
        user_id = user_row[0]

        if changes.get("membership_no"):
            cur.execute(
                "SELECT id FROM users WHERE membership_no=%s AND id != %s",
                (changes["membership_no"], user_id)
            )
            if cur.fetchone():
                raise HTTPException(400, "Member ID is already in use by another member")

        # 1. Apply scalar changes to users table
        if changes:
            set_u = ", ".join(f"{k}=%s" for k in changes)
            cur.execute(f"UPDATE users SET {set_u} WHERE id=%s", (*changes.values(), user_id))

            # 2. Mirror subset to members table
            m_changes = {k: v for k, v in changes.items() if k in MEMBERS_MIRROR_FIELDS}
            if m_changes:
                set_m = ", ".join(f"{k}=%s" for k in m_changes)
                cur.execute(f"UPDATE members SET {set_m} WHERE id=%s",
                            (*m_changes.values(), member_id))

        # 3. Replace children if provided
        if children is not None:
            cur.execute("DELETE FROM member_children WHERE user_id=%s", (user_id,))
            for c in children:
                if c.get("full_name"):
                    cur.execute("""
                        INSERT INTO member_children
                            (user_id, full_name, date_of_birth, relationship, cert_number)
                        VALUES (%s,%s,%s,%s,%s)
                    """, (user_id, c.get("full_name"), c.get("date_of_birth"),
                          c.get("relationship"), c.get("cert_number")))

        # 4. Replace parents if provided
        if parents is not None:
            cur.execute("DELETE FROM member_parents WHERE user_id=%s", (user_id,))
            for p in parents:
                if p.get("full_name"):
                    cur.execute("""
                        INSERT INTO member_parents
                            (user_id, full_name, status, id_number, current_residence, contact_phone)
                        VALUES (%s,%s,%s,%s,%s,%s)
                    """, (user_id, p.get("full_name"), p.get("status"), p.get("id_number"),
                          p.get("current_residence"), p.get("contact_phone")))

        conn.commit()

        from app.routes.audit import log_action, get_actor_name
        actor = get_actor_name(cur, current_user)
        changed_fields = list(changes.keys()) + (["children"] if children is not None else []) + (["parents"] if parents is not None else [])
        log_action("Member Profile Updated", actor,
                    detail=f"Fields updated: {', '.join(changed_fields) or 'none'}",
                    target=f"member #{member_id}")

        return {"message": "Member profile updated by admin."}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        release_connection(conn)


@router.patch("/{member_id}/deactivate")
def deactivate_member(
    member_id: int,
    current_user=Depends(require_secretary)        # secretary and above can deactivate
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT full_name, phone_number FROM members WHERE id=%s", (member_id,))
    row = cur.fetchone()
    cur.execute("UPDATE members SET status='inactive' WHERE id=%s", (member_id,))
    # Also lock the member's login — status='inactive' alone did NOT stop
    # them from signing in; is_active is the field /auth/login actually
    # checks. Matched via phone_number, the same link /auth/me uses.
    if row and row[1]:
        cur.execute("UPDATE users SET is_active=false WHERE phone_number=%s", (row[1],))
    conn.commit()

    from app.routes.audit import log_action, get_actor_name
    actor = get_actor_name(cur, current_user)
    log_action("Member Deactivated", actor, detail="Status set to inactive; login access revoked",
                target=row[0] if row else f"member #{member_id}")

    cur.close()
    release_connection(conn)
    return {"message": "Member deactivated"}


@router.patch("/{member_id}/reactivate")
def reactivate_member(
    member_id: int,
    current_user=Depends(require_secretary)
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT full_name, phone_number FROM members WHERE id=%s", (member_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        release_connection(conn)
        raise HTTPException(status_code=404, detail="Member not found")
    cur.execute("UPDATE members SET status='active' WHERE id=%s", (member_id,))
    if row[1]:
        cur.execute("UPDATE users SET is_active=true WHERE phone_number=%s", (row[1],))
    conn.commit()

    from app.routes.audit import log_action, get_actor_name
    actor = get_actor_name(cur, current_user)
    log_action("Member Reactivated", actor, detail="Status set to active; login access restored",
                target=row[0])

    cur.close()
    release_connection(conn)
    return {"message": "Member reactivated"}


@router.delete("/{member_id}")
def delete_member(
    member_id: int,
    current_user=Depends(require_secretary)        # secretary and above can delete
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT full_name, phone_number FROM members WHERE id=%s", (member_id,))
    existing = cur.fetchone()
    cur.execute("DELETE FROM members WHERE id=%s RETURNING id", (member_id,))
    deleted = cur.fetchone()
    # members and users are only linked by phone_number, not a FK — deleting
    # the members row alone leaves the users row (and its is_active=true)
    # untouched, so the account can still log in. Lock it out here too.
    if deleted and existing and existing[1]:
        cur.execute(
            "UPDATE users SET is_active=false, registration_status='deleted' WHERE phone_number=%s",
            (existing[1],)
        )
    conn.commit()

    if deleted:
        from app.routes.audit import log_action, get_actor_name
        actor = get_actor_name(cur, current_user)
        log_action("Member Deleted", actor, detail="Member record permanently deleted",
                    target=existing[0] if existing else f"member #{member_id}")

    cur.close()
    release_connection(conn)
    if not deleted:
        raise HTTPException(status_code=404, detail="Member not found")
    return {"message": "Member deleted"}


@router.patch("/{member_id}/role")
def change_member_role(
    member_id: int,
    body: dict,
    current_user=Depends(require_super_admin)   # only super_admin can change roles
):
    """
    Change a member's role (in the members table).
    Also syncs the role to the users table if a matching phone_number exists.
    Only super_admin can call this.
    """
    valid_roles = ("member", "admin", "treasurer", "secretary", "chairperson", "super_admin")
    new_role = body.get("role")
    if new_role not in valid_roles:
        raise HTTPException(status_code=400, detail=f"Role must be one of {valid_roles}")

    # Prevent self-demotion/promotion (compare against user_id in JWT)
    if str(member_id) == str(current_user.get("user_id")) and False:
        raise HTTPException(status_code=400, detail="You cannot change your own role")

    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE members SET role=%s WHERE id=%s RETURNING full_name, phone_number",
            (new_role, member_id)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Member not found")
        full_name, phone_number = row

        # Sync to users table (same phone_number)
        cur.execute(
            "UPDATE users SET role=%s WHERE phone_number=%s",
            (new_role, phone_number)
        )
        conn.commit()

        from app.routes.audit import log_action, get_actor_name
        actor = get_actor_name(cur, current_user)
        log_action("Role Changed", actor, detail=f"Role changed to {new_role}", target=full_name)

        return {"message": f"{full_name}'s role updated to {new_role}"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        release_connection(conn)