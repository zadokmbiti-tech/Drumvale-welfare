# ChamaLink — Drumvale Riverside Welfare Association

![Drumvale Riverside Welfare Association](./Drumvale%20Riverside%20Welfare%20Association.png)

A full-stack web platform that digitizes and manages the operations of **Drumvale Riverside Welfare Association** — a community welfare group based in Kenya. Built to replace manual record-keeping with a secure, accessible, and modern system.

🌐 **Live:** [drumvale-welfare.vercel.app](https://drumvale-welfare.vercel.app)

---

## Features

### Member Portal
- Member registration with full profile (next of kin, children, parents)
- Secure login via phone number or email
- View personal contribution history and loan statements
- Report welfare cases (bereavement, illness, etc.) with urgency levels
- OTP-based password reset (admin-assisted)
- Profile update requests with admin approval workflow

### Admin Dashboard
- Approve, reject, or reinstate member registrations
- Manage monthly contributions and loan disbursements
- Post and manage notices for members
- Track meeting attendance
- Review and publish welfare case reports as events
- Finance management (income/expense tracking)
- Audit logs for all administrative actions
- Role-based access control (super_admin, admin, chairperson, secretary, treasurer)

### Welfare & Events
- Create and manage welfare events
- Track event contributions per member
- Disburse welfare funds with recorded amounts
- Generate defaulters reports

### Financial
- Member statements with contribution and loan history
- Finance reports (income vs expense)
- Assets and projects tracking

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI (Python) |
| Database | PostgreSQL (Neon) |
| Frontend | HTML, CSS, Vanilla JavaScript |
| Auth | JWT + bcrypt |
| Rate Limiting | SlowAPI |
| Deployment | Vercel |
| Static Files | Vercel CDN |

---

## Project Structure

```
├── app/
│   ├── routes/
│   │   ├── auth.py              # Login, register, logout, admin user management
│   │   ├── members.py           # Member CRUD
│   │   ├── contributions.py     # Monthly contributions
│   │   ├── loans.py             # Loan management
│   │   ├── events.py            # Welfare events
│   │   ├── finance.py           # Income/expense tracking
│   │   ├── statements.py        # Member statements
│   │   ├── audit.py             # Audit logs
│   │   ├── password_reset.py    # OTP-based password reset
│   │   ├── meeting_attendance.py
│   │   ├── disbursements.py
│   │   ├── profile_updates.py
│   │   └── ...
│   ├── database.py              # PostgreSQL connection
│   ├── models.py                # Pydantic models
│   ├── schemas.py               # Request/response schemas
│   └── utils.py
├── static/
│   └── files/                   # PDF documents
├── main.py                      # FastAPI app entry point, startup, routers
├── index.html                   # Public homepage / registration
├── dashboard.html               # Admin dashboard
├── member.html                  # Member portal
├── schema.sql                   # Database schema
├── vercel.json                  # Vercel deployment config
└── requirements.txt
```

---

## Getting Started (Local Development)

### Prerequisites
- Python 3.10+
- PostgreSQL database (or a [Neon](https://neon.tech) free account)

### Setup

```bash
# Clone the repo
git clone https://github.com/zadokmbiti-tech/Drumvale-welfare.git
cd Drumvale-welfare

# Install dependencies
pip install -r requirements.txt

# Create a .env file
cp .env.example .env
# Fill in your DATABASE_URL and SECRET_KEY
```

### Environment Variables

```env
DATABASE_URL=postgresql://user:password@host:5432/dbname
SECRET_KEY=your-secret-key-here
ALLOWED_ORIGINS=http://localhost:8000
```

Generate a secret key:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### Run

```bash
uvicorn main:app --reload
```

Visit `http://localhost:8000`

---

## Deployment

The app is deployed on **Vercel** with the following config in `vercel.json`:
- All routes proxied to the FastAPI app via a Python serverless function
- Static files served from `/static`

Environment variables are configured in the Vercel dashboard.

---

## Security

- Passwords hashed with **bcrypt**
- JWT tokens with 24-hour expiry
- Logout invalidates tokens via a **PostgreSQL blacklist**
- OTPs stored in PostgreSQL with 15-minute expiry
- Rate limiting on auth endpoints (login, register, OTP)
- Role-based access control on all admin endpoints
- `.env` and database backups excluded from version control

---

## Developer

Built by **Zadok Mutethia Mbiti**
- GitHub: [@zadokmbiti-tech](https://github.com/zadokmbiti-tech)
- Project: Final year BSc ICT Management — Maseno University

---

## License

Private project. All rights reserved — Drumvale Riverside Welfare Association.