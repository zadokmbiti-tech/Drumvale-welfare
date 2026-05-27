from fastapi import APIRouter, HTTPException, Depends
from app.database import get_connection
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
        conn.close()


@router.get("/")
def list_members(_=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, full_name, phone_number, role, status, date_joined FROM members ORDER BY full_name"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {"id": r[0], "full_name": r[1], "phone_number": r[2],
         "role": r[3], "status": r[4], "date_joined": r[5]}
        for r in rows
    ]


@router.get("/{member_id}")
def get_member(member_id: int, _=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM members WHERE id = %s", (member_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Member not found")
    return {
        "id": row[0], "full_name": row[1], "phone_number": row[2],
        "id_number": row[3], "role": row[4], "status": row[5],
        "date_joined": row[6], "next_of_kin_name": row[7],
        "next_of_kin_phone": row[8], "notes": row[9]
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
        conn.close()


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
    conn.close()
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
    conn.close()
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
        conn.close()


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
    conn.close()
    return [r[0] for r in rows]