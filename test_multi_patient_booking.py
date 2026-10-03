"""
Test to reproduce the multi-patient back-to-back booking response mismatch bug.

Scenario from user report:
- Patient 1 (Daniel) books for 90-min whitening at 10:00 → ✅ Succeeds
- Patient 2 (Mary) books for 90-min whitening at 11:30 → ✅ ACTUALLY booked BUT assistant said it failed

Expected behavior:
- Both bookings should succeed
- Both assistant responses should say "success"

Actual behavior:
- First booking: succeeds, assistant says success ✅
- Second booking: succeeds in backend ✅, but assistant says failure ❌
"""

import sys
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
from app import (
    reset_user_session,
    run_ai,
    get_all_bookings,
    update_booking_state,
    dispatch_tool,
    get_default_clinic,
    get_direct_reply,
)

TZ = ZoneInfo("Asia/Kuala_Lumpur")


class TestMultiPatientBackToBackBooking:
    """Reproduce response mismatch bug for back-to-back family bookings."""
    
    def test_back_to_back_90min_bookings(self):
        """
        Test booking two patients back-to-back for 90-min service.
        
        Timeline:
        - Daniel: 10:00-11:30 (whitening, 90 min)
        - Mary:   11:30-13:00 (whitening, 90 min) - edge-to-edge, should work
        """
        phone = "+60123456999"
        reset_user_session(phone)
        
        # Mock current time: 9:00 AM on Jan 15, 2026 (both appointments in future)
        mock_now = datetime(2026, 1, 15, 9, 0, tzinfo=TZ)
        
        # Get clinic config
        clinic = get_default_clinic()
        assert "whitening" in clinic["services"]
        assert clinic["services"]["whitening"] == 90  # 90-minute service
        
        with patch("app.now_local") as mock_now_fn:
            mock_now_fn.return_value = mock_now
            
            with patch("app.get_calendar") as mock_get_cal:
                # Track calendar events
                calendar_events = []
                
                def create_event_side_effect(calendarId, body):
                    """Simulate Google Calendar event creation."""
                    event_id = f"evt-{len(calendar_events) + 1}"
                    calendar_events.append({
                        "id": event_id,
                        "summary": body["summary"],
                        "start": body["start"],
                        "end": body["end"],
                        "description": body.get("description", ""),
                    })
                    return MagicMock(execute=lambda: {"id": event_id})
                
                def list_events_side_effect(calendarId, timeMin, timeMax, singleEvents=True, orderBy=None):
                    """Simulate Google Calendar event listing."""
                    # Parse the time window
                    from datetime import datetime
                    window_start = datetime.fromisoformat(timeMin)
                    window_end = datetime.fromisoformat(timeMax)
                    
                    # Find overlapping events
                    overlapping = []
                    for evt in calendar_events:
                        evt_start = datetime.fromisoformat(evt["start"]["dateTime"])
                        evt_end = datetime.fromisoformat(evt["end"]["dateTime"])
                        
                        # Google Calendar API semantics:
                        # Returns events where: event.end > timeMin AND event.start < timeMax
                        if evt_end > window_start and evt_start < window_end:
                            overlapping.append(evt)
                    
                    return MagicMock(execute=lambda: {"items": overlapping})
                
                mock_events = MagicMock()
                mock_events.insert.side_effect = create_event_side_effect
                mock_events.list.side_effect = list_events_side_effect
                mock_events.get.return_value.execute.return_value = {}
                
                mock_cal = MagicMock()
                mock_cal.events.return_value = mock_events
                mock_get_cal.return_value = mock_cal
                
                # === BOOKING 1: Daniel at 10:00 ===
                print("\n=== BOOKING 1: Daniel at 10:00 (whitening 90min) ===")
                
                update_booking_state(phone, clinic_id=clinic["id"], availability_ok=True)
                result1 = dispatch_tool(
                    "create_booking",
                    {
                        "name": "Daniel",
                        "service": "whitening",
                        "date": "2026-01-15",
                        "time": "10:00",
                    },
                    phone,
                    clinic
                )
                
                print(f"Result 1: {result1}")
                assert result1["ok"] is True, "Daniel's booking should succeed"
                assert result1["event_id"] == "evt-1"
                assert result1["name"] == "Daniel"
                
                # Verify event was created in "calendar"
                assert len(calendar_events) == 1
                daniel_evt = calendar_events[0]
                assert "Daniel" in daniel_evt["summary"]
                assert daniel_evt["start"]["dateTime"] == "2026-01-15T10:00:00+08:00"
                assert daniel_evt["end"]["dateTime"] == "2026-01-15T11:30:00+08:00"
                
                # === BOOKING 2: Mary at 11:30 (edge-to-edge with Daniel) ===
                print("\n=== BOOKING 2: Mary at 11:30 (whitening 90min) ===")
                
                update_booking_state(phone, clinic_id=clinic["id"], availability_ok=True)
                result2 = dispatch_tool(
                    "create_booking",
                    {
                        "name": "Mary",
                        "service": "whitening",
                        "date": "2026-01-15",
                        "time": "11:30",
                    },
                    phone,
                    clinic
                )
                
                print(f"Result 2: {result2}")
                
                # CRITICAL ASSERTION: Mary's booking should succeed
                # Daniel ends at 11:30, Mary starts at 11:30 - edge-to-edge is OK
                assert result2["ok"] is True, (
                    f"Mary's booking should succeed (edge-to-edge with Daniel). "
                    f"Got: {result2}"
                )
                assert result2["event_id"] == "evt-2"
                assert result2["name"] == "Mary"
                
                # Verify both events exist
                assert len(calendar_events) == 2
                mary_evt = calendar_events[1]
                assert "Mary" in mary_evt["summary"]
                assert mary_evt["start"]["dateTime"] == "2026-01-15T11:30:00+08:00"
                assert mary_evt["end"]["dateTime"] == "2026-01-15T13:00:00+08:00"
                
                # Verify database
                all_bookings = get_all_bookings(phone)
                assert len(all_bookings) == 2
                names = {b["name"] for b in all_bookings}
                assert "Daniel" in names
                assert "Mary" in names
                
                print("\n✅ Both bookings succeeded in backend")
                print(f"   Daniel: 10:00-11:30 (evt-1)")
                print(f"   Mary:   11:30-13:00 (evt-2)")


    def test_availability_check_edge_to_edge(self):
        """
        Test that check_availability correctly handles edge-to-edge bookings.
        
        If an appointment ends at 11:30, the next appointment should be
        able to start at 11:30 (no conflict).
        """
        from app import check_availability, save_booking_record
        
        phone = "+60123456998"
        reset_user_session(phone)
        
        mock_now = datetime(2026, 1, 15, 9, 0, tzinfo=TZ)
        clinic = get_default_clinic()
        
        with patch("app.now_local") as mock_now_fn:
            mock_now_fn.return_value = mock_now
            
            with patch("app.get_calendar") as mock_get_cal:
                # Pre-existing event: 10:00-11:30
                existing_event = {
                    "id": "evt-existing",
                    "summary": "Whitening - Daniel",
                    "start": {"dateTime": "2026-01-15T10:00:00+08:00"},
                    "end": {"dateTime": "2026-01-15T11:30:00+08:00"},
                }
                
                def list_events_side_effect(calendarId, timeMin, timeMax, singleEvents=True, orderBy=None):
                    window_start = datetime.fromisoformat(timeMin)
                    window_end = datetime.fromisoformat(timeMax)
                    evt_start = datetime.fromisoformat(existing_event["start"]["dateTime"])
                    evt_end = datetime.fromisoformat(existing_event["end"]["dateTime"])
                    
                    # Google Calendar semantics: event.end > timeMin AND event.start < timeMax
                    if evt_end > window_start and evt_start < window_end:
                        return MagicMock(execute=lambda: {"items": [existing_event]})
                    else:
                        return MagicMock(execute=lambda: {"items": []})
                
                mock_events = MagicMock()
                mock_events.list.side_effect = list_events_side_effect
                mock_cal = MagicMock()
                mock_cal.events.return_value = mock_events
                mock_get_cal.return_value = mock_cal
                
                # Check availability for 11:30-13:00 (should be AVAILABLE)
                result = check_availability(
                    service="whitening",
                    date="2026-01-15",
                    time="11:30",
                    clinic=clinic
                )
                
                print(f"Availability check result: {result}")
                
                # Edge-to-edge should NOT conflict
                assert result["ok"] is True, (
                    f"11:30 start should be available when prior event ends at 11:30. "
                    f"Got: {result}"
                )


