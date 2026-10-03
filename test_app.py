import os

# Must be set before importing app — app.py reads these at module level.
os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import app
from app import (
    check_date_available,
    build_system_prompt,
    find_next_available_slot,
    get_available_slots,
    cancel_booking,
    check_availability,
    create_booking,
    dispatch_tool,
    ensure_demo_clinic_seeded,
    get_booking_state,
    get_clinic_by_id,
    get_default_clinic,
    get_direct_reply,
    get_existing_booking,
    is_human_request,
    is_reset_command,
    is_within_business_hours,
    normalize_service,
    now_local,
    process_reminders,
    reminder_message_1d,
    reminder_message_2h,
    reschedule_booking,
    resolve_booking_datetime,
    resolve_relative_date,
    resolve_time_text,
    reset_booking_state,
    save_booking_record,
    send_telegram,
    _set_pending_date_clarification,
    _consume_pending_date_clarification,
    _clear_pending_date_clarification,
    _PENDING_DATE_CLARIFICATIONS,
    should_send_1d_reminder,
    should_send_2h_reminder,
    sync_booking_from_calendar,
    trigger_fallback,
    update_booking_state,
    BookingRecordModel,
    BookingStateModel,
    SessionLocal,
)
from googleapiclient.errors import HttpError


TZ = ZoneInfo("Asia/Kuala_Lumpur")


# ---------------------------------------------------------------------------
# Session fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def seed_clinic():
    """Seed demo clinic once for the full test session."""
    ensure_demo_clinic_seeded()


# ---------------------------------------------------------------------------
# Per-test cleanup
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_booking_data():
    """Wipe booking state and records after every test."""
    yield
    with SessionLocal() as db:
        db.query(BookingRecordModel).delete()
        db.query(BookingStateModel).delete()
        db.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slot(date_str: str, time_str: str) -> datetime:
    return datetime.strptime(
        f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=TZ)


CLINIC_HOURS = {"open_hour": 10, "close_hour": 18}


def _mock_cal_404():
    mock_resp = MagicMock()
    mock_resp.status = 404
    mock_cal = MagicMock()
    mock_cal.events.return_value.get.return_value.execute.side_effect = (
        HttpError(resp=mock_resp, content=b"Not Found")
    )
    return mock_cal


def _mock_cal_confirmed(appt_dt: datetime):
    mock_cal = MagicMock()
    mock_cal.events.return_value.get.return_value.execute.return_value = {
        "summary": "[Confirmed] Scaling - Test Patient",
        "start": {"dateTime": appt_dt.isoformat()},
    }
    mock_cal.events.return_value.list.return_value.execute.return_value = {"items": []}
    return mock_cal


# ===========================================================================
# Test 1: is_within_business_hours — boundary conditions
# ===========================================================================

class TestBusinessHours:
    """Clinic: open 10:00, close 18:00. Scaling=60 min, Polishing=30 min."""

    def test_exact_open_boundary_passes(self):
        assert is_within_business_hours(
            CLINIC_HOURS, _slot("2026-04-07", "10:00"), _slot("2026-04-07", "11:00")
        ) is True

    def test_one_minute_before_open_fails(self):
        assert is_within_business_hours(
            CLINIC_HOURS, _slot("2026-04-07", "09:59"), _slot("2026-04-07", "10:59")
        ) is False

    def test_last_valid_scaling_slot_passes(self):
        # 17:00 + 60 min ends exactly at close (18:00) — must pass
        assert is_within_business_hours(
            CLINIC_HOURS, _slot("2026-04-07", "17:00"), _slot("2026-04-07", "18:00")
        ) is True

    def test_scaling_one_minute_past_last_slot_fails(self):
        # 17:01 + 60 min = 18:01 — overflows close
        assert is_within_business_hours(
            CLINIC_HOURS, _slot("2026-04-07", "17:01"), _slot("2026-04-07", "18:01")
        ) is False

    def test_last_valid_polishing_slot_passes(self):
        # 17:30 + 30 min = 18:00 exactly
        assert is_within_business_hours(
            CLINIC_HOURS, _slot("2026-04-07", "17:30"), _slot("2026-04-07", "18:00")
        ) is True

    def test_polishing_one_minute_past_last_slot_fails(self):
        # 17:31 + 30 min = 18:01
        assert is_within_business_hours(
            CLINIC_HOURS, _slot("2026-04-07", "17:31"), _slot("2026-04-07", "18:01")
        ) is False

    def test_sunday_always_closed(self):
        # 2026-03-29 is a Sunday
        assert is_within_business_hours(
            CLINIC_HOURS, _slot("2026-03-29", "10:00"), _slot("2026-03-29", "11:00")
        ) is False

    def test_saturday_open(self):
        # 2026-04-04 is a Saturday
        assert is_within_business_hours(
            CLINIC_HOURS, _slot("2026-04-04", "10:00"), _slot("2026-04-04", "11:00")
        ) is True


# ===========================================================================
# Test 2: availability_ok reset on date/time change via dispatch_tool
# ===========================================================================

class TestAvailabilityOkReset:
    """
    After our fix: dispatch_tool("resolve_booking_datetime", ...) must always
    reset availability_ok to False, even if it was previously True.
    """

    def test_reset_when_date_changes(self):
        user = "qa_avail_date"
        update_booking_state(user, date="2099-04-07", time="10:00",
                             service="scaling", availability_ok=True)
        assert get_booking_state(user)["availability_ok"] is True

        result = dispatch_tool(
            "resolve_booking_datetime",
            {"date_text": "2099-04-08", "time_text": "10:00"},
            user,
        )

        assert result["ok"] is True
        assert get_booking_state(user)["availability_ok"] is False

    def test_reset_when_time_changes(self):
        user = "qa_avail_time"
        update_booking_state(user, date="2099-04-07", time="10:00",
                             service="scaling", availability_ok=True)

        result = dispatch_tool(
            "resolve_booking_datetime",
            {"date_text": "2099-04-07", "time_text": "14:00"},
            user,
        )

        assert result["ok"] is True
        assert get_booking_state(user)["availability_ok"] is False

    def test_resolve_never_sets_availability_ok_true(self):
        # resolve_booking_datetime alone must never grant availability_ok=True
        user = "qa_avail_never_set"

        result = dispatch_tool(
            "resolve_booking_datetime",
            {"date_text": "2099-04-07", "time_text": "10:00"},
            user,
        )

        assert result["ok"] is True
        assert get_booking_state(user)["availability_ok"] is False

    def test_failed_resolve_leaves_availability_ok_false(self):
        # If resolve fails, state must not gain availability_ok=True
        user = "qa_avail_bad_date"
        update_booking_state(user, availability_ok=False)

        result = dispatch_tool(
            "resolve_booking_datetime",
            {"date_text": "not-a-date", "time_text": "banana"},
            user,
        )

        assert result["ok"] is False
        assert get_booking_state(user)["availability_ok"] is False


# ===========================================================================
# Test 3: normalize_service — alias coverage
# ===========================================================================

class TestServiceAliases:

    def test_cleaning_to_scaling(self):
        assert normalize_service("cleaning") == "scaling"

    def test_teeth_cleaning_to_scaling(self):
        assert normalize_service("teeth cleaning") == "scaling"

    def test_dental_cleaning_to_scaling(self):
        assert normalize_service("dental cleaning") == "scaling"

    def test_braces_to_braces_consultation(self):
        assert normalize_service("braces") == "braces consultation"

    def test_consultation_to_braces_consultation(self):
        assert normalize_service("consultation") == "braces consultation"

    def test_brace_consultation_normalised(self):
        assert normalize_service("brace consultation") == "braces consultation"

    def test_tooth_filling_to_filling(self):
        assert normalize_service("tooth filling") == "filling"

    def test_unknown_service_passes_through(self):
        assert normalize_service("root canal") == "root canal"

    def test_case_insensitive_cleaning(self):
        assert normalize_service("CLEANING") == "scaling"

    def test_case_insensitive_braces(self):
        assert normalize_service("Braces") == "braces consultation"

    def test_empty_string_returns_empty(self):
        assert normalize_service("") == ""

    def test_canonical_service_names_unchanged(self):
        for svc in ("scaling", "polishing", "filling", "whitening", "braces consultation"):
            assert normalize_service(svc) == svc, f"Expected {svc!r} unchanged"

    # --- Typo / spelling variation aliases added for robustness ---

    def test_scaleing_typo(self):
        assert normalize_service("scaleing") == "scaling"

    def test_scalling_typo(self):
        assert normalize_service("scalling") == "scaling"

    def test_sclaing_typo(self):
        assert normalize_service("sclaing") == "scaling"

    def test_sacling_typo(self):
        assert normalize_service("sacling") == "scaling"

    def test_scale_shorthand(self):
        assert normalize_service("scale") == "scaling"

    def test_teeth_clean_shorthand(self):
        assert normalize_service("teeth clean") == "scaling"

    def test_whitning_typo(self):
        assert normalize_service("whitning") == "whitening"

    def test_whitenning_typo(self):
        assert normalize_service("whitenning") == "whitening"

    def test_whiteing_typo(self):
        assert normalize_service("whiteing") == "whitening"

    def test_teeth_whitening_alias(self):
        assert normalize_service("teeth whitening") == "whitening"

    def test_bleaching_alias(self):
        assert normalize_service("bleaching") == "whitening"

    def test_polshing_typo(self):
        assert normalize_service("polshing") == "polishing"

    def test_fill_shorthand(self):
        assert normalize_service("fill") == "filling"

    def test_fillings_plural(self):
        assert normalize_service("fillings") == "filling"

    def test_teeth_filling_alias(self):
        assert normalize_service("teeth filling") == "filling"

    def test_brace_shorthand(self):
        assert normalize_service("brace") == "braces consultation"

    def test_braces_consult_shorthand(self):
        assert normalize_service("braces consult") == "braces consultation"

    def test_case_insensitive_typo(self):
        assert normalize_service("WHITNING") == "whitening"
        assert normalize_service("Sclaing") == "scaling"


# ===========================================================================
# Test 4: reminder window boundaries
# ===========================================================================

class TestReminderWindows:

    NOW = datetime(2026, 4, 10, 10, 0, tzinfo=TZ)

    # 1D window: 20h–28h inclusive
    def test_1d_lower_bound_passes(self):
        assert should_send_1d_reminder(self.NOW + timedelta(hours=20), self.NOW) is True

    def test_1d_upper_bound_passes(self):
        assert should_send_1d_reminder(self.NOW + timedelta(hours=28), self.NOW) is True

    def test_1d_midpoint_passes(self):
        assert should_send_1d_reminder(self.NOW + timedelta(hours=24), self.NOW) is True

    def test_1d_just_below_lower_fails(self):
        assert should_send_1d_reminder(
            self.NOW + timedelta(hours=19, minutes=59), self.NOW
        ) is False

    def test_1d_just_above_upper_fails(self):
        assert should_send_1d_reminder(
            self.NOW + timedelta(hours=28, minutes=1), self.NOW
        ) is False

    # 2H window: 90–150 min inclusive
    def test_2h_lower_bound_passes(self):
        assert should_send_2h_reminder(self.NOW + timedelta(minutes=90), self.NOW) is True

    def test_2h_upper_bound_passes(self):
        assert should_send_2h_reminder(self.NOW + timedelta(minutes=150), self.NOW) is True

    def test_2h_midpoint_passes(self):
        assert should_send_2h_reminder(self.NOW + timedelta(minutes=120), self.NOW) is True

    def test_2h_just_below_lower_fails(self):
        assert should_send_2h_reminder(self.NOW + timedelta(minutes=89), self.NOW) is False

    def test_2h_just_above_upper_fails(self):
        assert should_send_2h_reminder(self.NOW + timedelta(minutes=151), self.NOW) is False


# ===========================================================================
# Test 5: process_reminders with mocked Calendar and Twilio
# ===========================================================================

class TestProcessReminders:

    @patch("app.get_calendar")
    def test_404_deletes_record_and_counts_as_skipped(self, mock_get_cal):
        """A 404 from Calendar means the event was manually deleted.
        The DB record should be removed and counted as skipped, not an error."""
        user = "qa_reminder_404"
        save_booking_record(
            user=user, event_id="evt_404", service="scaling", name="Test Patient",
            date="2099-04-11", time="10:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False,
        )
        mock_get_cal.return_value = _mock_cal_404()

        result = process_reminders()

        assert get_existing_booking(user) is None, "Record should be deleted on 404"
        assert result["errors"] == 0
        assert result["skipped"] >= 1

    @patch("app.get_calendar")
    @patch("app.send_whatsapp_outbound")
    def test_twilio_failure_does_not_set_reminder_flag(self, mock_send, mock_get_cal):
        """If Twilio fails, reminder_1d_sent must stay False so the next cron run retries."""
        user = "qa_reminder_twilio_fail"
        appt = now_local() + timedelta(hours=24)
        save_booking_record(
            user=user, event_id="evt_twilio_fail", service="scaling", name="Test Patient",
            date=appt.strftime("%Y-%m-%d"), time=appt.strftime("%H:%M"),
            status="Confirmed", reminder_1d_sent=False, reminder_2h_sent=False,
        )
        mock_get_cal.return_value = _mock_cal_confirmed(appt)
        mock_send.return_value = False

        result = process_reminders()

        assert result["sent_1d"] == 0
        assert result["errors"] >= 1
        assert get_existing_booking(user)["reminder_1d_sent"] is False

    @patch("app.get_calendar")
    @patch("app.send_whatsapp_outbound")
    def test_successful_send_sets_flag_and_no_duplicate_on_second_run(
        self, mock_send, mock_get_cal
    ):
        """On success, flag is set. A second cron run must not send again."""
        user = "qa_reminder_success"
        appt = now_local() + timedelta(hours=24)
        save_booking_record(
            user=user, event_id="evt_success", service="scaling", name="Test Patient",
            date=appt.strftime("%Y-%m-%d"), time=appt.strftime("%H:%M"),
            status="Confirmed", reminder_1d_sent=False, reminder_2h_sent=False,
        )
        mock_get_cal.return_value = _mock_cal_confirmed(appt)
        mock_send.return_value = True

        first = process_reminders()
        assert first["sent_1d"] == 1
        assert get_existing_booking(user)["reminder_1d_sent"] is True

        second = process_reminders()
        assert second["sent_1d"] == 0
        assert mock_send.call_count == 1  # Twilio called exactly once across both runs

    @patch("app.get_calendar")
    @patch("app.send_whatsapp_outbound")
    def test_pending_booking_not_reminded(self, mock_send, mock_get_cal):
        """Status=Pending bookings must never receive reminders."""
        user = "qa_reminder_pending"
        appt = now_local() + timedelta(hours=24)
        save_booking_record(
            user=user, event_id="evt_pending", service="scaling", name="Test Patient",
            date=appt.strftime("%Y-%m-%d"), time=appt.strftime("%H:%M"),
            status="Pending", reminder_1d_sent=False, reminder_2h_sent=False,
        )
        mock_cal = MagicMock()
        mock_cal.events.return_value.get.return_value.execute.return_value = {
            "summary": "[Pending] Scaling - Test Patient",
            "start": {"dateTime": appt.isoformat()},
        }
        mock_get_cal.return_value = mock_cal

        result = process_reminders()

        assert result["sent_1d"] == 0
        assert result["skipped"] >= 1
        mock_send.assert_not_called()


