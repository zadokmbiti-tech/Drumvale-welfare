"""
Member self-service & reporting routes
  GET  /statements/my                  — member's own full statement
  GET  /statements/defaulters          — admin: members with arrears
  GET  /statements/finance-report      — admin: monthly/annual P&L
  GET  /statements/member/{id}         — admin: any member's statement
  POST /auth/reset-password-request    — request OTP (phone-based)
  POST /auth/reset-password            — confirm OTP + new password
"""
from fastapi import APIRouter, Depends, HTTPException
from app.database import get_connection, release_connection
from app.routes.auth import get_current_user, require_admin
from datetime import date, datetime
from dateutil.relativedelta import relativedelta

router = APIRouter(prefix="/statements", tags=["Statements"])


def _member_statement(member_id: int):
    """Build a full statement dict for a member."""
    conn = get_connection()
    cur  = conn.cursor()
    try:
        # Basic info
        cur.execute("SELECT id, full_name, phone_number, role, status, date_joined FROM members WHERE id=%s", (member_id,))
        row = cur.fetchone()
        if not row:
            return None
        member = {"id": row[0], "full_name": row[1], "phone_number": row[2],
                  "role": row[3], "status": row[4], "date_joined": str(row[5]) if row[5] else None}

        # Monthly contributions
        cur.execute("""
            SELECT month, amount, payment_method, reference, recorded_at
            FROM monthly_contributions WHERE member_id=%s ORDER BY month DESC
        """, (member_id,))
        contributions = [{"month": r[0], "amount": float(r[1]), "payment_method": r[2],
                          "reference": r[3], "recorded_at": str(r[4])} for r in cur.fetchall()]
        total_contributions = sum(c["amount"] for c in contributions)

        # Loans
        cur.execute("""
            SELECT id, amount, interest_rate, total_repayable, amount_repaid,
                   status, due_date, created_at, disbursed_at
            FROM loans WHERE member_id=%s ORDER BY created_at DESC
        """, (member_id,))
        loans = []
        for r in cur.fetchall():
            total_repayable = float(r[3]) if r[3] else float(r[1]) * (1 + float(r[2]) / 100)
            balance = max(0, total_repayable - float(r[4]))
            overdue = False
            if r[5] == 'active' and r[6] and date.today() > r[6]:
                overdue = True
            loans.append({
                "id": r[0], "amount": float(r[1]), "interest_rate": float(r[2]),
                "total_repayable": total_repayable, "amount_repaid": float(r[4]),
                "balance": balance, "status": r[5],
                "due_date": str(r[6]) if r[6] else None,
                "created_at": str(r[7]), "disbursed_at": str(r[8]) if r[8] else None,
                "overdue": overdue
            })
        total_loan_balance = sum(l["balance"] for l in loans if l["status"] == "active")

        # Event contributions
        cur.execute("""
            SELECT e.title, c.amount, c.payment_method, c.recorded_at
            FROM contributions c JOIN events e ON e.id=c.event_id
            WHERE c.member_id=%s ORDER BY c.recorded_at DESC
        """, (member_id,))
        event_contribs = [{"event": r[0], "amount": float(r[1]),
                            "payment_method": r[2], "recorded_at": str(r[3])}
                          for r in cur.fetchall()]
        total_event_contribs = sum(e["amount"] for e in event_contribs)

        # Arrears — months from joining up to now where no contribution recorded
        if member["date_joined"]:
            try:
                joined = date.fromisoformat(member["date_joined"])
            except Exception:
                joined = date.today().replace(day=1)
        else:
            joined = date.today().replace(day=1)

        paid_months = {c["month"] for c in contributions}
        arrears_months = []
        cursor_month = joined.replace(day=1)
        this_month = date.today().replace(day=1)
        while cursor_month <= this_month:
            ym = cursor_month.strftime("%Y-%m")
            if ym not in paid_months:
                arrears_months.append(ym)
            cursor_month += relativedelta(months=1)

        # Standard contribution amount (get from most common)
        cur.execute("SELECT COALESCE(mode() WITHIN GROUP (ORDER BY amount), 500) FROM monthly_contributions")
        std_amount = float(cur.fetchone()[0] or 500)
        total_arrears_amount = len(arrears_months) * std_amount

        return {
            "member": member,
            "contributions": contributions,
            "total_contributions": total_contributions,
            "loans": loans,
            "total_loan_balance": total_loan_balance,
            "event_contributions": event_contribs,
            "total_event_contributions": total_event_contribs,
            "arrears_months": arrears_months,
            "arrears_count": len(arrears_months),
            "total_arrears_amount": total_arrears_amount,
            "std_monthly_amount": std_amount,
            "generated_at": datetime.now().isoformat(),
        }
    finally:
        cur.close()
        release_connection(conn)


