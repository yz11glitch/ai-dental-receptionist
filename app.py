import os
import re
import json
import html
import logging
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Any, Optional, Tuple

from flask import Flask, request, Response, redirect, abort, jsonify, g, session
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioRestClient
from twilio.request_validator import RequestValidator

try:
    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration
    _SENTRY_AVAILABLE = True
except ImportError:
    _SENTRY_AVAILABLE = False
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import create_engine, text, Column, Integer, String, Text, Boolean, DateTime, Date, Numeric, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai_receptionist")

# -----------------------------------------------------------------------------
# Env
# -----------------------------------------------------------------------------

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM")

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///app.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

REMINDER_SECRET = os.environ.get("REMINDER_SECRET", "change-me")
SENTRY_DSN = os.environ.get("SENTRY_DSN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
MODEL = "gpt-4o-mini"

if SENTRY_DSN and _SENTRY_AVAILABLE:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[FlaskIntegration()],
        traces_sample_rate=0.0,
        send_default_pii=False,
    )
    logger.info("Sentry initialized")
TIMEZONE = "Asia/Kuala_Lumpur"
DEFAULT_CLINIC_ID = 1
MAX_HISTORY_MESSAGES = 20
MAX_TOOL_LOOPS = 6

# In-memory stats (resets on process restart; good enough for pilot monitoring).
_stats = {"fallbacks": 0}
_PENDING_DATE_CLARIFICATIONS: Dict[Tuple[str, int], Dict[str, str]] = {}

client = OpenAI(api_key=OPENAI_API_KEY)
twilio_rest = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_rest = TwilioRestClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# -----------------------------------------------------------------------------
# DB
# -----------------------------------------------------------------------------

engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id = Column(Integer, primary_key=True)
    user = Column(String(64), index=True, nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    clinic_id = Column(Integer, nullable=True, index=True)


class BookingStateModel(Base):
    __tablename__ = "booking_states"

    user = Column(String(64), primary_key=True)
    service = Column(String(120), nullable=True)
    date = Column(String(20), nullable=True)
    time = Column(String(20), nullable=True)
    name = Column(String(200), nullable=True)
    availability_ok = Column(Boolean, default=False, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class BookingRecordModel(Base):
    __tablename__ = "booking_records"

    # BUG FIX #11: Changed from user as primary key to event_id.
    # This allows multiple bookings per phone number (family bookings).
    # event_id is unique per Google Calendar event, ensuring no duplicates.
    event_id = Column(String(255), primary_key=True)
    user = Column(String(64), nullable=False, index=True)  # Phone number, now indexed not PK
    clinic_id = Column(Integer, nullable=True, index=True)
    service = Column(String(120), nullable=False)
    name = Column(String(200), nullable=False)
    date = Column(String(20), nullable=False)
    time = Column(String(20), nullable=False)
    status = Column(String(50), default="Pending", nullable=False)
    reminder_1d_sent = Column(Boolean, default=False, nullable=False)
    reminder_2h_sent = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)



class Clinic(Base):
    __tablename__ = "clinics"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    location = Column(String(255), nullable=False)
    timezone = Column(String(100), nullable=False, default="Asia/Kuala_Lumpur")
    open_hour = Column(Integer, nullable=False)
    close_hour = Column(Integer, nullable=False)
    lunch_start = Column(String(5), nullable=True)
    lunch_end = Column(String(5), nullable=True)
    hours_text = Column(String(255), nullable=False)
    slot_minutes = Column(Integer, nullable=False, default=30)
    opening_message = Column(Text, nullable=True)
    promo_message = Column(Text, nullable=True)
    google_calendar_id = Column(String(255), nullable=True)
    twilio_number = Column(String(64), nullable=True)
    human_contact_number = Column(String(64), nullable=True)
    tone = Column(String(50), nullable=False, default="professional")
    is_active = Column(Boolean, default=True, nullable=False)
    calendar_healthy = Column(Boolean, default=True, nullable=False)
    calendar_last_checked_at = Column(DateTime, nullable=True)
    calendar_error = Column(Text, nullable=True)
    # Billing
    billing_plan = Column(String(20), nullable=True)          # "founding" | "standard" | null
    billing_start_date = Column(Date, nullable=True)           # first billing anchor date
    plan_price = Column(Numeric(10, 2), nullable=True)         # price override (beats plan default)
    billing_cycle_days = Column(Integer, nullable=False, default=30)
    last_paid_date = Column(Date, nullable=True)
    billing_status = Column(String(20), nullable=False, default="paid")  # stored only for "paused"
    billing_notes = Column(Text, nullable=True)


class ClinicService(Base):
    __tablename__ = "clinic_services"

    id = Column(Integer, primary_key=True)
    clinic_id = Column(Integer, index=True, nullable=False)
    service_name = Column(String(255), nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    price = Column(Numeric(10, 2), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)


class ClinicClosure(Base):
    __tablename__ = "clinic_closures"

    id = Column(Integer, primary_key=True)
    clinic_id = Column(Integer, index=True, nullable=False)
    date = Column(String(20), nullable=False)
    note = Column(String(255), nullable=True)


class ClinicStaff(Base):
    __tablename__ = "clinic_staff"

    id = Column(Integer, primary_key=True)
    clinic_id = Column(Integer, nullable=False, index=True)
    email = Column(String(255), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_login_at = Column(DateTime, nullable=True)


class ConversationFlag(Base):
    __tablename__ = "conversation_flags"

    id = Column(Integer, primary_key=True)
    clinic_id = Column(Integer, nullable=False, index=True)
    phone = Column(String(64), nullable=False, index=True)
    flag_type = Column(String(50), nullable=False)
    # Values: "human_requested", "booking_failed", "ai_uncertain"
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    resolved_at = Column(DateTime, nullable=True)
    resolved_by_staff_id = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True)
    clinic_id = Column(Integer, nullable=False, index=True)
    phone = Column(String(64), nullable=False)
    name = Column(String(255), nullable=True)
    first_contact_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_contact_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_appointment_at = Column(DateTime, nullable=True)
    total_bookings = Column(Integer, nullable=False, default=0)
    __table_args__ = (UniqueConstraint("clinic_id", "phone", name="uq_customer_clinic_phone"),)


class FeedbackEntry(Base):
    __tablename__ = "feedback_entries"

    id = Column(Integer, primary_key=True)
    clinic_id = Column(Integer, nullable=False, index=True)
    staff_id = Column(Integer, nullable=False, index=True)
    staff_name = Column(String(255), nullable=True)
    category = Column(String(20), nullable=False)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ClinicDailyMetric(Base):
    __tablename__ = "clinic_daily_metrics"

    id = Column(Integer, primary_key=True)
    clinic_id = Column(Integer, nullable=False, index=True)
    metric_date = Column(Date, nullable=False, index=True)
    conversations_handled = Column(Integer, nullable=False, default=0)
    messages_handled = Column(Integer, nullable=False, default=0)
    bookings_created = Column(Integer, nullable=False, default=0)
    after_hours_messages = Column(Integer, nullable=False, default=0)
    human_escalations = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("clinic_id", "metric_date", name="uq_clinic_daily_metrics_clinic_date"),
    )


class ClinicPromotion(Base):
    __tablename__ = "clinic_promotions"

    id = Column(Integer, primary_key=True)
    clinic_id = Column(Integer, nullable=False, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


Base.metadata.create_all(bind=engine)


def run_pending_migrations() -> None:
    """Idempotent schema migrations for PostgreSQL and local SQLite compatibility."""
    if DATABASE_URL.startswith("sqlite"):
        with engine.connect() as conn:
            clinics_exists = conn.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'clinics'"
            )).first()
            if not clinics_exists:
                return
            clinic_cols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(clinics)")).fetchall()
            }
            if "human_contact_number" not in clinic_cols:
                conn.execute(text(
                    "ALTER TABLE clinics ADD COLUMN human_contact_number VARCHAR(64) NULL"
                ))
            if "tone" not in clinic_cols:
                conn.execute(text(
                    "ALTER TABLE clinics ADD COLUMN tone VARCHAR(50) NOT NULL DEFAULT 'professional'"
                ))
            if "lunch_start" not in clinic_cols:
                conn.execute(text(
                    "ALTER TABLE clinics ADD COLUMN lunch_start VARCHAR(5) NULL"
                ))
            if "lunch_end" not in clinic_cols:
                conn.execute(text(
                    "ALTER TABLE clinics ADD COLUMN lunch_end VARCHAR(5) NULL"
                ))
            if "plan_price" not in clinic_cols:
                conn.execute(text(
                    "ALTER TABLE clinics ADD COLUMN plan_price NUMERIC(10,2) NULL"
                ))
            if "billing_cycle_days" not in clinic_cols:
                conn.execute(text(
                    "ALTER TABLE clinics ADD COLUMN billing_cycle_days INTEGER NOT NULL DEFAULT 30"
                ))
            if "last_paid_date" not in clinic_cols:
                conn.execute(text(
                    "ALTER TABLE clinics ADD COLUMN last_paid_date DATE NULL"
                ))
            if "billing_status" not in clinic_cols:
                conn.execute(text(
                    "ALTER TABLE clinics ADD COLUMN billing_status VARCHAR(20) NOT NULL DEFAULT 'paid'"
                ))
            if "billing_notes" not in clinic_cols:
                conn.execute(text(
                    "ALTER TABLE clinics ADD COLUMN billing_notes TEXT NULL"
                ))
            if "billing_plan" not in clinic_cols:
                conn.execute(text(
                    "ALTER TABLE clinics ADD COLUMN billing_plan VARCHAR(20) NULL"
                ))
            if "billing_start_date" not in clinic_cols:
                conn.execute(text(
                    "ALTER TABLE clinics ADD COLUMN billing_start_date DATE NULL"
                ))
            conn.commit()
        return

    if not DATABASE_URL.startswith("postgresql"):
        return
    with engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE clinic_services ADD COLUMN IF NOT EXISTS price NUMERIC(10, 2) NULL"
        ))
        conn.execute(text(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'unique_twilio_number'
                ) THEN
                    ALTER TABLE clinics ADD CONSTRAINT unique_twilio_number UNIQUE (twilio_number);
                END IF;
            END$$;
            """
        ))
        conn.execute(text(
            "ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS clinic_id INTEGER NULL"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_conversation_messages_clinic_id ON conversation_messages (clinic_id)"
        ))
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS clinic_staff (
                id SERIAL PRIMARY KEY,
                clinic_id INTEGER NOT NULL,
                email VARCHAR(255) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                full_name VARCHAR(255) NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                last_login_at TIMESTAMP NULL
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_clinic_staff_clinic_id ON clinic_staff (clinic_id)"
        ))
        # --- conversation_flags: handle partial migration state ---
        # Three possible states on production:
        #   1. Table does not exist → create it with the correct schema (phone column)
        #   2. Table exists with legacy "user" column, no "phone" column → rename the column
        #   3. Table already has "phone" column → nothing to do
        # Only after confirming "phone" exists do we create the indexes.

        table_exists_row = conn.execute(text(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_name = 'conversation_flags'"
        )).fetchone()

        if not table_exists_row:
            # Case 1: table is absent — create with correct schema
            conn.execute(text(
                """
                CREATE TABLE conversation_flags (
                    id SERIAL PRIMARY KEY,
                    clinic_id INTEGER NOT NULL,
                    phone VARCHAR(64) NOT NULL,
                    flag_type VARCHAR(50) NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    resolved_at TIMESTAMP NULL,
                    resolved_by_staff_id INTEGER NULL,
                    notes TEXT NULL
                )
                """
            ))
            logger.info("run_pending_migrations: created conversation_flags table")
        else:
            phone_col_row = conn.execute(text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'conversation_flags' AND column_name = 'phone'"
            )).fetchone()

            if not phone_col_row:
                # Case 2: table exists but has legacy "user" column — rename it
                user_col_row = conn.execute(text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'conversation_flags' AND column_name = 'user'"
                )).fetchone()

                if user_col_row:
                    conn.execute(text(
                        'ALTER TABLE conversation_flags RENAME COLUMN "user" TO phone'
                    ))
                    logger.info(
                        "run_pending_migrations: renamed conversation_flags.user -> phone"
                    )
                else:
                    # Neither column exists — table schema is unknown; add phone directly
                    conn.execute(text(
                        "ALTER TABLE conversation_flags ADD COLUMN phone VARCHAR(64) NOT NULL DEFAULT ''"
                    ))
                    logger.warning(
                        "run_pending_migrations: conversation_flags had neither 'user' nor"
                        " 'phone' column — added phone with empty-string default"
                    )
            # Case 3: phone already exists — nothing to do

        # Indexes are safe to create now that phone is guaranteed to exist
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_conversation_flags_clinic_id ON conversation_flags (clinic_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_conversation_flags_phone ON conversation_flags (phone)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_conversation_flags_clinic_unresolved ON conversation_flags (clinic_id, resolved_at)"
        ))
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id SERIAL PRIMARY KEY,
                clinic_id INTEGER NOT NULL,
                phone VARCHAR(64) NOT NULL,
                name VARCHAR(255) NULL,
                first_contact_at TIMESTAMP NOT NULL DEFAULT NOW(),
                last_contact_at TIMESTAMP NOT NULL DEFAULT NOW(),
                last_appointment_at TIMESTAMP NULL,
                total_bookings INTEGER NOT NULL DEFAULT 0,
                CONSTRAINT uq_customer_clinic_phone UNIQUE (clinic_id, phone)
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_customers_clinic_id ON customers (clinic_id)"
        ))
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS feedback_entries (
                id SERIAL PRIMARY KEY,
                clinic_id INTEGER NOT NULL,
                staff_id INTEGER NOT NULL,
                staff_name VARCHAR(255) NULL,
                category VARCHAR(20) NOT NULL,
                message TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_feedback_entries_clinic_id ON feedback_entries (clinic_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_feedback_entries_staff_id ON feedback_entries (staff_id)"
        ))
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS clinic_daily_metrics (
                id SERIAL PRIMARY KEY,
                clinic_id INTEGER NOT NULL,
                metric_date DATE NOT NULL,
                conversations_handled INTEGER NOT NULL DEFAULT 0,
                messages_handled INTEGER NOT NULL DEFAULT 0,
                bookings_created INTEGER NOT NULL DEFAULT 0,
                after_hours_messages INTEGER NOT NULL DEFAULT 0,
                human_escalations INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_clinic_daily_metrics_clinic_date UNIQUE (clinic_id, metric_date)
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_clinic_daily_metrics_clinic_id ON clinic_daily_metrics (clinic_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_clinic_daily_metrics_clinic_date ON clinic_daily_metrics (clinic_id, metric_date)"
        ))
        # Calendar health tracking columns
        conn.execute(text(
            "ALTER TABLE clinics ADD COLUMN IF NOT EXISTS calendar_healthy BOOLEAN NOT NULL DEFAULT TRUE"
        ))
        conn.execute(text(
            "ALTER TABLE clinics ADD COLUMN IF NOT EXISTS calendar_last_checked_at TIMESTAMP NULL"
        ))
        conn.execute(text(
            "ALTER TABLE clinics ADD COLUMN IF NOT EXISTS calendar_error TEXT NULL"
        ))
        conn.execute(text(
            "ALTER TABLE clinics ADD COLUMN IF NOT EXISTS human_contact_number VARCHAR(64) NULL"
        ))
        conn.execute(text(
            "ALTER TABLE clinics ADD COLUMN IF NOT EXISTS tone VARCHAR(50) NOT NULL DEFAULT 'professional'"
        ))
        conn.execute(text(
            "ALTER TABLE clinics ADD COLUMN IF NOT EXISTS lunch_start VARCHAR(5) NULL"
        ))
        conn.execute(text(
            "ALTER TABLE clinics ADD COLUMN IF NOT EXISTS lunch_end VARCHAR(5) NULL"
        ))
        conn.execute(text(
            "ALTER TABLE clinics ADD COLUMN IF NOT EXISTS plan_price NUMERIC(10,2) NULL"
        ))
        conn.execute(text(
            "ALTER TABLE clinics ADD COLUMN IF NOT EXISTS billing_cycle_days INTEGER NOT NULL DEFAULT 30"
        ))
        conn.execute(text(
            "ALTER TABLE clinics ADD COLUMN IF NOT EXISTS last_paid_date DATE NULL"
        ))
        conn.execute(text(
            "ALTER TABLE clinics ADD COLUMN IF NOT EXISTS billing_status VARCHAR(20) NOT NULL DEFAULT 'paid'"
        ))
        conn.execute(text(
            "ALTER TABLE clinics ADD COLUMN IF NOT EXISTS billing_notes TEXT NULL"
        ))
        conn.execute(text(
            "ALTER TABLE clinics ADD COLUMN IF NOT EXISTS billing_plan VARCHAR(20) NULL"
        ))
        conn.execute(text(
            "ALTER TABLE clinics ADD COLUMN IF NOT EXISTS billing_start_date DATE NULL"
        ))
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS clinic_promotions (
                id SERIAL PRIMARY KEY,
                clinic_id INTEGER NOT NULL,
                title VARCHAR(255) NOT NULL,
                description TEXT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_clinic_promotions_clinic_id ON clinic_promotions (clinic_id)"
        ))
        conn.commit()
    logger.info("run_pending_migrations: complete")


try:
    run_pending_migrations()
except Exception:
    logger.exception("run_pending_migrations failed — continuing without migration")

# -----------------------------------------------------------------------------
# Demo clinic seed data
# -----------------------------------------------------------------------------

DEMO_CLINIC = {
    "name": "Glow Dental Clinic",
    "location": "Kuala Lumpur",
    "timezone": "Asia/Kuala_Lumpur",
    "hours_text": "Monday to Saturday, 10:00 to 18:00. Closed Sunday.",
    "open_hour": 10,
    "close_hour": 18,
    "slot_minutes": 30,
    "opening_message": "Welcome to Glow Dental Clinic! How may we help you today?",
    "promo_message": "Current promo: Ask us about this month's whitening promotion.",
    "tone": "professional",
    "google_calendar_id": GOOGLE_CALENDAR_ID,
    "human_contact_number": "",
    "services": {
        "scaling": 60,
        "polishing": 30,
        "braces consultation": 60,
        "filling": 60,
        "whitening": 90,
    },
    "special_closures": [
        # "2026-03-20",
        # "2026-04-10",
    ],
    "closure_notes": {
        # "2026-03-20": "We are closed on 20 March for a public holiday.",
        # "2026-04-10": "We are closed on 10 April for staff training.",
    },
}

SUPPORTED_TOOL_NAMES = {
    "resolve_booking_datetime",
    "check_date_available",
    "check_availability",
    "find_next_available_slot",
    "get_available_slots",
    "create_booking",
    "reschedule_booking",
    "cancel_booking",
}

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _build_clinic_payload(clinic: Clinic, services: list, closures: list) -> Dict[str, Any]:
    service_catalog = [
        {
            "name": s.service_name,
            "duration_minutes": s.duration_minutes,
            "price": float(s.price) if s.price is not None else None,
        }
        for s in services
    ]
    return {
        "id": clinic.id,
        "name": clinic.name,
        "location": clinic.location,
        "timezone": clinic.timezone,
        "open_hour": clinic.open_hour,
        "close_hour": clinic.close_hour,
        "lunch_start": clinic.lunch_start,
        "lunch_end": clinic.lunch_end,
        "hours_text": clinic.hours_text,
        "slot_minutes": clinic.slot_minutes,
        "opening_message": clinic.opening_message or "",
        "promo_message": clinic.promo_message or "",
        "google_calendar_id": clinic.google_calendar_id or GOOGLE_CALENDAR_ID,
        "tone": clinic.tone or "professional",
        "twilio_number": clinic.twilio_number or "",
        "human_contact_number": clinic.human_contact_number or "",
        "services": {
            s.service_name: s.duration_minutes
            for s in services
        },
        "service_prices": {
            s.service_name: float(s.price) if s.price is not None else None
            for s in services
        },
        "service_catalog": service_catalog,
        "special_closures": [c.date for c in closures],
        "closure_notes": {
            c.date: (c.note or "We are closed on that date.")
            for c in closures
        },
    }


def get_clinic_by_twilio_number(twilio_number: str):
    with SessionLocal() as db:
        clinic = db.query(Clinic).filter(
            Clinic.twilio_number == twilio_number,
            Clinic.is_active == True
        ).first()

        if not clinic:
            return None

        services = db.query(ClinicService).filter(
            ClinicService.clinic_id == clinic.id,
            ClinicService.is_active == True
        ).all()

        closures = db.query(ClinicClosure).filter(
            ClinicClosure.clinic_id == clinic.id
        ).all()

        return _build_clinic_payload(clinic, services, closures)


def get_clinic_by_id(clinic_id: int = DEFAULT_CLINIC_ID) -> Optional[Dict[str, Any]]:
    with SessionLocal() as db:
        clinic = db.query(Clinic).filter(
            Clinic.id == clinic_id,
            Clinic.is_active == True
        ).first()

        if not clinic:
            return None

        services = db.query(ClinicService).filter(
            ClinicService.clinic_id == clinic.id,
            ClinicService.is_active == True
        ).all()

        closures = db.query(ClinicClosure).filter(
            ClinicClosure.clinic_id == clinic.id
        ).all()

        return _build_clinic_payload(clinic, services, closures)


# ---------------------------------------------------------------------------
# Timezone helpers — defined here so @patch("app.now_local") works in tests.
# ---------------------------------------------------------------------------

def now_local(clinic_tz: str = TIMEZONE) -> datetime:
    """Return the current datetime in the given timezone (default: KL)."""
    return datetime.now(ZoneInfo(clinic_tz))


def get_now_context(clinic_tz: str = TIMEZONE) -> Dict[str, Any]:
    """Return a dict with current datetime context keys used by build_system_prompt."""
    now = now_local(clinic_tz)
    return {
        "iso": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "readable": now.strftime("%A, %d %B %Y %H:%M"),
        "weekday": now.strftime("%A"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        # Legacy aliases kept for any callers using old keys
        "today": now.strftime("%Y-%m-%d"),
        "day_of_week": now.strftime("%A"),
        "current_time": now.strftime("%H:%M"),
    }


# ---------------------------------------------------------------------------
# Pure utilities — imported from utils.py. Re-exported here so existing
# callers (routes, tests, other modules) can still do `from app import X`.
# ---------------------------------------------------------------------------

from utils import (
    normalize_text,
    normalize_date,
    normalize_time,
    parse_slot,
    next_weekday_from,
    format_slot,
    format_time_only,
    is_reset_command,
    normalize_service,
    resolve_relative_date,
    resolve_time_text,
    detect_conflicting_date_phrases,
    is_within_business_hours,
    is_human_escalation_request,
    is_human_request,
    should_send_1d_reminder,
    should_send_2h_reminder,
    _is_multi_patient_same_time_question,
    _detect_multi_patient_booking_request,
    _ESCALATION_EN_KEYWORDS,
    _ESCALATION_EN_PHRASES,
    _ESCALATION_ZH,
    _ESCALATION_BM,
    _DENTAL_SERVICE_KEYWORDS,
)


def resolve_booking_datetime(date_text: str, time_text: str) -> Dict[str, Any]:
    # BUG FIX #9: Check for conflicting date phrases before parsing
    conflict_message = detect_conflicting_date_phrases(date_text)
    if conflict_message:
        return {"ok": False, "message": conflict_message}
    
    date_str = resolve_relative_date(date_text)
    time_str = resolve_time_text(time_text)

    if not date_str:
        return {"ok": False, "message": "Could not understand the date."}

    if not time_str:
        return {"ok": False, "message": "Could not understand the time."}

    try:
        dt = parse_slot(date_str, time_str)
    except Exception:
        return {"ok": False, "message": "Invalid date or time."}

    return {
        "ok": True,
        "date": date_str,
        "time": time_str,
        "formatted_slot": format_slot(dt),
        "is_past": dt < now_local(),
    }


def get_calendar():
    if GOOGLE_SERVICE_ACCOUNT_JSON:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
    else:
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE,
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
    return build("calendar", "v3", credentials=creds)


def ensure_demo_clinic_seeded() -> None:
    with SessionLocal() as db:
        clinic = db.query(Clinic).filter(Clinic.id == DEFAULT_CLINIC_ID).first()

        if clinic:
            return

        clinic = Clinic(
            id=DEFAULT_CLINIC_ID,
            name=DEMO_CLINIC["name"],
            location=DEMO_CLINIC["location"],
            timezone=DEMO_CLINIC["timezone"],
            open_hour=DEMO_CLINIC["open_hour"],
            close_hour=DEMO_CLINIC["close_hour"],
            hours_text=DEMO_CLINIC["hours_text"],
            slot_minutes=DEMO_CLINIC["slot_minutes"],
            opening_message=DEMO_CLINIC["opening_message"],
            promo_message=DEMO_CLINIC["promo_message"],
            google_calendar_id=DEMO_CLINIC["google_calendar_id"],
            human_contact_number=DEMO_CLINIC.get("human_contact_number") or None,
            is_active=True,
        )
        db.add(clinic)
        db.commit()

        for service_name, duration_minutes in DEMO_CLINIC["services"].items():
            db.add(ClinicService(
                clinic_id=DEFAULT_CLINIC_ID,
                service_name=service_name,
                duration_minutes=duration_minutes,
                is_active=True,
            ))

        for closure_date in DEMO_CLINIC.get("special_closures", []):
            db.add(ClinicClosure(
                clinic_id=DEFAULT_CLINIC_ID,
                date=closure_date,
                note=DEMO_CLINIC.get("closure_notes", {}).get(closure_date, "We are closed on that date."),
            ))

        db.commit()
        logger.info("Seeded default clinic into DB")


def get_default_clinic() -> Dict[str, Any]:
    ensure_demo_clinic_seeded()
    return get_clinic_by_id(DEFAULT_CLINIC_ID)


def _get_service_catalog(clinic: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build a normalized service catalog for display from clinic payload."""
    catalog = clinic.get("service_catalog")
    if isinstance(catalog, list) and catalog:
        normalized = []
        for item in catalog:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            duration = item.get("duration_minutes")
            if not name:
                continue
            try:
                duration = int(duration)
            except (TypeError, ValueError):
                continue
            price = item.get("price")
            if price is not None:
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    price = None
            normalized.append({
                "name": name,
                "duration_minutes": duration,
                "price": price,
            })
        if normalized:
            return normalized

    services = clinic.get("services", {}) or {}
    prices = clinic.get("service_prices", {}) or {}
    price_by_normalized = {
        normalize_text(str(name)): value
        for name, value in prices.items()
    }
    fallback_catalog: List[Dict[str, Any]] = []
    for name, duration in services.items():
        service_name = str(name).strip()
        if not service_name:
            continue
        try:
            duration_minutes = int(duration)
        except (TypeError, ValueError):
            continue
        price = prices.get(name)
        if price is None:
            price = price_by_normalized.get(normalize_text(service_name))
        if price is not None:
            try:
                price = float(price)
            except (TypeError, ValueError):
                price = None
        fallback_catalog.append({
            "name": service_name,
            "duration_minutes": duration_minutes,
            "price": price,
        })
    return fallback_catalog


# Default prices per plan name.
BILLING_PLANS = {
    "founding": 249.0,
    "standard": 349.0,
}


def compute_billing_info(clinic) -> dict:
    """Return computed billing fields for a Clinic ORM row.

    billing_status stored in DB is only authoritative for "paused".
    Everything else is derived from billing_plan, billing_start_date,
    last_paid_date, and billing_cycle_days.
    """
    stored_status = (clinic.billing_status or "paid") if hasattr(clinic, "billing_status") else "paid"
    billing_plan = (clinic.billing_plan or None) if hasattr(clinic, "billing_plan") else None
    billing_start_date = clinic.billing_start_date if hasattr(clinic, "billing_start_date") else None
    price_override = clinic.plan_price if hasattr(clinic, "plan_price") else None  # plan_price = override
    cycle_days = (clinic.billing_cycle_days or 30) if hasattr(clinic, "billing_cycle_days") else 30
    last_paid = clinic.last_paid_date if hasattr(clinic, "last_paid_date") else None
    notes = clinic.billing_notes if hasattr(clinic, "billing_notes") else None

    # Effective price: override wins, else plan default, else None
    plan_default_price = BILLING_PLANS.get(billing_plan) if billing_plan else None
    effective_price = float(price_override) if price_override is not None else plan_default_price
    using_price_override = price_override is not None

    # next_due_date: after payment it advances; before first payment it equals billing_start_date
    if last_paid is not None:
        next_due = last_paid + timedelta(days=cycle_days)
    elif billing_start_date is not None:
        next_due = billing_start_date  # first payment due
    else:
        next_due = None

    # Status
    today = date.today()
    if stored_status == "paused":
        status = "paused"
    elif last_paid is None:
        if billing_start_date is None or today < billing_start_date:
            status = "not_started"
        else:
            status = "unpaid"
    else:
        if next_due is None:
            status = "paid"
        elif today > next_due:
            status = "overdue"
        elif today == next_due:
            status = "due"
        else:
            status = "paid"

    return {
        "billing_plan": billing_plan,
        "plan_default_price": plan_default_price,
        "price_override": float(price_override) if price_override is not None else None,
        "effective_price": effective_price,
        "using_price_override": using_price_override,
        "billing_cycle_days": cycle_days,
        "billing_start_date": billing_start_date,
        "last_paid_date": last_paid,
        "next_due_date": next_due,
        "billing_status": status,
        "amount_due": (
            effective_price if effective_price is not None and status in ("unpaid", "due", "overdue")
            else 0.0
        ),
        "billing_notes": notes,
        # backward compat: templates that reference billing.plan_price still work
        "plan_price": effective_price,
    }


_VALID_TONES = {"professional", "friendly", "premium", "casual"}

_TONE_INSTRUCTIONS = {
    "professional": "professional and courteous",
    "friendly": "warm, friendly, and approachable",
    "premium": "refined, premium, and attentive",
    "casual": "casual and relaxed",
}


def get_active_promotions(clinic_id: int) -> list:
    """Return a list of active promotions for the given clinic, each as a dict."""
    with SessionLocal() as db:
        rows = db.query(ClinicPromotion).filter(
            ClinicPromotion.clinic_id == clinic_id,
            ClinicPromotion.is_active == True,
        ).order_by(ClinicPromotion.created_at.desc()).all()
        return [{"id": r.id, "title": r.title, "description": r.description or ""} for r in rows]


# ---------------------------------------------------------------------------
# Booking module helpers — imported from booking.py. Re-exported here so
# callers and tests can still do `from app import X`.
# ---------------------------------------------------------------------------

from booking import (
    get_special_closure_message,
    find_next_open_days,
    check_date_available,
    parse_event_status_from_summary,
    status_prefix,
    get_parallel_booking_capacity,
    reset_booking_state,
    get_booking_state,
    update_booking_state,
    get_existing_booking,
    get_all_bookings,
    save_booking_record,
    delete_booking_record,
    check_availability,
    find_next_available_slot,
    get_available_slots,
    create_booking,
    reschedule_booking,
    cancel_booking,
    reminder_message_1d,
    reminder_message_2h,
    sync_booking_from_calendar,
    process_reminders,
    run_calendar_health_checks,
)


def send_whatsapp_outbound(to_number: str, body: str, from_number: str = None) -> bool:
    sender = from_number or TWILIO_WHATSAPP_FROM
    if not twilio_rest or not sender:
        logger.warning("Twilio outbound not configured; skipping send to %s", to_number)
        return False

    try:
        twilio_rest.messages.create(
            from_=sender,
            to=to_number,
            body=body,
        )
        return True
    except Exception:
        logger.exception("Failed sending WhatsApp outbound to %s", to_number)
        return False


# -----------------------------------------------------------------------------
# Telegram monitoring
# -----------------------------------------------------------------------------

def send_telegram(text: str) -> None:
    """Fire-and-forget Telegram message. No-op when env vars are absent."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        logger.warning("Telegram send failed — monitoring alert lost", exc_info=True)


def alert_telegram(clinic_name: str, user: str, event: str, reason: str) -> None:
    message = (
        f"AI Receptionist Alert\n\n"
        f"Clinic: {clinic_name}\n"
        f"User: {user}\n"
        f"Event: {event}\n"
        f"Reason: {reason}"
    )
    send_telegram(message)


# -----------------------------------------------------------------------------
# Human fallback
# -----------------------------------------------------------------------------

# is_human_request, is_human_escalation_request, and escalation constants are
# imported from utils.py above.

HUMAN_HANDOFF_HINT_SOFT = "If you'd like to speak to our receptionist at any point, just type 'human'."
HUMAN_HANDOFF_HINT_STRONG = "If you'd prefer a receptionist to assist, just type 'human'."
_CONFUSION_HINT_PHRASES = {
    "huh",
    "what",
    "not sure",
    "confusing",
    "i don't understand",
    "i dont understand",
}


def _has_human_hint_in_thread(user: str, clinic_id: int) -> bool:
    with SessionLocal() as db:
        q = db.query(ConversationMessage.content).filter(
            ConversationMessage.user == user,
            ConversationMessage.role == "assistant",
        )
        if clinic_id is not None:
            q = q.filter(ConversationMessage.clinic_id == clinic_id)
        rows = q.all()
        return any("just type 'human'" in (row.content or "").lower() for row in rows)


def _user_message_count_in_thread(user: str, clinic_id: int) -> int:
    with SessionLocal() as db:
        q = db.query(ConversationMessage.id).filter(
            ConversationMessage.user == user,
            ConversationMessage.role == "user",
        )
        if clinic_id is not None:
            q = q.filter(ConversationMessage.clinic_id == clinic_id)
        return q.count()


def _message_indicates_confusion(message: str) -> bool:
    t = normalize_text(message or "")
    return any(phrase in t for phrase in _CONFUSION_HINT_PHRASES)


def _conversation_has_friction_signal(
    user: str,
    clinic_id: int,
    tool_results: List[Dict[str, Any]] = None,
    forced_friction: bool = False,
) -> bool:
    if forced_friction:
        return True

    for result in tool_results or []:
        if isinstance(result, dict) and not result.get("ok", False):
            return True

    with SessionLocal() as db:
        q = db.query(ConversationMessage.content).filter(
            ConversationMessage.user == user,
            ConversationMessage.role == "assistant",
        )
        if clinic_id is not None:
            q = q.filter(ConversationMessage.clinic_id == clinic_id)
        rows = q.order_by(ConversationMessage.created_at.desc()).limit(12).all()

    unavailable_count = 0
    clarification_count = 0
    for row in rows:
        text = (row.content or "").lower()
        if "unavailable" in text:
            unavailable_count += 1
        if any(marker in text for marker in ("clarify", "could you", "send your message again")):
            clarification_count += 1
    return unavailable_count >= 2 or clarification_count >= 2


def _maybe_add_human_handoff_hint(
    user: str,
    clinic_id: int,
    reply: str,
    last_user_message: str = "",
    tool_results: List[Dict[str, Any]] = None,
    forced_friction: bool = False,
) -> str:
    if not reply:
        return reply
    if _has_human_hint_in_thread(user, clinic_id):
        return reply

    user_count = _user_message_count_in_thread(user, clinic_id)
    # Never show the hint in the first message.
    if user_count <= 1:
        return reply

    confusion = _message_indicates_confusion(last_user_message)
    friction = _conversation_has_friction_signal(
        user=user,
        clinic_id=clinic_id,
        tool_results=tool_results,
        forced_friction=forced_friction,
    )
    long_conversation = user_count >= 6

    if confusion or friction:
        return f"{HUMAN_HANDOFF_HINT_STRONG}\n\n{reply}"
    if long_conversation:
        return f"{HUMAN_HANDOFF_HINT_SOFT}\n\n{reply}"
    return reply


def _human_escalation_reply(clinic: dict) -> str:
    clinic_human_number = (clinic or {}).get("human_contact_number")
    if clinic_human_number:
        return (
            "Sure — I'll notify our clinic team. "
            f"If you prefer, you can also contact us directly at {clinic_human_number}."
        )
    return "Sure — I'll notify our clinic team. Someone will assist you shortly."


def has_unresolved_human_flag(clinic_id: int, phone: str) -> bool:
    """Return True if an unresolved human_requested flag already exists for this user.

    Used to suppress the repeat outbound "Someone will get back to you" message
    while still short-circuiting AI responses.
    """
    if clinic_id is None:
        return False
    try:
        with SessionLocal() as db:
            existing = db.query(ConversationFlag).filter(
                ConversationFlag.clinic_id == clinic_id,
                ConversationFlag.phone == phone,
                ConversationFlag.flag_type == "human_requested",
                ConversationFlag.resolved_at == None,  # noqa: E711
            ).first()
            return existing is not None
    except Exception:
        logger.exception(
            "has_unresolved_human_flag: DB error for phone=%s clinic_id=%s", phone, clinic_id
        )
        return False


def trigger_fallback(user: str, clinic: dict, reason: str) -> str:
    """Log, alert, and return the appropriate fallback reply. Never raises."""
    _stats["fallbacks"] += 1
    logger.warning(
        "Fallback triggered: user=%s clinic=%s reason=%s",
        user, clinic.get("name", "unknown"), reason,
    )
    alert_telegram(
        clinic_name=clinic.get("name", "Unknown"),
        user=user,
        event="fallback triggered",
        reason=reason,
    )
    if reason == "human_requested":
        write_conversation_flag(clinic.get("id"), user, "human_requested")
        return _human_escalation_reply(clinic)
    contact = clinic.get("human_contact_number")
    if contact:
        return (
            f"Let me connect you with our clinic team for further assistance.\n"
            f"You can reach us at {contact}."
        )
    return (
        "Let me connect you with our clinic team for further assistance.\n"
        "Our team will follow up with you shortly."
    )


def write_conversation_flag(clinic_id: int, phone: str, flag_type: str) -> None:
    """Write a flag if no identical unresolved flag exists for this phone+flag_type.

    Guards:
    - If clinic_id is None, logs a warning and returns without writing.
    - Deduplicates: skips if an unresolved flag already exists for (clinic_id, phone, flag_type).
    - Wrapped in try/except so it NEVER raises or crashes the webhook.
    """
    if clinic_id is None:
        logger.warning(
            "write_conversation_flag: clinic_id is None, skipping flag type=%s phone=%s",
            flag_type, phone,
        )
        return
    try:
        with SessionLocal() as db:
            existing = db.query(ConversationFlag).filter(
                ConversationFlag.clinic_id == clinic_id,
                ConversationFlag.phone == phone,
                ConversationFlag.flag_type == flag_type,
                ConversationFlag.resolved_at == None,  # noqa: E711
            ).first()
            if existing:
                logger.info(
                    "write_conversation_flag: duplicate skipped flag_type=%s phone=%s clinic_id=%s",
                    flag_type, phone, clinic_id,
                )
                return
            db.add(ConversationFlag(
                clinic_id=clinic_id,
                phone=phone,
                flag_type=flag_type,
            ))
            db.commit()
            logger.info(
                "write_conversation_flag: flagged flag_type=%s phone=%s clinic_id=%s",
                flag_type, phone, clinic_id,
            )
    except Exception:
        logger.exception(
            "write_conversation_flag: error writing flag type=%s phone=%s clinic_id=%s",
            flag_type, phone, clinic_id,
        )


METRIC_FIELDS = {
    "conversations_handled",
    "messages_handled",
    "bookings_created",
    "after_hours_messages",
    "human_escalations",
}


def _local_day_bounds_utc(clinic_tz: str, at_local: datetime = None) -> tuple[datetime, datetime, datetime]:
    if at_local is None:
        at_local = now_local(clinic_tz)

    start_local = at_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    utc = ZoneInfo("UTC")
    start_utc = start_local.astimezone(utc).replace(tzinfo=None)
    end_utc = end_local.astimezone(utc).replace(tzinfo=None)
    return start_utc, end_utc, start_local


def increment_daily_metric(clinic_id: int, metric_field: str, at_local: datetime = None) -> None:
    if clinic_id is None:
        return
    if metric_field not in METRIC_FIELDS:
        raise ValueError(f"Unsupported metric field: {metric_field}")

    if at_local is None:
        at_local = now_local(TIMEZONE)
    metric_date = at_local.date()
    now_utc = datetime.utcnow()
    initial_counts = {
        "conversations_handled": 0,
        "messages_handled": 0,
        "bookings_created": 0,
        "after_hours_messages": 0,
        "human_escalations": 0,
    }
    initial_counts[metric_field] = 1

    statement = text(f"""
        INSERT INTO clinic_daily_metrics (
            clinic_id,
            metric_date,
            conversations_handled,
            messages_handled,
            bookings_created,
            after_hours_messages,
            human_escalations,
            created_at,
            updated_at
        ) VALUES (
            :clinic_id,
            :metric_date,
            :conversations_handled,
            :messages_handled,
            :bookings_created,
            :after_hours_messages,
            :human_escalations,
            :created_at,
            :updated_at
        )
        ON CONFLICT (clinic_id, metric_date)
        DO UPDATE SET
            {metric_field} = clinic_daily_metrics.{metric_field} + 1,
            updated_at = :updated_at
    """)
    with engine.begin() as conn:
        conn.execute(
            statement,
            {
                "clinic_id": clinic_id,
                "metric_date": metric_date,
                "created_at": now_utc,
                "updated_at": now_utc,
                **initial_counts,
            },
        )


def is_first_user_message_for_local_day(
    clinic_id: int, phone: str, clinic_tz: str, at_local: datetime = None
) -> bool:
    if clinic_id is None:
        return False
    start_utc, end_utc, _ = _local_day_bounds_utc(clinic_tz, at_local=at_local)
    with SessionLocal() as db:
        existing = (
            db.query(ConversationMessage.id)
            .filter(
                ConversationMessage.clinic_id == clinic_id,
                ConversationMessage.user == phone,
                ConversationMessage.role == "user",
                ConversationMessage.created_at >= start_utc,
                ConversationMessage.created_at < end_utc,
            )
            .first()
        )
    return existing is None


def is_after_hours_message(clinic: dict, at_local: datetime = None) -> bool:
    if at_local is None:
        clinic_tz = clinic.get("timezone", TIMEZONE)
        at_local = now_local(clinic_tz)

    if at_local.weekday() == 6:
        return True

    open_hour = clinic.get("open_hour", 10)
    close_hour = clinic.get("close_hour", 18)
    open_dt = at_local.replace(hour=open_hour, minute=0, second=0, microsecond=0)
    close_dt = at_local.replace(hour=close_hour, minute=0, second=0, microsecond=0)
    return at_local < open_dt or at_local >= close_dt


# -----------------------------------------------------------------------------
# DB access helpers
# -----------------------------------------------------------------------------

def get_history(user: str, clinic_id: Optional[int] = None) -> List[Dict[str, str]]:
    with SessionLocal() as db:
        q = db.query(ConversationMessage).filter(ConversationMessage.user == user)
        if clinic_id is not None:
            q = q.filter(ConversationMessage.clinic_id == clinic_id)
        rows = (
            q.order_by(ConversationMessage.id.desc())
            .limit(MAX_HISTORY_MESSAGES)
            .all()
        )
        rows.reverse()
        return [{"role": r.role, "content": r.content} for r in rows]


def append_history(user: str, role: str, content: str, clinic_id: int = None) -> None:
    with SessionLocal() as db:
        db.add(ConversationMessage(user=user, role=role, content=content, clinic_id=clinic_id))
        db.commit()
    # Track every inbound contact so the Customers panel auto-populates.
    # Name is not known at message-time; a later booking upsert will fill it in.
    if role == "user" and clinic_id is not None:
        upsert_customer(clinic_id, user)


# reset_booking_state, get_booking_state, update_booking_state,
# get_existing_booking, get_all_bookings, save_booking_record, delete_booking_record,
# upsert_customer, record_booking_for_customer, and multi-patient helpers are
# imported from booking.py / customers.py / utils.py above.

from customers import upsert_customer, record_booking_for_customer


def clear_conversation_runtime_state(user: str, clinic_id: Optional[int]) -> None:
    """Clear conversational runtime state for one user within one clinic scope.

    Preserves booking_records intentionally.
    """
    _clear_pending_date_clarification(user, clinic_id)
    reset_booking_state(user, clinic_id=clinic_id)

    with SessionLocal() as db:
        messages_q = db.query(ConversationMessage).filter(ConversationMessage.user == user)
        if clinic_id is not None:
            messages_q = messages_q.filter(ConversationMessage.clinic_id == clinic_id)
        messages_q.delete(synchronize_session=False)

        if clinic_id is not None:
            db.query(ConversationFlag).filter(
                ConversationFlag.clinic_id == clinic_id,
                ConversationFlag.phone == user,
                ConversationFlag.resolved_at == None,  # noqa: E711
            ).delete(synchronize_session=False)

        db.commit()


def reset_user_session(user: str) -> None:
    with SessionLocal() as db:
        db.query(ConversationMessage).filter(ConversationMessage.user == user).delete()
        db.query(BookingStateModel).filter(BookingStateModel.user == user).delete()
        db.query(BookingRecordModel).filter(BookingRecordModel.user == user).delete()
        db.commit()


# -----------------------------------------------------------------------------
# Direct reply guardrails
# -----------------------------------------------------------------------------

def get_direct_reply(user: str, text: str, clinic=None) -> Optional[str]:
    if clinic is None:
        clinic = get_default_clinic()

    if is_human_request(text):
        return trigger_fallback(user, clinic, "human_requested")

    # Multi-patient same-time question guard.
    # We book one patient at a time. Redirect users who ask to share a slot.
    if _is_multi_patient_same_time_question(text):
        logger.info("MULTI_PATIENT_MODE_REQUEST | user=%s | msg=%r", user, text)
        return (
            "We book one patient at a time and appointments run back-to-back. "
            "I can book the second patient right after the first one finishes — "
            "just let me know when the first booking is confirmed."
        )

    # Multi-patient initial detection guard.
    # Intercept before the LLM so it never tries to handle compound names in one pass.
    _mp_name1, _mp_name2 = _detect_multi_patient_booking_request(text)
    if _mp_name1 and _mp_name2:
        logger.info(
            "MULTI_PATIENT_MODE_DETECTED | user=%s | name1=%r | name2=%r | msg=%r",
            user, _mp_name1, _mp_name2, text,
        )
        return (
            f"Sure! Let's book them one at a time. "
            f"Who should I start with — {_mp_name1} or {_mp_name2}?"
        )

    if is_reset_command(text):
        t = normalize_text(text)
        if t == "cancel":
            # Standalone "cancel" — patient wants to cancel their appointment.
            all_bookings = get_all_bookings(user, clinic_id=clinic.get("id") if clinic else None)
            if all_bookings:
                cancel_result = cancel_booking(user, clinic)
                if cancel_result.get("ok"):
                    # Single booking was cancelled — report which one.
                    cancelled = all_bookings[0]
                    logger.info("Booking cancelled for user=%s via standalone cancel command", user)
                    return (
                        f"Your {cancelled['service']} appointment on {cancelled['date']} "
                        f"at {cancelled['time']} has been cancelled. "
                        "Is there anything else I can help you with?"
                    )
                elif "multiple bookings" in cancel_result.get("message", "").lower():
                    # Multiple bookings — ask which patient to cancel.
                    return cancel_result["message"]
                else:
                    # Calendar delete failed — still clear local state so user isn't stuck.
                    reset_booking_state(user, clinic_id=clinic.get("id") if clinic else None)
                    logger.warning(
                        "cancel_booking failed during cancel command for user=%s; state cleared anyway",
                        user,
                    )
                    return "I've cleared your session. Is there anything else I can help you with?"
            else:
                reset_booking_state(user, clinic_id=clinic.get("id") if clinic else None)
                logger.info("No booking to cancel for user=%s; state reset", user)
                return "Nothing to cancel. How may I help you today?"
        else:
            clinic_id = clinic.get("id") if clinic else None
            # reset / restart / start over — clear runtime conversation memory only.
            # Booking records are intentionally preserved so reminders still fire.
            clear_conversation_runtime_state(user, clinic_id=clinic_id)
            logger.info("Conversation runtime state reset for user=%s clinic_id=%s", user, clinic_id)
            return "Your chat has been reset. We can start fresh now."

    return None


def _pending_date_key(user: str, clinic_id: Optional[int]) -> Tuple[str, int]:
    return (user, clinic_id if clinic_id is not None else -1)


def _set_pending_date_clarification(user: str, clinic_id: Optional[int]) -> None:
    now_dt = now_local()
    today_date = now_dt.strftime("%Y-%m-%d")
    tomorrow_dt = now_dt + timedelta(days=1)
    tomorrow_date = tomorrow_dt.strftime("%Y-%m-%d")
    _PENDING_DATE_CLARIFICATIONS[_pending_date_key(user, clinic_id)] = {
        "today_date": today_date,
        "tomorrow_date": tomorrow_date,
        "today_weekday": now_dt.strftime("%A").lower(),
        "tomorrow_weekday": tomorrow_dt.strftime("%A").lower(),
    }


def _clear_pending_date_clarification(user: str, clinic_id: Optional[int]) -> None:
    _PENDING_DATE_CLARIFICATIONS.pop(_pending_date_key(user, clinic_id), None)


def _assistant_is_midnight_date_clarification(reply: str) -> bool:
    normalized = normalize_text(reply)
    return (
        "did you mean" in normalized
        and "(today)" in normalized
        and "(tomorrow)" in normalized
    )


# Phrases that strongly indicate the LLM is issuing a booking confirmation.
# Deliberately specific to avoid false positives on general conversation.
_BOOKING_CONFIRMATION_PHRASES = [
    "your booking has been",
    "your appointment has been",
    "successfully booked",
    "booking is confirmed",
    "appointment is confirmed",
    "i've booked you",
    "i have booked you",
    "you're all booked",
    "you are all booked",
    "you're booked in",
    "you are booked in",
]


def _reply_looks_like_booking_confirmation(reply: str) -> bool:
    """Return True if the reply reads like a booking confirmation message.

    Used by the server-side confirmation gate in run_ai() to intercept premature
    confirmations — cases where the LLM writes a confirmation without having
    called create_booking successfully in the current turn.
    """
    lower = reply.lower()
    return any(phrase in lower for phrase in _BOOKING_CONFIRMATION_PHRASES)


def _consume_pending_date_clarification(
    user: str,
    clinic_id: Optional[int],
    user_text: str,
) -> Dict[str, str]:
    pending = _PENDING_DATE_CLARIFICATIONS.get(_pending_date_key(user, clinic_id))
    if not pending:
        return {"status": "not_pending"}

    text = normalize_text(user_text)
    words = set(re.findall(r"[a-z]+", text))

    resolved_date = None
    resolved_label = None

    if {"today"} & words:
        resolved_date = pending["today_date"]
        resolved_label = f"{pending['today_weekday'].title()}, {pending['today_date']}"
    elif {"tomorrow", "tmr", "tmrw"} & words:
        resolved_date = pending["tomorrow_date"]
        resolved_label = f"{pending['tomorrow_weekday'].title()}, {pending['tomorrow_date']}"
    else:
        weekday_aliases = {
            "monday": "monday", "mon": "monday",
            "tuesday": "tuesday", "tue": "tuesday", "tues": "tuesday",
            "wednesday": "wednesday", "wed": "wednesday",
            "thursday": "thursday", "thu": "thursday", "thur": "thursday", "thurs": "thursday",
            "friday": "friday", "fri": "friday",
            "saturday": "saturday", "sat": "saturday",
            "sunday": "sunday", "sun": "sunday",
        }
        matched_weekdays = {weekday_aliases[w] for w in words if w in weekday_aliases}
        if pending["today_weekday"] in matched_weekdays:
            resolved_date = pending["today_date"]
            resolved_label = f"{pending['today_weekday'].title()}, {pending['today_date']}"
        elif pending["tomorrow_weekday"] in matched_weekdays:
            resolved_date = pending["tomorrow_date"]
            resolved_label = f"{pending['tomorrow_weekday'].title()}, {pending['tomorrow_date']}"

    if not resolved_date:
        return {
            "status": "needs_clarification",
            "reply": (
                f"Just to confirm, did you mean {pending['today_weekday'].title()} (today) "
                f"or {pending['tomorrow_weekday'].title()} (tomorrow)?"
            ),
        }

    _clear_pending_date_clarification(user, clinic_id)
    return {
        "status": "resolved",
        "date": resolved_date,
        "canonical_user_message": f"I mean {resolved_label}.",
        "override_system_message": (
            "DATE CLARIFICATION RESOLVED: The user explicitly selected "
            f"{resolved_label}. Ignore earlier ambiguous relative-date interpretations "
            "from previous turns and proceed using this date only."
        ),
    }

# Maps common dental symptom/intent words to a partial service-name keyword.
# Keys are what may appear in user messages; values are substrings of service names.
# Only services actually present in the clinic's catalog will be matched.
_DENTAL_SYMPTOM_HINTS = [
    ({"yellow", "stain", "staining", "discolor", "discolour", "brighten"}, "whiten"),
    ({"tartar", "plaque", "calculus", "clean teeth"}, "scal"),
    ({"cavity", "cavities", "decay", "caries"}, "fill"),
    ({"ache", "painful", "sensitive", "sensitivity", "hurt", "sore"}, "fill"),
    ({"brace", "braces", "crooked", "align", "alignment", "straighten", "gap"}, "brace"),
    ({"polish", "smooth"}, "polish"),
]


def _implied_services_from_symptoms(user_text: str, service_names: list) -> list:
    """Return service names from service_names implied by symptom/intent words in user_text.

    Only returns services that exist in the clinic's catalog.  Explicit service
    mentions (handled separately) take precedence over these implied matches.
    """
    implied = []
    for symptom_keywords, svc_keyword in _DENTAL_SYMPTOM_HINTS:
        if any(kw in user_text for kw in symptom_keywords):
            for svc in service_names:
                if svc_keyword in normalize_text(svc) and svc not in implied:
                    implied.append(svc)
    return implied


# -----------------------------------------------------------------------------
# Prompt
# -----------------------------------------------------------------------------

def build_system_prompt(user: str, clinic=None, latest_user_message: str = "") -> str:
    if clinic is None:
        clinic = get_default_clinic()
    now_ctx = get_now_context()
    state = get_booking_state(user, clinic_id=clinic.get("id") if clinic else None)
    existing = get_existing_booking(user, clinic_id=clinic.get("id") if clinic else None)

    # Tone
    raw_tone = clinic.get("tone", "professional")
    if raw_tone not in _VALID_TONES:
        raw_tone = "professional"
    tone_description = _TONE_INSTRUCTIONS[raw_tone]

    # Active promotions
    clinic_id = clinic.get("id")
    active_promos = get_active_promotions(clinic_id) if clinic_id else []
    latest_user_text = normalize_text(latest_user_message or "")
    if not latest_user_text:
        with SessionLocal() as db:
            q = db.query(ConversationMessage.content).filter(
                ConversationMessage.user == user,
                ConversationMessage.role == "user",
            )
            if clinic_id is not None:
                q = q.filter(ConversationMessage.clinic_id == clinic_id)
            latest_row = q.order_by(ConversationMessage.id.desc()).first()
            if latest_row:
                latest_user_text = normalize_text(latest_row[0] or "")

    asks_services_pricing_booking_or_promos = any(
        token in latest_user_text
        for token in (
            "service", "services", "price", "pricing", "cost", "how much", "fee",
            "book", "booking", "appointment", "slot", "available", "availability",
            "discount", "promo", "promotion", "offer", "deal", "special",
        )
    )
    service_catalog = _get_service_catalog(clinic)
    service_names = [item["name"] for item in service_catalog]
    # Explicit service mentions (e.g. user says "whitening")
    mentioned_services = [
        svc for svc in service_names
        if normalize_text(svc) in latest_user_text
    ]
    # Implied services from symptom/intent keywords (e.g. "yellow teeth" → whitening)
    implied_services = _implied_services_from_symptoms(latest_user_text, service_names)
    # Explicit beats implicit; both are used for promo matching
    all_relevant_services = mentioned_services or implied_services

    selected_promo = None
    if active_promos:
        # Case A: A specific service is relevant (explicit or implied) — try to match a promo
        if all_relevant_services:
            for promo in active_promos:
                promo_text = normalize_text(
                    f"{promo.get('title', '')} {promo.get('description', '')}"
                )
                if any(normalize_text(svc) in promo_text for svc in all_relevant_services):
                    selected_promo = promo
                    break
        # Case B: No service-specific promo matched, but user asked about pricing/discounts
        # → surface first promo as generic fallback (existing explicit-query behavior)
        if not selected_promo and asks_services_pricing_booking_or_promos:
            selected_promo = active_promos[0]

    if selected_promo:
        promo_line = selected_promo["title"] + (
            f": {selected_promo['description']}" if selected_promo.get("description") else ""
        )
        promotions_block = f"PROMOTION CANDIDATE (max one)\n- {promo_line}"
    else:
        promotions_block = "PROMOTION CANDIDATE (max one)\n- none"

    services_text = "\n".join(
        f"- {item['name']}: {item['duration_minutes']} minutes"
        + (f", RM {item['price']:.2f}" if item.get("price") is not None else "")
        for item in service_catalog
    )

    closures = clinic.get("special_closures", [])
    closure_notes = clinic.get("closure_notes", {})
    if closures:
        special_closure_text = "\n".join(
            f"- {date}: {closure_notes.get(date, 'Closed')}"
            for date in closures
        )
    else:
        special_closure_text = "- none"

    if existing:
        existing_booking_text = (
            f"- event_id exists: yes\n"
            f"- service: {existing.get('service')}\n"
            f"- date: {existing.get('date')}\n"
            f"- time: {existing.get('time')}\n"
            f"- name: {existing.get('name')}\n"
            f"- status: {existing.get('status')}"
        )
    else:
        existing_booking_text = "- no existing booking recorded"

    # Dynamic booking state advisory: tells the LLM exactly what is missing
    # and what action is required next, preventing premature confirmation.
    _s = state
    _missing = [
        f for f, v in [
            ("service", _s.get("service")),
            ("date", _s.get("date")),
            ("time", _s.get("time")),
            ("patient name", _s.get("name")),
        ] if not v
    ]
    if not any([_s.get("service"), _s.get("date"), _s.get("time"), _s.get("name"), _s.get("availability_ok")]):
        booking_state_advisory = "No booking in progress."
    elif _s.get("availability_ok") and not _s.get("name"):
        booking_state_advisory = (
            "⚠️ BOOKING NOT CREATED YET. Slot is available but patient name is missing. "
            "You MUST ask for the patient's full name before calling create_booking. "
            "Do NOT write any confirmation message yet."
        )
    elif _s.get("availability_ok") and _s.get("name"):
        booking_state_advisory = (
            "⚠️ BOOKING NOT CREATED YET. All required fields are present. "
            "You MUST call create_booking now. "
            "Do NOT write a confirmation message until create_booking returns ok=True."
        )
    elif _missing:
        booking_state_advisory = (
            f"⚠️ BOOKING INCOMPLETE. Still missing: {', '.join(_missing)}. "
            "Collect missing information before proceeding. Do NOT call create_booking yet."
        )
    else:
        booking_state_advisory = "Availability not yet checked. Call check_availability before create_booking."

    # Midnight ambiguity guard: during 23:00–01:59 local time, relative date
    # phrases like "today", "tomorrow", "tmr", and weekday names are genuinely
    # ambiguous — a user messaging at 12:05 AM may mean either calendar day.
    # Inject a prominent clarification instruction only during that window.
    _now_hr = now_local().hour
    _in_midnight_window = _now_hr >= 23 or _now_hr < 2
    if _in_midnight_window:
        # Compute the two calendar days the user could plausibly mean.
        _now_dt = now_local()
        _today_label = _now_dt.strftime("%A, %-d %B")        # e.g. "Wednesday, 15 April"
        _tomorrow_label = (_now_dt + timedelta(days=1)).strftime("%A, %-d %B")
        midnight_guard_block = f"""
⚠️ MIDNIGHT DATE CLARIFICATION GUARD (ACTIVE)
Current time is {now_ctx['time']} — you are in the midnight ambiguity window (11 PM – 2 AM).

When the user references ANY of these ambiguous date phrases in this message:
  "today", "tomorrow", "tmr", "tmrw", or any weekday name (Monday, Tuesday, Wednesday, etc.)

You MUST ask one short clarification question BEFORE calling any date or booking tools.

Example clarification (adapt naturally to the conversation):
  "Just to confirm — it's currently {now_ctx['time']}. Did you mean {_today_label} (today) or {_tomorrow_label} (tomorrow)?"

Do NOT call check_date_available, resolve_booking_datetime, check_availability, or any other tool until the user explicitly confirms which day they mean.
If the user already stated an unambiguous date (e.g. "15 April", "2026-04-16"), skip this guard.
""".strip()
    else:
        midnight_guard_block = ""

    return f"""
You are the AI WhatsApp receptionist for {clinic['name']} in {clinic['location']}.

CURRENT TIME CONTEXT
- Current Malaysia datetime: {now_ctx['iso']}
- Current Malaysia readable datetime: {now_ctx['readable']}
- Current Malaysia weekday: {now_ctx['weekday']}
- Current Malaysia date: {now_ctx['date']}
- Current Malaysia time: {now_ctx['time']}
- Timezone: {TIMEZONE}

TIME SOURCE OF TRUTH
- The provided Malaysia datetime above is the only source of truth for current date and time.
- Never guess the current date, time, weekday, month, or year from memory.
- Interpret phrases like "today", "tomorrow", "tmr", "next Friday", "this Saturday", and "later" only using the provided Malaysia datetime above.
- Never say a requested future time is already in the past unless the backend tool confirms that it is in the past.
- If there is any uncertainty, ask a short clarification question instead of guessing.
{midnight_guard_block}
CLINIC INFO
- Clinic name: {clinic['name']}
- Location: {clinic['location']}
- Business hours: {clinic['hours_text']}
- Opening message: {clinic.get('opening_message', '')}

SPECIAL CLOSURES
{special_closure_text}

SERVICES
{services_text}

{promotions_block}

ROLE
- You are a {tone_description} dental clinic receptionist.
- Help users with appointment booking and basic clinic FAQs only.
- If the clinic has an opening message set, use it as your greeting when a user first contacts you.
- Mention the promotion candidate when: (a) you are recommending a service that matches the promotion, OR (b) the user explicitly asks about pricing, discounts, or promotions. Never mention promotions in pure greetings or unrelated topics.
- Never mention promotions in greetings.
- Mention at most ONE promotion in a reply.
- If the user mentions a specific service, prefer a promotion that matches that service.
- If no service-specific match exists, you may use the single promotion candidate above.

ALLOWED TOPICS
- dental services
- appointment booking
- clinic hours
- clinic location
- basic treatment questions
- basic FAQs
- promo / opening info
- closure dates for this clinic

FORBIDDEN TOPICS
- politics
- finance
- sports
- coding
- news
- general knowledge

BOOKING FLOW

Path A — user specifies a date AND time:
1. Collect service
2. When the user mentions a date (including relative phrases like "tmr", "tomorrow", "next Sunday", weekday names), immediately call check_date_available with that date phrase — do NOT ask for time yet
3. If check_date_available returns closed=true, handle based on the reason field:
   - reason == "already_closed_today": the clinic is open on this day of the week but has already closed for today. Tell the user the clinic is already closed for the rest of today (do NOT say "closed on [weekday]"). Suggest booking for one of the next_open_days instead.
   - reason == "sunday" or "special_closure": the clinic is actually closed on that day. Inform the user the clinic is closed on that day and suggest the next 2 open days from next_open_days.
   In both cases: do NOT ask for a time. Wait for the user to pick an open day.
4. If check_date_available returns ok=true (clinic is open): then ask for the preferred time
5. Collect the user's intended time phrase
6. Call resolve_booking_datetime to convert the confirmed date/time into exact values
7. If resolve_booking_datetime says the slot is in the past, do not ask for the user's name yet; suggest another time
8. If resolve_booking_datetime succeeds and the slot is not in the past, restate the exact intended appointment day and time
9. Call check_availability using the exact resolved date and time
10. Only after availability looks valid, collect full name if still missing
11. If available and full name is known, call create_booking
12. Only after successful create_booking, confirm the appointment

Path B — user asks about a specific day without specifying a time (e.g. "any slots tomorrow?", "what's available Friday?", "any slots this Thursday?"):
1. Collect service if not yet known (ask if needed)
2. Call check_date_available with the mentioned date phrase
3. If check_date_available returns closed=true, handle based on the reason field:
   - reason == "already_closed_today": the clinic is open on this day of the week but has already closed for today. Tell the user the clinic has closed for today (do NOT say "closed on [weekday]"). Suggest the next_open_days. Do NOT call get_available_slots.
   - reason == "sunday" or "special_closure": the clinic is closed on that day. Inform the user and suggest the next 2 open days. Do NOT call get_available_slots.
4. If check_date_available returns ok=true: call get_available_slots with the resolved date — this returns up to 5 free times for that day
5. Present the available slots as a numbered or bulleted list and ask which time works best
6. Once the user picks a time, proceed from Path A step 9 (call check_availability, then create_booking)

Path C — user asks for the next earliest or soonest slot without specifying a day (e.g. "when's the next earliest slot?", "earliest available?", "any slot soon?", "what's the soonest I can come in?"):
1. Collect service if not yet known (ask if needed)
2. Call get_available_slots for today first (use today's date in YYYY-MM-DD). get_available_slots automatically skips past times, so it is always safe to start from today.
3. If slots are available, present them as a list and ask which time works best
4. If get_available_slots returns no slots (day is fully booked, already closed, or today is a closed day), call get_available_slots for tomorrow — repeat for up to 3 days total
5. Only use find_next_available_slot as a last resort when get_available_slots has found nothing across 3 days
6. Once the user picks a time, proceed from Path A step 7

MULTI-PATIENT BOOKING PROTOCOL
When a user wants to book for multiple people (e.g. "book for John and Mary", "scaling for Ali and Siti"):
1. Acknowledge all patients by name: "Sure! Let's book them one at a time."
2. Focus on the FIRST patient only: "Let's start with [Name 1]. What service do they need?" (skip if service already given)
3. Complete the full booking flow for [Name 1] — service, date, time, name, then create_booking.
4. After create_booking for [Name 1] succeeds, proactively say: "Great! Now let's book [Name 2]. What service do they need?"
5. Complete the full booking flow for [Name 2].
6. Never offer or check a time slot for Patient 2 before Patient 1 is fully confirmed.
7. Never say a slot is "unavailable for [Name]" unless you have actually attempted to book that slot for that specific patient in this session.

STRICT BOOKING RULES
- Never create a booking unless the intended service, exact date, exact time, and full name are all clear.
- Before create_booking, the final booking slot must reflect the latest user messages, not older conversation context.
- If there is any ambiguity between two possible dates or times, ask a short clarification question.
- Never interpret relative dates like "tomorrow" or weekday names by yourself when a tool can resolve them.
- ALWAYS call check_date_available immediately when the user mentions a date — before asking for any time. If the clinic is closed that day, do not ask for time; instead inform the user and suggest the next_open_days returned by the tool.
- Always use resolve_booking_datetime before check_availability when the user gave a natural language date or time.
- Always call check_availability before create_booking.
- NEVER write any message that tells the patient their appointment is confirmed, booked, set, or scheduled (in any language) unless create_booking returned ok=True in this exact tool-call sequence. availability_ok=True only means the slot is free — the booking does not exist in the calendar until create_booking succeeds.
- NEVER call create_booking without a patient name. If the patient name is missing after availability is confirmed, ask for it before calling create_booking.
- Do not confirm a booking unless create_booking returns ok=True.
- If check_availability says the requested time is in the past, accept that as truth and suggest another time.
- If check_availability says the slot is unavailable, accept that as truth. If the response includes an available_slots list, immediately present those times to the user as their real options for that day — do NOT ask the user to suggest another time themselves. Example: "That time isn't available. For [service] tomorrow, the available times are [list]. Which works for you?"
- If the user asks about availability for a specific day without a time, use get_available_slots (not check_availability) to show multiple options.
- If the user asks for the next earliest or soonest slot (no day specified), use get_available_slots starting from today — never skip today if valid same-day slots may still exist. If today has no slots, try tomorrow, then the day after. Do NOT use find_next_available_slot unless get_available_slots returns no slots on the first 3 days tried.
- Never call find_next_available_slot when a day has already been resolved — use get_available_slots for that day instead.
- Do not independently overrule backend tool results.
- If the user wants to move an existing booking to a new date or time, use reschedule_booking.
- If the user wants to change a booked name, service, date, or time in a way that should replace the old booking, use this sequence:
  1. ask only for any missing corrected info
  2. call cancel_booking
  3. call resolve_booking_datetime for the corrected booking if needed
  4. call check_availability for the corrected booking
  5. call create_booking with the corrected full details
- If the user says you got their booking details wrong, use the existing booking record below as the current source of truth and replace the old booking instead of creating a duplicate.
- If the user starts a new booking flow but then switches to correcting the existing booking, prioritize the correction request.
- If create_booking returns ok=False with a message about an existing booking, do NOT retry create_booking. Tell the user about their existing booking and ask if they want to cancel it first. Only call cancel_booking if the user confirms they want to cancel.
- When multiple patients are mentioned, NEVER suggest the same time slot for both. Always complete one full booking before starting the next.
- Do NOT say "that slot is unavailable for [Name]" unless you have actually attempted to book that slot for that specific patient in the current conversation.

HANDLING MESSY REAL-WORLD INPUT

SHORT OR STACCATO MESSAGES
- Users often send booking details across several short messages in quick succession, e.g. "whitening", then "tmr", then "2pm". Treat consecutive short user messages as parts of one combined intent. Piece together the service, date, and time from recent messages before responding.
- Only ask for missing information — never re-ask for something already stated in recent messages, even if it arrived in a separate message.
- Example: if the last 3 messages are "scaling", "Friday", "3pm" — treat this as "scaling on Friday at 3pm" and proceed with check_date_available.

TYPOS AND SPELLING VARIATIONS
- If the user's message contains a typo or informal spelling (e.g. "whitning", "sclaing", "polshing", "tmr", "tmrow"), interpret the most likely intended meaning and proceed without correcting or commenting on the spelling.
- For service names that are close but not exact matches (e.g. "teeth clean", "bleaching", "fill"), map them to the nearest available service and confirm your interpretation in a single short phrase before proceeding. Example: "Sure, I'll book a scaling (teeth cleaning) — when would you like to come in?"
- For slang or dialect (e.g. "tampal", "cuci gigi", "gigi putih", Mandarin service names), interpret them correctly and proceed naturally without switching language unless the user is clearly writing in a different language.

MID-BOOKING CHANGES
- If the user changes a detail mid-booking (different service, different date, different time), update only that detail and continue the booking flow from the current step. Do NOT restart from scratch.
- Example: user gave service + date + time, then says "actually make it 3pm instead of 2pm" — update the time and proceed to resolve_booking_datetime with the new time. Do not re-ask for service or date.
- If the user says "never mind" or "cancel that" mid-booking before any confirmed booking exists, ask one clarifying question: "Sure — would you like to start a new booking or is there something else I can help with?"
- Only call reset_booking_state if the user explicitly says to start over or cancel.

VAGUE TIME AND DATE HANDLING
- If the user gives a vague time such as "morning", "afternoon", "evening", "after work", "lepas kerja", "after lunch", "lunchtime", "later", "soon", "night", "midday", or "anytime", do NOT call resolve_booking_datetime yet. Ask one short clarifying question and suggest 2 specific times within business hours. Examples:
  - "morning" / "pagi" → "What time in the morning works for you — 10am or 11am?"
  - "afternoon" / "petang" → "What time in the afternoon suits you — 2pm or 3pm?"
  - "evening" / "after work" / "lepas kerja" → "What time works best — 5pm or 5:30pm?"
  - "midday" / "noon" / "lunch" → "Around noon — would 12pm or 12:30pm work for you?"
  - "night" / "malam" → "Our last slot is at [close hour minus service duration]. Would that work?"
  - "soon" / "asap" → "Happy to check the earliest slot! What service do you need?" (if service unknown), or call get_available_slots for today first.
- Only call resolve_booking_datetime once the user provides a specific time like "3pm" or "10:30am".
- If the user gives a vague date such as "later this week", "sometime soon", "this week", "anytime", or "this weekend", do NOT call resolve_booking_datetime. Ask which specific day they prefer. For "this weekend", suggest Saturday (we are closed Sundays).

CONVERSATION RULES
- Never say "I made a mistake", "I was wrong", "I apologize for the confusion", or any phrase that references a prior error. If you need to state corrected information, state it directly and confidently. Example: instead of "I made a mistake — the clinic is closed Sunday", say "The clinic is closed on Sundays. The next available days are Monday and Tuesday."
- If the user is simply thanking you, greeting you, acknowledging, or ending the chat, reply briefly and warmly without calling any tools. Do not ask follow-up questions in these cases.
- If the user asks about anything outside clinic scope (finance, politics, sports, news, general knowledge, coding, etc.), politely decline in one sentence: "I can only help with dental services, appointments, clinic hours, and location." Do not engage with the off-topic subject.
- Do not call booking tools unless the user is actively discussing a booking or changing an appointment.
- When create_booking returns ok=true and short_notice=false, write a warm 1–2 sentence confirmation that includes the service name, date and time, and patient name. End with "We look forward to seeing you." Do not ask any follow-up questions. Do not offer to change anything.
- When create_booking returns ok=true and short_notice=true, write the same confirmation but end with "Since this is a short-notice booking, our clinic team may contact you if any changes are needed." Do not ask any follow-up questions. Do not offer to change anything.
- When create_booking returns ok=false and partial_success=true, a subset of patients were booked and at least one was NOT booked. You MUST clearly state which patients were successfully booked AND which patient was NOT booked. Do NOT say all patients are confirmed. Do NOT use language that implies full success. Follow the message field exactly — it tells you who was booked and what the next step is for the unbooked patient.
- When reschedule_booking returns ok=true, write a warm 1–2 sentence confirmation that includes the service name, new date and time, and patient name. Do not ask any follow-up questions. Do not offer to change anything.
- After a booking is completed, do not continue modifying the booking unless the user clearly asks to change booking details.
- Keep the conversation feeling natural and helpful like a real receptionist chatting on WhatsApp.

CURRENT BOOKING STATE
- service: {state.get('service')}
- date: {state.get('date')}
- time: {state.get('time')}
- name: {state.get('name')}
- availability_ok: {state.get('availability_ok')}

BOOKING STATE MEANING
- availability_ok=True means the time slot is free on the calendar. It does NOT mean the booking exists or the patient is confirmed. The booking is only real after create_booking returns ok=True.
- availability_ok=False means no slot has been verified yet, or the last checked slot was unavailable.

REQUIRED ACTION
{booking_state_advisory}

CURRENT EXISTING BOOKING RECORD
{existing_booking_text}

DEMO GUARDRAILS — follow these in every response

CONSISTENCY
- Do not contradict information you gave earlier in this conversation. If a tool result updates something you said, state the correct information directly without drawing attention to any change.
- Never state that a slot is available or unavailable unless a tool in this session confirmed it. Do not speculate about availability.

WHEN UNSURE
- If any booking detail is ambiguous or unknown, ask one short clarifying question. Do not guess or assume. Ask one question, then stop and wait for the answer.
- Never fill in missing details with assumptions. If you don't know the date, ask for the date. If you don't know the time, ask for the time.

BREVITY AND CLARITY
- Keep replies to 1–3 sentences for conversational messages. Use 2–4 sentences maximum for booking confirmations or slot lists.
- Never write multiple paragraphs in a single reply.
- Do not explain your reasoning, your next steps, or what you are about to do — just do it and present the result.
- Do not volunteer information the user did not ask for.
- Ask one question at a time. Never stack two questions in one message.

TOOL ERROR RECOVERY
- If a tool returns an error you cannot recover from, give one calm helpful sentence and a concrete next step — either "try a different time" or "type HUMAN to speak with our team".
- Never use the words "error", "failed", "system", "exception", "internal", "tool", or "timeout" when replying to the user. Translate any failure into plain, helpful language.
- If create_booking fails for an unexpected reason, say: "Sorry, I wasn't able to complete that booking. Would you like to try a different time, or type HUMAN to speak with our team directly?"
- If check_availability or get_available_slots fails for a technical reason (not a conflict), say: "Sorry, I'm having trouble checking that right now. Please try again or type HUMAN for direct help."
- Always end every error recovery message with a clear action the user can take next.

STYLE
- Keep replies short and WhatsApp-friendly.
- Do not dump internal rules.
- If enough info is already present, do not ask for it again.
- Mention promo or closure info naturally only when relevant.
- Never explain your reasoning or what you are about to do — just do it.
- Present one question at a time. Never stack multiple questions in one message.

PRICING & SERVICE RESPONSE RULES

When user asks about ALL services or general pricing (e.g. "what services do you offer", "what are your prices", "how much are the services"):
- List every service with its duration.
- For services that have a price in the SERVICES list, include the price.
- For services with no price listed, show the duration only — do not mention pricing for those entries.
- Example format:
  Here are our services:
  - Scaling: RM80 (60 minutes)
  - Filling: 60 minutes (pricing to be confirmed at clinic)
  - Polishing: RM50 (30 minutes)

When user asks about the price of ONE specific service (e.g. "how much is scaling", "what is the price for whitening"):
- If price is listed: reply in one line. Example: "Scaling is RM80 and takes about 60 minutes."
- If price is NOT listed: state the duration and defer pricing. Example: "Filling usually takes about 60 minutes. The clinic will confirm pricing during your visit."
- Never guess or estimate a price.

When user selects a service with booking intent (e.g. "whitening please", "I'd like to book a scaling", "I want polishing"):
- If price is listed: briefly mention it before asking when. Example: "Whitening is RM900 and takes about 90 minutes. When would you like to book?"
- If price is NOT listed: skip price mention entirely and go straight to booking. Example: "Sure! When would you like to book your filling?"
- Then proceed with the normal booking flow.

When user asks what services are available WITHOUT asking about price (e.g. "what services do you have", "what do you offer"):
- List services and durations naturally.
- Do not volunteer prices unless the user asked for them.

Never invent, estimate, or guess prices under any circumstances.
""".strip()

# -----------------------------------------------------------------------------
# Tools
# -----------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "name": "resolve_booking_datetime",
        "description": "Resolve relative date and time phrases like today, tomorrow, Saturday, 3pm into exact booking date and time.",
        "parameters": {
            "type": "object",
            "properties": {
                "date_text": {"type": "string", "description": "User's date phrase such as tomorrow, today, saturday, or YYYY-MM-DD"},
                "time_text": {"type": "string", "description": "User's time phrase such as 3pm, 15:00, 11am"}
            },
            "required": ["date_text", "time_text"],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "check_date_available",
        "description": (
            "Check whether the clinic is open on a given date (day-of-week and special closures). "
            "Call this IMMEDIATELY after the user mentions a date — before asking for a time. "
            "If the clinic is closed that day, this returns the next 2 open days to suggest. "
            "Does NOT check calendar conflicts — use check_availability for that after a time is known."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date_text": {
                    "type": "string",
                    "description": "User's date phrase such as 'tomorrow', 'tmr', 'Sunday', 'next Monday', or YYYY-MM-DD"
                }
            },
            "required": ["date_text"],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "check_availability",
        "description": "Check whether an appointment slot is available for a supported clinic service.",
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Clinic service name"},
                "date": {"type": "string", "description": "Appointment date in YYYY-MM-DD"},
                "time": {"type": "string", "description": "Appointment time in HH:MM 24-hour format"}
            },
            "required": ["service", "date", "time"],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "find_next_available_slot",
        "description": (
            "Find the next open appointment slot for a service, scanning forward day by day. "
            "Use this ONLY as a last resort — when get_available_slots has returned no slots "
            "across multiple days, or when the user explicitly wants to search across the week "
            "without caring which day. Do NOT use this for a specific day or when the user asks "
            "for the next earliest slot (use get_available_slots for that instead)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Clinic service name"},
                "date": {"type": "string", "description": "Starting date for the scan in YYYY-MM-DD"},
                "time": {"type": "string", "description": "Starting time for the scan in HH:MM 24-hour format"}
            },
            "required": ["service", "date", "time"],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "get_available_slots",
        "description": (
            "Return up to 5 free appointment slots for a service on a given day. "
            "Use this for ALL day-based availability questions — both when the user names a day "
            "('any slots tomorrow?', 'what's available Friday?') AND when they ask for the next "
            "earliest slot without specifying a day ('when's the next earliest slot?', 'any slot "
            "soon?'). For the latter, start with today — get_available_slots skips past times automatically. "
            "Returns a list of available times for the user to choose from. "
            "After the user picks a time, still call check_availability before create_booking."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Clinic service name"},
                "date": {"type": "string", "description": "Date to scan in YYYY-MM-DD"},
            },
            "required": ["service", "date"],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "create_booking",
        "description": "Create a tentative appointment booking after availability has been checked.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Customer full name"},
                "service": {"type": "string", "description": "Clinic service name"},
                "date": {"type": "string", "description": "Appointment date in YYYY-MM-DD"},
                "time": {"type": "string", "description": "Appointment time in HH:MM 24-hour format"}
            },
            "required": ["name", "service", "date", "time"],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "reschedule_booking",
        "description": (
            "Move an existing booking to a new supported date and time. "
            "When multiple bookings exist (e.g. family bookings), always provide the 'name' "
            "parameter to target the correct patient's booking."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Clinic service name"},
                "date": {"type": "string", "description": "New appointment date in YYYY-MM-DD"},
                "time": {"type": "string", "description": "New appointment time in HH:MM 24-hour format"},
                "name": {"type": "string", "description": "Patient name whose booking should be rescheduled. Required when multiple bookings exist."}
            },
            "required": ["service", "date", "time"],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "cancel_booking",
        "description": (
            "Cancel a booking for this user. When multiple bookings exist (e.g. family bookings), "
            "always provide the 'name' parameter to target the correct patient. "
            "If you are unsure whose booking to cancel, ask the user to clarify first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Patient name whose booking should be cancelled. Required when multiple bookings exist."
                }
            },
            "required": [],
            "additionalProperties": False
        }
    }
]

# -----------------------------------------------------------------------------
# Tool Dispatcher
# -----------------------------------------------------------------------------

def safe_tool_args(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    clean = dict(args)

    if "service" in clean and clean["service"]:
        clean["service"] = normalize_service(clean["service"])

    if "date" in clean and clean["date"]:
        raw_date = str(clean["date"]).strip()
        try:
            clean["date"] = normalize_date(raw_date)
        except ValueError:
            resolved_date = resolve_relative_date(raw_date)
            if resolved_date:
                clean["date"] = resolved_date
            else:
                logger.warning("Date normalization failed for %s: %s", name, raw_date)

    if "time" in clean and clean["time"]:
        raw_time = str(clean["time"]).strip()
        try:
            clean["time"] = normalize_time(raw_time)
        except ValueError:
            resolved_time = resolve_time_text(raw_time)
            if resolved_time:
                clean["time"] = resolved_time
            else:
                logger.warning("Time normalization failed for %s: %s", name, raw_time)

    if "name" in clean and clean["name"]:
        clean["name"] = clean["name"].strip()

    logger.info("Normalized tool args for %s: %s", name, clean)
    return clean


def dispatch_tool(name: str, args: Dict[str, Any], user: str, clinic=None) -> Dict[str, Any]:
    clinic_id = clinic.get("id") if clinic else None
    state = get_booking_state(user, clinic_id=clinic_id)
    args = safe_tool_args(name, args)

    if name == "resolve_booking_datetime":
        result = resolve_booking_datetime(
            date_text=args.get("date_text", ""),
            time_text=args.get("time_text", "")
        )

        if result.get("ok"):
            # FIX #1: Reject past datetimes early — do not store them in booking state.
            # check_availability would catch this too, but rejecting here avoids a wasted
            # Calendar API call and gives the LLM a clear, actionable error message.
            if result.get("is_past"):
                return {
                    "ok": False,
                    "message": (
                        "That date and time has already passed. "
                        "Please choose an upcoming date and time."
                    ),
                }
            update_booking_state(
                user,
                clinic_id=clinic_id,
                date=result["date"],
                time=result["time"],
                availability_ok=False
            )

        return result

    if name == "check_date_available":
        result = check_date_available(
            date_text=args.get("date_text", ""),
            clinic=clinic if clinic is not None else get_default_clinic(),
        )
        # Persist resolved day so later turns (e.g., side questions before time
        # confirmation) keep the intended booking date in state.
        if result.get("ok") and not result.get("closed") and result.get("date"):
            update_booking_state(
                user,
                clinic_id=clinic_id,
                date=result["date"],
                time=None,
                availability_ok=False,
            )
        return result

    if name == "check_availability":
        result = check_availability(**args, clinic=clinic)

        if result.get("ok"):
            update_booking_state(
                user,
                clinic_id=clinic_id,
                service=args["service"],
                date=args["date"],
                time=args["time"],
                availability_ok=True
            )
        else:
            update_booking_state(user, clinic_id=clinic_id, availability_ok=False)

            # When the requested slot conflicts with an existing booking, proactively
            # fetch real available slots for the same day so the LLM can present them
            # immediately instead of asking the user to guess another time.
            # Triggered by "conflict": True (set by check_availability on overlap) —
            # not triggered for past-time or closed-day rejections.
            if result.get("conflict"):
                slots_result = get_available_slots(
                    service=args["service"], date=args["date"], clinic=clinic
                )
                if slots_result.get("ok") and slots_result.get("slots"):
                    result["available_slots"] = slots_result["slots"]

        return result

    if name == "find_next_available_slot":
        result = find_next_available_slot(**args, clinic=clinic)
        if result.get("ok"):
            update_booking_state(
                user,
                clinic_id=clinic_id,
                service=result["service"],
                date=result["date"],
                time=result["time"],
                availability_ok=True,
            )
        return result

    if name == "get_available_slots":
        # Read-only — does not set availability_ok. User must pick a slot and
        # go through check_availability before create_booking.
        return get_available_slots(**args, clinic=clinic)

    if name == "create_booking":
        if not state["availability_ok"]:
            return {"ok": False, "message": "Please check availability first."}

        # Language-agnostic name guard: enforce non-empty name before booking.
        # This catches cases where the LLM calls create_booking without collecting
        # the patient name — regardless of the conversation language.
        new_patient_name = args.get("name", "").strip()

        # Multi-patient name guard: if the LLM passes "Name1 and Name2", reject it.
        # This system books one patient at a time — multi-name bookings are not supported.
        if " and " in new_patient_name.lower():
            logger.info(
                "dispatch_tool: multi-patient name detected name=%r user=%s — single-patient policy enforced",
                new_patient_name, user,
            )
            # Try to split into two names for a helpful user-facing message.
            _parts = new_patient_name.split(" and ", 1)
            _n1 = _parts[0].strip().title() if _parts[0].strip() else ""
            _n2 = _parts[1].strip().title() if len(_parts) > 1 and _parts[1].strip() else ""
            if _n1 and _n2:
                _guide = (
                    f"Let's book them one at a time! "
                    f"Who would you like to book first — {_n1} or {_n2}?"
                )
            else:
                _guide = "Let's book them one at a time! Who should I start with?"
            return {
                "ok": False,
                "message": _guide,
            }

        if not new_patient_name:
            logger.warning(
                "dispatch_tool: create_booking called without patient name for user=%s", user
            )
            return {
                "ok": False,
                "message": "Patient name is required. Please ask for the patient's full name before creating a booking.",
            }

        # BUG FIX #11: Modified double-booking check for family bookings.
        # Allow multiple bookings per phone IF they have different patient names.
        # Block only if booking for the SAME patient name.
        # Scope to current clinic so cross-clinic bookings don't block each other.
        new_patient_name = args.get("name", "").strip()
        all_bookings = get_all_bookings(user, clinic_id=clinic.get("id") if clinic else None)
        
        # Check if this patient name already has a booking
        existing_for_patient = None
        for booking in all_bookings:
            if booking["name"].strip().lower() == new_patient_name.lower():
                existing_for_patient = booking
                break
        
        if existing_for_patient:
            return {
                "ok": False,
                "message": (
                    f"{new_patient_name} already has an existing booking: "
                    f"{existing_for_patient['service']} on {existing_for_patient['date']} "
                    f"at {existing_for_patient['time']}. "
                    "Please cancel it first with cancel_booking before creating a new one. "
                    "Inform the user of their existing booking and ask if they want to cancel it."
                ),
            }

        update_booking_state(user, clinic_id=clinic_id, name=args.get("name"))
        result = create_booking(phone=user, **args, clinic=clinic)

        if result.get("ok"):
            clinic_tz = clinic.get("timezone", TIMEZONE) if clinic else TIMEZONE
            increment_daily_metric(
                clinic_id=clinic_id,
                metric_field="bookings_created",
                at_local=now_local(clinic_tz),
            )
            reset_booking_state(user, clinic_id=clinic_id)
        elif not result.get("partial_success"):
            write_conversation_flag(clinic_id, user, "booking_failed")

        return result

    if name == "reschedule_booking":
        result = reschedule_booking(phone=user, **args, clinic=clinic)
        if result.get("ok"):
            reset_booking_state(user, clinic_id=clinic_id)
        return result

    if name == "cancel_booking":
        return cancel_booking(user, clinic=clinic, name=args.get("name") or None)

    return {"ok": False, "message": "Unknown tool."}

# -----------------------------------------------------------------------------
# AI Runner
# -----------------------------------------------------------------------------

def run_ai(user: str, message: str, clinic=None) -> str:
    if clinic is None:
        clinic = get_default_clinic()
    clinic_id = clinic.get("id") if clinic else None
    clean_message = (message or "").strip()

    if not clean_message:
        reply = "Could you please send your message again?"
        append_history(user, "assistant", reply, clinic_id=clinic_id)
        return reply

    override_system_message = None
    if _PENDING_DATE_CLARIFICATIONS.get(_pending_date_key(user, clinic_id)):
        if is_human_request(clean_message) or is_reset_command(clean_message):
            _clear_pending_date_clarification(user, clinic_id)
        else:
            clarification = _consume_pending_date_clarification(user, clinic_id, clean_message)
            if clarification["status"] == "needs_clarification":
                append_history(user, "user", clean_message, clinic_id=clinic_id)
                clarification_reply = _maybe_add_human_handoff_hint(
                    user=user,
                    clinic_id=clinic_id,
                    reply=clarification["reply"],
                    last_user_message=clean_message,
                )
                append_history(user, "assistant", clarification_reply, clinic_id=clinic_id)
                return clarification_reply
            if clarification["status"] == "resolved":
                clean_message = clarification["canonical_user_message"]
                override_system_message = clarification["override_system_message"]
                update_booking_state(user, clinic_id=clinic_id, date=clarification["date"], time=None, availability_ok=False)

    direct_reply = get_direct_reply(user, clean_message, clinic)
    if direct_reply:
        # Keep reset flows truly fresh: do not re-add reset command/confirmation
        # into persisted history after clearing runtime state.
        if is_reset_command(clean_message) and normalize_text(clean_message) != "cancel":
            return direct_reply
        append_history(user, "user", clean_message, clinic_id=clinic_id)
        append_history(user, "assistant", direct_reply, clinic_id=clinic_id)
        return direct_reply

    now = now_local()
    system_time_context = f"""
Current date and time: {now.strftime("%A, %d %B %Y, %I:%M %p")}
Timezone: {TIMEZONE}

IMPORTANT:
- Use this as the only source of truth for current time.
- Never invent today's date or time from memory.
- "Tomorrow" means the next calendar day from the date above.
- Never say a requested slot is in the past unless the backend tool confirms it.
- Use the backend tool results as truth for time validity and availability.
""".strip()

    system_prompt = build_system_prompt(user, clinic, latest_user_message=clean_message)
    append_history(user, "user", clean_message, clinic_id=clinic_id)

    input_messages = [
        {"role": "system", "content": system_time_context},
        {"role": "system", "content": system_prompt},
        *([{"role": "system", "content": override_system_message}] if override_system_message else []),
        *get_history(user, clinic_id=clinic_id),
    ]

    try:
        response = client.responses.create(
            model=MODEL,
            input=input_messages,
            tools=TOOLS
        )
    except Exception:
        logger.exception("Initial OpenAI response failed")
        reply = trigger_fallback(user, clinic, "openai_exception")
        reply = _maybe_add_human_handoff_hint(
            user=user,
            clinic_id=clinic_id,
            reply=reply,
            last_user_message=clean_message,
            forced_friction=True,
        )
        append_history(user, "assistant", reply, clinic_id=clinic_id)
        return reply

    run_tool_results = []
    _booking_confirmed_this_turn = False
    for _ in range(MAX_TOOL_LOOPS):
        calls = [
            item for item in response.output
            if getattr(item, "type", "") == "function_call"
        ]

        if not calls:
            reply = (response.output_text or "").strip()

            if not reply:
                reply = (
                    "Sorry, I can only help with dental services, appointment bookings, "
                    "clinic hours, clinic location, and treatment questions."
                )

            # FIX #5: Server-side confirmation gate.
            # If the LLM produced a booking confirmation message but create_booking did
            # not return ok=True in this turn, intercept and redirect rather than
            # returning a false confirmation to the patient.
            if not _booking_confirmed_this_turn and _reply_looks_like_booking_confirmation(reply):
                logger.warning(
                    "run_ai: premature booking confirmation intercepted for user=%s — redirecting",
                    user,
                )
                reply = (
                    "I still need a couple more details to finalise your booking. "
                    "Could you please confirm your full name?"
                )

            reply = _maybe_add_human_handoff_hint(
                user=user,
                clinic_id=clinic_id,
                reply=reply,
                last_user_message=clean_message,
                tool_results=run_tool_results,
            )

            # Duplicate consecutive message guard: if the LLM returned the exact same
            # reply as the last assistant message already in history, do not append it
            # again — this breaks repetition loops where the LLM re-asks the same
            # question on every turn (e.g. repeatedly asking for a name in Mandarin flows).
            history = get_history(user, clinic_id=clinic_id)
            last_assistant = next(
                (m["content"] for m in reversed(history) if m["role"] == "assistant"),
                None,
            )
            if reply != last_assistant:
                append_history(user, "assistant", reply, clinic_id=clinic_id)
            else:
                logger.warning(
                    "run_ai: duplicate consecutive assistant message suppressed for user=%s", user
                )

            if _assistant_is_midnight_date_clarification(reply):
                _set_pending_date_clarification(user, clinic_id)

            return reply

        outputs = []

        for call in calls:
            if call.name not in SUPPORTED_TOOL_NAMES:
                logger.warning("Unknown tool called by model: %s", call.name)
                run_tool_results.append({"ok": False, "message": f"Unknown tool: {call.name}"})
                outputs.append({
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps({
                        "ok": False,
                        "message": f"Unknown tool: {call.name}"
                    })
                })
                continue

            try:
                args = json.loads(call.arguments or "{}")
            except json.JSONDecodeError:
                logger.exception("Invalid JSON arguments from model")
                run_tool_results.append({"ok": False, "message": "Invalid tool arguments."})
                outputs.append({
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps({
                        "ok": False,
                        "message": "Invalid tool arguments."
                    })
                })
                continue

            result = dispatch_tool(call.name, args, user, clinic)
            run_tool_results.append(result)
            if call.name == "create_booking" and result.get("ok"):
                _booking_confirmed_this_turn = True

            outputs.append({
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": json.dumps(result)
            })

        if not outputs:
            break

        try:
            response = client.responses.create(
                model=MODEL,
                previous_response_id=response.id,
                input=outputs,
                tools=TOOLS
            )
        except Exception:
            logger.exception("Follow-up OpenAI response failed")
            reply = trigger_fallback(user, clinic, "openai_exception")
            reply = _maybe_add_human_handoff_hint(
                user=user,
                clinic_id=clinic_id,
                reply=reply,
                last_user_message=clean_message,
                tool_results=run_tool_results,
                forced_friction=True,
            )
            append_history(user, "assistant", reply, clinic_id=clinic_id)
            return reply

    reply = trigger_fallback(user, clinic, "max_iterations")
    reply = _maybe_add_human_handoff_hint(
        user=user,
        clinic_id=clinic_id,
        reply=reply,
        last_user_message=clean_message,
        tool_results=run_tool_results,
        forced_friction=True,
    )
    write_conversation_flag(clinic_id, user, "ai_uncertain")
    append_history(user, "assistant", reply, clinic_id=clinic_id)
    return reply

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@app.route("/chat", methods=["GET", "POST"])
def chat():
    user = "test_user"

    if request.method == "POST":
        msg = request.form.get("msg", "").strip()
        if msg:
            logger.info(
                "ESCALATION_CHECK | raw=%r | normalized=%r | is_escalation=%s",
                msg,
                msg,
                is_human_escalation_request(msg),
            )
            if is_human_escalation_request(msg):
                clinic = get_default_clinic()
                clinic_id = clinic.get("id") if clinic else None
                already_flagged = has_unresolved_human_flag(clinic_id, user)
                write_conversation_flag(clinic_id, user, "human_requested")
                escalation_reply = _human_escalation_reply(clinic)
                if already_flagged:
                    logger.info(
                        "chat: repeat human escalation — flag already exists, suppressed user=%s clinic_id=%s",
                        user, clinic_id,
                    )
                else:
                    logger.info(
                        "chat: human escalation detected — flag written user=%s clinic_id=%s",
                        user, clinic_id,
                    )
                append_history(user, "user", msg, clinic_id=clinic_id)
                append_history(user, "assistant", escalation_reply, clinic_id=clinic_id)
                return redirect("/chat")
            run_ai(user, msg)
        return redirect("/chat")

    convo = get_history(user)

    html_parts = []
    for m in convo:
        role = html.escape(m["role"])
        content = html.escape(m["content"])
        html_parts.append(f"<div><b>{role}:</b> {content}</div>")

    convo_html = "".join(html_parts)

    return f"""
    <h2>Bot Test</h2>
    {convo_html}
    <form method="post" style="margin-bottom: 12px;">
        <input name="msg" autofocus autocomplete="off">
        <button type="submit">Send</button>
    </form>
    <form method="post" action="/chat/reset">
        <button type="submit">Reset Chat</button>
    </form>
    """


@app.route("/chat/reset", methods=["POST"])
def chat_reset():
    user = "test_user"
    reset_user_session(user)
    return redirect("/chat")


@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    # --- Twilio signature validation ---
    if TWILIO_AUTH_TOKEN:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        signature = request.headers.get("X-Twilio-Signature", "")
        if not validator.validate(request.url, request.form, signature):
            logger.warning("Invalid Twilio signature — rejected request from %s", request.remote_addr)
            abort(403)

    raw_body = request.form.get("Body")
    msg = (raw_body or "").strip()
    user = request.form.get("From") or "unknown_user"
    to_number = request.form.get("To")

    # --- Sentry user context ---
    if _SENTRY_AVAILABLE and SENTRY_DSN:
        sentry_sdk.set_user({"id": user})
        sentry_sdk.set_tag("clinic_to", to_number or "unknown")

    clinic = get_clinic_by_twilio_number(to_number) if to_number else None

    if not clinic:
        if to_number:
            # A number was present but matched no active clinic — refuse to route.
            logger.warning(
                "whatsapp: unmatched To number '%s' from user=%s — returning safe error response",
                to_number, user,
            )
            twilio = MessagingResponse()
            twilio.message(
                "Sorry, this number is not configured. Please contact the clinic directly."
            )
            return Response(str(twilio), mimetype="application/xml")
        # to_number absent (e.g. Twilio sandbox edge case) — fall back to default clinic.
        logger.warning("whatsapp: To field absent — falling back to default clinic for user=%s", user)
        clinic = get_default_clinic()

    clinic_id = clinic.get("id") if clinic else None
    clinic_tz = clinic.get("timezone", TIMEZONE) if clinic else TIMEZONE
    received_at_local = now_local(clinic_tz)
    increment_daily_metric(clinic_id, "messages_handled", at_local=received_at_local)
    if is_after_hours_message(clinic, at_local=received_at_local):
        increment_daily_metric(clinic_id, "after_hours_messages", at_local=received_at_local)
    if is_first_user_message_for_local_day(
        clinic_id=clinic_id,
        phone=user,
        clinic_tz=clinic_tz,
        at_local=received_at_local,
    ):
        increment_daily_metric(clinic_id, "conversations_handled", at_local=received_at_local)

    # --- Human escalation short-circuit (webhook level, before LLM) ---
    # Must run before run_ai() so the AI never responds to escalation requests.
    logger.info(
        "ESCALATION_CHECK | raw=%r | normalized=%r | is_escalation=%s",
        raw_body,
        msg,
        is_human_escalation_request(msg),
    )
    if is_human_escalation_request(msg):
        logger.info(
            "ESCALATION_TRIGGERED | user=%s | clinic_id=%s | msg=%r",
            user, clinic_id, msg,
        )
        already_flagged = has_unresolved_human_flag(clinic_id, user)
        increment_daily_metric(clinic_id, "human_escalations", at_local=received_at_local)
        append_history(user, "user", msg, clinic_id=clinic_id)

        write_conversation_flag(clinic_id, user, "human_requested")

        if not already_flagged:
            # First escalation request: notify the patient once.
            send_whatsapp_outbound(
                to_number=user,
                body=_human_escalation_reply(clinic),
                from_number=clinic.get("twilio_number") if clinic else None,
            )
            logger.info(
                "whatsapp: human escalation detected — flag written, patient notified user=%s clinic_id=%s",
                user, clinic_id,
            )
        else:
            # Repeat escalation: flag already exists, skip outbound to avoid spamming patient.
            logger.info(
                "whatsapp: repeat human escalation — flag already exists, outbound suppressed user=%s clinic_id=%s",
                user, clinic_id,
            )

        # Always return empty TwiML — do NOT call run_ai().
        return Response(str(MessagingResponse()), mimetype="application/xml")

    logger.info("ESCALATION_SKIP | user=%s | is_escalation=False | proceeding_to_run_ai", user)
    twilio = MessagingResponse()

    try:
        reply = run_ai(user, msg, clinic)
        twilio.message(reply)
    except Exception:
        logger.exception("WhatsApp handler failed for user=%s", user)
        twilio.message(trigger_fallback(user, clinic, "unexpected_exception"))

    return Response(str(twilio), mimetype="application/xml")


@app.route("/tasks/process-reminders", methods=["POST", "GET"])
def process_reminders_route():
    token = request.headers.get("X-Reminder-Secret") or request.args.get("secret")
    if token != REMINDER_SECRET:
        abort(403)

    result = process_reminders()
    return jsonify(result)


@app.route("/tasks/daily-summary", methods=["POST", "GET"])
def daily_summary_route():
    token = request.headers.get("X-Reminder-Secret") or request.args.get("secret")
    if token != REMINDER_SECRET:
        abort(403)

    today_utc = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    with SessionLocal() as db:
        bookings_today = db.query(BookingRecordModel).filter(
            BookingRecordModel.created_at >= today_utc
        ).count()

        reminders_1d_today = db.query(BookingRecordModel).filter(
            BookingRecordModel.reminder_1d_sent == True,
            BookingRecordModel.updated_at >= today_utc,
        ).count()

        reminders_2h_today = db.query(BookingRecordModel).filter(
            BookingRecordModel.reminder_2h_sent == True,
            BookingRecordModel.updated_at >= today_utc,
        ).count()

    summary = {
        "bookings_created": bookings_today,
        "reminders_sent_1d": reminders_1d_today,
        "reminders_sent_2h": reminders_2h_today,
        "fallbacks_since_restart": _stats["fallbacks"],
    }

    message = (
        f"AI Receptionist Daily Summary\n\n"
        f"Bookings Created: {summary['bookings_created']}\n"
        f"Reminders Sent (1-day): {summary['reminders_sent_1d']}\n"
        f"Reminders Sent (2-hour): {summary['reminders_sent_2h']}\n"
        f"Fallbacks (since restart): {summary['fallbacks_since_restart']}"
    )
    send_telegram(message)
    logger.info("Daily summary sent: %s", summary)
    return jsonify(summary)


@app.route("/tasks/calendar-health-check", methods=["GET", "POST"])
def calendar_health_check_route():
    token = request.args.get("token") or request.headers.get("X-Reminder-Secret")
    if token != REMINDER_SECRET:
        abort(403)
    result = run_calendar_health_checks()
    return jsonify(result)


@app.route("/feedback", methods=["POST"])
def feedback():
    clinic_id = session.get("staff_clinic_id")
    staff_id = session.get("staff_id")
    staff_name = session.get("staff_name")

    if not clinic_id or not staff_id:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or request.form
    category_raw = (payload.get("category") or "").strip().lower()
    feedback_message = (payload.get("message") or "").strip()
    allowed_categories = {"bug", "feature", "general"}

    if category_raw not in allowed_categories:
        return jsonify({"ok": False, "error": "Invalid category"}), 400
    if not feedback_message:
        return jsonify({"ok": False, "error": "Message is required"}), 400
    if len(feedback_message) > 2000:
        return jsonify({"ok": False, "error": "Message too long"}), 400

    category = category_raw.capitalize()
    with SessionLocal() as db:
        clinic = db.query(Clinic).filter(Clinic.id == clinic_id).first()
        clinic_name = clinic.name if clinic else f"Clinic #{clinic_id}"

        entry = FeedbackEntry(
            clinic_id=clinic_id,
            staff_id=staff_id,
            staff_name=staff_name,
            category=category,
            message=feedback_message,
        )
        db.add(entry)
        db.commit()

    telegram_message = (
        "Dashboard Feedback\n\n"
        f"Clinic: {clinic_name}\n"
        f"Staff: {staff_name or 'Unknown'} (ID: {staff_id})\n"
        f"Type: {category}\n"
        f"Message: {feedback_message}"
    )
    send_telegram(telegram_message)

    return jsonify({"ok": True})


@app.route("/")
def home():
    return "AI receptionist running"


# -----------------------------------------------------------------------------
# Staff dashboard Blueprint
# -----------------------------------------------------------------------------

from dashboard import dashboard_bp  # noqa: E402
app.register_blueprint(dashboard_bp)
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY", "dev-secret-change-in-prod")


if __name__ == "__main__":
    ensure_demo_clinic_seeded()
    app.run(host="0.0.0.0", port=3000, debug=True)
