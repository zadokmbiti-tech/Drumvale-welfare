CREATE TABLE IF NOT EXISTS users (
    id                   SERIAL PRIMARY KEY,
    full_name            VARCHAR(200) NOT NULL,
    phone_number         VARCHAR(20)  UNIQUE NOT NULL,
    email                VARCHAR(200) UNIQUE,
    id_number            VARCHAR(20)  UNIQUE,
    role                 VARCHAR(30)  NOT NULL DEFAULT 'member',
    hashed_password      TEXT         NOT NULL,
    is_active            BOOLEAN      NOT NULL DEFAULT false,
    registration_status  VARCHAR(20)  NOT NULL DEFAULT 'pending',
    -- profile fields from UserRegister model
    date_of_birth        DATE,
    marital_status       VARCHAR(30),
    residence            VARCHAR(200),
    court                VARCHAR(100),
    house_number         VARCHAR(50),
    spouse_name          VARCHAR(200),
    next_of_kin_name     VARCHAR(200),
    next_of_kin_phone    VARCHAR(20),
    next_of_kin_2        VARCHAR(200),
    nok2_phone           VARCHAR(20),
    created_at           TIMESTAMP    NOT NULL DEFAULT NOW()
);

-- ============================================================
-- MEMBER CHILDREN  (linked to users)
-- ============================================================
CREATE TABLE IF NOT EXISTS member_children (
    id             SERIAL PRIMARY KEY,
    user_id        INT  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    full_name      VARCHAR(200),
    date_of_birth  DATE,
    relationship   VARCHAR(50),
    cert_number    VARCHAR(50)
);

-- ============================================================
-- MEMBERS  (operational roster — mirrored from users on register)
-- ============================================================
CREATE TABLE IF NOT EXISTS members (
    id                SERIAL PRIMARY KEY,
    full_name         VARCHAR(200) NOT NULL,
    phone_number      VARCHAR(20)  UNIQUE NOT NULL,
    id_number         VARCHAR(20)  UNIQUE,
    role              VARCHAR(30)  NOT NULL DEFAULT 'member',
    status            VARCHAR(20)  NOT NULL DEFAULT 'active',
    date_joined       DATE         NOT NULL,
    next_of_kin_name  VARCHAR(200),
    next_of_kin_phone VARCHAR(20),
    notes             TEXT
);

-- ============================================================
-- MONTHLY CONTRIBUTIONS
-- ============================================================
CREATE TABLE IF NOT EXISTS monthly_contributions (
    id              SERIAL PRIMARY KEY,
    member_id       INT            NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    amount          NUMERIC(12,2)  NOT NULL,
    month           VARCHAR(7)     NOT NULL,   -- format: YYYY-MM
    payment_method  VARCHAR(30)    NOT NULL DEFAULT 'M-Pesa',
    reference       VARCHAR(100),
    notes           TEXT,
    recorded_at     TIMESTAMP      NOT NULL DEFAULT NOW()
);

-- ============================================================
-- LOANS
-- ============================================================
CREATE TABLE IF NOT EXISTS loans (
    id              SERIAL PRIMARY KEY,
    member_id       INT            NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    amount          NUMERIC(12,2)  NOT NULL,
    interest_rate   NUMERIC(5,2)   NOT NULL DEFAULT 10.0,
    purpose         TEXT,
    status          VARCHAR(20)    NOT NULL DEFAULT 'pending',
    total_repayable NUMERIC(12,2),
    amount_repaid   NUMERIC(12,2)  NOT NULL DEFAULT 0,
    approved_by     INT            REFERENCES users(id),
    disbursed_at    TIMESTAMP,
    due_date        DATE,
    repaid_at       TIMESTAMP,
    created_at      TIMESTAMP      NOT NULL DEFAULT NOW()
);

-- ============================================================
-- LOAN REPAYMENTS
-- ============================================================
CREATE TABLE IF NOT EXISTS loan_repayments (
    id              SERIAL PRIMARY KEY,
    loan_id         INT            NOT NULL REFERENCES loans(id) ON DELETE CASCADE,
    amount          NUMERIC(12,2)  NOT NULL,
    payment_method  VARCHAR(30)    NOT NULL DEFAULT 'M-Pesa',
    reference       VARCHAR(100),
    notes           TEXT,
    paid_at         TIMESTAMP      NOT NULL DEFAULT NOW()
);

-- ============================================================
-- MEETINGS
-- ============================================================
CREATE TABLE IF NOT EXISTS meetings (
    id       SERIAL PRIMARY KEY,
    title    VARCHAR(200) NOT NULL,
    date     DATE         NOT NULL,
    time     TIME,
    venue    VARCHAR(200),
    agenda   TEXT,
    minutes  TEXT,
    status   VARCHAR(20)  NOT NULL DEFAULT 'scheduled',
    created_at TIMESTAMP  NOT NULL DEFAULT NOW()
);

-- ============================================================
-- ATTENDANCE  (one row per member per meeting)
-- ============================================================
CREATE TABLE IF NOT EXISTS attendance (
    id          SERIAL PRIMARY KEY,
    meeting_id  INT     NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    member_id   INT     NOT NULL REFERENCES members(id)  ON DELETE CASCADE,
    present     BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (meeting_id, member_id)
);

