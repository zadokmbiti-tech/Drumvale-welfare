from fastapi import APIRouter, Depends, HTTPException
from app.database import get_connection, release_connection
from app.routes.auth import get_current_user, require_admin

router = APIRouter(prefix="/assets", tags=["assets"])


@router.get("/")
def list_assets(_=Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, name, category, value, status, created_at FROM assets ORDER BY id DESC"
        )
        rows = cur.fetchall()
        return [
            {"id": r[0], "name": r[1], "category": r[2],
             "value": float(r[3]) if r[3] else 0,
             "status": r[4], "created_at": str(r[5])}
            for r in rows
        ]
    finally:
        cur.close()
        release_connection(conn)


@router.post("/")
def add_asset(data: dict, current_user=Depends(require_admin)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO assets (name, category, value, status) VALUES (%s, %s, %s, %s) RETURNING id",
            (data.get("name"), data.get("category"), data.get("value", 0), data.get("status", "available"))
        )
        new_id = cur.fetchone()[0]
        conn.commit()

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Asset Created",
                         detail=f"Category: {data.get('category','—')}, Value: {data.get('value',0)}",
                         target=data.get("name", f"asset #{new_id}"))

        return {"id": new_id, "message": "Asset added"}
    finally:
        cur.close()
        release_connection(conn)


@router.delete("/{asset_id}")
def delete_asset(asset_id: int, current_user=Depends(require_admin)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT name FROM assets WHERE id=%s", (asset_id,))
        existing = cur.fetchone()
        cur.execute("DELETE FROM assets WHERE id=%s", (asset_id,))
        conn.commit()

        from app.routes.audit import log_user_action
        log_user_action(current_user, "Asset Deleted", detail="Asset removed",
                         target=existing[0] if existing else f"asset #{asset_id}")

        return {"message": "Asset deleted"}
    finally:
        cur.close()
        release_connection(conn)