# ===========================================================================
# Test 6: resolve_relative_date — "next week / next {weekday}" regression
# ===========================================================================

_THURSDAY = datetime(2026, 3, 26, 9, 0, tzinfo=TZ)   # Thursday


class TestRelativeDateResolution:

    # --- Regression: exact phrase from production bug ---

    @patch("app.now_local", return_value=_THURSDAY)
    def test_next_week_tuesday_regression(self, _):
        """'next week tuesday' from Thursday must resolve to the Tuesday of next week."""
        assert resolve_relative_date("next week tuesday") == "2026-03-31"

    # --- "next {weekday}" ---

    @patch("app.now_local", return_value=_THURSDAY)
    def test_next_tuesday_from_thursday(self, _):
        """'next tuesday' from Thursday = next occurrence = 31 March."""
        assert resolve_relative_date("next tuesday") == "2026-03-31"

    @patch("app.now_local", return_value=_THURSDAY)
    def test_next_friday_from_thursday(self, _):
        """'next friday' from Thursday = tomorrow, 27 March."""
        assert resolve_relative_date("next friday") == "2026-03-27"

    @patch("app.now_local", return_value=_THURSDAY)
    def test_next_monday_from_thursday(self, _):
        """'next monday' from Thursday = 30 March."""
        assert resolve_relative_date("next monday") == "2026-03-30"

    # --- "next week {weekday}" ---

    @patch("app.now_local", return_value=_THURSDAY)
    def test_next_week_saturday_from_thursday(self, _):
        """'next week saturday' from Thursday = 4 April (not 28 March)."""
        assert resolve_relative_date("next week saturday") == "2026-04-04"

    @patch("app.now_local", return_value=_THURSDAY)
    def test_next_week_monday_from_thursday(self, _):
        """'next week monday' from Thursday = 30 March (start of next week)."""
        assert resolve_relative_date("next week monday") == "2026-03-30"

    # --- "next week {weekday}" does not bleed into current week ---

    @patch("app.now_local", return_value=datetime(2026, 3, 30, 9, 0, tzinfo=TZ))  # Monday
    def test_next_week_tuesday_from_monday_jumps_full_week(self, _):
        """'next week tuesday' from Monday must skip to the Tuesday of the FOLLOWING week,
        not tomorrow."""
        assert resolve_relative_date("next week tuesday") == "2026-04-07"

    # --- Abbreviated weekday names still work ---

    @patch("app.now_local", return_value=_THURSDAY)
    def test_next_tue_abbreviated(self, _):
        assert resolve_relative_date("next tue") == "2026-03-31"

    @patch("app.now_local", return_value=_THURSDAY)
    def test_next_week_sat_abbreviated(self, _):
        assert resolve_relative_date("next week sat") == "2026-04-04"

    # --- Bare weekday names still behave as before (no regression) ---

    @patch("app.now_local", return_value=_THURSDAY)
    def test_bare_tuesday_unchanged(self, _):
        assert resolve_relative_date("tuesday") == "2026-03-31"

    @patch("app.now_local", return_value=_THURSDAY)
    def test_bare_friday_unchanged(self, _):
        assert resolve_relative_date("friday") == "2026-03-27"


# ===========================================================================
# Test 7: Multi-clinic routing — routed clinic is used, not default
# ===========================================================================

_SECOND_CLINIC = {
    "name": "Second Dental",
    "location": "Petaling Jaya",
    "timezone": "Asia/Kuala_Lumpur",
    "open_hour": 9,
    "close_hour": 18,
    "hours_text": "Monday to Saturday, 9:00 to 18:00. Closed Sunday.",
    "slot_minutes": 30,
    "opening_message": "Welcome to Second Dental!",
    "promo_message": "",
    "services": {"scaling": 30, "filling": 60},
    "service_prices": {},
    "special_closures": [],
    "closure_notes": {},
    "google_calendar_id": "second-clinic-calendar@group.calendar.google.com",
    "twilio_number": "+60199999999",
}

_FUTURE_WEEKDAY = datetime(2026, 3, 30, 9, 0, tzinfo=TZ)  # Monday — business day


class TestMultiClinicRouting:

    def test_build_system_prompt_uses_passed_clinic(self):
        """build_system_prompt must embed the passed clinic name, not the default clinic."""
        default = get_default_clinic()
        assert default["name"] != _SECOND_CLINIC["name"], "test assumes distinct clinic names"

        prompt = build_system_prompt("test_user_routing", _SECOND_CLINIC)

        assert _SECOND_CLINIC["name"] in prompt
        assert default["name"] not in prompt

    def test_get_direct_reply_greeting_passes_to_llm(self):
        """Greetings are now handled by the LLM — get_direct_reply must return None."""
        reply = get_direct_reply("test_user_routing", "hi", _SECOND_CLINIC)
        assert reply is None

    def test_get_direct_reply_uses_passed_clinic_greeting(self):
        """trigger_fallback uses the passed clinic's contact number when present."""
        clinic_with_contact = {**_SECOND_CLINIC, "human_contact_number": "+60112345678"}
        with patch("app.send_telegram"):
            reply = get_direct_reply("test_greeting_routing", "human", clinic_with_contact)
        assert reply is not None
        assert "+60112345678" in reply

    @patch("app.now_local", return_value=_FUTURE_WEEKDAY)
    def test_check_availability_uses_passed_clinic_calendar(self, _mock_now):
        """check_availability must query the passed clinic's google_calendar_id."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}

        mock_cal = MagicMock()
        mock_cal.events.return_value = mock_events

        with patch("app.get_calendar", return_value=mock_cal):
            result = check_availability("filling", "2026-03-31", "10:00", _SECOND_CLINIC)

        assert result["ok"] is True
        mock_events.list.assert_called_once()
        call_kwargs = mock_events.list.call_args[1]
        assert call_kwargs["calendarId"] == _SECOND_CLINIC["google_calendar_id"]

    @patch("app.now_local", return_value=_FUTURE_WEEKDAY)
    def test_check_availability_rejects_service_not_in_passed_clinic(self, _mock_now):
        """check_availability must reject a service not offered by the passed clinic."""
        result = check_availability("extraction", "2026-03-31", "10:00", _SECOND_CLINIC)
        assert result["ok"] is False

    @patch("app.now_local", return_value=_FUTURE_WEEKDAY)
    def test_create_booking_uses_passed_clinic_calendar(self, _mock_now):
        """create_booking must insert into the passed clinic's google_calendar_id."""
        phone = "+60100000002"
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "evt-second-123"}

        mock_cal = MagicMock()
        mock_cal.events.return_value = mock_events

        with patch("app.get_calendar", return_value=mock_cal):
            update_booking_state(phone, availability_ok=True)
            result = create_booking(
                name="Test Patient",
                service="filling",
                date="2026-03-31",
                time="10:00",
                phone=phone,
                clinic=_SECOND_CLINIC,
            )

        assert result["ok"] is True
        insert_kwargs = mock_events.insert.call_args[1]
        assert insert_kwargs["calendarId"] == _SECOND_CLINIC["google_calendar_id"]

    @patch("app.now_local", return_value=_FUTURE_WEEKDAY)
    def test_cancel_booking_uses_passed_clinic_calendar(self, _mock_now):
        """cancel_booking must delete from the passed clinic's google_calendar_id."""
        phone = "+60100000003"
        save_booking_record(
            user=phone,
            event_id="evt-cancel-test",
            service="cleaning",
            name="Test Patient",
            date="2026-03-31",
            time="10:00",
            status="Pending",
            reminder_1d_sent=False,
            reminder_2h_sent=False,
        )

        mock_events = MagicMock()
        mock_events.delete.return_value.execute.return_value = {}

        mock_cal = MagicMock()
        mock_cal.events.return_value = mock_events

        with patch("app.get_calendar", return_value=mock_cal):
            result = cancel_booking(phone, clinic=_SECOND_CLINIC)

        assert result["ok"] is True
        delete_kwargs = mock_events.delete.call_args[1]
        assert delete_kwargs["calendarId"] == _SECOND_CLINIC["google_calendar_id"]

    @patch("app.now_local", return_value=_FUTURE_WEEKDAY)
    def test_dispatch_tool_check_availability_uses_clinic(self, _mock_now):
        """dispatch_tool must pass the clinic into check_availability."""
        phone = "+60100000004"
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}

        mock_cal = MagicMock()
        mock_cal.events.return_value = mock_events

        with patch("app.get_calendar", return_value=mock_cal):
            result = dispatch_tool(
                "check_availability",
                {"service": "cleaning", "date": "2026-03-31", "time": "10:00"},
                phone,
                _SECOND_CLINIC,
            )

        assert result["ok"] is True
        call_kwargs = mock_events.list.call_args[1]
        assert call_kwargs["calendarId"] == _SECOND_CLINIC["google_calendar_id"]


# ===========================================================================
# Test 8: clinic_id stored in booking_records and used throughout reminder path
# ===========================================================================

_CLINIC_A = {
    "id": 901,
    "name": "Clinic Alpha",
    "location": "KL",
    "timezone": "Asia/Kuala_Lumpur",
    "open_hour": 10,
    "close_hour": 18,
    "hours_text": "Mon-Sat 10-18",
    "slot_minutes": 30,
    "opening_message": "",
    "promo_message": "",
    "services": {"scaling": 60},
    "service_prices": {},
    "special_closures": [],
    "closure_notes": {},
    "google_calendar_id": "clinic-a-cal@group.calendar.google.com",
    "twilio_number": "+60111111111",
}

_CLINIC_B = {
    "id": 902,
    "name": "Clinic Beta",
    "location": "PJ",
    "timezone": "Asia/Kuala_Lumpur",
    "open_hour": 9,
    "close_hour": 17,
    "hours_text": "Mon-Sat 9-17",
    "slot_minutes": 30,
    "opening_message": "",
    "promo_message": "",
    "services": {"polishing": 30},
    "service_prices": {},
    "special_closures": [],
    "closure_notes": {},
    "google_calendar_id": "clinic-b-cal@group.calendar.google.com",
    "twilio_number": "+60122222222",
}


class TestClinicIdInReminders:

    @patch("app.now_local", return_value=_FUTURE_WEEKDAY)
    def test_create_booking_stores_clinic_id(self, _mock_now):
        """create_booking must persist the clinic's id into the booking record."""
        phone = "+60190001"
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "evt-clinic-a"}
        mock_cal = MagicMock()
        mock_cal.events.return_value = mock_events

        with patch("app.get_calendar", return_value=mock_cal):
            result = create_booking(
                name="Patient A",
                service="scaling",
                date="2026-03-31",
                time="10:00",
                phone=phone,
                clinic=_CLINIC_A,
            )

        assert result["ok"] is True
        record = get_existing_booking(phone)
        assert record is not None
        assert record["clinic_id"] == 901

    def test_reminder_1d_uses_clinic_name(self):
        """reminder_message_1d must embed the clinic's name in the message."""
        dt = datetime(2026, 4, 11, 10, 0, tzinfo=TZ)
        msg = reminder_message_1d("Ali", "scaling", dt, _CLINIC_A)
        assert "Clinic Alpha" in msg
        assert "Ali" in msg
        assert "scaling" in msg

    def test_reminder_2h_uses_clinic_name(self):
        """reminder_message_2h must embed the clinic's name in the message."""
        dt = datetime(2026, 4, 11, 10, 0, tzinfo=TZ)
        msg = reminder_message_2h("Ali", "scaling", dt, _CLINIC_A)
        assert "Clinic Alpha" in msg

    @patch("app.now_local", return_value=_FUTURE_WEEKDAY)
    def test_sync_uses_passed_clinic_calendar(self, _mock_now):
        """sync_booking_from_calendar must query the passed clinic's calendar ID."""
        phone = "+60190002"
        save_booking_record(
            user=phone, event_id="evt-sync-a", service="scaling", name="Patient",
            date="2026-03-31", time="10:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False, clinic_id=901,
        )
        with SessionLocal() as db:
            record = db.query(BookingRecordModel).filter_by(user=phone).first()

        mock_cal = MagicMock()
        mock_cal.events.return_value.get.return_value.execute.return_value = {
            "summary": "[Confirmed] Scaling - Patient",
            "start": {"dateTime": "2026-03-31T10:00:00+08:00"},
        }

        with patch("app.get_calendar", return_value=mock_cal):
            info, err = sync_booking_from_calendar(record, _CLINIC_A)

        assert err is None
        mock_cal.events.return_value.get.assert_called_once()
        call_kwargs = mock_cal.events.return_value.get.call_args[1]
        assert call_kwargs["calendarId"] == _CLINIC_A["google_calendar_id"]

    @patch("app.get_calendar")
    @patch("app.send_whatsapp_outbound")
    @patch("app.get_clinic_by_id")
    def test_process_reminders_uses_clinic_calendar_and_twilio_number(
        self, mock_get_clinic, mock_send, mock_get_cal
    ):
        """process_reminders must use the booking's clinic_id to fetch clinic config."""
        phone = "+60190003"
        appt = now_local() + timedelta(hours=24)
        save_booking_record(
            user=phone, event_id="evt-remind-a", service="scaling", name="Patient A",
            date=appt.strftime("%Y-%m-%d"), time=appt.strftime("%H:%M"),
            status="Confirmed", reminder_1d_sent=False, reminder_2h_sent=False,
            clinic_id=901,
        )
        mock_get_clinic.return_value = _CLINIC_A
        mock_get_cal.return_value = _mock_cal_confirmed(appt)
        mock_send.return_value = True

        process_reminders()

        mock_get_clinic.assert_called_with(901)
        assert mock_send.called
        assert mock_send.call_args[1]["from_number"] == _CLINIC_A["twilio_number"]

    @patch("app.get_calendar")
    @patch("app.send_whatsapp_outbound")
    @patch("app.get_clinic_by_id")
    def test_process_reminders_two_clinics_correct_isolation(
        self, mock_get_clinic, mock_send, mock_get_cal
    ):
        """Two patients from different clinics must each receive reminders from their clinic."""
        phone_a = "+60190010"
        phone_b = "+60190011"
        appt = now_local() + timedelta(hours=24)

        save_booking_record(
            user=phone_a, event_id="evt-a-iso", service="scaling", name="Patient A",
            date=appt.strftime("%Y-%m-%d"), time=appt.strftime("%H:%M"),
            status="Confirmed", reminder_1d_sent=False, reminder_2h_sent=False,
            clinic_id=901,
        )
        save_booking_record(
            user=phone_b, event_id="evt-b-iso", service="polishing", name="Patient B",
            date=appt.strftime("%Y-%m-%d"), time=appt.strftime("%H:%M"),
            status="Confirmed", reminder_1d_sent=False, reminder_2h_sent=False,
            clinic_id=902,
        )

        def _get_clinic_side_effect(cid):
            return _CLINIC_A if cid == 901 else _CLINIC_B

        mock_get_clinic.side_effect = _get_clinic_side_effect
        mock_get_cal.return_value = _mock_cal_confirmed(appt)
        mock_send.return_value = True

        process_reminders()

        calls = mock_send.call_args_list
        from_numbers = {c[1]["from_number"] for c in calls}
        assert _CLINIC_A["twilio_number"] in from_numbers
        assert _CLINIC_B["twilio_number"] in from_numbers


