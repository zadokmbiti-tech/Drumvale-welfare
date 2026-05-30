import csv
import io
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from fastapi.responses import StreamingResponse
from app.database import get_connection, release_connection
from app.models import MemberCreate
from app.routes.auth import get_current_user
from app.auth_deps import require_secretary, require_chairperson, require_super_admin

router = APIRouter()


@router.post("/")
def add_member(
    member: MemberCreate,
    _=Depends(require_secretary)        # secretary and above can add members
):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO members (full_name, phone_number, id_number, role, status,
                date_joined, next_of_kin_name, next_of_kin_phone, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (
            member.full_name, member.phone_number, member.id_number,
            member.role, member.status, member.date_joined,
            member.next_of_kin_name, member.next_of_kin_phone, member.notes
        ))
        new_id = cur.fetchone()[0]
        conn.commit()
        return {"message": "Member added", "id": new_id}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        release_connection(conn)


@router.get("/")
def list_members(_=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, full_name, phone_number, role, status, date_joined FROM members ORDER BY full_name"
    )
    rows = cur.fetchall()
    cur.close()
    release_connection(conn)
    return [
        {"id": r[0], "full_name": r[1], "phone_number": r[2],
         "role": r[3], "status": r[4], "date_joined": r[5]}
        for r in rows
    ]


@router.get("/{member_id}")
def get_member(member_id: int, _=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        # Get member + full registration details from users table
        cur.execute("""
            SELECT m.id, m.full_name, m.phone_number, m.id_number, m.role, m.status,
                   m.date_joined, m.next_of_kin_name, m.next_of_kin_phone, m.notes,
                   u.email, u.date_of_birth, u.marital_status, u.residence,
                   u.court, u.house_number, u.spouse_name,
                   u.next_of_kin_2, u.nok2_phone
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
            SELECT full_name, id_number, current_residence, contact_phone
            FROM member_parents
            WHERE user_id = (SELECT id FROM users WHERE phone_number = %s)
            ORDER BY id
        """, (row[2],))
        parents = [
            {"full_name": p[0], "id_number": p[1],
             "current_residence": p[2], "contact_phone": p[3]}
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
        "children": children,
        "parents": parents
    }


@router.put("/{member_id}")
def update_member(
    member_id: int,
    member: MemberCreate,
    _=Depends(require_secretary)        # secretary and above can edit members
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
        return {"message": "Member updated"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        release_connection(conn)


@router.patch("/{member_id}/deactivate")
def deactivate_member(
    member_id: int,
    _=Depends(require_secretary)        # secretary and above can deactivate
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE members SET status='inactive' WHERE id=%s", (member_id,))
    conn.commit()
    cur.close()
    release_connection(conn)
    return {"message": "Member deactivated"}


@router.delete("/{member_id}")
def delete_member(
    member_id: int,
    _=Depends(require_secretary)        # secretary and above can delete
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM members WHERE id=%s RETURNING id", (member_id,))
    deleted = cur.fetchone()
    conn.commit()
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
    valid_roles = ("member", "treasurer", "secretary", "chairperson", "super_admin")
    new_role = body.get("role")
    if new_role not in valid_roles:
        raise HTTPException(status_code=400, detail=f"Role must be one of {valid_roles}")

    # Prevent self-demotion/promotion (compare against user_id in JWT)
    if str(member_id) == str(current_user.get("user_id")):
        raise HTTPException(status_code=400, detail="You cannot change your own role")

    conn = get_connection()
    cur = conn.cursor()
    try:
        # Update members table
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
        return {"message": f"{full_name}'s role updated to {new_role}"}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        release_connection(conn)


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


@router.post("/bulk-import")
async def bulk_import_members(
    file: UploadFile = File(...),
    _=Depends(require_secretary)
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
            id_number        = (row.get("id_number") or "").strip() or None
            role             = (row.get("role") or "member").strip()
            status           = (row.get("status") or "active").strip()
            date_joined      = (row.get("date_joined") or "").strip() or None
            nok_name         = (row.get("next_of_kin_name") or "").strip() or None
            nok_phone        = (row.get("next_of_kin_phone") or "").strip() or None
            notes            = (row.get("notes") or "").strip() or None
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
            except Exception as e:
                conn.rollback()
                errors.append({"row": i, "error": str(e)})
                continue
        conn.commit()
    finally:
        cur.close()
        release_connection(conn)

    return {
        "inserted": inserted,
        "errors": errors,
        "message": f"{inserted} member(s) imported, {len(errors)} error(s)"
    }


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