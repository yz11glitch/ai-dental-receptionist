"""
booking.py — Booking lifecycle functions for the AI WhatsApp Dental Receptionist.

Covers: availability checking, slot scanning, booking creation/reschedule/cancel,
booking state management, DB record helpers, and the reminder processing loop.

Imports shared infrastructure (models, DB session, logger, calendar) from app.py
via deferred imports inside each function. This pattern avoids circular import
errors while keeping all booking logic in one focused module.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from utils import (
    normalize_service,
    normalize_date,
    normalize_time,
    parse_slot,
    format_slot,
    format_time_only,
    is_within_business_hours,
    should_send_1d_reminder,
    should_send_2h_reminder,
    TIMEZONE,
)

# now_local is intentionally NOT imported from utils here.
# It lives in app.py so that @patch("app.now_local") works in tests.
# Every function in this module that needs now_local does a deferred import.


def _now() -> "datetime":
    """Internal shim — always fetches now_local from app so @patch('app.now_local') works."""
    from app import now_local
    return now_local()


MAX_DAYS_AHEAD = 90


def _check_booking_horizon(date_str: str) -> Optional[Dict[str, Any]]:
    """Return an error dict if date_str is more than MAX_DAYS_AHEAD days from today, else None."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return None  # let the caller handle the bad format
    days_ahead = (dt.date() - _now().date()).days
    if days_ahead > MAX_DAYS_AHEAD:
        return {
            "ok": False,
            "message": (
                f"We can only accept bookings up to {MAX_DAYS_AHEAD} days in advance. "
                "Please choose a date within the next 3 months."
            ),
        }
    return None


def _get_lunch_break_window(clinic: dict, ref_dt: datetime) -> Optional[Tuple[datetime, datetime]]:
    lunch_start = clinic.get("lunch_start")
    lunch_end = clinic.get("lunch_end")
    if not lunch_start or not lunch_end:
        return None
    try:
        lunch_start_h, lunch_start_m = [int(x) for x in lunch_start.split(":", 1)]
        lunch_end_h, lunch_end_m = [int(x) for x in lunch_end.split(":", 1)]
        lunch_start_dt = ref_dt.replace(
            hour=lunch_start_h, minute=lunch_start_m, second=0, microsecond=0
        )
        lunch_end_dt = ref_dt.replace(
            hour=lunch_end_h, minute=lunch_end_m, second=0, microsecond=0
        )
        if lunch_start_dt >= lunch_end_dt:
            return None
        return lunch_start_dt, lunch_end_dt
    except Exception:
        return None


logger = logging.getLogger("ai_receptionist")


# ---------------------------------------------------------------------------
# Internal helpers — deferred imports from app
# ---------------------------------------------------------------------------

def _get_db():
    from app import SessionLocal
    return SessionLocal


def _get_models():
    from app import BookingRecordModel, BookingStateModel
    return BookingRecordModel, BookingStateModel


def _get_calendar():
    from app import get_calendar
    return get_calendar()


# ---------------------------------------------------------------------------
# Booking state helpers
# ---------------------------------------------------------------------------

def _booking_state_key(user: str, clinic_id=None) -> str:
    """Compound PK for BookingStateModel: scopes ephemeral booking wizard state per clinic.

    With clinic_id=None, returns the bare phone number for backward compatibility
    with existing rows and tests that do not set up a clinic.
    With a real clinic_id, returns "{phone}:{clinic_id}" so that patients who
    message multiple clinics never share booking state across them.
    """
    if clinic_id is None:
        return user
    return f"{user}:{clinic_id}"


def reset_booking_state(user: str, clinic_id=None) -> None:
    key = _booking_state_key(user, clinic_id)
    from app import SessionLocal, BookingStateModel
    with SessionLocal() as db:
        row = db.get(BookingStateModel, key)
        if not row:
            row = BookingStateModel(user=key)
            db.add(row)
        row.service = None
        row.date = None
        row.time = None
        row.name = None
        row.availability_ok = False
        row.updated_at = datetime.utcnow()
        db.commit()


def get_booking_state(user: str, clinic_id=None) -> Dict[str, Any]:
    key = _booking_state_key(user, clinic_id)
    from app import SessionLocal, BookingStateModel
    with SessionLocal() as db:
        row = db.get(BookingStateModel, key)
        if not row:
            row = BookingStateModel(
                user=key,
                service=None,
                date=None,
                time=None,
                name=None,
                availability_ok=False,
                updated_at=datetime.utcnow(),
            )
            db.add(row)
            db.commit()

        return {
            "service": row.service,
            "date": row.date,
            "time": row.time,
            "name": row.name,
            "availability_ok": bool(row.availability_ok),
        }


def update_booking_state(user: str, clinic_id=None, **kwargs) -> None:
    key = _booking_state_key(user, clinic_id)
    from app import SessionLocal, BookingStateModel
    with SessionLocal() as db:
        row = db.get(BookingStateModel, key)
        if not row:
            row = BookingStateModel(user=key)
            db.add(row)

        # BUG FIX #6: If service changes after availability check, invalidate availability.
        # This prevents booking conflicts when switching from short service (30min) to long
        # service (90min) without re-checking the slot duration.
        if "service" in kwargs and row.service and row.availability_ok:
            if kwargs["service"] != row.service:
                logger.info(
                    "Service changed from '%s' to '%s' after availability_ok=True. "
                    "Resetting availability_ok to force re-check.",
                    row.service, kwargs["service"]
                )
                row.availability_ok = False

        for key, value in kwargs.items():
            setattr(row, key, value)
        row.updated_at = datetime.utcnow()
        db.commit()


# ---------------------------------------------------------------------------
# Booking record helpers
# ---------------------------------------------------------------------------