# ===========================================================================
# Test 9: Phase 1 hardening — human fallback, telegram, guardrails
# ===========================================================================

class TestPhase1Hardening:

    def test_human_keyword_recognised(self):
        assert is_human_request("human") is True

    def test_staff_keyword_recognised(self):
        assert is_human_request("staff") is True

    def test_agent_keyword_recognised(self):
        assert is_human_request("agent") is True

    def test_talk_to_human_phrase_recognised(self):
        assert is_human_request("talk to human") is True

    def test_speak_to_staff_phrase_recognised(self):
        assert is_human_request("speak to staff") is True

    def test_empty_string_not_human_request(self):
        assert is_human_request("") is False

    def test_regular_booking_message_not_human_request(self):
        assert is_human_request("I want to book an appointment") is False

    def test_human_dentist_sentence_not_human_request(self):
        assert is_human_request("the human dentist is nice") is False

    def test_our_staff_sentence_not_human_request(self):
        assert is_human_request("your staff is great") is False

    def test_fallback_reply_without_contact_number(self):
        """trigger_fallback without human_contact_number uses generic message."""
        clinic = {**_SECOND_CLINIC}
        clinic.pop("human_contact_number", None)
        with patch("app.send_telegram"):
            reply = trigger_fallback("user1", clinic, "test")
        assert "team" in reply.lower() or "follow up" in reply.lower()
        assert reply is not None

    def test_fallback_reply_with_contact_number(self):
        """trigger_fallback with human_contact_number includes the number."""
        clinic = {**_SECOND_CLINIC, "human_contact_number": "+60198765432"}
        with patch("app.send_telegram"):
            reply = trigger_fallback("user2", clinic, "test")
        assert "+60198765432" in reply

    def test_fallback_increments_stats_counter(self):
        """trigger_fallback increments _stats['fallbacks']."""
        before = app._stats["fallbacks"]
        with patch("app.send_telegram"):
            trigger_fallback("user3", _SECOND_CLINIC, "test")
        assert app._stats["fallbacks"] == before + 1

    def test_fallback_logs_warning(self, caplog):
        """trigger_fallback logs a warning."""
        import logging
        with patch("app.send_telegram"):
            with caplog.at_level(logging.WARNING, logger="ai_receptionist"):
                trigger_fallback("user4", _SECOND_CLINIC, "test_reason")
        assert any("fallback" in r.message.lower() for r in caplog.records)

    def test_get_direct_reply_human_returns_fallback_message(self):
        """get_direct_reply for human request must return a fallback string."""
        with patch("app.send_telegram"):
            reply = get_direct_reply("user5", "human", _SECOND_CLINIC)
        assert reply is not None
        assert len(reply) > 0

    def test_get_direct_reply_human_does_not_clear_booking_state(self):
        """Fallback trigger must not wipe the user's booking state."""
        user = "qa_fallback_state"
        update_booking_state(user, service="scaling", date="2026-04-10", time="10:00")
        with patch("app.send_telegram"):
            get_direct_reply(user, "staff", _SECOND_CLINIC)
        state = get_booking_state(user)
        assert state["service"] == "scaling"

    def test_get_direct_reply_talk_to_human_phrase(self):
        with patch("app.send_telegram"):
            reply = get_direct_reply("user6", "talk to human", _SECOND_CLINIC)
        assert reply is not None

    def test_send_telegram_noop_without_credentials(self):
        """send_telegram must silently no-op when env vars are absent."""
        with patch("app.TELEGRAM_BOT_TOKEN", None), \
             patch("app.TELEGRAM_CHAT_ID", None):
            send_telegram("test message")  # Must not raise

    @patch("urllib.request.urlopen")
    def test_send_telegram_calls_urlopen_when_configured(self, mock_urlopen):
        """send_telegram calls urlopen when bot token and chat ID are set."""
        mock_urlopen.return_value = MagicMock()
        with patch("app.TELEGRAM_BOT_TOKEN", "test-token"), \
             patch("app.TELEGRAM_CHAT_ID", "12345"):
            send_telegram("test alert")
        mock_urlopen.assert_called_once()

    @patch("app.send_telegram")
    def test_telegram_alert_sent_on_openai_exception(self, mock_telegram):
        """trigger_fallback must call send_telegram when Telegram is configured."""
        with patch("app.TELEGRAM_BOT_TOKEN", "tok"), \
             patch("app.TELEGRAM_CHAT_ID", "123"):
            trigger_fallback("user_alert", _SECOND_CLINIC, "openai_exception")
        mock_telegram.assert_called_once()

    @patch("app.send_telegram")
    def test_daily_summary_requires_secret(self, _mock_tg):
        """Daily summary endpoint returns 403 without the correct secret."""
        with app.app.test_client() as c:
            response = c.get("/tasks/daily-summary")
        assert response.status_code == 403

    @patch("app.send_telegram")
    def test_daily_summary_returns_200_with_correct_secret(self, _mock_tg):
        """Daily summary returns 200 with correct secret."""
        from app import REMINDER_SECRET
        with app.app.test_client() as c:
            response = c.get("/tasks/daily-summary", headers={"X-Reminder-Secret": REMINDER_SECRET})
        assert response.status_code == 200

    @patch("app.send_telegram")
    def test_daily_summary_calls_send_telegram(self, mock_tg):
        """Daily summary must call send_telegram."""
        from app import REMINDER_SECRET
        with app.app.test_client() as c:
            c.get("/tasks/daily-summary", headers={"X-Reminder-Secret": REMINDER_SECRET})
        mock_tg.assert_called_once()


# ===========================================================================
# Test 10: Production improvements — reset, webhook validation
# ===========================================================================

class TestProductionImprovements:

    def test_reset_keyword_recognised(self):
        assert is_reset_command("reset") is True

    def test_slash_reset_keyword_recognised(self):
        assert is_reset_command("/reset") is True

    def test_restart_keyword_recognised(self):
        assert is_reset_command("restart") is True

    def test_start_over_recognised(self):
        assert is_reset_command("start over") is True

    def test_cancel_standalone_recognised(self):
        assert is_reset_command("cancel") is True

    def test_cancel_my_appointment_not_reset(self):
        """'cancel my appointment' must NOT trigger the reset shortcut."""
        assert is_reset_command("cancel my appointment") is False

    def test_regular_message_not_reset(self):
        assert is_reset_command("book appointment") is False

    def test_empty_message_not_reset(self):
        assert is_reset_command("") is False

    def test_reset_clears_populated_state(self):
        """get_direct_reply for 'reset' clears the booking state."""
        user = "qa_reset_state"
        # Use clinic_id=1 (DEFAULT_CLINIC_ID) to match what get_direct_reply uses
        # internally via get_default_clinic() → clinic.id == 1.
        update_booking_state(user, clinic_id=1, service="scaling", date="2026-04-10",
                             time="10:00", name="Test", availability_ok=True)
        get_direct_reply(user, "reset")
        state = get_booking_state(user, clinic_id=1)
        assert state["service"] is None
        assert state["date"] is None

    def test_reset_clears_empty_state(self):
        """get_direct_reply for 'reset' works even when state is empty."""
        user = "qa_reset_empty"
        reply = get_direct_reply(user, "reset")
        assert reply is not None

    def test_start_over_clears_state(self):
        user = "qa_start_over"
        update_booking_state(user, clinic_id=1, service="whitening")
        get_direct_reply(user, "start over")
        assert get_booking_state(user, clinic_id=1)["service"] is None

    def test_booking_record_preserved_after_reset(self):
        """'reset'/'restart'/'start over' clears booking state but NOT the booking record."""
        user = "qa_reset_keeps_record"
        save_booking_record(
            user=user, event_id="evt-keep", service="polishing", name="Patient",
            date="2026-04-15", time="11:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False,
        )
        get_direct_reply(user, "reset")
        # BookingRecordModel (Calendar event) must still exist
        assert get_existing_booking(user) is not None

    def test_slash_reset_clears_runtime_state_scoped_to_current_clinic(self):
        user = "qa_slash_reset_scope"
        clinic_1 = {"id": 1}
        clinic_2 = {"id": 2}

        update_booking_state(user, clinic_id=1, service="scaling", date="2026-04-10", time="10:00")
        update_booking_state(user, clinic_id=2, service="filling", date="2026-04-11", time="11:00")
        _set_pending_date_clarification(user, clinic_id=1)
        _set_pending_date_clarification(user, clinic_id=2)
        app.append_history(user, "user", "old c1", clinic_id=1)
        app.append_history(user, "assistant", "old c2", clinic_id=2)
        app.write_conversation_flag(1, user, "human_requested")
        app.write_conversation_flag(2, user, "human_requested")

        reply = get_direct_reply(user, "/reset", clinic_1)
        assert reply == "Your chat has been reset. We can start fresh now."

        state_1 = get_booking_state(user, clinic_id=1)
        state_2 = get_booking_state(user, clinic_id=2)
        assert state_1["service"] is None
        assert state_2["service"] == "filling"
        assert (user, 1) not in _PENDING_DATE_CLARIFICATIONS
        assert (user, 2) in _PENDING_DATE_CLARIFICATIONS

        with SessionLocal() as db:
            c1_messages = db.query(app.ConversationMessage).filter(
                app.ConversationMessage.user == user,
                app.ConversationMessage.clinic_id == 1,
            ).count()
            c2_messages = db.query(app.ConversationMessage).filter(
                app.ConversationMessage.user == user,
                app.ConversationMessage.clinic_id == 2,
            ).count()
            c1_flags = db.query(app.ConversationFlag).filter(
                app.ConversationFlag.clinic_id == 1,
                app.ConversationFlag.phone == user,
                app.ConversationFlag.resolved_at == None,  # noqa: E711
            ).count()
            c2_flags = db.query(app.ConversationFlag).filter(
                app.ConversationFlag.clinic_id == 2,
                app.ConversationFlag.phone == user,
                app.ConversationFlag.resolved_at == None,  # noqa: E711
            ).count()

        assert c1_messages == 0
        assert c2_messages == 1
        assert c1_flags == 0
        assert c2_flags == 1

    @patch("app.client.responses.create")
    def test_run_ai_reset_does_not_readd_history(self, mock_create):
        user = "qa_run_ai_slash_reset"
        app.append_history(user, "user", "previous user msg", clinic_id=1)
        app.append_history(user, "assistant", "previous assistant msg", clinic_id=1)

        reply = app.run_ai(user, "/reset", clinic={"id": 1})
        assert reply == "Your chat has been reset. We can start fresh now."
        mock_create.assert_not_called()

        with SessionLocal() as db:
            count = db.query(app.ConversationMessage).filter(
                app.ConversationMessage.user == user,
                app.ConversationMessage.clinic_id == 1,
            ).count()
        assert count == 0

    def test_no_auth_token_rejects_unsigned_webhook(self):
        """When TWILIO_AUTH_TOKEN is not set, the webhook fails closed (403)."""
        with patch("app.TWILIO_AUTH_TOKEN", None), \
             patch("app.ALLOW_UNSIGNED_WEBHOOKS", False), \
             patch("app.run_ai", return_value="ok") as mock_run_ai, \
             patch("app.get_clinic_by_twilio_number", return_value=None):
            with app.app.test_client() as c:
                response = c.post("/whatsapp", data={
                    "Body": "hello",
                    "From": "+60123456789",
                    "To": "+60100000000",
                })
        assert response.status_code == 403
        mock_run_ai.assert_not_called()

    def test_no_auth_token_skips_validation_with_explicit_dev_flag(self):
        """ALLOW_UNSIGNED_WEBHOOKS=1 is the only way to accept unsigned webhooks."""
        with patch("app.TWILIO_AUTH_TOKEN", None), \
             patch("app.ALLOW_UNSIGNED_WEBHOOKS", True), \
             patch("app.run_ai", return_value="ok"), \
             patch("app.get_clinic_by_twilio_number", return_value=None):
            with app.app.test_client() as c:
                response = c.post("/whatsapp", data={
                    "Body": "hello",
                    "From": "+60123456789",
                    "To": "+60100000000",
                })
        assert response.status_code == 200

    def test_invalid_signature_rejected_when_auth_token_set(self):
        """When TWILIO_AUTH_TOKEN is set, invalid signature must return 403."""
        with patch("app.TWILIO_AUTH_TOKEN", "test-secret-token"):
            with app.app.test_client() as c:
                response = c.post("/whatsapp", data={
                    "Body": "hello",
                    "From": "+60123456789",
                    "To": "+60100000000",
                })
        assert response.status_code == 403


# ===========================================================================
# Test 11: normalize_service — adversarial inputs
# ===========================================================================

class TestNormalizeServiceAdversarial:

    def test_none_returns_empty(self):
        assert normalize_service(None) == ""

    def test_empty_string_returns_empty(self):
        assert normalize_service("") == ""

    def test_extra_whitespace(self):
        assert normalize_service("  cleaning  ") == "scaling"

    def test_mixed_case_alias(self):
        assert normalize_service("Teeth Cleaning") == "scaling"

    def test_number_passes_through(self):
        assert normalize_service("123") == "123"

    def test_very_long_string_does_not_crash(self):
        long = "a" * 10000
        result = normalize_service(long)
        assert result == long

    def test_sql_injection_style_passes_through_safely(self):
        injection = "scaling'; DROP TABLE clinics; --"
        assert normalize_service(injection) == injection.lower()


# ===========================================================================
# Test 12: is_reset_command — adversarial inputs
# ===========================================================================

class TestResetCommandAdversarial:

    def test_cancel_alone_triggers_reset(self):
        assert is_reset_command("cancel") is True

    def test_cancel_my_appointment_does_not_trigger_reset(self):
        assert is_reset_command("cancel my appointment") is False

    def test_please_cancel_does_not_trigger_reset(self):
        assert is_reset_command("please cancel") is False

    def test_start_over(self):
        assert is_reset_command("start over") is True

    def test_restart_caps(self):
        assert is_reset_command("RESTART") is True

    def test_reset_with_trailing_space(self):
        assert is_reset_command("reset ") is True

    def test_whitespace_only(self):
        assert is_reset_command("   ") is False

    def test_empty_string(self):
        assert is_reset_command("") is False

    def test_partial_word_no_match(self):
        assert is_reset_command("res") is False

    def test_random_sentence(self):
        assert is_reset_command("I want to book a scaling") is False


