from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.routes import members, events, meetings, auth, contributions, loans, event_contributions
from app.routes.auth import get_current_user
from app.database import init_pool, get_connection, release_connection
import os

app = FastAPI(title="ChamaLink API")


@app.on_event("startup")
def startup():
    init_pool()


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