def get_existing_booking(user: str, clinic_id: int = None) -> Optional[Dict[str, Any]]:
    """
    BUG FIX #11: Returns the most recent booking for this user.
    Now that multiple bookings per phone are allowed, this fetches the latest one.

    When clinic_id is provided, only bookings belonging to that clinic are returned.
    This prevents cross-clinic data leakage when a patient has bookings at multiple clinics.
    Pass clinic_id=None only for admin/testing paths that intentionally see all clinics.
    """
    from app import SessionLocal, BookingRecordModel
    with SessionLocal() as db:
        q = db.query(BookingRecordModel).filter(BookingRecordModel.user == user)
        if clinic_id is not None:
            q = q.filter(BookingRecordModel.clinic_id == clinic_id)
        row = q.order_by(BookingRecordModel.created_at.desc()).first()
        if not row:
            return None
        return {
            "clinic_id": row.clinic_id,
            "event_id": row.event_id,
            "service": row.service,
            "name": row.name,
            "date": row.date,
            "time": row.time,
            "status": row.status,
            "reminder_1d_sent": bool(row.reminder_1d_sent),
            "reminder_2h_sent": bool(row.reminder_2h_sent),
        }


def get_all_bookings(user: str, clinic_id: int = None) -> list:
    """
    BUG FIX #11: Returns ALL bookings for this user (for family bookings).

    When clinic_id is provided, only bookings belonging to that clinic are returned.
    This prevents cross-clinic data leakage when a patient has bookings at multiple clinics.
    Pass clinic_id=None only for admin/testing paths that intentionally see all clinics.
    """
    from app import SessionLocal, BookingRecordModel
    with SessionLocal() as db:
        q = db.query(BookingRecordModel).filter(BookingRecordModel.user == user)
        if clinic_id is not None:
            q = q.filter(BookingRecordModel.clinic_id == clinic_id)
        rows = q.order_by(BookingRecordModel.created_at.desc()).all()
        return [
            {
                "clinic_id": row.clinic_id,
                "event_id": row.event_id,
                "service": row.service,
                "name": row.name,
                "date": row.date,
                "time": row.time,
                "status": row.status,
                "reminder_1d_sent": bool(row.reminder_1d_sent),
                "reminder_2h_sent": bool(row.reminder_2h_sent),
            }
            for row in rows
        ]


def save_booking_record(
    user: str,
    event_id: str,
    service: str,
    name: str,
    date: str,
    time: str,
    status: str = "Pending",
    reminder_1d_sent: bool = False,
    reminder_2h_sent: bool = False,
    clinic_id: int = None,
) -> None:
    """
    BUG FIX #11: Now uses event_id as primary key instead of user.
    This allows multiple bookings per phone number (family bookings).
    """
    from app import SessionLocal, BookingRecordModel
    with SessionLocal() as db:
        row = db.get(BookingRecordModel, event_id)
        if not row:
            row = BookingRecordModel(
                event_id=event_id,
                user=user,
            )
            db.add(row)

        row.clinic_id = clinic_id
        row.service = service
        row.name = name
        row.date = date
        row.time = time
        row.status = status
        row.reminder_1d_sent = reminder_1d_sent
        row.reminder_2h_sent = reminder_2h_sent
        row.updated_at = datetime.utcnow()
        db.commit()


def delete_booking_record(event_id: str) -> None:
    """
    BUG FIX #11: Now deletes by event_id instead of user.
    """
    from app import SessionLocal, BookingRecordModel
    with SessionLocal() as db:
        row = db.get(BookingRecordModel, event_id)
        if row:
            db.delete(row)
            db.commit()


# ---------------------------------------------------------------------------
# Calendar / availability helpers
# ---------------------------------------------------------------------------

def parse_event_status_from_summary(summary: str) -> str:
    import re
    m = re.match(r"^\[(.*?)\]\s*", summary or "")
    if not m:
        return "Pending"
    status = m.group(1).strip().title()
    return status or "Pending"


def status_prefix(status: str) -> str:
    return f"[{status.title()}]"


def get_parallel_booking_capacity(clinic: Dict[str, Any]) -> int:
    """Return the number of simultaneous appointments the clinic can hold.

    Defaults to 1 (single chair). Reads the optional 'parallel_booking_capacity'
    field from the clinic dict. Clamps to at least 1.
    """
    raw = clinic.get("parallel_booking_capacity", 1)
    try:
        capacity = int(raw)
    except (TypeError, ValueError):
        capacity = 1
    return max(capacity, 1)


def get_special_closure_message(clinic: Dict[str, Any], date: str) -> Optional[str]:
    if date in clinic.get("special_closures", []):
        return clinic.get("closure_notes", {}).get(date, "We are closed on that date.")
    return None


def find_next_open_days(from_date: str, clinic: Dict[str, Any], count: int = 2) -> List[str]:
    """Return the next `count` calendar dates (YYYY-MM-DD) when the clinic is open.

    Iterates forward from from_date + 1 day, skipping Sundays and ClinicClosure dates.
    Returns only dates, not times — used to suggest alternatives after a closed-day rejection.
    """
    special_closures = set(clinic.get("special_closures", []))
    try:
        base = datetime.strptime(from_date, "%Y-%m-%d").date()
    except Exception:
        base = _now().date()

    open_days: List[str] = []
    candidate = base + timedelta(days=1)
    max_scan = 30  # safety cap
    while len(open_days) < count and max_scan > 0:
        max_scan -= 1
        if candidate.weekday() == 6:  # Sunday
            candidate += timedelta(days=1)
            continue
        date_str = candidate.strftime("%Y-%m-%d")
        if date_str in special_closures:
            candidate += timedelta(days=1)
            continue
        open_days.append(date_str)
        candidate += timedelta(days=1)

    return open_days


