from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.routes import members, events, meetings, auth, contributions, loans, event_contributions, finance
from app.routes.auth import get_current_user, require_admin
from datetime import datetime
from app.database import init_pool, get_connection, release_connection
import os

app = FastAPI(title="ChamaLink API")


@app.on_event("startup")
def startup():
    init_pool()

@app.get("/member")
def member_portal():
    return FileResponse("member.html")


@app.get("/notices")
def get_notices():
    """Public — anyone can read notices"""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, title, body, priority, created_by, created_at
            FROM notices
            ORDER BY created_at DESC
            LIMIT 20
        """)
        rows = cur.fetchall()
        return [{"id":r[0],"title":r[1],"body":r[2],"priority":r[3],
                 "created_by":r[4],"created_at":str(r[5])} for r in rows]
    except:
        return []
    finally:
        cur.close()
        release_connection(conn)

@app.post("/notices")
def post_notice(body: dict, current_user=Depends(require_admin)):
    """Admin only — post a notice"""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notices (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                priority TEXT DEFAULT 'normal',
                created_by TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            INSERT INTO notices (title, body, priority, created_by)
            VALUES (%s, %s, %s, %s) RETURNING id
        """, (body.get("title"), body.get("body"),
              body.get("priority","normal"), current_user.get("sub")))
        new_id = cur.fetchone()[0]
        conn.commit()
        return {"id": new_id, "message": "Notice posted successfully"}
    except Exception as e:
        conn.rollback()
        raise
    finally:
        cur.close()
        release_connection(conn)

@app.delete("/notices/{notice_id}")
def delete_notice(notice_id: int, current_user=Depends(require_admin)):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM notices WHERE id=%s", (notice_id,))
        conn.commit()
        return {"message": "Notice deleted"}
    finally:
        cur.close()
        release_connection(conn)

ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:8080,http://127.0.0.1:8080,https://drumvale-welfare.onrender.com"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,          prefix="/auth",          tags=["Auth"])
app.include_router(members.router,       prefix="/members",       tags=["Members"])
app.include_router(events.router,        prefix="/events",        tags=["Events"])
app.include_router(meetings.router,      prefix="/meetings",      tags=["Meetings"])
app.include_router(contributions.router, prefix="/contributions", tags=["Contributions"])
app.include_router(loans.router,         prefix="/loans",         tags=["Loans"])
app.include_router(event_contributions.router, prefix="/events", tags=["Event Contributions"])
app.include_router(finance.router,             prefix="/finance",        tags=["Finance"])

if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def root():
    return FileResponse("index.html")


@app.get("/about")
def about():
    return FileResponse("index.html")


@app.get("/contact")
def contact():
    return FileResponse("index.html")


@app.get("/dashboard")
def dashboard():
    return FileResponse("dashboard.html")


@app.get("/stats")
def dashboard_stats(_=Depends(get_current_user)):
    """Single endpoint for dashboard summary cards — avoids 5 separate fetches."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM members WHERE status='active'")
        active_members = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM members")
        total_members = cur.fetchone()[0]

        from datetime import date
        this_month = date.today().strftime("%Y-%m")
        cur.execute(
            "SELECT COALESCE(SUM(amount),0) FROM monthly_contributions WHERE month=%s",
            (this_month,)
        )
        this_month_contrib = float(cur.fetchone()[0])

        cur.execute("SELECT COALESCE(SUM(amount),0) FROM monthly_contributions")
        all_time_contrib = float(cur.fetchone()[0])

        cur.execute("SELECT COUNT(*) FROM loans WHERE status='disbursed'")
        active_loans = cur.fetchone()[0]

        cur.execute(
            "SELECT COALESCE(SUM(total_repayable - amount_repaid),0) FROM loans WHERE status='disbursed'"
        )
        outstanding = float(cur.fetchone()[0])

        cur.execute("SELECT COUNT(*) FROM events WHERE status='open'")
        open_events = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM users WHERE registration_status='pending'")
        pending_users = cur.fetchone()[0]

        return {
            "active_members": active_members,
            "total_members": total_members,
            "this_month_contributions": this_month_contrib,
            "all_time_contributions": all_time_contrib,
            "active_loans": active_loans,
            "outstanding_balance": outstanding,
            "open_events": open_events,
            "pending_registrations": pending_users,
        }
    finally:
        cur.close()
        release_connection(conn)