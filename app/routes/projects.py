from fastapi import APIRouter, Depends, HTTPException
from app.database import get_connection, release_connection
from app.routes.auth import get_current_user, require_admin

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("/")
def list_projects(_=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, name, budget, status, deadline, created_at FROM projects ORDER BY id DESC"
        )
        rows = cur.fetchall()
        return [
            {"id": r[0], "name": r[1],
             "budget": float(r[2]) if r[2] else 0,
             "status": r[3], "deadline": str(r[4]) if r[4] else None,
             "created_at": str(r[5])}
            for r in rows
        ]
    finally:
        cur.close()
        release_connection(conn)


@router.post("/")
def add_project(data: dict, _=Depends(require_admin)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO projects (name, budget, status, deadline) VALUES (%s, %s, %s, %s) RETURNING id",
            (data.get("name"), data.get("budget", 0), data.get("status", "planning"), data.get("deadline"))
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return {"id": new_id, "message": "Project added"}
    finally:
        cur.close()
        release_connection(conn)


@router.delete("/{project_id}")
def delete_project(project_id: int, _=Depends(require_admin)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM projects WHERE id=%s", (project_id,))
        conn.commit()
        return {"message": "Project deleted"}
    finally:
        cur.close()
        release_connection(conn)
