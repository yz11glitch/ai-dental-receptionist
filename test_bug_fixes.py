"""
Targeted tests for critical bug fixes #6, #9, #11, #14.

These tests verify the specific fixes implemented to address
QA stress testing findings.
"""

import os

# Must be set before importing app
os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import app
from app import (
    detect_conflicting_date_phrases,
    update_booking_state,
    get_booking_state,
    get_all_bookings,
    save_booking_record,
    create_booking,
    dispatch_tool,
    is_within_business_hours,
    ensure_demo_clinic_seeded,
    SessionLocal,
    BookingRecordModel,
    BookingStateModel,
)

TZ = ZoneInfo("Asia/Kuala_Lumpur")


@pytest.fixture(scope="session", autouse=True)
def seed_clinic():
    """Seed demo clinic once for the full test session."""
    ensure_demo_clinic_seeded()


@pytest.fixture(autouse=True)
def clean_test_data():
    """Clean booking data before and after each test."""
    yield
    with SessionLocal() as db:
        db.query(BookingRecordModel).delete()
        db.query(BookingStateModel).delete()
        db.commit()


class TestBugFix06ServiceChangeValidation:
    """
    BUG #6: Service change after availability check must re-validate slot.
    
    Scenario: User books "cleaning" (30min), system checks availability OK,
    user changes to "root canal" (90min), booking should NOT proceed without
    re-checking with new duration.
    """

    def test_service_change_resets_availability_ok(self):
        """When service changes after availability_ok=True, flag is reset to False."""
        user = "test_service_change"
        
        # Initial booking state with availability checked
        update_booking_state(
            user,
            service="scaling",  # 30 min service
            date="2026-04-10",
            time="14:00",
            availability_ok=True
        )
        
        # Verify availability is True
        state = get_booking_state(user)
        assert state["availability_ok"] is True
        assert state["service"] == "scaling"
        
        # User changes service to longer duration service
        update_booking_state(user, service="whitening")  # 60 min service
        
        # Verify availability_ok was reset to False
        state = get_booking_state(user)
        assert state["availability_ok"] is False, "availability_ok should be reset when service changes"
        assert state["service"] == "whitening"

    def test_service_same_does_not_reset_availability(self):
        """When service doesn't change, availability_ok should remain True."""
        user = "test_service_same"
        
        update_booking_state(
            user,
            service="scaling",
            date="2026-04-10",
            time="14:00",
            availability_ok=True
        )
        
        # Update with same service
        update_booking_state(user, service="scaling")
        
        # Availability should still be True
        state = get_booking_state(user)
        assert state["availability_ok"] is True

    def test_service_change_before_availability_check_no_reset(self):
        """If availability_ok is already False, changing service doesn't reset it."""
        user = "test_no_reset"
        
        update_booking_state(
            user,
            service="scaling",
            availability_ok=False
        )
        
        # Change service
        update_booking_state(user, service="whitening")
        
        # Should still be False (not reset, just remained False)
        state = get_booking_state(user)
        assert state["availability_ok"] is False


class TestBugFix09DateConflictDetection:
    """
    BUG #9: Detect conflicting date phrases and ask clarification.
    
    Scenario: User says "tomorrow Friday" when tomorrow is Tuesday.
    System should detect conflict and ask clarification instead of
    booking wrong date.
    """

    def test_tomorrow_plus_weekday_conflict(self):
        """'tomorrow Friday' should trigger conflict detection."""
        result = detect_conflicting_date_phrases("tomorrow Friday at 3pm")
        assert result is not None
        assert "clarify" in result.lower()

    def test_today_plus_weekday_conflict(self):
        """'today Monday' should trigger conflict detection."""
        result = detect_conflicting_date_phrases("today Monday 10am")
        assert result is not None
        assert "clarify" in result.lower()

    def test_tmr_plus_weekday_conflict(self):
        """'tmr wed' (abbreviations) should trigger conflict detection."""
        result = detect_conflicting_date_phrases("book for tmr wed")
        assert result is not None

    def test_weekday_only_no_conflict(self):
        """'Friday at 3pm' should NOT trigger conflict."""
        result = detect_conflicting_date_phrases("Friday at 3pm")
        assert result is None

    def test_relative_only_no_conflict(self):
        """'tomorrow at 3pm' should NOT trigger conflict."""
        result = detect_conflicting_date_phrases("tomorrow at 3pm")
        assert result is None

    def test_next_week_no_conflict(self):
        """'next week' alone should NOT trigger conflict."""
        result = detect_conflicting_date_phrases("next week")
        assert result is None

    def test_empty_string_no_conflict(self):
        """Empty string should NOT trigger conflict."""
        result = detect_conflicting_date_phrases("")
        assert result is None