-- ============================================================
-- EVENTS  (welfare / fundraising)
-- ============================================================
CREATE TABLE IF NOT EXISTS events (
    id              SERIAL PRIMARY KEY,
    title           VARCHAR(200) NOT NULL,
    event_type      VARCHAR(50)  NOT NULL,
    beneficiary_id  INT          REFERENCES members(id),
    description     TEXT,
    target_amount   NUMERIC(12,2) NOT NULL DEFAULT 0,
    status          VARCHAR(20)  NOT NULL DEFAULT 'open',
    date_raised     DATE         NOT NULL DEFAULT CURRENT_DATE,
    date_closed     DATE
);

-- ============================================================
-- EVENT CONTRIBUTIONS  (per-event member contributions)
-- ============================================================
CREATE TABLE IF NOT EXISTS contributions (
    id              SERIAL PRIMARY KEY,
    event_id        INT           NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    member_id       INT           NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    amount          NUMERIC(12,2) NOT NULL,
    payment_method  VARCHAR(30)   NOT NULL DEFAULT 'M-Pesa',
    reference       VARCHAR(100),
    notes           TEXT,
    recorded_at     TIMESTAMP     NOT NULL DEFAULT NOW()
);

-- ============================================================
-- Useful indexes
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_contributions_month    ON monthly_contributions(month);
CREATE INDEX IF NOT EXISTS idx_contributions_member   ON monthly_contributions(member_id);
CREATE INDEX IF NOT EXISTS idx_loans_status           ON loans(status);
CREATE INDEX IF NOT EXISTS idx_loans_member           ON loans(member_id);
CREATE INDEX IF NOT EXISTS idx_attendance_meeting     ON attendance(meeting_id);
CREATE INDEX IF NOT EXISTS idx_event_contribs_event   ON contributions(event_id);

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

CREATE TABLE IF NOT EXISTS notices (
    id         SERIAL PRIMARY KEY,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    priority   TEXT DEFAULT 'normal',
    created_by TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
-- ============================================================
-- PROFILE UPDATE REQUESTS  (member-initiated, admin-approved)
-- ============================================================
CREATE TABLE IF NOT EXISTS profile_update_requests (
    id            SERIAL PRIMARY KEY,
    user_id       INT          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    requested_at  TIMESTAMP    NOT NULL DEFAULT NOW(),
    status        VARCHAR(20)  NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    reviewed_by   INT          REFERENCES users(id),
    reviewed_at   TIMESTAMP,
    reject_reason TEXT,
    -- proposed new values (NULL = no change requested for that field)
    full_name         VARCHAR(200),
    email             VARCHAR(200),
    id_number         VARCHAR(20),
    date_of_birth     DATE,
    marital_status    VARCHAR(30),
    residence         VARCHAR(200),
    court             VARCHAR(100),
    house_number      VARCHAR(50),
    spouse_name       VARCHAR(200),
    next_of_kin_name  VARCHAR(200),
    next_of_kin_phone VARCHAR(20),
    next_of_kin_2     VARCHAR(200),
    nok2_phone        VARCHAR(20)
);

-- ============================================================
-- MEMBER PARENTS  (linked to users)
-- ============================================================
CREATE TABLE IF NOT EXISTS member_parents (
    id                 SERIAL PRIMARY KEY,
    user_id            INT  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    full_name          VARCHAR(200),
    id_number          VARCHAR(20),
    current_residence  VARCHAR(200),
    contact_phone      VARCHAR(20)
);

-- ============================================================
-- PROFILE UPDATE REQUESTS — extra columns for children/parents
-- ============================================================
ALTER TABLE profile_update_requests
    ADD COLUMN IF NOT EXISTS phone_number  VARCHAR(20),
    ADD COLUMN IF NOT EXISTS children_json TEXT,
    ADD COLUMN IF NOT EXISTS parents_json  TEXT;

-- ============================================================
-- CASE REPORTS  (member-reported cases awaiting admin review)
-- ============================================================
CREATE TABLE IF NOT EXISTS case_reports (
    id                   SERIAL PRIMARY KEY,
    user_id              INT          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title                VARCHAR(300) NOT NULL,
    event_type           VARCHAR(50)  NOT NULL,  -- bereavement|medical|accident|fire|welfare
    description          TEXT,
    occurrence_date      DATE,                   -- when the event occurred
    affected_member_name VARCHAR(200),           -- person affected (free text)
    status               VARCHAR(20)  NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
    reject_reason        TEXT,
    published_event_id   INT          REFERENCES events(id),       -- linked event if approved
    reviewed_by          INT          REFERENCES users(id),        -- admin who reviewed
    reviewed_at          TIMESTAMP,
    submitted_at         TIMESTAMP    NOT NULL DEFAULT NOW()
);
-- ============================================================
-- ASSETS
-- ============================================================
CREATE TABLE IF NOT EXISTS assets (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(200) NOT NULL,
    category   VARCHAR(100),
    value      NUMERIC(12,2) DEFAULT 0,
    status     VARCHAR(50)  DEFAULT 'available',
    created_at TIMESTAMP    DEFAULT NOW()
);

-- ============================================================
-- PROJECTS
-- ============================================================
CREATE TABLE IF NOT EXISTS projects (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(200) NOT NULL,
    budget     NUMERIC(12,2) DEFAULT 0,
    status     VARCHAR(50)  DEFAULT 'planning',
    deadline   DATE,
    created_at TIMESTAMP    DEFAULT NOW()
);
