from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, List, Literal
from datetime import date, datetime
from decimal import Decimal

class EventCreate(BaseModel):
    title: str
    event_type: str
    beneficiary_id: int
    description: Optional[str] = None
    target_amount: float = 0

class ContributionCreate(BaseModel):
    member_id: int
    amount: float
    payment_method: str = "M-Pesa"
    reference: Optional[str] = None
    notes: Optional[str] = None

class MeetingCreate(BaseModel):
    title: str
    date: date
    time: str
    venue: str
    agenda: Optional[str] = None

class AttendanceUpdate(BaseModel):
    member_ids: list[int]

class ChildIn(BaseModel):
    full_name:     Optional[str] = None
    date_of_birth: Optional[date] = None
    relationship:  Optional[str] = None
    cert_number:   Optional[str] = None

class ParentIn(BaseModel):
    full_name:         Optional[str] = None
    status:            Optional[str] = None
    id_number:         Optional[str] = None
    current_residence: Optional[str] = None
    contact_phone:     Optional[str] = None

class MemberCreate(BaseModel):
    full_name: str
    phone_number: str
    id_number: Optional[str] = None
    role: str = "member"
    status: str = "active"
    date_joined: date
    next_of_kin_name: Optional[str] = None
    next_of_kin_phone: Optional[str] = None
    notes: Optional[str] = None
    # extended profile fields (mirrors UserRegister, used by admin "Add Member")
    member_id: Optional[str] = None
    email: Optional[str] = None
    date_of_birth: Optional[date] = None
    marital_status: Optional[str] = None
    residence: Optional[str] = None
    court: Optional[str] = None
    house_number: Optional[str] = None
    spouse_name: Optional[str] = None
    next_of_kin_2: Optional[str] = None
    nok2_phone: Optional[str] = None
    children: Optional[List[ChildIn]] = []
    parents: Optional[List[ParentIn]] = []
    privacy_accepted: bool = False

class UserRegister(BaseModel):
    member_id:        Optional[str] = None
    full_name:        str
    phone_number:     str
    password:         str
    email:            Optional[str] = None
    id_number:        Optional[str] = None
    date_of_birth:    Optional[date] = None
    marital_status:   Optional[str] = None
    residence:        Optional[str] = None
    court:            Optional[str] = None
    house_number:     Optional[str] = None
    spouse_name:      Optional[str] = None
    next_of_kin_name: Optional[str] = None
    next_of_kin_phone: Optional[str] = None
    next_of_kin_2:    Optional[str] = None
    nok2_phone:       Optional[str] = None
    role:             str = "member"
    children:         Optional[List[ChildIn]] = []
    parents:          Optional[List[ParentIn]] = []

class UserLogin(BaseModel):
    phone_number: Optional[str] = None
    email:        Optional[str] = None
    password:     str

class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    user_id:      int
    full_name:    str
    role:         str
    phone_number: str = ""
    must_change_password: bool = False

class LoanCreate(BaseModel):
    member_id:     int
    amount:        float
    purpose:       Optional[str] = None
    interest_rate: float = 10.0

class LoanRepayment(BaseModel):
    amount:         float
    payment_method: str = "M-Pesa"
    reference:      Optional[str] = None
    notes:          Optional[str] = None

class LoanStatusUpdate(BaseModel):
    status: str

class MonthlyContributionCreate(BaseModel):
    member_id:      int
    amount:         float
    month:          str
    payment_method: str = "M-Pesa"
    reference:      Optional[str] = None
    notes:          Optional[str] = None

class EventContributionCreate(BaseModel):
    member_id: int
    amount: float
    note: Optional[str] = None
    paid_at: Optional[datetime] = None

class EventContributionOut(BaseModel):
    id:             int
    event_id:       int
    member_id:      int
    member_name:    str
    amount:         Decimal
    payment_method: str
    reference:      Optional[str]
    notes:          Optional[str]
    recorded_at:    datetime

    class Config:
        from_attributes = True

class EventContributionSummary(BaseModel):
    event_id:          int
    event_title:       str
    target_amount:     Decimal
    total_raised:      Decimal
    contributor_count: int
    contributions:     list[EventContributionOut]

class FinanceTransactionCreate(BaseModel):
    type: Literal["income", "expense"]
    category: str
    amount: float
    description: Optional[str] = ""
    date: date

    @field_validator("amount")
    @classmethod
    def must_be_positive(cls, v):
        if v <= 0:
            raise ValueError("amount must be positive")
        return v

    @field_validator("category")
    @classmethod
    def category_not_empty(cls, v):
        if not v.strip():
            raise ValueError("category cannot be empty")
        return v.strip()

class NoticeCreate(BaseModel):
    title: str
    body: str
    priority: str = "normal"

class ProfileUpdateRequest(BaseModel):
    full_name:         Optional[str]  = None
    email:             Optional[str]  = None
    id_number:         Optional[str]  = None
    date_of_birth:     Optional[date] = None
    marital_status:    Optional[str]  = None
    residence:         Optional[str]  = None
    court:             Optional[str]  = None
    house_number:      Optional[str]  = None
    spouse_name:       Optional[str]  = None
    next_of_kin_name:  Optional[str]  = None
    next_of_kin_phone: Optional[str]  = None
    next_of_kin_2:     Optional[str]  = None
    nok2_phone:        Optional[str]  = None
    phone_number:      Optional[str]       = None
    children:          Optional[List[ChildIn]]  = None
    parents:           Optional[List[ParentIn]] = None