@router.get("/my")
def my_statement(current_user: dict = Depends(get_current_user)):
    """Member views their own statement."""
    # The JWT payload only carries sub/user_id/role/exp — phone_number and
    # full_name aren't in it, so they must be looked up from the DB via
    # user_id rather than read off current_user directly.
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT phone_number, full_name FROM users WHERE id=%s",
                    (current_user.get("user_id"),))
        user_row = cur.fetchone()
        if not user_row:
            raise HTTPException(status_code=404, detail="No account found for this token")
        phone_number, full_name = user_row

        cur.execute("SELECT id FROM members WHERE phone_number=%s AND status='active'",
                    (phone_number,))
        row = cur.fetchone()
        if not row:
            # Try by name
            cur.execute("SELECT id FROM members WHERE full_name=%s LIMIT 1",
                        (full_name,))
            row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No member record found for your account")
        member_id = row[0]
    finally:
        cur.close()
        release_connection(conn)

    stmt = _member_statement(member_id)
    if not stmt:
        raise HTTPException(status_code=404, detail="Member record not found")
    return stmt


@router.get("/member/{member_id}")
def member_statement(member_id: int, _=Depends(require_admin)):
    """Admin views any member's statement."""
    stmt = _member_statement(member_id)
    if not stmt:
        raise HTTPException(status_code=404, detail="Member not found")
    return stmt


@router.get("/defaulters")
def defaulters_report(_=Depends(require_admin)):
    """List all members with contribution arrears or overdue loans."""
    conn = get_connection()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT id, full_name, phone_number, date_joined FROM members WHERE status='active' ORDER BY full_name")
        members = cur.fetchall()

        cur.execute("SELECT COALESCE(mode() WITHIN GROUP (ORDER BY amount), 500) FROM monthly_contributions")
        std_amount = float(cur.fetchone()[0] or 500)

        this_month = date.today().replace(day=1)
        result = []

        for m in members:
            member_id, full_name, phone, joined_date = m

            if joined_date:
                joined = joined_date if isinstance(joined_date, date) else date.fromisoformat(str(joined_date))
                joined = joined.replace(day=1)
            else:
                joined = this_month

            cur.execute("SELECT month FROM monthly_contributions WHERE member_id=%s", (member_id,))
            paid_months = {r[0] for r in cur.fetchall()}

            arrears = []
            cursor_m = joined
            while cursor_m <= this_month:
                ym = cursor_m.strftime("%Y-%m")
                if ym not in paid_months:
                    arrears.append(ym)
                cursor_m += relativedelta(months=1)

            cur.execute("""
                SELECT COALESCE(SUM(amount - amount_repaid), 0)
                FROM loans WHERE member_id=%s AND status='active'
            """, (member_id,))
            loan_balance = float(cur.fetchone()[0] or 0)

            cur.execute("""
                SELECT COUNT(*) FROM loans
                WHERE member_id=%s AND status='active' AND due_date < %s
            """, (member_id, date.today()))
            overdue_loans = cur.fetchone()[0]

            if arrears or loan_balance > 0:
                result.append({
                    "member_id": member_id,
                    "full_name": full_name,
                    "phone_number": phone,
                    "arrears_months": len(arrears),
                    "arrears_amount": len(arrears) * std_amount,
                    "loan_balance": loan_balance,
                    "overdue_loans": overdue_loans,
                    "total_owed": (len(arrears) * std_amount) + loan_balance,
                })

        result.sort(key=lambda x: x["total_owed"], reverse=True)
        return {"defaulters": result, "std_monthly_amount": std_amount, "generated_at": datetime.now().isoformat()}
    finally:
        cur.close()
        release_connection(conn)