class TestBugFix11FamilyBookings:
    """
    BUG #11: Support multiple bookings per phone number.
    
    Scenario: Parent can book for Emily, Jason, and Sarah using same phone.
    Each booking is tracked separately by patient name.
    """

    def test_multiple_bookings_different_names_allowed(self):
        """Same phone number can have multiple bookings for different patient names."""
        phone = "+60123456789"
        
        # Save booking for Emily
        save_booking_record(
            user=phone,
            event_id="evt-emily",
            service="scaling",
            name="Emily Tan",
            date="2026-04-10",
            time="10:00"
        )
        
        # Save booking for Jason
        save_booking_record(
            user=phone,
            event_id="evt-jason",
            service="polishing",
            name="Jason Tan",
            date="2026-04-10",
            time="14:00"
        )
        
        # Get all bookings
        all_bookings = get_all_bookings(phone)
        assert len(all_bookings) == 2
        
        names = {b["name"] for b in all_bookings}
        assert "Emily Tan" in names
        assert "Jason Tan" in names

    def test_duplicate_patient_name_blocked_via_dispatch(self):
        """dispatch_tool should block creating second booking for same patient name."""
        phone = "+60123456780"
        
        # Create existing booking for Emily
        save_booking_record(
            user=phone,
            event_id="evt-emily-1",
            service="scaling",
            name="Emily",
            date="2026-04-10",
            time="10:00"
        )
        
        # Try to create another booking for Emily
        update_booking_state(phone, availability_ok=True)
        result = dispatch_tool(
            "create_booking",
            {"name": "Emily", "service": "whitening", "date": "2026-04-12", "time": "14:00"},
            phone
        )
        
        # Should be blocked
        assert result["ok"] is False
        assert "emily already has an existing booking" in result["message"].lower()

    def test_different_patient_names_not_blocked(self):
        """dispatch_tool should ALLOW booking for different patient name."""
        phone = "+60123456781"
        
        # Create booking for Farid
        save_booking_record(
            user=phone,
            event_id="evt-farid",
            service="scaling",
            name="Farid",
            date="2026-04-10",
            time="10:00"
        )
        
        # Try to create booking for Ain (different person)
        update_booking_state(phone, availability_ok=True)
        
        with patch("app.now_local") as mock_now:
            mock_now.return_value = datetime(2026, 4, 1, 10, 0, tzinfo=TZ)
            with patch("app.get_calendar") as mock_get_cal:
                mock_events = MagicMock()
                mock_events.list.return_value.execute.return_value = {"items": []}
                mock_events.insert.return_value.execute.return_value = {"id": "evt-ain"}
                mock_get_cal.return_value = MagicMock()
                mock_get_cal.return_value.events.return_value = mock_events
                
                result = dispatch_tool(
                    "create_booking",
                    {"name": "Ain", "service": "whitening", "date": "2026-04-16", "time": "14:00"},
                    phone
                )
        
        # Should succeed
        assert result["ok"] is True, f"Expected success but got: {result.get('message')}"
        
        # Should have 2 bookings now
        all_bookings = get_all_bookings(phone)
        assert len(all_bookings) == 2

    @patch("app.now_local")
    @patch("app.get_calendar")
    def test_same_patient_name_replaces_old_booking(self, mock_get_cal, mock_now):
        """When booking for same patient name, old event is deleted."""
        mock_now.return_value = datetime(2026, 4, 1, 10, 0, tzinfo=TZ)
        
        phone = "+60123456782"
        
        # Create initial booking for Sarah (clinic_id=1 so it's visible to default clinic)
        save_booking_record(
            user=phone,
            event_id="evt-sarah-old",
            service="scaling",
            name="Sarah",
            date="2026-04-10",
            time="10:00",
            clinic_id=1,
        )
        
        # Mock calendar to check delete was called
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "evt-sarah-new"}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events
        
        # Book again for Sarah (same name) on Friday April 11
        result = create_booking(
            name="Sarah",
            service="polishing",
            date="2026-04-11",  # Friday, not Sunday
            time="14:00",
            phone=phone
        )
        
        # Should have deleted old event
        assert result["ok"] is True, f"Expected ok=True but got: {result}"
        mock_events.delete.assert_called_once()
        delete_kwargs = mock_events.delete.call_args[1]
        assert delete_kwargs["eventId"] == "evt-sarah-old"

    @patch("app.now_local")
    @patch("app.get_calendar")
    def test_different_patient_name_keeps_old_booking(self, mock_get_cal, mock_now):
        """When booking for different patient, old event is NOT deleted."""
        mock_now.return_value = datetime(2026, 4, 1, 10, 0, tzinfo=TZ)
        
        phone = "+60123456783"
        
        # Create booking for Old Patient
        save_booking_record(
            user=phone,
            event_id="evt-old-patient",
            service="scaling",
            name="Old Patient",
            date="2026-04-10",
            time="10:00"
        )
        
        # Mock calendar
        mock_events = MagicMock()
        mock_events.list.return_value.execute.return_value = {"items": []}
        mock_events.insert.return_value.execute.return_value = {"id": "evt-new-patient"}
        mock_get_cal.return_value = MagicMock()
        mock_get_cal.return_value.events.return_value = mock_events
        
        # Book for New Patient (different name) on Friday April 11
        result = create_booking(
            name="New Patient",
            service="polishing",
            date="2026-04-11",  # Friday, not Sunday
            time="14:00",
            phone=phone
        )
        
        # Should NOT have deleted old event (different patient)
        assert result["ok"] is True, f"Expected ok=True but got: {result}"
        mock_events.delete.assert_not_called()


