from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.routes import members, events, meetings, auth, contributions, loans, event_contributions, finance, profile_updates, assets, projects
from app.routes import statements, audit, password_reset, meeting_attendance, disbursements
from app.routes.auth import get_current_user, require_admin
from app.database import init_pool, get_connection, release_connection
from app.schemas import NoticeCreate
from app.routes import event_reports
from app.routers import case_management

import os
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

app = FastAPI(title="ChamaLink API", redirect_slashes=True)

Limiter = Limiter(key_func=get_remote_address)
app.state.Limiter = Limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:8080,http://127.0.0.1:8080"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,                    prefix="/auth",          tags=["Auth"])
app.include_router(members.router,                 prefix="/members",       tags=["Members"])
app.include_router(events.router,                  prefix="/events",        tags=["Events"])
app.include_router(meetings.router,                prefix="/meetings",      tags=["Meetings"])
app.include_router(contributions.router,           prefix="/contributions", tags=["Contributions"])
app.include_router(loans.router,                   prefix="/loans",         tags=["Loans"])
app.include_router(event_contributions.router,     prefix="/events",        tags=["Event Contributions"])
app.include_router(finance.router,                 prefix="/finance",       tags=["Finance"])
app.include_router(profile_updates.router,       prefix="/profile-updates", tags=["Profile Updates"])
app.include_router(event_reports.router,           prefix="/event-reports", tags=["Event Reports"])
app.include_router(case_management.router)
app.include_router(assets.router)
app.include_router(projects.router)
app.include_router(statements.router)
app.include_router(audit.router)
app.include_router(password_reset.router)
app.include_router(meeting_attendance.router)
app.include_router(disbursements.router)
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
def startup():
    init_pool()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS finance (
                id          SERIAL PRIMARY KEY,
                type        TEXT NOT NULL CHECK (type IN ('income','expense')),
                category    TEXT NOT NULL,
                amount      NUMERIC(12,2) NOT NULL,
                description TEXT,
                date        DATE NOT NULL DEFAULT CURRENT_DATE,
                recorded_by TEXT,
                created_at  TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS profile_update_requests (
                id SERIAL PRIMARY KEY,
                user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                requested_at TIMESTAMP NOT NULL DEFAULT NOW(),
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                reviewed_by INT REFERENCES users(id),
                reviewed_at TIMESTAMP,
                reject_reason TEXT,
                full_name VARCHAR(200), email VARCHAR(200), id_number VARCHAR(20),
                date_of_birth DATE, marital_status VARCHAR(30), residence VARCHAR(200),
                court VARCHAR(100), house_number VARCHAR(50), spouse_name VARCHAR(200),
                next_of_kin_name VARCHAR(200), next_of_kin_phone VARCHAR(20),
                next_of_kin_2 VARCHAR(200), nok2_phone VARCHAR(20)
            );

            CREATE TABLE IF NOT EXISTS notices (
                id         SERIAL PRIMARY KEY,
                title      TEXT NOT NULL,
                body       TEXT NOT NULL,
                priority   TEXT DEFAULT 'normal',
                created_by TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS member_parents (
                id                 SERIAL PRIMARY KEY,
                user_id            INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                full_name          VARCHAR(200),
                id_number          VARCHAR(20),
                current_residence  VARCHAR(200),
                contact_phone      VARCHAR(20)
            );

            CREATE TABLE IF NOT EXISTS case_reports (
                id                   SERIAL PRIMARY KEY,
                user_id              INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title                VARCHAR(300) NOT NULL,
                event_type           VARCHAR(50)  NOT NULL,
                description          TEXT,
                occurrence_date      DATE,
                affected_member_name VARCHAR(200),
                status               VARCHAR(20)  NOT NULL DEFAULT 'pending'
                                         CHECK (status IN ('pending','approved','rejected')),
                reject_reason        TEXT,
                published_event_id   INT REFERENCES events(id),
                reviewed_by          INT REFERENCES users(id),
                reviewed_at          TIMESTAMP,
                submitted_at         TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """)
        for col_sql in [
            "ALTER TABLE profile_update_requests ADD COLUMN IF NOT EXISTS phone_number  VARCHAR(20)",
            "ALTER TABLE profile_update_requests ADD COLUMN IF NOT EXISTS children_json TEXT",
            "ALTER TABLE profile_update_requests ADD COLUMN IF NOT EXISTS parents_json  TEXT",
            "ALTER TABLE member_parents ADD COLUMN IF NOT EXISTS status VARCHAR(20)",
        ]:
            cur.execute(col_sql)
        conn.commit()
    finally:
        cur.close()
        release_connection(conn)


# ── Static page routes ────────────────────────────────────────────────
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

@app.get("/member")
def member_portal():
    return FileResponse("member.html")


# ── Notices (public read, admin write) ───────────────────────────────
@app.get("/notices")
def get_notices():
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, title, body, priority, created_by, created_at
            FROM notices ORDER BY created_at DESC LIMIT 20
        """)
        rows = cur.fetchall()
        return [{"id": r[0], "title": r[1], "body": r[2], "priority": r[3],
                 "created_by": r[4], "created_at": str(r[5])} for r in rows]
    except Exception:
        return []
    finally:
        cur.close()
        release_connection(conn)


@app.post("/notices")
def post_notice(body: NoticeCreate, current_user=Depends(require_admin)):
    """Admin only — post a notice. Table is guaranteed to exist from startup."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO notices (title, body, priority, created_by)
            VALUES (%s, %s, %s, %s) RETURNING id
        """, (body.title, body.body, body.priority, current_user.get("sub")))
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
        cur.execute("DELETE FROM notices WHERE id=%s RETURNING id", (notice_id,))
        deleted = cur.fetchone()
        conn.commit()
        if not deleted:
            from fastapi import HTTPException
            raise HTTPException(404, "Notice not found")
        return {"message": "Notice deleted"}
    finally:
        cur.close()
        release_connection(conn)


# ── Dashboard stats ───────────────────────────────────────────────────
@app.get("/stats")
def dashboard_stats(_=Depends(get_current_user)):
    """Single endpoint for dashboard summary cards."""
    from datetime import date
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*) FROM members WHERE status='active'")
        active_members = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM members")
        total_members = cur.fetchone()[0]

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

        cur.execute("SELECT COUNT(*) FROM events WHERE case_no IS NOT NULL")
        total_cases = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM events WHERE case_no IS NOT NULL AND status='open'")
        open_cases = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM users WHERE registration_status='pending'")
        pending_users = cur.fetchone()[0]

        # Assets & Projects counts (tables may not exist yet — handle gracefully)
        try:
            cur.execute("SELECT COUNT(*) FROM assets")
            total_assets = cur.fetchone()[0]
        except Exception:
            conn.rollback()
            total_assets = 0

        try:
            cur.execute("SELECT COUNT(*) FROM projects")
            total_projects = cur.fetchone()[0]
        except Exception:
            conn.rollback()
            total_projects = 0

        return {
            "active_members": active_members,
            "total_members": total_members,
            "this_month_contributions": this_month_contrib,
            "all_time_contributions": all_time_contrib,
            "active_loans": active_loans,
            "outstanding_balance": outstanding,
            "open_events": open_events,
            "total_cases": total_cases,
            "open_cases": open_cases,
            "pending_registrations": pending_users,
            "total_assets": total_assets,
            "total_projects": total_projects,
        }
    finally:
        cur.close()
        release_connection(conn)