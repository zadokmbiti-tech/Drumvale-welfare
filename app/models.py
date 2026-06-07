# app/models.py
# ---------------------------------------------------------------
# All Pydantic request/response models live in schemas.py.
# This file re-exports everything so that routes can use either
#   from app.models import ...
#   from app.schemas import ...
# ---------------------------------------------------------------

from app.schemas import (
    # Members
    MemberCreate,

    # Auth / Users
    UserRegister,
    UserLogin,
    TokenResponse,
    ChildIn,
    ParentIn,

    # Events & contributions
    EventCreate,
    ContributionCreate,
    EventContributionCreate,
    EventContributionOut,
    EventContributionSummary,

    # Monthly contributions
    MonthlyContributionCreate,

    # Meetings
    MeetingCreate,
    AttendanceUpdate,

    # Loans
    LoanCreate,
    LoanRepayment,
    LoanStatusUpdate,

    # Finance
    FinanceTransactionCreate,

    # Notices
    NoticeCreate,

    # Profile updates
    ProfileUpdateRequest,
)

__all__ = [
    "MemberCreate",
    "UserRegister",
    "UserLogin",
    "TokenResponse",
    "ChildIn",
    "ParentIn",
    "EventCreate",
    "ContributionCreate",
    "EventContributionCreate",
    "EventContributionOut",
    "EventContributionSummary",
    "MonthlyContributionCreate",
    "MeetingCreate",
    "AttendanceUpdate",
    "LoanCreate",
    "LoanRepayment",
    "LoanStatusUpdate",
    "FinanceTransactionCreate",
    "NoticeCreate",
    "ProfileUpdateRequest",
]