# ===========================================================================
# Test 13: resolve_booking_datetime — adversarial inputs
# ===========================================================================

class TestResolveDateTimeAdversarial:

    @patch("app.now_local", return_value=datetime(2026, 3, 26, 9, 0, tzinfo=TZ))
    def test_tmr_resolves_to_tomorrow(self, _):
        result = resolve_booking_datetime("tmr", "10:00")
        assert result["ok"] is True
        assert result["date"] == "2026-03-27"

    @patch("app.now_local", return_value=datetime(2026, 3, 26, 9, 0, tzinfo=TZ))
    def test_tmrw_resolves_to_tomorrow(self, _):
        result = resolve_booking_datetime("tmrw", "10:00")
        assert result["ok"] is True
        assert result["date"] == "2026-03-27"

    def test_3pm_resolves_to_1500(self):
        result = resolve_booking_datetime("2026-04-10", "3pm")
        assert result["ok"] is True
        assert result["time"] == "15:00"

    def test_9am_with_dot_notation(self):
        result = resolve_booking_datetime("2026-04-10", "9.00am")
        assert result["ok"] is True
        assert result["time"] == "09:00"

    def test_12pm_resolves_to_noon(self):
        result = resolve_booking_datetime("2026-04-10", "12pm")
        assert result["ok"] is True
        assert result["time"] == "12:00"

    def test_12am_resolves_to_midnight(self):
        result = resolve_booking_datetime("2026-04-10", "12am")
        assert result["ok"] is True
        assert result["time"] == "00:00"

    def test_noon_resolves_correctly(self):
        result = resolve_booking_datetime("2026-04-10", "noon")
        assert result["ok"] is True
        assert result["time"] == "12:00"

    def test_garbage_date_returns_error(self):
        result = resolve_booking_datetime("not-a-date", "10:00")
        assert result["ok"] is False

    def test_garbage_time_returns_error(self):
        result = resolve_booking_datetime("2026-04-10", "banana")
        assert result["ok"] is False

    def test_empty_date_returns_error(self):
        result = resolve_booking_datetime("", "10:00")
        assert result["ok"] is False

    def test_empty_time_returns_error(self):
        result = resolve_booking_datetime("2026-04-10", "")
        assert result["ok"] is False

    def test_past_slot_is_flagged(self):
        result = resolve_booking_datetime("2020-01-01", "10:00")
        assert result["ok"] is True
        assert result["is_past"] is True

    def test_future_slot_not_flagged_as_past(self):
        result = resolve_booking_datetime("2099-01-01", "10:00")
        assert result["ok"] is True
        assert result["is_past"] is False

    @patch("app.now_local", return_value=datetime(2026, 3, 30, 9, 0, tzinfo=TZ))
    def test_sunday_resolves_but_booking_rejected_later(self, _):
        """resolve_booking_datetime itself does not reject Sundays — check_availability does."""
        result = resolve_booking_datetime("2026-04-05", "10:00")  # Sunday
        assert result["ok"] is True  # resolve succeeds
        # check_availability will reject it — but that is a separate step


# ===========================================================================
# Test 14: check_availability — adversarial inputs
# ===========================================================================

_AVAIL_FUTURE = datetime(2026, 3, 30, 9, 0, tzinfo=TZ)  # Monday


class TestCheckAvailabilityAdversarial:

    @patch("app.now_local", return_value=_AVAIL_FUTURE)
    def test_unknown_service_rejected(self, _):
        result = check_availability("root canal", "2026-03-31", "10:00")
        assert result["ok"] is False

    @patch("app.now_local", return_value=_AVAIL_FUTURE)
    def test_sunday_rejected(self, _):
        # 2026-04-05 is Sunday
        result = check_availability("scaling", "2026-04-05", "10:00")
        assert result["ok"] is False

    @patch("app.now_local", return_value=_AVAIL_FUTURE)
    def test_past_slot_rejected(self, _):
        result = check_availability("scaling", "2020-01-01", "10:00")
        assert result["ok"] is False
        assert "past" in result["message"].lower()

    def test_malformed_date_rejected(self):
        result = check_availability("scaling", "not-a-date", "10:00")
        assert result["ok"] is False

    def test_malformed_time_rejected(self):
        result = check_availability("scaling", "2026-04-10", "99:99")
        assert result["ok"] is False

    @patch("app.now_local", return_value=_AVAIL_FUTURE)
    def test_closure_date_always_rejected(self, _):
        clinic_with_closure = {
            **get_default_clinic(),
            "special_closures": ["2026-03-31"],
            "closure_notes": {"2026-03-31": "Closed for training"},
        }
        result = check_availability("scaling", "2026-03-31", "10:00", clinic_with_closure)
        assert result["ok"] is False
        assert "closed" in result["message"].lower() or "training" in result["message"].lower()

    @patch("app.now_local", return_value=_AVAIL_FUTURE)
    @patch("app.get_calendar")
    def test_slot_starting_at_open_accepted(self, mock_get_cal, _):
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = check_availability("polishing", "2026-03-31", "10:00")
        assert result["ok"] is True

    @patch("app.now_local", return_value=_AVAIL_FUTURE)
    @patch("app.get_calendar")
    def test_slot_ending_exactly_at_close_accepted(self, mock_get_cal, _):
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        # scaling=60min, 17:00→18:00 exactly at close
        result = check_availability("scaling", "2026-03-31", "17:00")
        assert result["ok"] is True

    @patch("app.now_local", return_value=_AVAIL_FUTURE)
    def test_slot_already_today_but_after_close_rejected(self, _now):
        # Time 19:00 is after close
        result = check_availability("scaling", "2026-03-30", "19:00")
        assert result["ok"] is False

    @patch("app.now_local", return_value=_AVAIL_FUTURE)
    @patch("app.get_calendar")
    def test_service_alias_resolved_before_availability(self, mock_get_cal, _):
        """'cleaning' alias must be resolved to 'scaling' before checking availability."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = check_availability("cleaning", "2026-03-31", "10:00")
        assert result["ok"] is True
        assert result["service"] == "scaling"


# ===========================================================================
# Test 15: find_next_available_slot
# ===========================================================================

_FIND_FUTURE = datetime(2026, 3, 30, 9, 0, tzinfo=TZ)


class TestFindNextAvailableSlot:

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_same_day_next_slot_returned(self, mock_get_cal, _):
        """When the starting slot is free, it must be returned immediately."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = find_next_available_slot("polishing", "2026-03-31", "10:00")
        assert result["ok"] is True
        assert result["date"] == "2026-03-31"
        assert result["time"] == "10:00"

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_full_day_advances_to_next_day(self, mock_get_cal, _):
        """When a day is fully booked, scan must advance to the next day."""
        # Return a booked event spanning the whole day
        def list_side_effect(**kwargs):
            time_min = kwargs.get("timeMin", "")
            mock_result = MagicMock()
            mock_result.execute.return_value = {
                "items": [
                    {
                        "start": {"dateTime": "2026-03-31T10:00:00+08:00"},
                        "end": {"dateTime": "2026-03-31T18:00:00+08:00"},
                    }
                ]
            }
            return mock_result

        mock_events = MagicMock()
        mock_events.list.side_effect = list_side_effect
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = find_next_available_slot("polishing", "2026-03-31", "10:00")
        assert result["ok"] is True
        # Must be on the next available day (not Sunday 2026-04-05)
        assert result["date"] != "2026-03-31"

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_sunday_skipped(self, mock_get_cal, _):
        """Scan must skip Sundays."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        # Start from Saturday — next day is Sunday which must be skipped
        result = find_next_available_slot("polishing", "2026-04-04", "17:31")
        assert result["ok"] is True
        assert result["date"] != "2026-04-05"  # Not Sunday

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_closure_date_skipped(self, mock_get_cal, _):
        """Scan must skip dates listed in special_closures."""
        clinic = {**get_default_clinic(), "special_closures": ["2026-03-31"]}
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = find_next_available_slot("polishing", "2026-03-31", "10:00", clinic)
        assert result["ok"] is True
        assert result["date"] != "2026-03-31"

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_multiple_bookings_returns_first_gap(self, mock_get_cal, _):
        """When there are gaps between bookings, the first free slot is returned."""
        # Block 10:00–11:00 and 11:30–12:30; gap at 11:00–11:30
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {
            "items": [
                {
                    "start": {"dateTime": "2026-03-31T10:00:00+08:00"},
                    "end": {"dateTime": "2026-03-31T11:00:00+08:00"},
                },
                {
                    "start": {"dateTime": "2026-03-31T11:30:00+08:00"},
                    "end": {"dateTime": "2026-03-31T12:30:00+08:00"},
                },
            ]
        }
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = find_next_available_slot("polishing", "2026-03-31", "10:00")
        assert result["ok"] is True
        assert result["time"] == "11:00"

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_no_slots_in_7_days_returns_error(self, mock_get_cal, _):
        """Exhausting 7 days must return ok=False."""
        mock_events = MagicMock()
        # Single event spanning the entire 7-day search window blocks all slots.
        mock_events.list.return_value.execute.return_value = {
            "items": [
                {
                    "start": {"dateTime": "2026-03-31T08:00:00+08:00"},
                    "end": {"dateTime": "2026-04-10T20:00:00+08:00"},
                }
            ]
        }
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = find_next_available_slot("polishing", "2026-03-31", "10:00")
        assert result["ok"] is False

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_dispatch_sets_availability_ok_on_success(self, mock_get_cal, _):
        """dispatch_tool find_next_available_slot must set availability_ok=True on success."""
        user = "qa_find_avail"
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = dispatch_tool(
            "find_next_available_slot",
            {"service": "polishing", "date": "2026-03-31", "time": "10:00"},
            user,
        )
        assert result["ok"] is True
        assert get_booking_state(user)["availability_ok"] is True


# ===========================================================================
# Test 16: get_available_slots
# ===========================================================================

class TestGetAvailableSlots:

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_empty_day_returns_first_five_slots(self, mock_get_cal, _):
        """Empty calendar day must return up to 5 slots starting from open."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = get_available_slots("polishing", "2026-03-31")
        assert result["ok"] is True
        assert len(result["slots"]) == 5
        assert result["slots"][0]["time"] == "10:00"

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_booked_slots_are_excluded(self, mock_get_cal, _):
        """A booked 10:00–11:00 slot must not appear in available slots."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {
            "items": [
                {
                    "start": {"dateTime": "2026-03-31T10:00:00+08:00"},
                    "end": {"dateTime": "2026-03-31T11:00:00+08:00"},
                }
            ]
        }
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = get_available_slots("scaling", "2026-03-31")
        assert result["ok"] is True
        times = [s["time"] for s in result["slots"]]
        assert "10:00" not in times

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_fully_booked_day_returns_no_slots(self, mock_get_cal, _):
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {
            "items": [
                {
                    "start": {"dateTime": "2026-03-31T08:00:00+08:00"},
                    "end": {"dateTime": "2026-03-31T20:00:00+08:00"},
                }
            ]
        }
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = get_available_slots("scaling", "2026-03-31")
        assert result["ok"] is False

    def test_sunday_returns_closed(self):
        result = get_available_slots("polishing", "2026-04-05")  # Sunday
        assert result["ok"] is False
        assert "sunday" in result["message"].lower()

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_allday_event_does_not_block_slots(self, mock_get_cal, _):
        """All-day calendar events (date not dateTime) must not block time slots."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {
            "items": [
                {
                    "start": {"date": "2026-03-31"},
                    "end": {"date": "2026-04-01"},
                }
            ]
        }
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = get_available_slots("polishing", "2026-03-31")
        assert result["ok"] is True
        assert len(result["slots"]) == 5

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_90min_service_excludes_overlapping_slots(self, mock_get_cal, _):
        """A 60-min booking at 10:00 must block the whitening (90-min) slot at 10:00."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {
            "items": [
                {
                    "start": {"dateTime": "2026-03-31T10:00:00+08:00"},
                    "end": {"dateTime": "2026-03-31T11:00:00+08:00"},
                }
            ]
        }
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = get_available_slots("whitening", "2026-03-31")
        assert result["ok"] is True
        times = [s["time"] for s in result["slots"]]
        assert "10:00" not in times

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_dispatch_tool_get_available_slots(self, mock_get_cal, _):
        """dispatch_tool for get_available_slots must not set availability_ok=True."""
        user = "qa_avail_slots"
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = dispatch_tool(
            "get_available_slots",
            {"service": "polishing", "date": "2026-03-31"},
            user,
        )
        assert result["ok"] is True
        assert get_booking_state(user)["availability_ok"] is False


# ===========================================================================
# Test 17: get_available_slots — adversarial
# ===========================================================================

class TestGetAvailableSlotsAdversarial:

    def test_closure_date_returns_closed(self):
        clinic = {**get_default_clinic(), "special_closures": ["2026-03-31"]}
        result = get_available_slots("scaling", "2026-03-31", clinic)
        assert result["ok"] is False

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_returns_at_most_5_slots(self, mock_get_cal, _):
        """get_available_slots must return at most 5 slots regardless of day size."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = get_available_slots("polishing", "2026-03-31")
        assert result["ok"] is True
        assert len(result["slots"]) <= 5

    @patch("app.now_local", return_value=datetime(2026, 3, 31, 19, 0, tzinfo=TZ))
    @patch("app.get_calendar")
    def test_today_after_close_returns_no_slots(self, mock_get_cal, _now):
        """When now is after closing time, same-day must return no slots."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = get_available_slots("polishing", "2026-03-31")
        assert result["ok"] is False

    @patch("app.now_local", return_value=datetime(2026, 3, 31, 17, 35, tzinfo=TZ))
    @patch("app.get_calendar")
    def test_today_near_close_only_returns_fitting_slots(self, mock_get_cal, _now):
        """Near close of business, only slots that fit within hours are returned."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        # Now=17:35, polishing=30min, last valid slot is 17:30 → already past scan_from
        result = get_available_slots("polishing", "2026-03-31")
        if result["ok"]:
            for slot in result["slots"]:
                h, m = map(int, slot["time"].split(":"))
                end_m = h * 60 + m + 30
                assert end_m <= 18 * 60


# ===========================================================================
# Test 18: Next earliest slot logic
# ===========================================================================