def check_date_available(date_text: str, clinic: Dict[str, Any]) -> Dict[str, Any]:
    """Lightweight closed-day check that does NOT require a time.

    Resolves a date phrase, checks whether the clinic is open on that day
    (weekday rule + special closures), and returns the next 2 open days if closed.
    Does NOT call Google Calendar — purely local logic, safe to call eagerly.
    """
    from utils import resolve_relative_date
    date_str = resolve_relative_date(date_text)
    if not date_str:
        return {"ok": False, "closed": False, "message": "Could not understand the date."}

    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return {"ok": False, "closed": False, "message": "Invalid date format."}

    # Reject dates that have already passed.
    if dt.date() < _now().date():
        return {
            "ok": False,
            "closed": False,
            "message": (
                f"That date ({dt.strftime('%-d %B')}) has already passed. "
                "Please choose an upcoming date."
            ),
        }

    # Reject dates too far in the future.
    horizon_err = _check_booking_horizon(date_str)
    if horizon_err:
        return {**horizon_err, "closed": False}

    weekday_name = dt.strftime("%A")

    # Check Sunday
    if dt.weekday() == 6:
        next_open = find_next_open_days(date_str, clinic, count=2)
        next_open_fmt = " or ".join(
            datetime.strptime(d, "%Y-%m-%d").strftime("%A, %-d %B") for d in next_open
        )
        msg = f"The clinic is closed on Sundays ({weekday_name}, {dt.strftime('%-d %B')})."
        if next_open_fmt:
            msg += f" The next available days are {next_open_fmt}."
        return {
            "ok": False,
            "closed": True,
            "reason": "sunday",
            "date": date_str,
            "weekday": weekday_name,
            "message": msg,
            "next_open_days": next_open,
        }

    # Check special closure
    closure_message = get_special_closure_message(clinic, date_str)
    if closure_message:
        next_open = find_next_open_days(date_str, clinic, count=2)
        next_open_fmt = " or ".join(
            datetime.strptime(d, "%Y-%m-%d").strftime("%A, %-d %B") for d in next_open
        )
        msg = closure_message
        if next_open_fmt:
            msg += f" The next available days are {next_open_fmt}."
        return {
            "ok": False,
            "closed": True,
            "reason": "special_closure",
            "date": date_str,
            "weekday": weekday_name,
            "message": msg,
            "next_open_days": next_open,
        }

    # Check if user is asking about today but the clinic has already closed.
    # e.g. user asks "can I book today" at 11 PM when clinic closes at 6 PM.
    # Use the clinic's own timezone (not the server default) for this comparison.
    from app import now_local as _now_local
    clinic_tz = clinic.get("timezone", TIMEZONE)
    now = _now_local(clinic_tz)
    if date_str == now.strftime("%Y-%m-%d"):
        open_hour = clinic.get("open_hour", 10)
        close_hour = clinic.get("close_hour", 18)
        # Safety guard: if we're currently within operating hours, never return "already closed".
        currently_open = open_hour <= now.hour < close_hour
        if not currently_open and now.hour >= close_hour:
            next_open = find_next_open_days(date_str, clinic, count=2)
            next_open_fmt = " or ".join(
                datetime.strptime(d, "%Y-%m-%d").strftime("%A, %-d %B") for d in next_open
            )
            msg = "The clinic is already closed for today."
            if next_open_fmt:
                msg += f" The next available days are {next_open_fmt}."
            return {
                "ok": False,
                "closed": True,
                "reason": "already_closed_today",
                "date": date_str,
                "weekday": weekday_name,
                "message": msg,
                "next_open_days": next_open,
            }

    return {
        "ok": True,
        "closed": False,
        "date": date_str,
        "weekday": weekday_name,
        "message": f"The clinic is open on {weekday_name}, {dt.strftime('%-d %B')}.",
    }