@router.get("/finance-report")
def finance_report(year: int = None, month: str = None, _=Depends(require_admin)):
    """Monthly or annual financial summary."""
    conn = get_connection()
    cur  = conn.cursor()
    try:
        today = date.today()
        y = year or today.year

        # Monthly contributions per month
        cur.execute("""
            SELECT month, SUM(amount) FROM monthly_contributions
            WHERE month LIKE %s GROUP BY month ORDER BY month
        """, (f"{y}-%",))
        monthly_contribs = {r[0]: float(r[1]) for r in cur.fetchall()}

        # Loan repayments per month
        cur.execute("""
            SELECT TO_CHAR(paid_at,'YYYY-MM'), SUM(amount) FROM loan_repayments
            WHERE EXTRACT(YEAR FROM paid_at)=%s GROUP BY 1 ORDER BY 1
        """, (y,))
        loan_repayments = {r[0]: float(r[1]) for r in cur.fetchall()}

        # Finance ledger income/expense per month
        cur.execute("""
            SELECT TO_CHAR(date,'YYYY-MM'), type, SUM(amount) FROM finance
            WHERE EXTRACT(YEAR FROM date)=%s GROUP BY 1,2 ORDER BY 1
        """, (y,))
        ledger = {}
        for r in cur.fetchall():
            ledger.setdefault(r[0], {"income": 0, "expense": 0})
            ledger[r[0]][r[1]] += float(r[2])

        # Loans disbursed per month
        cur.execute("""
            SELECT TO_CHAR(disbursed_at,'YYYY-MM'), SUM(amount) FROM loans
            WHERE EXTRACT(YEAR FROM disbursed_at)=%s AND status IN ('active','repaid')
            GROUP BY 1 ORDER BY 1
        """, (y,))
        disbursements = {r[0]: float(r[1]) for r in cur.fetchall()}

        # Event contributions per month
        cur.execute("""
            SELECT TO_CHAR(recorded_at,'YYYY-MM'), SUM(amount) FROM contributions
            WHERE EXTRACT(YEAR FROM recorded_at)=%s GROUP BY 1 ORDER BY 1
        """, (y,))
        event_contribs = {r[0]: float(r[1]) for r in cur.fetchall()}

        # Totals
        total_income = sum(monthly_contribs.values()) + sum(loan_repayments.values()) + \
                       sum(v["income"] for v in ledger.values()) + sum(event_contribs.values())
        total_expense = sum(v["expense"] for v in ledger.values()) + sum(disbursements.values())

        # Active loan book
        cur.execute("SELECT COALESCE(SUM(total_repayable - amount_repaid),0) FROM loans WHERE status='active'")
        loan_book = float(cur.fetchone()[0])

        months = []
        for m_num in range(1, 13):
            ym = f"{y}-{m_num:02d}"
            mc  = monthly_contribs.get(ym, 0)
            lr  = loan_repayments.get(ym, 0)
            ec  = event_contribs.get(ym, 0)
            inc = ledger.get(ym, {}).get("income", 0)
            exp = ledger.get(ym, {}).get("expense", 0)
            dis = disbursements.get(ym, 0)
            months.append({
                "month": ym,
                "monthly_contributions": mc,
                "loan_repayments": lr,
                "event_contributions": ec,
                "other_income": inc,
                "total_income": mc + lr + ec + inc,
                "expenses": exp,
                "loans_disbursed": dis,
                "total_outflow": exp + dis,
                "net": (mc + lr + ec + inc) - (exp + dis),
            })

        return {
            "year": y,
            "months": months,
            "totals": {
                "total_income": total_income,
                "total_expense": total_expense,
                "net": total_income - total_expense,
                "loan_book": loan_book,
            },
            "generated_at": datetime.now().isoformat(),
        }
    finally:
        cur.close()
        release_connection(conn)