class TestNextEarliestSlotLogic:

    def test_prompt_has_path_c_for_next_earliest_slot(self):
        prompt = build_system_prompt("qa_path_c")
        assert "Path C" in prompt or "earliest" in prompt.lower()

    def test_prompt_instructs_tomorrow_as_starting_day_for_next_earliest(self):
        prompt = build_system_prompt("qa_tomorrow_start")
        assert "tomorrow" in prompt.lower()

    def test_prompt_demotes_find_next_to_last_resort(self):
        prompt = build_system_prompt("qa_last_resort")
        assert "last resort" in prompt.lower()

    def test_prompt_says_do_not_use_find_next_when_day_resolved(self):
        prompt = build_system_prompt("qa_no_find_next")
        assert "find_next_available_slot" in prompt

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_get_available_slots_prefers_morning_for_next_day(self, mock_get_cal, _):
        """get_available_slots starting from open_hour returns morning slots first."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = get_available_slots("polishing", "2026-03-31")
        assert result["ok"] is True
        first_hour = int(result["slots"][0]["time"].split(":")[0])
        assert first_hour == 10  # Clinic opens at 10

    @patch("app.now_local", return_value=datetime(2026, 3, 31, 14, 0, tzinfo=TZ))
    @patch("app.get_calendar")
    def test_thursday_still_shows_morning_slots_despite_afternoon_bookings(
        self, mock_get_cal, _now
    ):
        """Afternoon bookings must not affect morning slot availability next day."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {
            "items": [
                {
                    "start": {"dateTime": "2026-04-01T14:00:00+08:00"},
                    "end": {"dateTime": "2026-04-01T15:00:00+08:00"},
                }
            ]
        }
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = get_available_slots("polishing", "2026-04-01")
        assert result["ok"] is True
        times = [s["time"] for s in result["slots"]]
        assert "10:00" in times


# ===========================================================================
# Test 19: booking state adversarial
# ===========================================================================

class TestBookingStateAdversarial:

    def test_create_booking_blocked_without_availability_check(self):
        """dispatch_tool create_booking must refuse without availability_ok=True."""
        user = "qa_no_avail"
        update_booking_state(user, availability_ok=False)
        result = dispatch_tool(
            "create_booking",
            {"name": "Test", "service": "polishing", "date": "2099-04-10", "time": "10:00"},
            user,
        )
        assert result["ok"] is False
        assert "availability" in result["message"].lower()

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_stale_availability_ok_cleared_on_date_change(self, mock_get_cal, _):
        """Changing date via resolve_booking_datetime clears stale availability_ok."""
        user = "qa_stale_avail"
        update_booking_state(user, date="2099-04-07", time="10:00",
                             service="scaling", availability_ok=True)

        dispatch_tool(
            "resolve_booking_datetime",
            {"date_text": "2099-04-08", "time_text": "10:00"},
            user,
        )
        assert get_booking_state(user)["availability_ok"] is False

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_availability_ok_reset_after_failed_check(self, mock_get_cal, _):
        """A failed check_availability must reset availability_ok to False."""
        user = "qa_failed_check"
        update_booking_state(user, availability_ok=True)
        # Past slot → check will fail
        dispatch_tool(
            "check_availability",
            {"service": "scaling", "date": "2020-01-01", "time": "10:00"},
            user,
        )
        assert get_booking_state(user)["availability_ok"] is False

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_booking_state_cleared_after_successful_booking(self, mock_get_cal, _):
        """dispatch_tool create_booking must clear booking state on success."""
        user = "qa_state_cleared"
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "evt-state-clear"}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        update_booking_state(user, service="polishing", date="2026-03-31",
                             time="10:00", availability_ok=True)

        result = dispatch_tool(
            "create_booking",
            {"name": "Patient", "service": "polishing", "date": "2026-03-31", "time": "10:00"},
            user,
        )
        assert result["ok"] is True
        state = get_booking_state(user)
        assert state["service"] is None
        assert state["availability_ok"] is False

    @patch("app.now_local", return_value=_FIND_FUTURE)
    def test_check_date_available_persists_resolved_date(self, _):
        user = "qa_date_state_persist"
        result = dispatch_tool(
            "check_date_available",
            {"date_text": "next week monday"},
            user,
        )
        assert result["ok"] is True
        state = get_booking_state(user)
        assert state["date"] == "2026-04-06"
        assert state["time"] is None
        assert state["availability_ok"] is False

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_check_availability_accepts_time_with_confirmation_suffix(self, mock_get_cal, _):
        user = "qa_time_suffix_check"
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = dispatch_tool(
            "check_availability",
            {"service": "whitening", "date": "next week monday", "time": "10am yes"},
            user,
        )
        assert result["ok"] is True
        assert result["date"] == "2026-04-06"
        assert result["time"] == "10:00"

        state = get_booking_state(user)
        assert state["service"] == "whitening"
        assert state["date"] == "2026-04-06"
        assert state["time"] == "10:00"
        assert state["availability_ok"] is True


# ===========================================================================
# Test 20: Auto-reschedule on new booking (called directly, not via dispatch_tool)
# ===========================================================================

class TestAutoRescheduleOnNewBooking:
    """
    create_booking (called directly) auto-deletes the prior Calendar event
    when a user re-books. The overwrite guard is in dispatch_tool only, so
    direct calls still exercise this code path.
    """

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_new_booking_deletes_prior_calendar_event(self, mock_get_cal, _):
        """create_booking must delete the prior event for same patient before inserting."""
        phone = "+60191001"
        save_booking_record(
            user=phone, event_id="evt-prior", service="scaling", name="Patient A",
            date="2026-03-31", time="10:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False,
            clinic_id=1,
        )
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "evt-new"}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        # Book for same patient (Patient A)
        result = create_booking(
            name="Patient A", service="polishing", date="2026-03-31",
            time="14:00", phone=phone,
        )
        assert result["ok"] is True
        mock_events.delete.assert_called_once()
        delete_kwargs = mock_events.delete.call_args[1]
        assert delete_kwargs["eventId"] == "evt-prior"

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_new_booking_replaces_db_record(self, mock_get_cal, _):
        """After rebooking, the DB record must reflect the new service and time."""
        phone = "+60191002"
        save_booking_record(
            user=phone, event_id="evt-old", service="scaling", name="Patient",
            date="2026-03-31", time="10:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False,
        )
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "evt-new2"}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        create_booking(
            name="Patient", service="polishing", date="2026-03-31",
            time="14:00", phone=phone,
        )
        record = get_existing_booking(phone)
        assert record["service"] == "polishing"
        assert record["time"] == "14:00"
        assert record["event_id"] == "evt-new2"

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_new_booking_resets_reminder_flags(self, mock_get_cal, _):
        """Rebooking must reset reminder_1d_sent and reminder_2h_sent to False."""
        phone = "+60191003"
        save_booking_record(
            user=phone, event_id="evt-remind-old", service="scaling", name="Patient",
            date="2026-03-31", time="10:00", status="Confirmed",
            reminder_1d_sent=True, reminder_2h_sent=True,
        )
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "evt-remind-new"}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        create_booking(
            name="Patient", service="polishing", date="2026-04-01",
            time="10:00", phone=phone,
        )
        record = get_existing_booking(phone)
        assert record["reminder_1d_sent"] is False
        assert record["reminder_2h_sent"] is False

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_no_prior_booking_creates_normally(self, mock_get_cal, _):
        """create_booking without prior record must succeed and not call delete."""
        phone = "+60191004"
        assert get_existing_booking(phone) is None

        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "evt-fresh"}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = create_booking(
            name="New Patient", service="polishing", date="2026-03-31",
            time="10:00", phone=phone,
        )
        assert result["ok"] is True
        mock_events.delete.assert_not_called()

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_prior_event_404_does_not_block_new_booking(self, mock_get_cal, _):
        """If prior Calendar event is already gone (404), new booking must still succeed."""
        phone = "+60191005"
        save_booking_record(
            user=phone, event_id="evt-gone", service="scaling", name="Patient",
            date="2026-03-31", time="10:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False,
        )
        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_events = MagicMock()
        mock_events.delete.return_value.execute.side_effect = HttpError(
            resp=mock_resp, content=b"Not Found"
        )
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "evt-after-404"}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = create_booking(
            name="Patient", service="polishing", date="2026-04-01",
            time="10:00", phone=phone,
        )
        assert result["ok"] is True

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_cross_clinic_does_not_delete_other_clinic_calendar(self, mock_get_cal, _):
        """A booking at Clinic A must NOT cause deletion from Clinic A's calendar
        when a new booking is made at Clinic B — cross-clinic isolation prevents this.

        With CRITICAL-3 fixed, Clinic B cannot see Clinic A's prior booking, so no
        delete attempt is made against Clinic A's calendar. Clinic B simply inserts
        a new event into its own calendar.
        """
        phone = "+60191006"
        save_booking_record(
            user=phone, event_id="evt-clinic-a-cross", service="scaling", name="Patient",
            date="2026-03-31", time="10:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False, clinic_id=_CLINIC_A["id"],
        )
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "evt-clinic-b-new"}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        # Booking at Clinic B — must NOT touch Clinic A's calendar.
        result = create_booking(
            name="Patient", service="polishing", date="2026-04-01",
            time="10:00", phone=phone, clinic=_CLINIC_B,
        )

        assert result["ok"] is True
        # Calendar delete must NOT have been called (Clinic B can't see Clinic A's booking).
        mock_events.delete.assert_not_called()
        # Insert must have been called once for the new Clinic B booking.
        mock_events.insert.assert_called_once()


# ===========================================================================
# Test 21: Availability bug fixes — all-day events, whitening edge cases
# ===========================================================================