class TestBugFix14EndTimeValidation:
    """
    BUG #14: Appointment end time must be validated against closing hours.
    
    Scenario: Clinic closes at 9pm. User books 90-min service at 8pm.
    Appointment would end at 9:30pm, past closing. Should be rejected.
    """

    def test_end_time_within_hours_passes(self):
        """Appointment that ends within business hours should pass."""
        clinic = {
            "open_hour": 9,   # 9am
            "close_hour": 21, # 9pm
        }
        
        # 60-min appointment from 8pm to 9pm (within hours)
        start = datetime(2026, 4, 10, 20, 0, tzinfo=TZ)  # 8pm
        end = datetime(2026, 4, 10, 21, 0, tzinfo=TZ)    # 9pm
        
        result = is_within_business_hours(clinic, start, end)
        assert result is True

    def test_end_time_past_closing_fails(self):
        """Appointment that ends after closing time should fail."""
        clinic = {
            "open_hour": 9,   # 9am
            "close_hour": 21, # 9pm
        }
        
        # 90-min appointment from 8pm to 9:30pm (past closing)
        start = datetime(2026, 4, 10, 20, 0, tzinfo=TZ)  # 8pm
        end = datetime(2026, 4, 10, 21, 30, tzinfo=TZ)   # 9:30pm
        
        result = is_within_business_hours(clinic, start, end)
        assert result is False

    def test_start_time_before_opening_fails(self):
        """Appointment that starts before opening should fail."""
        clinic = {
            "open_hour": 9,
            "close_hour": 21,
        }
        
        # Appointment from 8:30am to 9:30am (starts before opening)
        start = datetime(2026, 4, 10, 8, 30, tzinfo=TZ)
        end = datetime(2026, 4, 10, 9, 30, tzinfo=TZ)
        
        result = is_within_business_hours(clinic, start, end)
        assert result is False

    def test_exactly_at_closing_time_passes(self):
        """Appointment that ends exactly at closing time should pass."""
        clinic = {
            "open_hour": 9,
            "close_hour": 21,
        }
        
        # Appointment from 8:30pm to 9pm (ends exactly at closing)
        start = datetime(2026, 4, 10, 20, 30, tzinfo=TZ)
        end = datetime(2026, 4, 10, 21, 0, tzinfo=TZ)
        
        result = is_within_business_hours(clinic, start, end)
        assert result is True