class TestSinglePatientPolicy:
    """
    The system books one patient at a time only.
    Multi-patient features (sequential_flow, partial_success with booked_patients/failed_patient/
    suggested_next_slot) are NOT implemented. These tests verify the single-patient policy
    and replace previously phantom assertions that referenced unimplemented behaviour.
    """

    def test_same_time_question_redirects_to_back_to_back(self, caplog):
        """
        When a user asks if two patients can share the same slot,
        get_direct_reply must return a back-to-back explanation and log
        MULTI_PATIENT_MODE_REQUEST — never silently confirm concurrent booking.
        """
        phone = "+60123456010"
        reset_user_session(phone)
        clinic = get_default_clinic()

        update_booking_state(
            phone,
            clinic_id=clinic["id"],
            service="braces consultation",
            date="2026-01-15",
            time="10:00",
            availability_ok=True,
        )

        with caplog.at_level("INFO"):
            reply = get_direct_reply(phone, "they can both be the same time?", clinic=clinic)

        assert reply is not None
        assert "back-to-back" in reply.lower()
        # Must not give a simple "yes" confirmation that concurrent booking is possible.
        assert reply.strip().lower() != "yes"
        assert any("MULTI_PATIENT_MODE_REQUEST" in r.message for r in caplog.records)

    def test_multi_name_in_create_booking_is_rejected(self):
        """
        When the LLM passes a combined name like "Daniel and John" to create_booking,
        dispatch_tool must return ok=False with a single-patient instruction.
        No booking must be created.
        """
        phone = "+60123456012"
        reset_user_session(phone)
        clinic = get_default_clinic()

        update_booking_state(
            phone,
            clinic_id=clinic["id"],
            service="scaling",
            date="2026-01-15",
            time="10:00",
            availability_ok=True,
        )

        result = dispatch_tool(
            "create_booking",
            {
                "name": "Daniel and John",
                "service": "scaling",
                "date": "2026-01-15",
                "time": "10:00",
            },
            phone,
            clinic,
        )

        assert result["ok"] is False
        # The message must tell the LLM to book patients separately.
        msg = result.get("message", "").lower()
        assert "one at a time" in msg
        # No booking should have been created.
        all_bookings = get_all_bookings(phone)
        assert len(all_bookings) == 0

    def test_second_patient_at_same_slot_is_rejected(self):
        """
        After Daniel is booked at 10:00, attempting to book Mary at the same
        10:00 slot must fail at check_availability (slot is unavailable).
        This is not partial_success — it is a plain availability rejection.
        No phantom partial_success/booked_patients/failed_patient keys should appear.
        """
        phone = "+60123456011"
        reset_user_session(phone)
        clinic = get_default_clinic()
        mock_now = datetime(2026, 1, 15, 9, 0, tzinfo=TZ)

        with patch("app.now_local", return_value=mock_now):
            with patch("app.get_calendar") as mock_get_cal:
                calendar_events = []

                def create_event_side_effect(calendarId, body):
                    event_id = f"evt-partial-{len(calendar_events) + 1}"
                    calendar_events.append({
                        "id": event_id,
                        "summary": body["summary"],
                        "start": body["start"],
                        "end": body["end"],
                    })
                    return MagicMock(execute=lambda: {"id": event_id})

                def list_events_side_effect(calendarId, timeMin, timeMax, singleEvents=True, orderBy=None):
                    window_start = datetime.fromisoformat(timeMin)
                    window_end = datetime.fromisoformat(timeMax)
                    overlapping = []
                    for evt in calendar_events:
                        evt_start = datetime.fromisoformat(evt["start"]["dateTime"])
                        evt_end = datetime.fromisoformat(evt["end"]["dateTime"])
                        if evt_end > window_start and evt_start < window_end:
                            overlapping.append(evt)
                    return MagicMock(execute=lambda: {"items": overlapping})

                mock_events = MagicMock()
                mock_events.insert.side_effect = create_event_side_effect
                mock_events.list.side_effect = list_events_side_effect
                mock_get_cal.return_value = MagicMock()
                mock_get_cal.return_value.events.return_value = mock_events

                # Book Daniel at 10:00 successfully.
                update_booking_state(phone, clinic_id=clinic["id"], availability_ok=True)
                result1 = dispatch_tool(
                    "create_booking",
                    {
                        "name": "Daniel",
                        "service": "braces consultation",
                        "date": "2026-01-15",
                        "time": "10:00",
                    },
                    phone,
                    clinic,
                )
                assert result1["ok"] is True

                # Attempt to book Mary at the same 10:00 slot — must fail.
                update_booking_state(phone, clinic_id=clinic["id"], availability_ok=True)
                result2 = dispatch_tool(
                    "create_booking",
                    {
                        "name": "Mary",
                        "service": "braces consultation",
                        "date": "2026-01-15",
                        "time": "10:00",
                    },
                    phone,
                    clinic,
                )

                assert result2["ok"] is False
                # Must NOT expose phantom multi-patient keys.
                assert "partial_success" not in result2
                assert "booked_patients" not in result2
                assert "failed_patient" not in result2
                assert "suggested_next_slot" not in result2

                # Only Daniel's booking exists.
                all_bookings = get_all_bookings(phone)
                assert len(all_bookings) == 1
                assert all_bookings[0]["name"] == "Daniel"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