class TestAvailabilityBugFixes:

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_allday_event_does_not_block_check_availability(self, mock_get_cal, _):
        """All-day events (date key, not dateTime) must not block check_availability."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {
            "items": [{"start": {"date": "2026-03-31"}, "end": {"date": "2026-04-01"}}]
        }
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = check_availability("polishing", "2026-03-31", "10:00")
        assert result["ok"] is True

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_allday_event_does_not_block_find_next_available_slot(self, mock_get_cal, _):
        """All-day events must not block find_next_available_slot."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {
            "items": [{"start": {"date": "2026-03-31"}, "end": {"date": "2026-04-01"}}]
        }
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = find_next_available_slot("polishing", "2026-03-31", "10:00")
        assert result["ok"] is True
        assert result["date"] == "2026-03-31"
        assert result["time"] == "10:00"

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_whitening_availability_within_hours(self, mock_get_cal, _):
        """Whitening (90min) starting at 16:00 ends at 17:30 — within hours."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = check_availability("whitening", "2026-03-31", "16:00")
        assert result["ok"] is True

    @patch("app.now_local", return_value=_FIND_FUTURE)
    def test_whitening_rejected_when_ends_after_close(self, _):
        """Whitening (90min) starting at 16:31 ends at 18:01 — must be rejected."""
        result = check_availability("whitening", "2026-03-31", "16:31")
        assert result["ok"] is False

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_whitening_find_next_skips_full_90min_conflict(self, mock_get_cal, _):
        """A 60-min booking at 10:00 partially overlaps whitening's 90-min window."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {
            "items": [
                {
                    "start": {"dateTime": "2026-03-31T10:00:00+08:00"},
                    "end": {"dateTime": "2026-03-31T11:00:00+08:00"},
                }
            ]
        }
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = find_next_available_slot("whitening", "2026-03-31", "10:00")
        assert result["ok"] is True
        assert result["time"] != "10:00"  # 10:00 is blocked by the existing 60-min event

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_both_functions_agree_on_timed_conflict(self, mock_get_cal, _):
        """check_availability and find_next_available_slot must both see the same conflict."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {
            "items": [
                {
                    "start": {"dateTime": "2026-03-31T10:00:00+08:00"},
                    "end": {"dateTime": "2026-03-31T11:00:00+08:00"},
                }
            ]
        }
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        avail_result = check_availability("scaling", "2026-03-31", "10:00")
        assert avail_result["ok"] is False

        find_result = find_next_available_slot("scaling", "2026-03-31", "10:00")
        assert find_result["ok"] is True
        assert find_result["time"] != "10:00"


# ===========================================================================
# Test 22: cancel_booking atomicity
# ===========================================================================

class TestCancelAtomicity:

    def test_cancel_no_booking_returns_error(self):
        """cancel_booking with no record must return ok=False."""
        result = cancel_booking("+60199000001")
        assert result["ok"] is False
        assert "no booking" in result["message"].lower()

    @patch("app.get_calendar")
    def test_cancel_proceeds_after_calendar_failure(self, mock_get_cal):
        """If Calendar delete fails, cancel_booking must still clean up the DB record."""
        phone = "+60199000002"
        save_booking_record(
            user=phone, event_id="evt-cal-fail", service="scaling", name="Patient",
            date="2026-04-10", time="10:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False,
            clinic_id=1,
        )
        mock_events = MagicMock()
        mock_events.delete.return_value.execute.side_effect = Exception("calendar error")
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        # Should not raise even though calendar failed
        cancel_booking(phone)
        # DB record should be cleaned up
        assert get_existing_booking(phone) is None


# ===========================================================================
# Test 23: create_booking atomicity — Calendar succeeds but DB write fails
# ===========================================================================

class TestCreateBookingAtomicity:

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_calendar_event_created_before_db_fails(self, mock_get_cal, _):
        """If save_booking_record raises, create_booking returns ok=False."""
        phone = "+60199100001"
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "evt-atomicity"}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        with patch("app.save_booking_record", side_effect=Exception("DB error")):
            result = create_booking(
                name="Patient", service="polishing", date="2026-03-31",
                time="10:00", phone=phone,
            )

        assert result["ok"] is False


# ===========================================================================
# Test 24: reschedule_booking atomicity
# ===========================================================================

class TestRescheduleAtomicity:

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_reschedule_resets_reminder_flags(self, mock_get_cal, _):
        """reschedule_booking must reset both reminder flags to False."""
        phone = "+60199200001"
        save_booking_record(
            user=phone, event_id="evt-reschedule", service="scaling", name="Patient",
            date="2026-03-31", time="10:00", status="Confirmed",
            reminder_1d_sent=True, reminder_2h_sent=True,
            clinic_id=1,
        )
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.get.return_value.execute.return_value = {
            "summary": "[Confirmed] Scaling - Patient",
            "start": {"dateTime": "2026-03-31T10:00:00+08:00", "timeZone": "Asia/Kuala_Lumpur"},
            "end": {"dateTime": "2026-03-31T11:00:00+08:00", "timeZone": "Asia/Kuala_Lumpur"},
        }
        mock_events.update.return_value.execute.return_value = {}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = reschedule_booking(phone=phone, service="scaling",
                                    date="2026-04-01", time="10:00")
        assert result["ok"] is True
        record = get_existing_booking(phone)
        assert record["reminder_1d_sent"] is False
        assert record["reminder_2h_sent"] is False

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_reschedule_calendar_succeeds_db_fails_returns_error(self, mock_get_cal, _):
        """If save_booking_record raises during reschedule, it returns ok=False."""
        phone = "+60199200002"
        save_booking_record(
            user=phone, event_id="evt-reschedule-db", service="scaling", name="Patient",
            date="2026-03-31", time="10:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False,
        )
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.get.return_value.execute.return_value = {
            "summary": "[Confirmed] Scaling - Patient",
            "start": {"dateTime": "2026-03-31T10:00:00+08:00", "timeZone": "Asia/Kuala_Lumpur"},
            "end": {"dateTime": "2026-03-31T11:00:00+08:00", "timeZone": "Asia/Kuala_Lumpur"},
        }
        mock_events.update.return_value.execute.return_value = {}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        with patch("app.save_booking_record", side_effect=Exception("DB error")):
            result = reschedule_booking(phone=phone, service="scaling",
                                        date="2026-04-01", time="10:00")
        assert result["ok"] is False


# ===========================================================================
# Test 25: Reminder adversarial
# ===========================================================================

class TestReminderAdversarial:

    NOW = datetime(2026, 4, 10, 10, 0, tzinfo=TZ)

    def test_1d_reminder_boundary_lower(self):
        assert should_send_1d_reminder(self.NOW + timedelta(hours=20), self.NOW) is True

    def test_1d_reminder_boundary_upper(self):
        assert should_send_1d_reminder(self.NOW + timedelta(hours=28), self.NOW) is True

    def test_2h_reminder_boundary_lower(self):
        assert should_send_2h_reminder(self.NOW + timedelta(minutes=90), self.NOW) is True

    def test_2h_reminder_boundary_upper(self):
        assert should_send_2h_reminder(self.NOW + timedelta(minutes=150), self.NOW) is True

    def test_reminder_not_sent_for_past_appointment(self):
        past = self.NOW - timedelta(hours=1)
        assert should_send_1d_reminder(past, self.NOW) is False
        assert should_send_2h_reminder(past, self.NOW) is False

    def test_reminder_not_sent_when_appointment_too_far_ahead(self):
        far_future = self.NOW + timedelta(days=7)
        assert should_send_1d_reminder(far_future, self.NOW) is False
        assert should_send_2h_reminder(far_future, self.NOW) is False

    def test_2h_reminder_not_sent_for_appointment_tomorrow(self):
        """2h reminder must not fire for appointments 24h away."""
        tomorrow = self.NOW + timedelta(hours=24)
        assert should_send_2h_reminder(tomorrow, self.NOW) is False

    def test_1d_and_2h_windows_do_not_overlap(self):
        """The 1d and 2h windows must not overlap."""
        for minutes_ahead in range(1, 200):
            dt = self.NOW + timedelta(minutes=minutes_ahead)
            both = should_send_1d_reminder(dt, self.NOW) and should_send_2h_reminder(dt, self.NOW)
            assert not both, f"Windows overlap at {minutes_ahead} min ahead"


# ===========================================================================
# Test 26: Short notice booking
# ===========================================================================

_CLINIC_ADV = {
    "id": 1,
    "name": "Glow Dental Clinic",
    "location": "Kuala Lumpur",
    "timezone": "Asia/Kuala_Lumpur",
    "open_hour": 10,
    "close_hour": 18,
    "hours_text": "Monday to Saturday, 10:00 to 18:00. Closed Sunday.",
    "slot_minutes": 30,
    "opening_message": "",
    "promo_message": "",
    "services": {"scaling": 60, "polishing": 30, "filling": 60,
                 "braces consultation": 60, "whitening": 90},
    "service_prices": {},
    "special_closures": [],
    "closure_notes": {},
    "google_calendar_id": "primary",
    "twilio_number": None,
}


class TestShortNoticeBooking:

    def test_booking_under_threshold_is_short_notice(self):
        """A booking < 24h from now must set short_notice=True."""
        now = datetime(2026, 4, 8, 9, 0, tzinfo=TZ)
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "ev-sn1"}

        with patch("app.get_calendar") as mock_cal, \
             patch("app.now_local", return_value=now), \
             patch("app.get_existing_booking", return_value=None), \
             patch("app.save_booking_record"):
            mock_cal.return_value = MagicMock()
            mock_cal.return_value.events.return_value = mock_events
            result = create_booking("Patient", "polishing", "2026-04-08", "16:00",
                                    "+60190006")

        assert result["ok"] is True
        assert result["short_notice"] is True

    def test_booking_over_threshold_is_not_short_notice(self):
        """A booking > 24h from now must set short_notice=False."""
        now = datetime(2026, 4, 8, 9, 0, tzinfo=TZ)
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "ev-sn2"}

        with patch("app.get_calendar") as mock_cal, \
             patch("app.now_local", return_value=now), \
             patch("app.get_existing_booking", return_value=None), \
             patch("app.save_booking_record"):
            mock_cal.return_value = MagicMock()
            mock_cal.return_value.events.return_value = mock_events
            result = create_booking("Patient", "polishing", "2026-04-10", "10:00",
                                    "+60190007")

        assert result["ok"] is True
        assert result["short_notice"] is False

    def test_booking_exactly_at_threshold_is_not_short_notice(self):
        """A booking exactly 24h from now is not short-notice (>= not <)."""
        now = datetime(2026, 4, 8, 10, 0, tzinfo=TZ)
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "ev-sn3"}

        with patch("app.get_calendar") as mock_cal, \
             patch("app.now_local", return_value=now), \
             patch("app.get_existing_booking", return_value=None), \
             patch("app.save_booking_record"):
            mock_cal.return_value = MagicMock()
            mock_cal.return_value.events.return_value = mock_events
            result = create_booking("Patient", "polishing", "2026-04-09", "10:00",
                                    "+60190009")

        assert result["ok"] is True
        assert result["short_notice"] is False

    def test_custom_threshold_respected(self):
        """Clinic with short_notice_hours=48 must flag 36h-ahead booking as short-notice."""
        clinic_48h = {**get_default_clinic(), "short_notice_hours": 48}
        now = datetime(2026, 4, 8, 10, 0, tzinfo=TZ)
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "ev-sn4"}

        with patch("app.get_calendar") as mock_cal, \
             patch("app.now_local", return_value=now), \
             patch("app.get_existing_booking", return_value=None), \
             patch("app.save_booking_record"):
            mock_cal.return_value = MagicMock()
            mock_cal.return_value.events.return_value = mock_events
            # 36h ahead of 2026-04-08 10:00 → 2026-04-09 22:00 is outside hours;
            # use 2026-04-09 16:00 (30h ahead) which is still < 48h threshold.
            result = create_booking("Patient", "polishing", "2026-04-09", "16:00",
                                    "+60190010", clinic=clinic_48h)

        assert result["ok"] is True
        assert result["short_notice"] is True

    def test_prompt_contains_short_notice_confirmation_wording(self):
        """System prompt must contain instructions for short-notice booking confirmation."""
        prompt = build_system_prompt("qa_sn_prompt")
        assert "short_notice" in prompt or "short notice" in prompt.lower()

    def test_prompt_contains_normal_confirmation_wording(self):
        prompt = build_system_prompt("qa_normal_prompt")
        assert "look forward to seeing you" in prompt.lower()

    def test_prompt_keys_short_notice_on_result_field(self):
        prompt = build_system_prompt("qa_sn_field")
        assert "short_notice=true" in prompt.lower() or "short_notice" in prompt


# ===========================================================================
# Test 27: Short notice adversarial
# ===========================================================================

class TestShortNoticeAdversarial:

    @patch("app.now_local", return_value=datetime(2026, 4, 8, 9, 0, tzinfo=TZ))
    def test_short_notice_flag_false_for_2_day_ahead(self, _):
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "ev-sn"}

        with patch("app.get_calendar") as mock_cal, \
             patch("app.get_existing_booking", return_value=None), \
             patch("app.save_booking_record"):
            mock_cal.return_value = MagicMock()
            mock_cal.return_value.events.return_value = mock_events
            # 2 days ahead: 2026-04-10 10:00 (49h from 2026-04-08 09:00) > 24h default.
            result = create_booking("Ivan", "polishing", "2026-04-10", "10:00",
                                    "+60190007")

        assert result["ok"] is True
        assert result["short_notice"] is False

    @patch("app.now_local", return_value=datetime(2026, 4, 8, 9, 0, tzinfo=TZ))
    def test_short_notice_flag_true_for_same_day(self, _):
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "ev-sn2"}

        with patch("app.get_calendar") as mock_cal, \
             patch("app.get_existing_booking", return_value=None), \
             patch("app.save_booking_record"):
            mock_cal.return_value = MagicMock()
            mock_cal.return_value.events.return_value = mock_events
            result = create_booking("Jin", "polishing", "2026-04-08", "16:00",
                                    "+60190008")

        assert result["ok"] is True
        assert result["short_notice"] is True


# ===========================================================================
# Test 28: System prompt adversarial
# ===========================================================================

class TestSystemPromptAdversarial:

    def test_closure_dates_appear_in_prompt(self):
        clinic = {**get_default_clinic(),
                  "special_closures": ["2026-03-20"],
                  "closure_notes": {"2026-03-20": "Closed for staff training"}}
        prompt = build_system_prompt("qa_closure_prompt", clinic)
        assert "2026-03-20" in prompt
        assert "Closed for staff training" in prompt

    def test_no_closures_shows_none(self):
        clinic = {**get_default_clinic(), "special_closures": [], "closure_notes": {}}
        prompt = build_system_prompt("qa_no_closures", clinic)
        assert "none" in prompt.lower()

    def test_special_closures_none_does_not_crash(self):
        clinic = {**get_default_clinic()}
        clinic.pop("special_closures", None)
        clinic.pop("closure_notes", None)
        # Should not raise
        prompt = build_system_prompt("qa_no_closures_key", clinic)
        assert prompt

    def test_prompt_contains_path_b_and_path_c(self):
        prompt = build_system_prompt("qa_paths")
        assert "Path B" in prompt
        assert "Path C" in prompt

    def test_prompt_contains_last_resort_for_find_next(self):
        prompt = build_system_prompt("qa_last_resort_prompt")
        assert "last resort" in prompt.lower()

    def test_prompt_contains_short_notice_rules(self):
        prompt = build_system_prompt("qa_sn_rules")
        assert "short_notice" in prompt or "short notice" in prompt.lower()


# ===========================================================================
# Test 29: Pricing conversational rules
# ===========================================================================

class TestPricingConversationalRules:

    def _clinic_with_prices(self):
        clinic = get_default_clinic()
        clinic["service_prices"] = {
            "scaling": 80.0,
            "polishing": 50.0,
            "whitening": 900.0,
            "filling": None,
            "braces consultation": None,
        }
        return clinic

    def test_prompt_has_general_pricing_rule(self):
        prompt = build_system_prompt("qa_pricing", self._clinic_with_prices())
        assert "price" in prompt.lower() or "rm" in prompt.lower()

    def test_prompt_has_single_service_price_rule(self):
        prompt = build_system_prompt("qa_single_price", self._clinic_with_prices())
        assert "one specific service" in prompt.lower() or "how much" in prompt.lower()

    def test_prompt_has_list_without_price_rule(self):
        prompt = build_system_prompt("qa_list_no_price", self._clinic_with_prices())
        assert "duration" in prompt.lower()

    def test_prompt_has_null_price_fallback_rule(self):
        prompt = build_system_prompt("qa_null_price", self._clinic_with_prices())
        assert "clinic will confirm" in prompt.lower() or "pricing to be confirmed" in prompt.lower()

    def test_prompt_has_service_selection_booking_rule(self):
        prompt = build_system_prompt("qa_booking_rule", self._clinic_with_prices())
        assert "booking" in prompt.lower()

    def test_prompt_forbids_price_hallucination(self):
        prompt = build_system_prompt("qa_no_hallucinate", self._clinic_with_prices())
        assert "never" in prompt.lower() and "price" in prompt.lower()

    def test_services_block_priced_entry_has_rm(self):
        prompt = build_system_prompt("qa_rm_price", self._clinic_with_prices())
        assert "RM 80" in prompt or "RM80" in prompt

    def test_services_block_unpriced_entry_has_no_rm(self):
        clinic = self._clinic_with_prices()
        prompt = build_system_prompt("qa_no_rm", clinic)
        # filling has no price — ", RM XX.XX" must not appear next to it in the
        # services listing (the ", rm " pattern distinguishes actual prices from
        # words like "confirmed" which also contain "rm" as a substring).
        lines = [l for l in prompt.split("\n") if "filling" in l.lower()]
        assert lines, "filling should appear in prompt"
        for line in lines:
            assert ", rm " not in line.lower(), f"filling line should not have RM price: {line}"


# ===========================================================================
# Test 30: Service pricing
# ===========================================================================

class TestServicePricing:

    def _clinic_priced(self):
        clinic = get_default_clinic()
        clinic["service_prices"] = {"polishing": 50.0, "scaling": None}
        return clinic

    def test_services_text_includes_price_when_set(self):
        prompt = build_system_prompt("qa_price_set", self._clinic_priced())
        assert "RM 50" in prompt or "RM50" in prompt

    def test_services_text_omits_price_when_none(self):
        clinic = self._clinic_priced()
        prompt = build_system_prompt("qa_price_none", clinic)
        # scaling has None price — ", RM XX.XX" must not appear in its services line
        # (", rm " distinguishes actual prices from substrings like "confirmed").
        lines = [l for l in prompt.split("\n") if "scaling" in l.lower()]
        for line in lines:
            assert ", rm " not in line.lower(), f"scaling should not have RM price: {line}"

    def test_services_text_no_price_key_backward_compat(self):
        """Clinics without service_prices key must not crash prompt building."""
        clinic = get_default_clinic()
        clinic.pop("service_prices", None)
        prompt = build_system_prompt("qa_no_price_key", clinic)
        assert prompt

    def test_services_block_includes_price_for_polishing(self):
        prompt = build_system_prompt("qa_polishing_price", self._clinic_priced())
        assert "RM 50" in prompt or "RM50" in prompt

    def test_services_text_prefers_service_catalog_over_legacy_mismatch(self):
        clinic = get_default_clinic()
        clinic["services"] = {"Testing": 30}
        clinic["service_prices"] = {"testing": None}
        clinic["service_catalog"] = [
            {"name": "Testing", "duration_minutes": 30, "price": 100.0}
        ]
        prompt = build_system_prompt("qa_catalog_priority", clinic)
        assert "Testing: 30 minutes, RM 100.00" in prompt

    def test_pricing_rules_present_in_prompt(self):
        prompt = build_system_prompt("qa_price_rules", self._clinic_priced())
        assert "price" in prompt.lower()

    @patch("app.now_local", return_value=_FIND_FUTURE)
    def test_check_availability_unaffected_by_price(self, _):
        """Pricing info must not affect slot availability logic."""
        clinic = self._clinic_priced()
        result = check_availability("polishing", "2026-04-05", "10:00", clinic)
        # 2026-04-05 is Sunday — should be rejected regardless of price
        assert result["ok"] is False

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_create_booking_succeeds_for_priced_service(self, mock_get_cal, _):
        phone = "+60195001"
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "evt-priced"}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = create_booking(
            name="Patient", service="polishing", date="2026-03-31",
            time="10:00", phone=phone, clinic=self._clinic_priced(),
        )
        assert result["ok"] is True

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_create_booking_succeeds_for_unpriced_service(self, mock_get_cal, _):
        phone = "+60195002"
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "evt-unpriced"}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        result = create_booking(
            name="Patient", service="scaling", date="2026-03-31",
            time="10:00", phone=phone, clinic=self._clinic_priced(),
        )
        assert result["ok"] is True


# ===========================================================================
# Test 31: Multi-clinic isolation adversarial
# ===========================================================================

class TestMultiClinicIsolationAdversarial:

    @patch("app.now_local", return_value=_FIND_FUTURE)
    @patch("app.get_calendar")
    def test_check_availability_queries_correct_calendar(self, mock_get_cal, _):
        """check_availability for clinic A must not query clinic B's calendar."""
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        check_availability("scaling", "2026-03-31", "10:00", _CLINIC_A)

        call_kwargs = mock_events.list.call_args[1]
        assert call_kwargs["calendarId"] == _CLINIC_A["google_calendar_id"]
        assert call_kwargs["calendarId"] != _CLINIC_B["google_calendar_id"]

    @patch("app.get_calendar")
    def test_cancel_uses_correct_clinic_calendar(self, mock_get_cal):
        """cancel_booking must delete from the current clinic's calendar.
        The record must have the matching clinic_id so the scoped lookup finds it.
        """
        phone = "+60196001"
        save_booking_record(
            user=phone, event_id="evt-iso-cancel", service="scaling", name="Patient",
            date="2026-04-10", time="10:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False,
            clinic_id=_CLINIC_A["id"],  # Must match the clinic passed to cancel_booking.
        )
        mock_events = MagicMock()
        mock_events.delete.return_value.execute.return_value = {}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        cancel_booking(phone, clinic=_CLINIC_A)

        delete_kwargs = mock_events.delete.call_args[1]
        assert delete_kwargs["calendarId"] == _CLINIC_A["google_calendar_id"]

    def test_booking_for_clinic_a_not_visible_in_clinic_b(self):
        """A booking made at Clinic A must NOT be visible to Clinic B.

        With cross-clinic isolation (CRITICAL-3), Clinic B's bot should not find
        Clinic A's booking when looking up by phone — so a new booking at Clinic B
        for the same patient name is allowed (no spurious 'existing booking' block).
        """
        from app import get_existing_booking
        phone = "+60196002"
        save_booking_record(
            user=phone, event_id="evt-clinic-a-iso", service="scaling", name="Patient A",
            date="2026-04-10", time="10:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False, clinic_id=_CLINIC_A["id"],
        )

        # Unscoped lookup (admin path) should still find the record.
        record_unscoped = get_existing_booking(phone)
        assert record_unscoped is not None
        assert record_unscoped["clinic_id"] == _CLINIC_A["id"]

        # Scoped to Clinic B — must NOT find the Clinic A booking.
        record_clinic_b = get_existing_booking(phone, clinic_id=_CLINIC_B["id"])
        assert record_clinic_b is None