def check_availability(service: str, date: str, time: str, clinic=None) -> Dict[str, Any]:
    from app import get_default_clinic, get_calendar
    try:
        if clinic is None:
            clinic = get_default_clinic()
        service = normalize_service(service)

        if service not in clinic["services"]:
            return {"ok": False, "message": "Unsupported service."}

        closure_message = get_special_closure_message(clinic, date)
        if closure_message:
            return {"ok": False, "message": closure_message}

        start_dt = parse_slot(date, time)

        if start_dt < _now():
            return {"ok": False, "message": "That time is already in the past."}

        horizon_err = _check_booking_horizon(date)
        if horizon_err:
            return horizon_err

        duration = clinic["services"][service]
        end_dt = start_dt + timedelta(minutes=duration)

        if not is_within_business_hours(clinic, start_dt, end_dt):
            lunch_window = _get_lunch_break_window(clinic, start_dt)
            if lunch_window:
                lunch_start_dt, lunch_end_dt = lunch_window
                if start_dt < lunch_end_dt and end_dt > lunch_start_dt:
                    return {
                        "ok": False,
                        "message": (
                            f"We have a lunch break from {format_time_only(lunch_start_dt)} to "
                            f"{format_time_only(lunch_end_dt)}. Please choose a different time."
                        ),
                    }
            if start_dt.weekday() == 6:
                return {"ok": False, "message": "We are closed on Sundays."}
            open_dt = start_dt.replace(
                hour=clinic["open_hour"], minute=0, second=0, microsecond=0
            )
            close_dt = start_dt.replace(
                hour=clinic["close_hour"], minute=0, second=0, microsecond=0
            )
            if start_dt < open_dt:
                return {
                    "ok": False,
                    "message": (
                        f"Our clinic opens at {format_time_only(open_dt)}. "
                        f"Please choose a time from {format_time_only(open_dt)} onwards."
                    ),
                }
            # start_dt is within hours but end_dt runs past closing time
            latest_start = close_dt - timedelta(minutes=duration)
            return {
                "ok": False,
                "message": (
                    f"A {duration}-minute {service} appointment starting at "
                    f"{format_time_only(start_dt)} would end at {format_time_only(end_dt)}, "
                    f"after our {format_time_only(close_dt)} closing time. "
                    f"The latest we can start a {service} is {format_time_only(latest_start)}."
                ),
            }

        cal = get_calendar()

        # Fetch the full day window so we can apply a local overlap check.
        # Previously this queried [start_dt, end_dt] and relied on Google's
        # boundary semantics for timeMin/timeMax.  That caused edge-to-edge
        # appointments (prev end == new start) to be incorrectly blocked when
        # Google treated timeMin as inclusive (>=) rather than strictly greater-
        # than (>).  Fetching the whole day and running the explicit half-open
        # interval check `candidate < ev_end and end_candidate > ev_start`
        # removes that ambiguity and mirrors the logic in find_next_available_slot
        # and get_available_slots.
        day_open = start_dt.replace(
            hour=clinic["open_hour"], minute=0, second=0, microsecond=0
        )
        day_close = start_dt.replace(
            hour=clinic["close_hour"], minute=0, second=0, microsecond=0
        )

        all_events = cal.events().list(
            calendarId=clinic["google_calendar_id"],
            timeMin=day_open.isoformat(),
            timeMax=day_close.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute().get("items", [])

        # Build booked spans from timed events only.
        # All-day events ("date" key, no "dateTime") are not appointment bookings.
        booked_spans = []
        skipped_allday = 0
        for ev in all_events:
            ev_start_str = ev.get("start", {}).get("dateTime")
            ev_end_str = ev.get("end", {}).get("dateTime")
            if ev_start_str and ev_end_str:
                try:
                    booked_spans.append((
                        datetime.fromisoformat(ev_start_str),
                        datetime.fromisoformat(ev_end_str),
                    ))
                except Exception:
                    pass
            else:
                skipped_allday += 1

        # Half-open interval overlap: [start_dt, end_dt) overlaps [ev_start, ev_end)
        # when start_dt < ev_end AND end_dt > ev_start.
        # Edge-to-edge (start_dt == ev_end or end_dt == ev_start) is NOT an overlap.
        parallel_capacity = get_parallel_booking_capacity(clinic)
        overlapping = sum(
            1 for ev_start, ev_end in booked_spans
            if start_dt < ev_end and end_dt > ev_start
        )
        is_full = overlapping >= parallel_capacity

        if is_full:
            logger.info(
                "check_availability conflict: service=%s date=%s time=%s duration=%dmin "
                "window=[%s, %s] overlapping=%d booked_spans=%d allday_skipped=%d "
                "parallel_capacity=%d clinic=%s",
                service, date, time, duration,
                start_dt.isoformat(), end_dt.isoformat(),
                overlapping, len(booked_spans), skipped_allday,
                parallel_capacity,
                clinic.get("name", "unknown"),
            )
            return {
                "ok": False,
                "conflict": True,
                "message": (
                    f"Sorry, {format_time_only(start_dt)} is already taken — "
                    f"it overlaps with an existing appointment. "
                    f"Please choose a different time."
                ),
            }

        logger.info(
            "check_availability ok: service=%s date=%s time=%s duration=%dmin clinic=%s",
            service, date, time, duration, clinic.get("name", "unknown"),
        )
        return {
            "ok": True,
            "service": service,
            "date": date,
            "time": time,
            "formatted_slot": format_slot(start_dt)
        }

    except ValueError:
        return {"ok": False, "message": "Invalid date or time format."}
    except Exception:
        logger.exception("check_availability failed: service=%s date=%s time=%s", service, date, time)
        return {"ok": False, "message": "Unable to check availability right now."}


def find_next_available_slot(service: str, date: str, time: str, clinic=None) -> Dict[str, Any]:
    """Scan forward from (date, time) for the first open slot.

    Checks remaining slots on the requested day before advancing to the next.
    One Google Calendar API call per day. Skips Sundays and special closures.
    All-day calendar events (start.date, not start.dateTime) are ignored —
    they are not appointment bookings.
    """
    from app import get_default_clinic, get_calendar
    try:
        if clinic is None:
            clinic = get_default_clinic()
        service = normalize_service(service)
        if service not in clinic["services"]:
            return {"ok": False, "message": "Unsupported service."}

        duration = clinic["services"][service]
        slot_minutes = clinic.get("slot_minutes", 30)
        tz = ZoneInfo(clinic.get("timezone", TIMEZONE))
        cal = get_calendar()
        cal_id = clinic["google_calendar_id"]
        special_closures = set(clinic.get("special_closures", []))
        clinic_name = clinic.get("name", "unknown")
        parallel_capacity = get_parallel_booking_capacity(clinic)

        horizon_err = _check_booking_horizon(date)
        if horizon_err:
            return horizon_err

        scan_start = parse_slot(date, time)

        # If starting point is already past, jump to now rounded up to next slot boundary.
        now_dt = _now()
        if scan_start < now_dt:
            scan_start = now_dt.replace(second=0, microsecond=0)
            excess = scan_start.minute % slot_minutes
            if excess:
                scan_start += timedelta(minutes=slot_minutes - excess)

        logger.info(
            "find_next_available_slot start: service=%s date=%s time=%s duration=%dmin "
            "scan_start=%s clinic=%s",
            service, date, time, duration, scan_start.isoformat(), clinic_name,
        )

        for day_offset in range(7):
            if day_offset == 0:
                day_scan_start = scan_start
            else:
                candidate_day = (scan_start + timedelta(days=day_offset)).date()
                day_scan_start = datetime(
                    candidate_day.year, candidate_day.month, candidate_day.day,
                    clinic["open_hour"], 0, tzinfo=tz,
                )

            if day_scan_start.weekday() == 6:  # Sunday
                logger.info("find_next_available_slot skip: %s is Sunday", day_scan_start.strftime("%Y-%m-%d"))
                continue

            day_str = day_scan_start.strftime("%Y-%m-%d")
            if _check_booking_horizon(day_str):
                break  # all subsequent days are also beyond the horizon

            if day_str in special_closures:
                logger.info("find_next_available_slot skip: %s is a special closure", day_str)
                continue

            day_end = day_scan_start.replace(
                hour=clinic["close_hour"], minute=0, second=0, microsecond=0
            )

            # Fetch all events for this day window — one Calendar API call per day.
            day_events = cal.events().list(
                calendarId=cal_id,
                timeMin=day_scan_start.isoformat(),
                timeMax=day_end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute().get("items", [])

            booked_spans = []
            skipped_allday = 0
            for ev in day_events:
                ev_start_str = ev.get("start", {}).get("dateTime")
                ev_end_str = ev.get("end", {}).get("dateTime")
                if ev_start_str and ev_end_str:
                    try:
                        booked_spans.append((
                            datetime.fromisoformat(ev_start_str),
                            datetime.fromisoformat(ev_end_str),
                        ))
                    except Exception:
                        pass
                else:
                    skipped_allday += 1

            logger.info(
                "find_next_available_slot day=%s timed_events=%d allday_skipped=%d scan_from=%s",
                day_str, len(booked_spans), skipped_allday, day_scan_start.strftime("%H:%M"),
            )

            candidate = day_scan_start
            while True:
                end_candidate = candidate + timedelta(minutes=duration)
                if not is_within_business_hours(clinic, candidate, end_candidate):
                    if end_candidate > day_end:
                        break  # Past closing time — move to next day.
                    candidate += timedelta(minutes=slot_minutes)
                    continue

                overlapping = sum(
                    1 for ev_start, ev_end in booked_spans
                    if candidate < ev_end and end_candidate > ev_start
                )
                if overlapping < parallel_capacity:
                    logger.info(
                        "find_next_available_slot found: service=%s slot=%s clinic=%s",
                        service, candidate.isoformat(), clinic_name,
                    )
                    return {
                        "ok": True,
                        "service": service,
                        "date": candidate.strftime("%Y-%m-%d"),
                        "time": candidate.strftime("%H:%M"),
                        "formatted_slot": format_slot(candidate),
                    }

                candidate += timedelta(minutes=slot_minutes)

        logger.info(
            "find_next_available_slot exhausted 7 days: service=%s date=%s time=%s clinic=%s",
            service, date, time, clinic_name,
        )
        return {"ok": False, "message": "No available slots found in the next 7 days."}

    except Exception:
        logger.exception("find_next_available_slot failed: service=%s date=%s time=%s", service, date, time)
        return {"ok": False, "message": "Unable to find next available slot right now."}


def get_available_slots(service: str, date: str, clinic=None, max_slots: int = 5) -> Dict[str, Any]:
    """Scan an entire day and return up to max_slots free time windows.

    Used when the user asks about availability on a day without specifying a
    time (e.g. "any slots tomorrow?"). Returns a list of formatted slot strings
    that the LLM can present as options. Does NOT set availability_ok — the
    user must pick a slot and go through check_availability before booking.

    All-day calendar events are ignored (not appointment bookings).
    """
    from app import get_default_clinic, get_calendar
    try:
        if clinic is None:
            clinic = get_default_clinic()
        service = normalize_service(service)
        if service not in clinic["services"]:
            return {"ok": False, "message": "Unsupported service."}

        duration = clinic["services"][service]
        slot_minutes = clinic.get("slot_minutes", 30)
        tz = ZoneInfo(clinic.get("timezone", TIMEZONE))
        cal_id = clinic["google_calendar_id"]
        special_closures = set(clinic.get("special_closures", []))

        # Parse date — time component is ignored; we always start from open_hour.
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
        day_open = datetime(
            target_date.year, target_date.month, target_date.day,
            clinic["open_hour"], 0, tzinfo=tz,
        )
        day_close = day_open.replace(hour=clinic["close_hour"], minute=0)

        # Respect closures and Sunday before making any API calls.
        if day_open.weekday() == 6:
            return {"ok": False, "message": "The clinic is closed on Sundays."}
        if date in special_closures:
            return {"ok": False, "message": f"The clinic is closed on {date}."}

        horizon_err = _check_booking_horizon(date)
        if horizon_err:
            return horizon_err

        cal = get_calendar()

        # Start scanning from now if the date is today and we're already into the day.
        now_dt = _now()
        scan_from = day_open
        if now_dt > day_open:
            scan_from = now_dt.replace(second=0, microsecond=0)
            excess = scan_from.minute % slot_minutes
            if excess:
                scan_from += timedelta(minutes=slot_minutes - excess)

        # Fetch timed events for the day.
        day_events = cal.events().list(
            calendarId=cal_id,
            timeMin=day_open.isoformat(),
            timeMax=day_close.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute().get("items", [])

        booked_spans = []
        for ev in day_events:
            ev_start_str = ev.get("start", {}).get("dateTime")
            ev_end_str = ev.get("end", {}).get("dateTime")
            if ev_start_str and ev_end_str:
                try:
                    booked_spans.append((
                        datetime.fromisoformat(ev_start_str),
                        datetime.fromisoformat(ev_end_str),
                    ))
                except Exception:
                    pass

        # Collect up to max_slots free windows.
        available = []
        candidate = scan_from
        while len(available) < max_slots:
            end_candidate = candidate + timedelta(minutes=duration)
            if not is_within_business_hours(clinic, candidate, end_candidate):
                if end_candidate > day_close:
                    break
                candidate += timedelta(minutes=slot_minutes)
                continue
            conflict = any(
                candidate < ev_end and end_candidate > ev_start
                for ev_start, ev_end in booked_spans
            )
            if not conflict:
                available.append({
                    "time": candidate.strftime("%H:%M"),
                    "formatted": format_slot(candidate),
                })
            candidate += timedelta(minutes=slot_minutes)

        logger.info(
            "get_available_slots: service=%s date=%s found=%d clinic=%s",
            service, date, len(available), clinic.get("name", "unknown"),
        )

        if not available:
            return {"ok": False, "message": f"No available slots for {service} on {date}."}

        return {
            "ok": True,
            "service": service,
            "date": date,
            "slots": available,
        }

    except Exception:
        logger.exception("get_available_slots failed: service=%s date=%s", service, date)
        return {"ok": False, "message": "Unable to retrieve available slots right now."}


# ---------------------------------------------------------------------------
# Booking creation / modification / cancellation
# ---------------------------------------------------------------------------

def create_booking(name: str, service: str, date: str, time: str, phone: str, clinic=None) -> Dict[str, Any]:
    from app import get_default_clinic, get_calendar, get_clinic_by_id, save_booking_record, get_all_bookings, check_availability, parse_slot, format_slot
    from customers import record_booking_for_customer
    try:
        if clinic is None:
            clinic = get_default_clinic()
        service = normalize_service(service)
        availability = check_availability(service, date, time, clinic)

        if not availability["ok"]:
            return availability

        start_dt = parse_slot(date, time)
        end_dt = start_dt + timedelta(minutes=clinic["services"][service])

        cal = get_calendar()

        # BUG FIX #11: Only delete prior booking if it's for the SAME patient.
        # With family bookings, we don't want to delete other family members' appointments.
        # Scope to current clinic — never touch a prior booking at a different clinic.
        all_prior = get_all_bookings(phone, clinic_id=clinic.get("id") if clinic else None)
        prior_for_this_patient = None
        for booking in all_prior:
            if booking["name"].strip().lower() == name.strip().lower():
                prior_for_this_patient = booking
                break

        if prior_for_this_patient and prior_for_this_patient.get("event_id"):
            prior_cal_id = clinic["google_calendar_id"]
            if prior_for_this_patient.get("clinic_id") and prior_for_this_patient["clinic_id"] != clinic.get("id"):
                try:
                    prior_clinic = get_clinic_by_id(prior_for_this_patient["clinic_id"])
                    if prior_clinic:
                        prior_cal_id = prior_clinic["google_calendar_id"]
                except Exception:
                    logger.warning("Could not load prior clinic %s for event cleanup", prior_for_this_patient["clinic_id"])
            try:
                cal.events().delete(
                    calendarId=prior_cal_id,
                    eventId=prior_for_this_patient["event_id"],
                ).execute()
                logger.info(
                    "Deleted prior booking event_id=%s for patient=%s (phone=%s) before creating new booking",
                    prior_for_this_patient["event_id"], name, phone,
                )
            except HttpError as e:
                if e.resp.status == 404:
                    logger.info("Prior event_id=%s already gone from calendar", prior_for_this_patient["event_id"])
                else:
                    logger.exception("Failed to delete prior calendar event for patient=%s", name)
            except Exception:
                logger.exception("Unexpected error deleting prior calendar event for patient=%s", name)

        event_status = "Confirmed"

        event = {
            "summary": f"{status_prefix(event_status)} {service.title()} - {name}",
            "description": (
                f"Customer: {name}\n"
                f"Phone: {phone}\n"
                f"Status: {event_status}\n"
                f"Service: {service}"
            ),
            "start": {"dateTime": start_dt.isoformat(), "timeZone": clinic["timezone"]},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": clinic["timezone"]},
        }

        created = cal.events().insert(
            calendarId=clinic["google_calendar_id"],
            body=event
        ).execute()

        event_id = created.get("id")

        save_booking_record(
            user=phone,
            event_id=event_id,
            service=service,
            name=name,
            date=date,
            time=time,
            status=event_status,
            reminder_1d_sent=False,
            reminder_2h_sent=False,
            clinic_id=clinic.get("id"),
        )

        record_booking_for_customer(
            clinic_id=clinic.get("id"),
            phone=phone,
            appointment_dt=start_dt,
            name=name,
        )

        # Flag same-day bookings for staff awareness — no friction for the user.
        if start_dt.date() == _now().date():
            try:
                from app import write_conversation_flag
                clinic_id_for_flag = clinic.get("id")
                if clinic_id_for_flag:
                    write_conversation_flag(clinic_id_for_flag, phone, "same_day_booking")
            except Exception:
                logger.warning("Failed to write same_day_booking flag for phone=%s", phone)

        short_notice_hours = clinic.get("short_notice_hours", 24)
        short_notice = (start_dt - _now()).total_seconds() < short_notice_hours * 3600

        return {
            "ok": True,
            "event_id": event_id,
            "service": service,
            "name": name,
            "date": date,
            "time": time,
            "formatted_slot": format_slot(start_dt),
            "short_notice": short_notice,
        }

    except Exception:
        logger.exception("create_booking failed")
        return {"ok": False, "message": "Unable to create booking right now."}


def reschedule_booking(phone: str, service: str, date: str, time: str, clinic=None, name: str = None) -> Dict[str, Any]:
    """
    Reschedule an existing booking to a new date/time.

    When name is provided, target the booking for that patient name. When name is
    omitted and multiple bookings exist, refuse and ask for clarification.
    """
    from app import get_default_clinic, get_calendar, get_all_bookings, save_booking_record, check_availability, parse_slot, format_slot
    try:
        if clinic is None:
            clinic = get_default_clinic()
        service = normalize_service(service)

        if service not in clinic["services"]:
            return {"ok": False, "message": "Unsupported service."}

        all_bookings = get_all_bookings(phone, clinic_id=clinic.get("id") if clinic else None)

        if not all_bookings:
            return {"ok": False, "message": "No booking found to reschedule."}

        if name:
            name_lower = name.strip().lower()
            matches = [b for b in all_bookings if name_lower in b["name"].strip().lower()]
            if not matches:
                return {"ok": False, "message": f"No booking found for '{name}'."}
            existing = matches[0]
        elif len(all_bookings) > 1:
            names_list = ", ".join(b["name"] for b in all_bookings)
            return {
                "ok": False,
                "message": (
                    f"There are multiple bookings on this number ({names_list}). "
                    "Please specify whose booking to reschedule."
                ),
            }
        else:
            existing = all_bookings[0]

        availability = check_availability(service, date, time, clinic)
        if not availability["ok"]:
            return availability

        start_dt = parse_slot(date, time)
        end_dt = start_dt + timedelta(minutes=clinic["services"][service])

        cal = get_calendar()
        event = cal.events().get(
            calendarId=clinic["google_calendar_id"],
            eventId=existing["event_id"]
        ).execute()

        current_status = parse_event_status_from_summary(event.get("summary", "")) or existing.get("status", "Pending")
        current_name = existing.get("name", "Customer")

        event["start"]["dateTime"] = start_dt.isoformat()
        event["start"]["timeZone"] = clinic["timezone"]
        event["end"]["dateTime"] = end_dt.isoformat()
        event["end"]["timeZone"] = clinic["timezone"]
        event["summary"] = f"{status_prefix(current_status)} {service.title()} - {current_name}"
        event["description"] = (
            f"Customer: {current_name}\n"
            f"Phone: {phone}\n"
            f"Status: {current_status}\n"
            f"Service: {service}"
        )

        cal.events().update(
            calendarId=clinic["google_calendar_id"],
            eventId=existing["event_id"],
            body=event
        ).execute()

        save_booking_record(
            user=phone,
            event_id=existing["event_id"],
            service=service,
            name=current_name,
            date=date,
            time=time,
            status=current_status,
            reminder_1d_sent=False,
            reminder_2h_sent=False,
            clinic_id=clinic.get("id"),
        )

        return {
            "ok": True,
            "formatted_slot": format_slot(start_dt)
        }

    except Exception:
        logger.exception("reschedule_booking failed")
        return {"ok": False, "message": "Unable to reschedule right now."}


def cancel_booking(phone: str, clinic=None, name: str = None) -> Dict[str, Any]:
    """
    Cancel a booking for this phone number.

    When name is provided, cancel only the booking matching that patient name
    (case-insensitive, partial match). When name is omitted and multiple bookings
    exist, refuse and ask for clarification to avoid cancelling the wrong patient.

    clinic_id is used to scope the lookup to the current clinic so that a patient
    messaging Clinic B cannot accidentally cancel a booking they made at Clinic A.
    """
    from app import get_default_clinic, get_calendar, get_all_bookings, delete_booking_record, reset_booking_state
    try:
        if clinic is None:
            clinic = get_default_clinic()

        all_bookings = get_all_bookings(phone, clinic_id=clinic.get("id") if clinic else None)

        if not all_bookings:
            return {"ok": False, "message": "No booking found."}

        if name:
            name_lower = name.strip().lower()
            matches = [b for b in all_bookings if name_lower in b["name"].strip().lower()]
            if not matches:
                return {"ok": False, "message": f"No booking found for '{name}'."}
            # Use the first (most recent) match for this patient name.
            existing = matches[0]
        elif len(all_bookings) > 1:
            names_list = ", ".join(b["name"] for b in all_bookings)
            return {
                "ok": False,
                "message": (
                    f"There are multiple bookings on this number ({names_list}). "
                    "Please specify whose booking to cancel."
                ),
            }
        else:
            existing = all_bookings[0]

        cal = get_calendar()

        try:
            cal.events().delete(
                calendarId=clinic["google_calendar_id"],
                eventId=existing["event_id"]
            ).execute()
        except Exception:
            logger.exception("Calendar delete failed for %s", phone)

        delete_booking_record(existing["event_id"])
        reset_booking_state(phone, clinic_id=clinic.get("id") if clinic else None)

        return {"ok": True}

    except Exception:
        logger.exception("cancel_booking failed")
        return {"ok": False, "message": "Unable to cancel booking right now."}


# ---------------------------------------------------------------------------
# Reminder processing
# ---------------------------------------------------------------------------

def reminder_message_1d(name: str, service: str, start_dt: datetime, clinic: dict) -> str:
    return (
        f"Hi {name}, this is a reminder of your {service} appointment at {clinic['name']} "
        f"on {format_slot(start_dt)}. Please let us know if you need to make any changes."
    )


def reminder_message_2h(name: str, service: str, start_dt: datetime, clinic: dict) -> str:
    return (
        f"Hi {name}, this is a reminder that your {service} appointment at {clinic['name']} "
        f"is today at {format_time_only(start_dt)}. See you soon."
    )


def sync_booking_from_calendar(record, clinic: dict) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    from app import get_calendar
    try:
        cal = get_calendar()
        event = cal.events().get(
            calendarId=clinic["google_calendar_id"],
            eventId=record.event_id
        ).execute()

        summary = event.get("summary", "")
        status = parse_event_status_from_summary(summary)

        start_raw = event.get("start", {}).get("dateTime")
        if not start_raw:
            return None, "missing start dateTime"

        start_dt = datetime.fromisoformat(start_raw)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=ZoneInfo(clinic["timezone"]))
        else:
            start_dt = start_dt.astimezone(ZoneInfo(clinic["timezone"]))

        info = {
            "status": status,
            "date": start_dt.strftime("%Y-%m-%d"),
            "time": start_dt.strftime("%H:%M"),
            "service": record.service,
            "name": record.name,
        }

        return info, None
    except HttpError as exc:
        if exc.resp.status == 404:
            logger.warning("Calendar event not found for %s (event_id=%s); marking cancelled", record.user, record.event_id)
            return None, "event_not_found"
        logger.exception("sync_booking_from_calendar failed for %s", record.user)
        return None, str(exc)
    except Exception as exc:
        logger.exception("sync_booking_from_calendar failed for %s", record.user)
        return None, str(exc)


def process_reminders() -> Dict[str, Any]:
    from app import SessionLocal, BookingRecordModel, get_clinic_by_id, get_default_clinic, send_whatsapp_outbound, alert_telegram
    results = {
        "checked": 0,
        "sent_1d": 0,
        "sent_2h": 0,
        "skipped": 0,
        "errors": 0,
    }

    now_dt = _now()

    with SessionLocal() as db:
        records = db.query(BookingRecordModel).all()

        for record in records:
            results["checked"] += 1

            clinic = get_clinic_by_id(record.clinic_id) if record.clinic_id else get_default_clinic()
            if not clinic:
                logger.warning("Clinic not found for booking record user=%s clinic_id=%s; skipping", record.user, record.clinic_id)
                results["errors"] += 1
                continue

            info, err = sync_booking_from_calendar(record, clinic)
            if err == "event_not_found":
                db.delete(record)
                db.commit()
                results["skipped"] += 1
                continue
            if err or not info:
                results["errors"] += 1
                continue

            changed = (
                record.status != info["status"]
                or record.date != info["date"]
                or record.time != info["time"]
            )

            if changed:
                record.status = info["status"]
                if record.date != info["date"] or record.time != info["time"]:
                    record.reminder_1d_sent = False
                    record.reminder_2h_sent = False
                record.date = info["date"]
                record.time = info["time"]
                record.updated_at = datetime.utcnow()
                db.commit()

            if record.status != "Confirmed":
                results["skipped"] += 1
                continue

            try:
                start_dt = parse_slot(record.date, record.time)
            except Exception:
                results["errors"] += 1
                continue

            if start_dt <= now_dt:
                results["skipped"] += 1
                continue

            clinic_from = clinic.get("twilio_number") or None

            if not record.reminder_1d_sent and should_send_1d_reminder(start_dt, now_dt):
                sent = send_whatsapp_outbound(
                    record.user,
                    reminder_message_1d(record.name, record.service, start_dt, clinic),
                    from_number=clinic_from,
                )
                if sent:
                    record.reminder_1d_sent = True
                    record.updated_at = datetime.utcnow()
                    db.commit()
                    results["sent_1d"] += 1
                else:
                    results["errors"] += 1
                    alert_telegram(
                        clinic_name=clinic.get("name", "Unknown"),
                        user=record.user,
                        event="reminder send failed",
                        reason=f"1-day reminder failed for {record.user} ({record.service} on {record.date})",
                    )

            if not record.reminder_2h_sent and should_send_2h_reminder(start_dt, now_dt):
                sent = send_whatsapp_outbound(
                    record.user,
                    reminder_message_2h(record.name, record.service, start_dt, clinic),
                    from_number=clinic_from,
                )
                if sent:
                    record.reminder_2h_sent = True
                    record.updated_at = datetime.utcnow()
                    db.commit()
                    results["sent_2h"] += 1
                else:
                    results["errors"] += 1
                    alert_telegram(
                        clinic_name=clinic.get("name", "Unknown"),
                        user=record.user,
                        event="reminder send failed",
                        reason=f"2-hour reminder failed for {record.user} ({record.service} on {record.date})",
                    )


# ---------------------------------------------------------------------------
# Calendar health check
# ---------------------------------------------------------------------------

def check_calendar_health(clinic: dict) -> dict:
    """Attempt a minimal Google Calendar API call to verify the connection is working.

    Makes a cheap calendars().get() call against the clinic's calendar ID.
    Returns {"ok": True} on success, or {"ok": False, "error": <message>} on failure.
    Never raises.
    """
    try:
        cal = _get_calendar()
        cal_id = clinic.get("google_calendar_id") or "primary"
        cal.calendars().get(calendarId=cal_id).execute()
        return {"ok": True}
    except HttpError as exc:
        status = exc.resp.status if exc.resp else 0
        if status in (401, 403):
            return {"ok": False, "error": f"Token expired or revoked (HTTP {status})"}
        return {"ok": False, "error": f"Google Calendar API error (HTTP {status}): {exc}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def run_calendar_health_checks() -> dict:
    """Check Google Calendar connectivity for all active clinics.

    Updates calendar_healthy, calendar_last_checked_at, and calendar_error on each
    Clinic row. Sends a Telegram alert when a clinic's calendar becomes unreachable.
    Commits after each clinic so one failure doesn't block the rest.

    Returns summary {"checked": N, "healthy": N, "unhealthy": N}.
    """
    from app import SessionLocal, Clinic
    from utils import send_telegram_alert

    results = {"checked": 0, "healthy": 0, "unhealthy": 0}
    now_utc = datetime.utcnow()

    with SessionLocal() as db:
        active_clinics = db.query(Clinic).filter(Clinic.is_active == True).all()

    for clinic_row in active_clinics:
        results["checked"] += 1
        clinic_dict = {
            "id": clinic_row.id,
            "name": clinic_row.name,
            "google_calendar_id": clinic_row.google_calendar_id or "primary",
        }

        health = check_calendar_health(clinic_dict)

        try:
            with SessionLocal() as db:
                row = db.query(Clinic).filter(Clinic.id == clinic_row.id).first()
                if not row:
                    continue
                row.calendar_last_checked_at = now_utc
                if health["ok"]:
                    row.calendar_healthy = True
                    row.calendar_error = None
                    results["healthy"] += 1
                    logger.info(
                        "CALENDAR_HEALTH_OK | clinic_id=%s | clinic=%s",
                        clinic_row.id, clinic_row.name,
                    )
                else:
                    error_msg = health["error"]
                    row.calendar_healthy = False
                    row.calendar_error = error_msg
                    results["unhealthy"] += 1
                    logger.error(
                        "CALENDAR_HEALTH_FAIL | clinic_id=%s | clinic=%s | error=%s",
                        clinic_row.id, clinic_row.name, error_msg,
                    )
                    send_telegram_alert(
                        f"Atria AI — Calendar Disconnected\n"
                        f"Clinic: {clinic_row.name}\n"
                        f"Error: {error_msg}\n"
                        f"Check Google OAuth token."
                    )
                db.commit()
        except Exception:
            logger.exception(
                "run_calendar_health_checks: DB update failed for clinic_id=%s", clinic_row.id
            )

    logger.info("run_calendar_health_checks: complete | %s", results)
    return results

    return results