# ===========================================================================
# Test 32: BM/Manglish service aliases (Fix 4)
# ===========================================================================

class TestBMServiceAliases:
    """All BM/Manglish alias terms must resolve to correct canonical service names."""

    # --- Filling / tampal ---

    def test_tampal_to_filling(self):
        assert normalize_service("tampal") == "filling"

    def test_tampal_gigi_to_filling(self):
        assert normalize_service("tampal gigi") == "filling"

    def test_tampal_case_insensitive(self):
        assert normalize_service("TAMPAL") == "filling"

    # --- Scaling / cuci gigi ---

    def test_cuci_gigi_to_scaling(self):
        assert normalize_service("cuci gigi") == "scaling"

    def test_pembersihan_gigi_to_scaling(self):
        assert normalize_service("pembersihan gigi") == "scaling"

    def test_pembersihan_to_scaling(self):
        assert normalize_service("pembersihan") == "scaling"

    def test_scaler_to_scaling(self):
        assert normalize_service("scaler") == "scaling"

    # --- Whitening ---

    def test_gigi_putih_to_whitening(self):
        assert normalize_service("gigi putih") == "whitening"

    def test_memutihkan_gigi_to_whitening(self):
        assert normalize_service("memutihkan gigi") == "whitening"

    def test_pemutihan_gigi_to_whitening(self):
        assert normalize_service("pemutihan gigi") == "whitening"

    def test_pemutihan_to_whitening(self):
        assert normalize_service("pemutihan") == "whitening"

    # --- Braces consultation / pendakap ---

    def test_pendakap_to_braces_consultation(self):
        assert normalize_service("pendakap") == "braces consultation"

    def test_pendakap_gigi_to_braces_consultation(self):
        assert normalize_service("pendakap gigi") == "braces consultation"

    def test_kawat_gigi_to_braces_consultation(self):
        assert normalize_service("kawat gigi") == "braces consultation"

    # --- Existing English aliases must not regress ---

    def test_cleaning_still_works(self):
        assert normalize_service("cleaning") == "scaling"

    def test_braces_still_works(self):
        assert normalize_service("braces") == "braces consultation"

    def test_tooth_filling_still_works(self):
        assert normalize_service("tooth filling") == "filling"

    # --- Unknown BM term passes through safely ---

    def test_unknown_bm_passthrough(self):
        assert normalize_service("cabut gigi") == "cabut gigi"


# ===========================================================================
# Test 33: Expanded human handoff detection (Fix 3)
# ===========================================================================

class TestHumanHandoffDetection:

    # --- Original phrases must still work ---

    def test_human_exact(self):
        assert is_human_request("human") is True

    def test_staff_exact(self):
        assert is_human_request("staff") is True

    def test_agent_exact(self):
        assert is_human_request("agent") is True

    def test_talk_to_human(self):
        assert is_human_request("talk to human") is True

    def test_speak_to_staff(self):
        assert is_human_request("speak to staff") is True

    # --- New: receptionist ---

    def test_receptionist_exact(self):
        assert is_human_request("receptionist") is True

    # --- New: informal English ---

    def test_connect_me_to_someone(self):
        assert is_human_request("connect me to someone") is True

    def test_connect_me_to_reception(self):
        assert is_human_request("connect me to reception") is True

    def test_can_i_speak_with_someone(self):
        assert is_human_request("can i speak with someone") is True

    def test_can_i_talk_to_someone(self):
        assert is_human_request("can i talk to someone") is True

    def test_i_prefer_to_call(self):
        assert is_human_request("i prefer to call") is True

    def test_call_the_clinic(self):
        assert is_human_request("call the clinic") is True

    # --- New: Manglish / BM ---

    def test_nak_cakap_dengan_staff(self):
        assert is_human_request("nak cakap dengan staff") is True

    def test_nak_cakap_dengan_orang(self):
        assert is_human_request("nak cakap dengan orang") is True

    def test_boleh_cakap_dengan_staff(self):
        assert is_human_request("boleh cakap dengan staff") is True

    def test_boleh_hubungi_staff(self):
        assert is_human_request("boleh hubungi staff") is True

    def test_tolong_sambungkan_dengan_staff(self):
        assert is_human_request("tolong sambungkan dengan staff") is True

    # --- Case insensitivity ---

    def test_upper_case_staff(self):
        assert is_human_request("STAFF") is True

    def test_mixed_case_connect(self):
        assert is_human_request("Connect Me To Someone") is True

    # --- Should NOT trigger ---

    def test_book_appointment_not_human(self):
        assert is_human_request("I want to book an appointment") is False

    def test_cancel_appointment_not_human(self):
        assert is_human_request("cancel my appointment") is False

    def test_thank_you_not_human(self):
        assert is_human_request("thank you") is False

    def test_empty_not_human(self):
        assert is_human_request("") is False


# ===========================================================================
# Test 34: Standalone "cancel" properly cancels booking (Fix 2)
# ===========================================================================

class TestStandaloneCancelCommand:

    def test_cancel_with_no_booking_returns_nothing_to_cancel(self):
        """No booking: 'cancel' returns a 'nothing to cancel' reply."""
        user = "qa_cancel_no_booking"
        update_booking_state(user, service="scaling", date="2026-04-10", time="10:00")

        reply = get_direct_reply(user, "cancel")

        assert reply is not None
        assert "nothing to cancel" in reply.lower() or "help" in reply.lower()

    @patch("app.get_calendar")
    def test_cancel_with_existing_booking_calls_cancel_booking(self, mock_get_cal):
        """Existing booking: 'cancel' must delete the Calendar event and DB record."""
        user = "qa_cancel_with_booking"
        save_booking_record(
            user=user, event_id="evt-cancel-standalone",
            service="scaling", name="Test Patient",
            date="2026-04-10", time="10:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False,
            clinic_id=1,
        )
        mock_events = MagicMock()
        mock_events.delete.return_value.execute.return_value = {}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        reply = get_direct_reply(user, "cancel")

        mock_events.delete.assert_called_once()
        assert get_existing_booking(user) is None
        assert reply is not None
        assert "cancelled" in reply.lower()

    @patch("app.get_calendar")
    def test_cancel_reply_includes_booking_details(self, mock_get_cal):
        """Reply after cancellation must mention the service, date, and time."""
        user = "qa_cancel_details"
        save_booking_record(
            user=user, event_id="evt-cancel-details",
            service="polishing", name="Siti Aminah",
            date="2026-04-11", time="14:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False,
            clinic_id=1,
        )
        mock_events = MagicMock()
        mock_events.delete.return_value.execute.return_value = {}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        reply = get_direct_reply(user, "cancel")

        assert "polishing" in reply.lower()
        assert "2026-04-11" in reply
        assert "14:00" in reply

    @patch("app.get_calendar")
    def test_cancel_with_calendar_error_still_clears_state(self, mock_get_cal):
        """If Calendar delete fails, local state must still be cleared."""
        user = "qa_cancel_cal_error"
        save_booking_record(
            user=user, event_id="evt-cal-error",
            service="filling", name="Ali",
            date="2026-04-12", time="11:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False,
            clinic_id=1,
        )
        mock_events = MagicMock()
        mock_events.delete.return_value.execute.side_effect = Exception("calendar down")
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events

        reply = get_direct_reply(user, "cancel")

        assert reply is not None
        assert get_existing_booking(user) is None

    def test_reset_preserves_booking_record(self):
        """'reset' clears booking state but must NOT cancel the booking record."""
        user = "qa_reset_preserves"
        save_booking_record(
            user=user, event_id="evt-reset-preserve",
            service="filling", name="Patient",
            date="2026-04-15", time="10:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False,
        )
        get_direct_reply(user, "reset")
        assert get_existing_booking(user) is not None

    def test_cancel_my_appointment_not_intercepted(self):
        """'cancel my appointment' must not be caught by is_reset_command."""
        assert is_reset_command("cancel my appointment") is False

    def test_cancel_alone_is_reset_command(self):
        assert is_reset_command("cancel") is True


# ===========================================================================
# Test 35: Overwrite guard in dispatch_tool create_booking (Fix 1)
# ===========================================================================

class TestCreateBookingOverwriteGuard:

    def test_create_booking_blocked_when_booking_exists(self):
        """dispatch_tool create_booking returns ok=False if same patient name already has booking."""
        user = "qa_overwrite_guard"
        save_booking_record(
            user=user, event_id="evt-existing", service="scaling", name="Farid",
            date="2026-04-14", time="10:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False,
        )
        update_booking_state(user, availability_ok=True)

        # Try to book for same patient name (Farid)
        result = dispatch_tool(
            "create_booking",
            {"name": "Farid", "service": "whitening", "date": "2026-04-16", "time": "14:00"},
            user,
        )

        assert result["ok"] is False
        assert "existing booking" in result["message"].lower()
        assert "farid" in result["message"].lower()

    def test_create_booking_blocked_message_includes_details(self):
        """Error message must include the existing booking's service, date, time."""
        user = "qa_overwrite_msg"
        save_booking_record(
            user=user, event_id="evt-msg-test", service="polishing", name="Lim",
            date="2026-04-15", time="11:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False,
        )
        update_booking_state(user, availability_ok=True)

        # Try to book for same patient name (Lim)
        result = dispatch_tool(
            "create_booking",
            {"name": "Lim", "service": "filling",
             "date": "2026-04-17", "time": "10:00"},
            user,
        )

        assert result["ok"] is False
        assert "polishing" in result["message"]
        assert "2026-04-15" in result["message"]
        assert "11:00" in result["message"]

    @patch("app.now_local", return_value=datetime(2026, 3, 30, 9, 0, tzinfo=TZ))
    def test_create_booking_succeeds_after_cancel(self, _mock_now):
        """After cancel_booking removes the record, create_booking must succeed."""
        user = "qa_cancel_then_create"
        save_booking_record(
            user=user, event_id="evt-to-cancel", service="scaling", name="Hassan",
            date="2026-04-14", time="10:00", status="Confirmed",
            reminder_1d_sent=False, reminder_2h_sent=False,
            clinic_id=1,
        )
        mock_events = MagicMock()
        mock_events.delete.return_value.execute.return_value = {}
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "evt-new"}
        mock_cal = MagicMock()
        mock_cal.events.return_value = mock_events

        with patch("app.get_calendar", return_value=mock_cal):
            cancel_result = dispatch_tool("cancel_booking", {}, user)
            assert cancel_result["ok"] is True
            assert get_existing_booking(user) is None

            update_booking_state(user, availability_ok=True)

            create_result = dispatch_tool(
                "create_booking",
                {"name": "Hassan", "service": "filling",
                 "date": "2026-04-16", "time": "10:00"},
                user,
            )

        assert create_result["ok"] is True
        assert get_existing_booking(user)["service"] == "filling"

    def test_create_booking_without_existing_booking_hits_availability_guard(self):
        """Guard must not interfere when there is no existing booking."""
        user = "qa_no_existing"
        assert get_existing_booking(user) is None
        result = dispatch_tool(
            "create_booking",
            {"name": "New", "service": "polishing", "date": "2026-04-16", "time": "10:00"},
            user,
        )
        assert result["ok"] is False
        assert "availability" in result["message"].lower()


# ===========================================================================
# Test 36: Vague time phrases resolve to None (Fix 5)
# ===========================================================================

class TestVagueTimeHandling:

    # --- Known vague phrases must return None ---

    def test_morning_returns_none(self):
        assert resolve_time_text("morning") is None

    def test_afternoon_returns_none(self):
        assert resolve_time_text("afternoon") is None

    def test_evening_returns_none(self):
        assert resolve_time_text("evening") is None

    def test_after_work_returns_none(self):
        assert resolve_time_text("after work") is None

    def test_lepas_kerja_returns_none(self):
        assert resolve_time_text("lepas kerja") is None

    def test_lunchtime_returns_none(self):
        assert resolve_time_text("lunchtime") is None

    def test_lunch_returns_none(self):
        assert resolve_time_text("lunch") is None

    def test_after_lunch_returns_none(self):
        assert resolve_time_text("after lunch") is None

    def test_later_returns_none(self):
        assert resolve_time_text("later") is None

    def test_anytime_returns_none(self):
        assert resolve_time_text("anytime") is None

    def test_pagi_returns_none(self):
        assert resolve_time_text("pagi") is None

    def test_petang_returns_none(self):
        assert resolve_time_text("petang") is None

    def test_tengah_hari_returns_none(self):
        assert resolve_time_text("tengah hari") is None

    def test_malam_returns_none(self):
        assert resolve_time_text("malam") is None

    # --- Specific times must still resolve correctly ---

    def test_3pm_resolves(self):
        assert resolve_time_text("3pm") == "15:00"

    def test_10am_resolves(self):
        assert resolve_time_text("10am") == "10:00"

    def test_10am_yes_resolves(self):
        assert resolve_time_text("10am yes") == "10:00"

    def test_1400_resolves(self):
        assert resolve_time_text("14:00") == "14:00"

    def test_noon_resolves(self):
        assert resolve_time_text("noon") == "12:00"

    def test_dot_notation_resolves(self):
        assert resolve_time_text("3.30pm") == "15:30"

    # --- resolve_booking_datetime must return ok=False for vague times ---

    def test_resolve_booking_datetime_fails_for_afternoon(self):
        result = resolve_booking_datetime("tomorrow", "afternoon")
        assert result["ok"] is False

    def test_resolve_booking_datetime_fails_for_morning(self):
        result = resolve_booking_datetime("2026-04-10", "morning")
        assert result["ok"] is False

    def test_resolve_booking_datetime_succeeds_for_specific_time(self):
        result = resolve_booking_datetime("2026-04-10", "10am")
        assert result["ok"] is True
        assert result["time"] == "10:00"

    # --- System prompt includes vague time guidance ---

    # --- New vague phrase additions ---

    def test_soon_returns_none(self):
        assert resolve_time_text("soon") is None

    def test_asap_returns_none(self):
        assert resolve_time_text("asap") is None

    def test_night_returns_none(self):
        assert resolve_time_text("night") is None

    def test_tonight_returns_none(self):
        assert resolve_time_text("tonight") is None

    def test_midday_returns_none(self):
        assert resolve_time_text("midday") is None

    def test_noontime_returns_none(self):
        assert resolve_time_text("noontime") is None

    def test_around_noon_returns_none(self):
        assert resolve_time_text("around noon") is None

    def test_late_morning_returns_none(self):
        assert resolve_time_text("late morning") is None

    def test_late_afternoon_returns_none(self):
        assert resolve_time_text("late afternoon") is None

    def test_after_dinner_returns_none(self):
        assert resolve_time_text("after dinner") is None

    def test_not_too_early_returns_none(self):
        assert resolve_time_text("not too early") is None

    def test_whenever_returns_none(self):
        assert resolve_time_text("whenever") is None

    def test_flexible_returns_none(self):
        assert resolve_time_text("flexible") is None

    def test_pagi_pagi_returns_none(self):
        assert resolve_time_text("pagi-pagi") is None

    def test_malam_nanti_returns_none(self):
        assert resolve_time_text("malam nanti") is None

    def test_lepas_makan_returns_none(self):
        assert resolve_time_text("lepas makan") is None

    def test_petang_nanti_returns_none(self):
        assert resolve_time_text("petang nanti") is None

    def test_system_prompt_contains_vague_time_guidance(self):
        prompt = build_system_prompt("qa_prompt_vague_time_test")
        assert "morning" in prompt.lower()
        assert "afternoon" in prompt.lower()
        assert "after work" in prompt.lower() or "lepas kerja" in prompt.lower()
        assert "resolve_booking_datetime" in prompt


# ===========================================================================
# Test: check_date_available — after-hours "today" detection
# ===========================================================================

class TestCheckDateAvailableAfterHours:
    """check_date_available should treat today as closed when current time >= close_hour."""

    def _clinic(self):
        return get_default_clinic()

    def test_today_after_close_returns_closed(self):
        # Simulate 11:35 PM on a weekday (Monday 2026-04-13 is safe — not Sunday, not holiday)
        fake_now = datetime(2026, 4, 13, 23, 35, 0, tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
        with patch("app.now_local", return_value=fake_now):
            result = check_date_available("today", self._clinic())
        assert result["ok"] is False
        assert result["closed"] is True
        assert result["reason"] == "already_closed_today"
        assert "already closed" in result["message"].lower()
        assert "next_open_days" in result

    def test_today_exactly_at_close_returns_closed(self):
        # Exactly at closing hour (18:00) — should be treated as closed
        fake_now = datetime(2026, 4, 13, 18, 0, 0, tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
        with patch("app.now_local", return_value=fake_now):
            result = check_date_available("today", self._clinic())
        assert result["ok"] is False
        assert result["reason"] == "already_closed_today"

    def test_today_before_close_returns_open(self):
        # 2 PM — clinic is still open
        fake_now = datetime(2026, 4, 13, 14, 0, 0, tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
        with patch("app.now_local", return_value=fake_now):
            result = check_date_available("today", self._clinic())
        assert result["ok"] is True
        assert result["closed"] is False

    def test_tomorrow_after_close_is_not_affected(self):
        # Even if current time is 11 PM, "tomorrow" should return ok=True (open weekday)
        fake_now = datetime(2026, 4, 13, 23, 0, 0, tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
        with patch("app.now_local", return_value=fake_now):
            result = check_date_available("tomorrow", self._clinic())
        assert result["ok"] is True
        assert result["closed"] is False


# ===========================================================================
# Test: dispatch_tool attaches available_slots when check_availability fails
# ===========================================================================

_CDA_FUTURE = datetime(2026, 4, 15, 9, 0, tzinfo=TZ)  # Wednesday, before open


def _mock_cal_with_morning_block():
    """Calendar mock: 10:00–14:00 booking blocks morning whitening slots."""
    tz = ZoneInfo("Asia/Kuala_Lumpur")
    ev_start = datetime(2026, 4, 15, 10, 0, tzinfo=tz)
    ev_end = datetime(2026, 4, 15, 14, 0, tzinfo=tz)
    mock_events = MagicMock()
    mock_events.list.return_value.execute.return_value = {
        "items": [{
            "start": {"dateTime": ev_start.isoformat()},
            "end": {"dateTime": ev_end.isoformat()},
        }]
    }
    mock_cal = MagicMock()
    mock_cal.events.return_value = mock_events
    return mock_cal


class TestCheckAvailabilityAvailableSlotsFallback:
    """When check_availability says a slot is unavailable, dispatch_tool must
    attach available_slots for the same day so the LLM presents real options."""

    @patch("app.now_local", return_value=_CDA_FUTURE)
    @patch("app.get_calendar", return_value=None)
    def test_available_slots_attached_when_slot_unavailable(self, mock_get_cal, _):
        mock_get_cal.return_value = _mock_cal_with_morning_block()
        phone = "+60199000001"

        # First set availability_ok so the state is clean
        reset_booking_state(phone)

        # Try to book whitening at 10am — blocked by morning event
        result = dispatch_tool(
            "check_availability",
            {"service": "whitening", "date": "2026-04-15", "time": "10:00"},
            phone,
        )

        assert result["ok"] is False, "Slot should be blocked by morning event"
        assert "available_slots" in result, (
            "dispatch_tool must attach available_slots when slot is unavailable"
        )
        slots = result["available_slots"]
        assert len(slots) > 0, "At least one afternoon slot should be available"
        # All returned slots should be at or after 14:00 (after the morning block)
        for slot in slots:
            h, m = map(int, slot["time"].split(":"))
            slot_minutes = h * 60 + m
            assert slot_minutes >= 14 * 60, (
                f"Slot {slot['time']} overlaps the morning block (10:00–14:00)"
            )

    @patch("app.now_local", return_value=_CDA_FUTURE)
    @patch("app.get_calendar", return_value=None)
    def test_available_slots_absent_when_past_or_closed(self, mock_get_cal, _):
        """Past slots and closed-day rejections should NOT trigger the slots fetch."""
        # No calendar mock needed — the function returns before hitting the calendar
        phone = "+60199000002"
        reset_booking_state(phone)

        # Sunday — clinic closed, not "unavailable"
        result = dispatch_tool(
            "check_availability",
            {"service": "whitening", "date": "2026-04-19", "time": "10:00"},  # Sunday
            phone,
        )
        assert result["ok"] is False
        assert "available_slots" not in result, (
            "Closed-day rejection should not trigger available_slots fallback"
        )

    @patch("app.now_local", return_value=_CDA_FUTURE)
    @patch("app.get_calendar", return_value=None)
    def test_available_slots_not_attached_when_slot_ok(self, mock_get_cal, _):
        """Successful check_availability must NOT include available_slots."""
        mock_get_cal.return_value = _mock_cal_with_morning_block()
        phone = "+60199000003"
        reset_booking_state(phone)

        # 14:00 is free (after the morning block); whitening 14:00–15:30 fits
        result = dispatch_tool(
            "check_availability",
            {"service": "whitening", "date": "2026-04-15", "time": "14:00"},
            phone,
        )
        assert result["ok"] is True
        assert "available_slots" not in result


# ===========================================================================
# Test: midnight clarification guard in build_system_prompt
# ===========================================================================

class TestMidnightClarificationGuard:
    """build_system_prompt must inject the midnight guard only between 23:00–01:59."""

    def _prompt(self, hour: int, minute: int = 5) -> str:
        fake_now = datetime(2026, 4, 15, hour, minute, 0, tzinfo=TZ)
        with patch("app.now_local", return_value=fake_now):
            return build_system_prompt("qa_midnight_test")

    # --- Guard is ACTIVE ---

    def test_guard_active_at_2300(self):
        prompt = self._prompt(23, 0)
        assert "MIDNIGHT DATE CLARIFICATION GUARD" in prompt
        assert "ACTIVE" in prompt

    def test_guard_active_at_2359(self):
        prompt = self._prompt(23, 59)
        assert "MIDNIGHT DATE CLARIFICATION GUARD" in prompt

    def test_guard_active_at_0000(self):
        prompt = self._prompt(0, 5)
        assert "MIDNIGHT DATE CLARIFICATION GUARD" in prompt

    def test_guard_active_at_0100(self):
        prompt = self._prompt(1, 30)
        assert "MIDNIGHT DATE CLARIFICATION GUARD" in prompt

    def test_guard_active_at_0159(self):
        prompt = self._prompt(1, 59)
        assert "MIDNIGHT DATE CLARIFICATION GUARD" in prompt

    # --- Guard is INACTIVE ---

    def test_guard_inactive_at_0200(self):
        prompt = self._prompt(2, 0)
        assert "MIDNIGHT DATE CLARIFICATION GUARD" not in prompt

    def test_guard_inactive_at_1000(self):
        prompt = self._prompt(10, 0)
        assert "MIDNIGHT DATE CLARIFICATION GUARD" not in prompt

    def test_guard_inactive_at_1800(self):
        prompt = self._prompt(18, 0)
        assert "MIDNIGHT DATE CLARIFICATION GUARD" not in prompt

    def test_guard_inactive_at_2259(self):
        prompt = self._prompt(22, 59)
        assert "MIDNIGHT DATE CLARIFICATION GUARD" not in prompt

    # --- Content when active ---

    def test_guard_includes_today_and_tomorrow_labels(self):
        # 23:05 on Tuesday 2026-04-14 → today=Tuesday 14 April, tomorrow=Wednesday 15 April
        fake_now = datetime(2026, 4, 14, 23, 5, 0, tzinfo=TZ)
        with patch("app.now_local", return_value=fake_now):
            prompt = build_system_prompt("qa_midnight_content_test")
        assert "Tuesday" in prompt
        assert "Wednesday" in prompt

    def test_guard_lists_ambiguous_phrases(self):
        prompt = self._prompt(23, 30)
        assert "today" in prompt
        assert "tomorrow" in prompt
        assert "tmr" in prompt

    def test_guard_instructs_no_tool_calls(self):
        prompt = self._prompt(0, 15)
        assert "check_date_available" in prompt
        assert "resolve_booking_datetime" in prompt


class TestPendingDateClarificationState:
    @pytest.fixture(autouse=True)
    def _cleanup_pending(self):
        _PENDING_DATE_CLARIFICATIONS.clear()
        yield
        _PENDING_DATE_CLARIFICATIONS.clear()

    @patch("app.now_local", return_value=datetime(2026, 4, 15, 0, 10, tzinfo=TZ))
    def test_next_reply_weekday_overrides_ambiguous_date(self, _):
        user = "qa_pending_date_weekday"
        _set_pending_date_clarification(user, clinic_id=1)

        result = _consume_pending_date_clarification(user, 1, "no i meant wednesday")
        assert result["status"] == "resolved"
        assert result["date"] == "2026-04-15"
        assert "Wednesday, 2026-04-15" in result["canonical_user_message"]
        assert (user, 1) not in _PENDING_DATE_CLARIFICATIONS

    @patch("app.now_local", return_value=datetime(2026, 4, 15, 0, 10, tzinfo=TZ))
    def test_next_reply_supports_today_and_tomorrow(self, _):
        user = "qa_pending_date_relative"
        _set_pending_date_clarification(user, clinic_id=1)
        today_pick = _consume_pending_date_clarification(user, 1, "for today")
        assert today_pick["status"] == "resolved"
        assert today_pick["date"] == "2026-04-15"

        _set_pending_date_clarification(user, clinic_id=1)
        tomorrow_pick = _consume_pending_date_clarification(user, 1, "tomorrow")
        assert tomorrow_pick["status"] == "resolved"
        assert tomorrow_pick["date"] == "2026-04-16"

    @patch("app.now_local", return_value=datetime(2026, 4, 15, 0, 10, tzinfo=TZ))
    def test_unrecognized_next_reply_reasks_same_two_day_choice(self, _):
        user = "qa_pending_date_reask"
        _set_pending_date_clarification(user, clinic_id=1)

        result = _consume_pending_date_clarification(user, 1, "not sure")
        assert result["status"] == "needs_clarification"
        assert "Wednesday (today) or Thursday (tomorrow)" in result["reply"]
        assert (user, 1) in _PENDING_DATE_CLARIFICATIONS

    @patch("app.now_local", return_value=datetime(2026, 4, 15, 0, 10, tzinfo=TZ))
    @patch("app.client.responses.create")
    def test_run_ai_uses_authoritative_clarified_date(self, mock_create, _):
        user = "qa_pending_date_run_ai"
        _set_pending_date_clarification(user, clinic_id=1)

        mock_response = MagicMock()
        mock_response.output = []
        mock_response.output_text = "Yes, Wednesday is available. What time would you like?"
        mock_create.return_value = mock_response

        reply = app.run_ai(user, "Wednesday")
        assert "Wednesday is available" in reply

        sent_input = mock_create.call_args.kwargs["input"]
        assert any(
            m.get("role") == "system"
            and "DATE CLARIFICATION RESOLVED" in m.get("content", "")
            for m in sent_input
        )
        assert any(
            m.get("role") == "user"
            and "I mean Wednesday, 2026-04-15." in m.get("content", "")
            for m in sent_input
        )

        # run_ai routes via get_default_clinic() → clinic_id=1, so check with that scope.
        state = get_booking_state(user, clinic_id=1)
        assert state["date"] == "2026-04-15"
        assert state["time"] is None
        assert state["availability_ok"] is False

        _clear_pending_date_clarification(user, 1)
