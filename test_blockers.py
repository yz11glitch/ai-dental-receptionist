"""
Tests for five production-blocking bug fixes:
  C6 - duplicate get_clinic_by_id
  C1 - cancel_booking name targeting
  C2 - reschedule_booking name targeting
  C4 - partial_success LLM instruction
  m4 - find_next_available_slot parallel capacity
"""

import sys
import pytest
from unittest.mock import patch, MagicMock, call
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")


# ---------------------------------------------------------------------------
# C6: only one get_clinic_by_id, returns None on missing clinic
# ---------------------------------------------------------------------------

class TestGetClinicById:
    def test_single_definition(self):
        """Only one get_clinic_by_id must exist in app module."""
        import app
        import inspect
        # Count how many names map to a function named get_clinic_by_id
        # (can't have two in Python, but the original duplicate shadowed silently).
        assert hasattr(app, "get_clinic_by_id")
        fn = getattr(app, "get_clinic_by_id")
        assert callable(fn)

    def test_returns_none_for_missing_clinic(self):
        """get_clinic_by_id must return None (not raise) for unknown clinic id."""
        from app import get_clinic_by_id
        with patch("app.SessionLocal") as mock_session_cls:
            mock_db = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_db.query.return_value.filter.return_value.first.return_value = None

            result = get_clinic_by_id(999)
            assert result is None, "Expected None for missing clinic, not an exception"

    def test_process_reminders_guard_is_reachable(self):
        """process_reminders must not crash when clinic is missing (guard was previously unreachable)."""
        from app import BookingRecordModel, get_clinic_by_id
        with patch("app.SessionLocal") as mock_session_cls, \
             patch("app.get_clinic_by_id", return_value=None):
            mock_db = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)

            record = MagicMock(spec=BookingRecordModel)
            record.clinic_id = 999
            record.user = "+60111111111"
            mock_db.query.return_value.all.return_value = [record]

            from app import process_reminders
            # Should not raise — the "if not clinic" guard must be reachable now.
            result = process_reminders()
            assert result["errors"] >= 1  # Missing clinic counts as an error


# ---------------------------------------------------------------------------
# C1: cancel_booking name targeting
# ---------------------------------------------------------------------------

class TestCancelBookingNameTargeting:
    """cancel_booking must cancel the right patient when a name is given."""

    def _make_booking(self, name, service="scaling", date="2026-05-01", time="10:00"):
        return {
            "event_id": f"evt_{name.lower().replace(' ', '_')}",
            "name": name,
            "service": service,
            "date": date,
            "time": time,
            "status": "Confirmed",
            "clinic_id": 1,
            "reminder_1d_sent": False,
            "reminder_2h_sent": False,
        }

    def test_cancel_correct_patient_by_name(self):
        """Cancels only the booking whose name matches; does not touch others."""
        from app import cancel_booking

        daniel_booking = self._make_booking("Daniel")
        mary_booking = self._make_booking("Mary", time="11:00")

        with patch("app.get_all_bookings", return_value=[daniel_booking, mary_booking]), \
             patch("app.get_default_clinic", return_value={
                 "google_calendar_id": "cal123", "id": 1, "name": "Test Clinic",
                 "timezone": "Asia/Kuala_Lumpur",
             }), \
             patch("app.get_calendar") as mock_get_cal, \
             patch("app.delete_booking_record") as mock_delete, \
             patch("app.reset_booking_state"):

            mock_cal = MagicMock()
            mock_get_cal.return_value = mock_cal

            result = cancel_booking(phone="+60111111111", name="Daniel")

            assert result["ok"] is True
            # Should delete Daniel's event_id, not Mary's.
            mock_delete.assert_called_once_with("evt_daniel")

    def test_cancel_wrong_name_returns_error(self):
        """Returns ok=False when the named patient has no booking."""
        from app import cancel_booking

        bookings = [self._make_booking("Daniel")]

        with patch("app.get_all_bookings", return_value=bookings), \
             patch("app.get_default_clinic", return_value={
                 "google_calendar_id": "cal123", "id": 1, "name": "Test Clinic",
                 "timezone": "Asia/Kuala_Lumpur",
             }):
            result = cancel_booking(phone="+60111111111", name="NotExisting")
            assert result["ok"] is False
            assert "NotExisting" in result["message"]

    def test_cancel_no_name_multiple_bookings_returns_clarification(self):
        """When multiple bookings exist and no name is given, refuses and asks for clarification."""
        from app import cancel_booking

        daniel_booking = self._make_booking("Daniel")
        mary_booking = self._make_booking("Mary", time="11:00")

        with patch("app.get_all_bookings", return_value=[daniel_booking, mary_booking]), \
             patch("app.get_default_clinic", return_value={
                 "google_calendar_id": "cal123", "id": 1, "name": "Test Clinic",
                 "timezone": "Asia/Kuala_Lumpur",
             }), \
             patch("app.delete_booking_record") as mock_delete:

            result = cancel_booking(phone="+60111111111")
            assert result["ok"] is False
            assert "multiple bookings" in result["message"].lower()
            # Nothing should be deleted.
            mock_delete.assert_not_called()

    def test_cancel_no_name_single_booking_works(self):
        """Single booking, no name: cancel proceeds normally (no regression)."""
        from app import cancel_booking

        booking = self._make_booking("Daniel")

        with patch("app.get_all_bookings", return_value=[booking]), \
             patch("app.get_default_clinic", return_value={
                 "google_calendar_id": "cal123", "id": 1, "name": "Test Clinic",
                 "timezone": "Asia/Kuala_Lumpur",
             }), \
             patch("app.get_calendar") as mock_get_cal, \
             patch("app.delete_booking_record") as mock_delete, \
             patch("app.reset_booking_state"):

            mock_cal = MagicMock()
            mock_get_cal.return_value = mock_cal

            result = cancel_booking(phone="+60111111111")
            assert result["ok"] is True
            mock_delete.assert_called_once_with("evt_daniel")


# ---------------------------------------------------------------------------
# C2: reschedule_booking name targeting
# ---------------------------------------------------------------------------

class TestRescheduleBookingNameTargeting:
    """reschedule_booking must act on the correct patient when a name is given."""

    def _make_booking(self, name, event_id=None, service="scaling", date="2026-05-01", time="10:00"):
        return {
            "event_id": event_id or f"evt_{name.lower().replace(' ', '_')}",
            "name": name,
            "service": service,
            "date": date,
            "time": time,
            "status": "Confirmed",
            "clinic_id": 1,
            "reminder_1d_sent": False,
            "reminder_2h_sent": False,
        }

    def _make_clinic(self):
        return {
            "id": 1,
            "name": "Test Clinic",
            "timezone": "Asia/Kuala_Lumpur",
            "open_hour": 10,
            "close_hour": 18,
            "slot_minutes": 30,
            "google_calendar_id": "cal123",
            "services": {"scaling": 60},
            "special_closures": [],
            "closure_notes": {},
        }

    def test_reschedule_correct_patient_by_name(self):
        """Rescheduling targets only the named patient's event_id."""
        from app import reschedule_booking

        daniel = self._make_booking("Daniel", event_id="evt_daniel")
        mary = self._make_booking("Mary", event_id="evt_mary", time="11:00")
        clinic = self._make_clinic()

        new_date = "2026-05-10"
        new_time = "14:00"

        with patch("app.get_all_bookings", return_value=[daniel, mary]), \
             patch("app.check_availability", return_value={"ok": True}), \
             patch("app.parse_slot") as mock_parse, \
             patch("app.get_calendar") as mock_get_cal, \
             patch("app.save_booking_record"), \
             patch("app.format_slot", return_value="Saturday, 10 May 2026 at 2:00 PM"):

            tz = ZoneInfo("Asia/Kuala_Lumpur")
            start_dt = datetime(2026, 5, 10, 14, 0, tzinfo=tz)
            mock_parse.return_value = start_dt

            mock_cal = MagicMock()
            mock_cal.events.return_value.get.return_value.execute.return_value = {
                "summary": "[Confirmed] Scaling - Daniel",
                "start": {"dateTime": "2026-05-01T10:00:00+08:00"},
                "end": {"dateTime": "2026-05-01T11:00:00+08:00"},
            }
            mock_get_cal.return_value = mock_cal

            result = reschedule_booking(
                phone="+60111111111",
                service="scaling",
                date=new_date,
                time=new_time,
                clinic=clinic,
                name="Daniel",
            )

            assert result["ok"] is True
            # Must have called get on Daniel's event_id, not Mary's.
            mock_cal.events.return_value.get.assert_called_with(
                calendarId="cal123",
                eventId="evt_daniel",
            )

    def test_reschedule_no_name_multiple_bookings_returns_clarification(self):
        """When multiple bookings exist and no name is given, refuse to reschedule."""
        from app import reschedule_booking

        daniel = self._make_booking("Daniel")
        mary = self._make_booking("Mary", time="11:00")
        clinic = self._make_clinic()

        with patch("app.get_all_bookings", return_value=[daniel, mary]):
            result = reschedule_booking(
                phone="+60111111111",
                service="scaling",
                date="2026-05-10",
                time="14:00",
                clinic=clinic,
            )
            assert result["ok"] is False
            assert "multiple bookings" in result["message"].lower()

    def test_reschedule_single_booking_no_name_works(self):
        """Single booking, no name: reschedule proceeds normally (no regression)."""
        from app import reschedule_booking

        daniel = self._make_booking("Daniel", event_id="evt_daniel")
        clinic = self._make_clinic()

        with patch("app.get_all_bookings", return_value=[daniel]), \
             patch("app.check_availability", return_value={"ok": True}), \
             patch("app.parse_slot") as mock_parse, \
             patch("app.get_calendar") as mock_get_cal, \
             patch("app.save_booking_record"), \
             patch("app.format_slot", return_value="Saturday, 10 May 2026 at 2:00 PM"):

            tz = ZoneInfo("Asia/Kuala_Lumpur")
            mock_parse.return_value = datetime(2026, 5, 10, 14, 0, tzinfo=tz)

            mock_cal = MagicMock()
            mock_cal.events.return_value.get.return_value.execute.return_value = {
                "summary": "[Confirmed] Scaling - Daniel",
                "start": {"dateTime": "2026-05-01T10:00:00+08:00"},
                "end": {"dateTime": "2026-05-01T11:00:00+08:00"},
            }
            mock_get_cal.return_value = mock_cal

            result = reschedule_booking(
                phone="+60111111111",
                service="scaling",
                date="2026-05-10",
                time="14:00",
                clinic=clinic,
            )
            assert result["ok"] is True


# ---------------------------------------------------------------------------
# C4: partial_success system prompt instruction
# ---------------------------------------------------------------------------

class TestPartialSuccessSystemPromptInstruction:
    """The system prompt must instruct the LLM correctly about partial_success=True + ok=False."""

    def test_system_prompt_has_partial_success_instruction(self):
        """System prompt must contain an explicit instruction for the partial_success=True + ok=False case."""
        from app import build_system_prompt

        with patch("app.get_default_clinic", return_value={
            "id": 1,
            "name": "Test Clinic",
            "location": "KL",
            "timezone": "Asia/Kuala_Lumpur",
            "open_hour": 10,
            "close_hour": 18,
            "slot_minutes": 30,
            "hours_text": "Mon-Sat 10-18",
            "opening_message": "",
            "promo_message": "",
            "google_calendar_id": "cal123",
            "twilio_number": "",
            "services": {"scaling": 60},
            "service_prices": {},
            "special_closures": [],
            "closure_notes": {},
        }), \
        patch("app.get_all_bookings", return_value=[]), \
        patch("app.get_booking_state", return_value={
            "service": None, "date": None, "time": None, "name": None, "availability_ok": False
        }):
            prompt = build_system_prompt("+60111111111")

        # Must have instruction covering partial_success=True + ok=False
        assert "partial_success" in prompt
        assert "ok=false" in prompt.lower()
        # Must warn against false confirmation
        assert "not booked" in prompt.lower() or "failed" in prompt.lower()

    def test_system_prompt_instruction_distinguishes_partial_from_full_success(self):
        """The instruction must clarify the LLM cannot say all patients are confirmed on partial success."""
        from app import build_system_prompt

        with patch("app.get_default_clinic", return_value={
            "id": 1,
            "name": "Test Clinic",
            "location": "KL",
            "timezone": "Asia/Kuala_Lumpur",
            "open_hour": 10,
            "close_hour": 18,
            "slot_minutes": 30,
            "hours_text": "Mon-Sat 10-18",
            "opening_message": "",
            "promo_message": "",
            "google_calendar_id": "cal123",
            "twilio_number": "",
            "services": {"scaling": 60},
            "service_prices": {},
            "special_closures": [],
            "closure_notes": {},
        }), \
        patch("app.get_all_bookings", return_value=[]), \
        patch("app.get_booking_state", return_value={
            "service": None, "date": None, "time": None, "name": None, "availability_ok": False
        }):
            prompt = build_system_prompt("+60111111111")

        # The specific instruction added for Fix C4 must be present
        assert "partial_success=true" in prompt.lower() or "partial_success=True" in prompt
        assert "Do NOT" in prompt or "do not" in prompt.lower()


# ---------------------------------------------------------------------------
# m4: find_next_available_slot respects parallel_capacity
# ---------------------------------------------------------------------------

class TestFindNextAvailableSlotCapacity:
    """find_next_available_slot must skip a slot only when it is full (>= capacity), not just occupied."""

    def _make_clinic(self, parallel_capacity=1):
        return {
            "id": 1,
            "name": "Test Clinic",
            "timezone": "Asia/Kuala_Lumpur",
            "open_hour": 10,
            "close_hour": 18,
            "slot_minutes": 30,
            "google_calendar_id": "cal123",
            "services": {"scaling": 60},
            "special_closures": [],
            "closure_notes": {},
            "parallel_booking_capacity": parallel_capacity,
        }

    def _make_event(self, start_iso, end_iso):
        return {
            "start": {"dateTime": start_iso},
            "end": {"dateTime": end_iso},
        }

    def test_slot_not_skipped_when_under_capacity(self):
        """A slot with 1 booking is still available when capacity=2."""
        from app import find_next_available_slot

        clinic = self._make_clinic(parallel_capacity=2)
        tz = "Asia/Kuala_Lumpur"

        # One existing event at 10:00-11:00. With capacity=2 the 10:00 slot should still be returned.
        events = [self._make_event(
            f"2026-05-01T10:00:00+08:00",
            f"2026-05-01T11:00:00+08:00",
        )]

        mock_cal = MagicMock()
        mock_cal.events.return_value.list.return_value.execute.return_value = {"items": events}

        with patch("app.get_calendar", return_value=mock_cal), \
             patch("app.now_local", return_value=datetime(2026, 4, 30, 9, 0, tzinfo=ZoneInfo(tz))):

            result = find_next_available_slot(
                service="scaling",
                date="2026-05-01",
                time="10:00",
                clinic=clinic,
            )

        assert result["ok"] is True
        assert result["date"] == "2026-05-01"
        assert result["time"] == "10:00"

    def test_slot_skipped_when_at_capacity(self):
        """A slot at capacity=1 with 1 booking must be skipped."""
        from app import find_next_available_slot

        clinic = self._make_clinic(parallel_capacity=1)
        tz = "Asia/Kuala_Lumpur"

        # One existing event blocks 10:00-11:00. Next slot is 10:30.
        events = [self._make_event(
            "2026-05-01T10:00:00+08:00",
            "2026-05-01T11:00:00+08:00",
        )]

        mock_cal = MagicMock()
        mock_cal.events.return_value.list.return_value.execute.return_value = {"items": events}

        with patch("app.get_calendar", return_value=mock_cal), \
             patch("app.now_local", return_value=datetime(2026, 4, 30, 9, 0, tzinfo=ZoneInfo(tz))):

            result = find_next_available_slot(
                service="scaling",
                date="2026-05-01",
                time="10:00",
                clinic=clinic,
            )

        assert result["ok"] is True
        # 10:00 is blocked; 10:30 overlaps too (10:30-11:30 overlaps 10:00-11:00);
        # 11:00 does not overlap — that should be the result.
        assert result["time"] == "11:00"

    def test_slot_skipped_when_at_capacity_multi_chair(self):
        """A slot with 2 bookings at capacity=2 must be skipped."""
        from app import find_next_available_slot

        clinic = self._make_clinic(parallel_capacity=2)
        tz = "Asia/Kuala_Lumpur"

        events = [
            self._make_event("2026-05-01T10:00:00+08:00", "2026-05-01T11:00:00+08:00"),
            self._make_event("2026-05-01T10:00:00+08:00", "2026-05-01T11:00:00+08:00"),
        ]

        mock_cal = MagicMock()
        mock_cal.events.return_value.list.return_value.execute.return_value = {"items": events}

        with patch("app.get_calendar", return_value=mock_cal), \
             patch("app.now_local", return_value=datetime(2026, 4, 30, 9, 0, tzinfo=ZoneInfo(tz))):

            result = find_next_available_slot(
                service="scaling",
                date="2026-05-01",
                time="10:00",
                clinic=clinic,
            )

        assert result["ok"] is True
        # Both chairs occupied at 10:00 — should advance to 11:00.
        assert result["time"] == "11:00"


# ---------------------------------------------------------------------------
# Human escalation detection and webhook short-circuit
# ---------------------------------------------------------------------------

class TestIsHumanEscalationRequest:
    """is_human_escalation_request must reliably detect escalation intent."""

    def test_english_single_word_exact(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("human") is True
        assert is_human_escalation_request("agent") is True
        assert is_human_escalation_request("staff") is True
        assert is_human_escalation_request("receptionist") is True
        assert is_human_escalation_request("operator") is True
        assert is_human_escalation_request("call me") is True
        assert is_human_escalation_request("real person") is True

    def test_english_combined_phrases(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("can i talk to agent") is True
        assert is_human_escalation_request("i wanna talk to human") is True
        assert is_human_escalation_request("give me agent") is True
        assert is_human_escalation_request("i want a real person") is True
        assert is_human_escalation_request("let me speak to staff") is True
        assert is_human_escalation_request("connect me to receptionist") is True
        assert is_human_escalation_request("I need a human") is True
        assert is_human_escalation_request("Can I speak with a real person please?") is True
        assert is_human_escalation_request("talk to human") is True
        assert is_human_escalation_request("speak to agent") is True

    def test_english_case_insensitive(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("CAN I TALK TO AGENT") is True
        assert is_human_escalation_request("HUMAN") is True
        assert is_human_escalation_request("I Want To Speak With Staff") is True

    def test_mandarin_escalation(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("人工") is True
        assert is_human_escalation_request("客服") is True
        assert is_human_escalation_request("真人") is True
        assert is_human_escalation_request("转人工") is True
        assert is_human_escalation_request("我要找人") is True
        assert is_human_escalation_request("帮我转接") is True
        assert is_human_escalation_request("请转人工服务") is True
        assert is_human_escalation_request("让我跟真人说话") is True

    def test_bm_escalation(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("saya nak cakap dengan manusia") is True
        assert is_human_escalation_request("tolong hubungkan") is True
        assert is_human_escalation_request("saya nak cakap dengan resepsionis") is True
        assert is_human_escalation_request("bercakap dengan kakitangan") is True
        assert is_human_escalation_request("orang sebenar") is True

    def test_non_escalation_booking_messages(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("I want to book scaling") is False
        assert is_human_escalation_request("Can I make an appointment for tomorrow?") is False
        assert is_human_escalation_request("What are your opening hours?") is False
        assert is_human_escalation_request("I need to reschedule") is False
        assert is_human_escalation_request("cancel my appointment") is False
        assert is_human_escalation_request("") is False
        assert is_human_escalation_request("Hello") is False

    def test_non_escalation_bm_booking(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("saya nak buat temujanji") is False
        assert is_human_escalation_request("boleh saya tahu waktu operasi?") is False


class TestEscalationWebhookBehavior:
    """Webhook-level escalation must short-circuit run_ai and send exact message."""

    CLINIC = {
        "id": 1,
        "name": "Test Clinic",
        "twilio_number": "whatsapp:+60111111111",
        "human_contact_number": "+60177777777",
        "location": "KL",
        "timezone": "Asia/Kuala_Lumpur",
        "open_hour": 10,
        "close_hour": 18,
        "slot_minutes": 30,
        "hours_text": "Mon-Sat 10-18",
        "opening_message": "",
        "promo_message": "",
        "google_calendar_id": "cal123",
        "services": {"scaling": 60},
        "service_prices": {},
        "special_closures": [],
        "closure_notes": {},
    }

    def _post_whatsapp(self, app_client, msg, from_="+60199999999", to="whatsapp:+60111111111"):
        """POST a correctly signed webhook (real Twilio signature validation runs)."""
        import app as app_module
        from twilio.request_validator import RequestValidator
        data = {"Body": msg, "From": from_, "To": to}
        signature = RequestValidator(app_module.TWILIO_AUTH_TOKEN).compute_signature(
            "http://localhost/whatsapp", data
        )
        return app_client.post("/whatsapp", data=data, headers={"X-Twilio-Signature": signature})

    def test_escalation_short_circuits_ai(self):
        """run_ai must NOT be called when escalation is detected."""
        import app as app_module
        with app_module.app.test_client() as client:
            with patch("app.get_clinic_by_twilio_number", return_value=self.CLINIC), \
                 patch("app.is_human_escalation_request", return_value=True), \
                 patch("app.has_unresolved_human_flag", return_value=False), \
                 patch("app.write_conversation_flag") as mock_flag, \
                 patch("app.send_whatsapp_outbound") as mock_send, \
                 patch("app.run_ai") as mock_run_ai:

                self._post_whatsapp(client, "can i talk to agent")

                mock_run_ai.assert_not_called()
                mock_flag.assert_called_once_with(1, "+60199999999", "human_requested")

    def test_escalation_sends_correct_message(self):
        """Outbound must use the exact escalation acknowledgement string."""
        import app as app_module
        with app_module.app.test_client() as client:
            with patch("app.get_clinic_by_twilio_number", return_value=self.CLINIC), \
                 patch("app.is_human_escalation_request", return_value=True), \
                 patch("app.has_unresolved_human_flag", return_value=False), \
                 patch("app.write_conversation_flag"), \
                 patch("app.send_whatsapp_outbound") as mock_send, \
                 patch("app.run_ai"):

                self._post_whatsapp(client, "i wanna talk to human")

                mock_send.assert_called_once()
                call_args = mock_send.call_args
                body_sent = call_args[0][1] if call_args[0] else call_args[1].get("body")
                assert body_sent == (
                    "Sure — I'll notify our clinic team. "
                    "If you prefer, you can also contact us directly at +60177777777."
                )

    def test_escalation_creates_flag(self):
        """write_conversation_flag must be called with 'human_requested'."""
        import app as app_module
        with app_module.app.test_client() as client:
            with patch("app.get_clinic_by_twilio_number", return_value=self.CLINIC), \
                 patch("app.is_human_escalation_request", return_value=True), \
                 patch("app.has_unresolved_human_flag", return_value=False), \
                 patch("app.write_conversation_flag") as mock_flag, \
                 patch("app.send_whatsapp_outbound"), \
                 patch("app.run_ai"):

                self._post_whatsapp(client, "give me agent", from_="+60188888888")

                mock_flag.assert_called_once_with(1, "+60188888888", "human_requested")

    def test_repeated_escalation_suppresses_outbound_but_blocks_ai(self):
        """Second escalation: outbound suppressed (flag exists), run_ai still blocked."""
        import app as app_module
        with app_module.app.test_client() as client:
            with patch("app.get_clinic_by_twilio_number", return_value=self.CLINIC), \
                 patch("app.is_human_escalation_request", return_value=True), \
                 patch("app.has_unresolved_human_flag", return_value=True), \
                 patch("app.write_conversation_flag"), \
                 patch("app.send_whatsapp_outbound") as mock_send, \
                 patch("app.run_ai") as mock_run_ai:

                self._post_whatsapp(client, "agent please")

                mock_run_ai.assert_not_called()
                mock_send.assert_not_called()

    def test_normal_booking_message_reaches_ai(self):
        """Non-escalation messages must still reach run_ai."""
        import app as app_module
        with app_module.app.test_client() as client:
            with patch("app.get_clinic_by_twilio_number", return_value=self.CLINIC), \
                 patch("app.run_ai", return_value="Hello! How can I help?") as mock_run_ai:

                self._post_whatsapp(client, "I want to book scaling")

                mock_run_ai.assert_called_once()


# ---------------------------------------------------------------------------
# CRITICAL-2: Malaysian phrasing escalation detection
# ---------------------------------------------------------------------------

class TestMalaysianEscalationPhrases:
    """Common Malaysian phrasings that must trigger is_human_escalation_request."""

    def test_agent_pls(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("agent pls") is True

    def test_agent_please(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("agent please") is True

    def test_agent_lah(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("agent lah") is True

    def test_human_pls(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("human pls") is True

    def test_human_please(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("human please") is True

    def test_i_want_staff(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("I want staff") is True

    def test_i_want_receptionist(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("I want receptionist") is True

    def test_i_want_human(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("I want human") is True

    def test_get_me_agent(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("get me agent") is True

    def test_get_me_staff(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("get me staff") is True

    def test_get_me_human(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("get me human") is True

    def test_talk_to_someone(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("talk to someone") is True

    def test_speak_to_someone(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("speak to someone") is True

    def test_talk_to_doctor(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("talk to doctor") is True

    # False positive checks — these must NOT trigger escalation.

    def test_no_false_positive_i_want_to_book(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("I want to book scaling") is False

    def test_no_false_positive_i_want_scaling(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("I want scaling") is False

    def test_no_false_positive_agent_recommended_scaling(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("agent recommended scaling") is False

    def test_no_false_positive_i_want_appointment(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("I want to make an appointment") is False

    def test_no_false_positive_i_want_whitening(self):
        from app import is_human_escalation_request
        assert is_human_escalation_request("I want whitening") is False


# ---------------------------------------------------------------------------
# CRITICAL-3: Cross-clinic booking data isolation
# ---------------------------------------------------------------------------

class TestClinicIsolation:
    """get_all_bookings and get_existing_booking must be scoped by clinic_id."""

    def _make_record(self, event_id, phone, clinic_id, name, service="scaling",
                     date="2026-05-01", time="10:00"):
        from app import BookingRecordModel
        from datetime import datetime
        row = BookingRecordModel(
            event_id=event_id,
            user=phone,
            clinic_id=clinic_id,
            service=service,
            name=name,
            date=date,
            time=time,
            status="Confirmed",
            reminder_1d_sent=False,
            reminder_2h_sent=False,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        return row

    def test_get_all_bookings_isolated_by_clinic(self):
        """Two bookings for same phone at different clinics — filter returns only one."""
        from app import get_all_bookings, SessionLocal, BookingRecordModel
        from datetime import datetime

        phone = "+60199000001"
        # Clean slate.
        with SessionLocal() as db:
            db.query(BookingRecordModel).filter(BookingRecordModel.user == phone).delete()
            db.commit()

        # Insert one booking per clinic.
        with SessionLocal() as db:
            db.add(self._make_record("evt-c1-001", phone, clinic_id=1, name="Alice"))
            db.add(self._make_record("evt-c2-001", phone, clinic_id=2, name="Alice", time="11:00"))
            db.commit()

        try:
            # Scoped to clinic 1 — must see only Alice's clinic-1 booking.
            clinic1_bookings = get_all_bookings(phone, clinic_id=1)
            assert len(clinic1_bookings) == 1
            assert clinic1_bookings[0]["event_id"] == "evt-c1-001"

            # Scoped to clinic 2 — must see only Alice's clinic-2 booking.
            clinic2_bookings = get_all_bookings(phone, clinic_id=2)
            assert len(clinic2_bookings) == 1
            assert clinic2_bookings[0]["event_id"] == "evt-c2-001"

            # Unscoped — sees both (admin/test path).
            all_bookings = get_all_bookings(phone)
            assert len(all_bookings) == 2
        finally:
            with SessionLocal() as db:
                db.query(BookingRecordModel).filter(BookingRecordModel.user == phone).delete()
                db.commit()

    def test_get_existing_booking_isolated(self):
        """get_existing_booking scoped to wrong clinic returns None even if booking exists."""
        from app import get_existing_booking, SessionLocal, BookingRecordModel
        from datetime import datetime

        phone = "+60199000002"
        with SessionLocal() as db:
            db.query(BookingRecordModel).filter(BookingRecordModel.user == phone).delete()
            db.commit()

        with SessionLocal() as db:
            db.add(self._make_record("evt-iso-001", phone, clinic_id=1, name="Bob"))
            db.commit()

        try:
            # Correct clinic — finds booking.
            result = get_existing_booking(phone, clinic_id=1)
            assert result is not None
            assert result["event_id"] == "evt-iso-001"

            # Wrong clinic — must return None.
            result_wrong = get_existing_booking(phone, clinic_id=2)
            assert result_wrong is None
        finally:
            with SessionLocal() as db:
                db.query(BookingRecordModel).filter(BookingRecordModel.user == phone).delete()
                db.commit()

    def test_cancel_does_not_see_other_clinic_bookings(self):
        """cancel_booking scoped to clinic 2 must not find a booking belonging to clinic 1."""
        from app import cancel_booking

        clinic_1_booking = {
            "event_id": "evt-xc-001",
            "name": "Carol",
            "service": "scaling",
            "date": "2026-05-01",
            "time": "10:00",
            "status": "Confirmed",
            "clinic_id": 1,
            "reminder_1d_sent": False,
            "reminder_2h_sent": False,
        }
        clinic_2 = {
            "id": 2,
            "name": "Clinic 2",
            "google_calendar_id": "cal-2",
            "timezone": "Asia/Kuala_Lumpur",
        }

        # Simulate: clinic 1's booking is visible to get_all_bookings(phone)
        # but NOT to get_all_bookings(phone, clinic_id=2).
        def scoped_get_all_bookings(phone, clinic_id=None):
            if clinic_id == 2:
                return []  # Clinic 2 has no bookings for this phone.
            return [clinic_1_booking]

        with patch("app.get_all_bookings", side_effect=scoped_get_all_bookings), \
             patch("app.get_default_clinic", return_value=clinic_2):
            result = cancel_booking("+60199000003", clinic=clinic_2)

        assert result["ok"] is False
        assert "no booking" in result["message"].lower()


# ---------------------------------------------------------------------------
# Edge-to-edge sequential booking — check_availability overlap fix
# ---------------------------------------------------------------------------

class TestEdgeToEdgeSequentialBooking:
    """
    John books Filling (60 min) at 11:00 → ends 12:00.
    Mary requests Whitening (90 min) at 12:00 → starts exactly when John ends.

    12:00 must be AVAILABLE (not blocked), because the half-open interval check
    [start, end) means edge-to-edge slots do not overlap.

    Previously check_availability delegated the boundary check to Google's
    events().list() API.  If Google treated timeMin as >= rather than >, John's
    event (end=12:00) would be included in Mary's query (timeMin=12:00) and
    incorrectly block the slot.  The fix fetches the full day window and applies
    the explicit local overlap check, making behaviour independent of Google's
    boundary semantics.
    """

    def _make_clinic(self):
        return {
            "id": 1,
            "name": "Test Clinic",
            "timezone": "Asia/Kuala_Lumpur",
            "open_hour": 10,
            "close_hour": 18,
            "slot_minutes": 30,
            "google_calendar_id": "cal123",
            "services": {
                "filling": 60,
                "whitening": 90,
                "scaling": 60,
            },
            "service_prices": {},
            "special_closures": [],
            "closure_notes": {},
        }

    def _make_event(self, start_iso, end_iso):
        return {
            "start": {"dateTime": start_iso},
            "end": {"dateTime": end_iso},
        }

    def test_12pm_available_when_previous_appointment_ends_at_12pm(self):
        """Mary's 12:00 whitening must not be blocked by John's 11:00-12:00 filling."""
        from app import check_availability

        clinic = self._make_clinic()
        tz = "Asia/Kuala_Lumpur"

        # John's filling: 11:00–12:00.  Stored in calendar as a timed event.
        john_event = self._make_event(
            "2026-05-01T11:00:00+08:00",
            "2026-05-01T12:00:00+08:00",
        )

        mock_cal = MagicMock()
        mock_cal.events.return_value.list.return_value.execute.return_value = {
            "items": [john_event]
        }

        with patch("app.get_calendar", return_value=mock_cal), \
             patch("app.now_local", return_value=datetime(2026, 5, 1, 10, 0, tzinfo=ZoneInfo(tz))):

            result = check_availability(
                service="whitening",
                date="2026-05-01",
                time="12:00",
                clinic=clinic,
            )

        assert result["ok"] is True, (
            f"12:00 should be available when the previous appointment ends at 12:00, "
            f"but got: {result}"
        )

    def test_11_30_blocked_because_it_overlaps_john(self):
        """11:30 whitening (ends 13:00) overlaps John's 11:00-12:00 — must be blocked."""
        from app import check_availability

        clinic = self._make_clinic()
        tz = "Asia/Kuala_Lumpur"

        john_event = self._make_event(
            "2026-05-01T11:00:00+08:00",
            "2026-05-01T12:00:00+08:00",
        )

        mock_cal = MagicMock()
        mock_cal.events.return_value.list.return_value.execute.return_value = {
            "items": [john_event]
        }

        with patch("app.get_calendar", return_value=mock_cal), \
             patch("app.now_local", return_value=datetime(2026, 5, 1, 10, 0, tzinfo=ZoneInfo(tz))):

            result = check_availability(
                service="whitening",
                date="2026-05-01",
                time="11:30",
                clinic=clinic,
            )

        assert result["ok"] is False, (
            f"11:30 whitening (ends 13:00) overlaps John's 11:00-12:00 filling — "
            f"must be blocked, but got ok=True"
        )

    def test_11_00_blocked_because_it_is_john_exact_slot(self):
        """11:00 for whitening (90 min, ends 12:30) starts inside John's slot — must be blocked."""
        from app import check_availability

        clinic = self._make_clinic()
        tz = "Asia/Kuala_Lumpur"

        john_event = self._make_event(
            "2026-05-01T11:00:00+08:00",
            "2026-05-01T12:00:00+08:00",
        )

        mock_cal = MagicMock()
        mock_cal.events.return_value.list.return_value.execute.return_value = {
            "items": [john_event]
        }

        with patch("app.get_calendar", return_value=mock_cal), \
             patch("app.now_local", return_value=datetime(2026, 5, 1, 10, 0, tzinfo=ZoneInfo(tz))):

            result = check_availability(
                service="whitening",
                date="2026-05-01",
                time="11:00",
                clinic=clinic,
            )

        assert result["ok"] is False, (
            f"11:00 whitening starts at the same time as John's filling — "
            f"must be blocked, but got ok=True"
        )

    def test_allday_event_does_not_block_slot(self):
        """An all-day calendar event must not block a timed appointment slot."""
        from app import check_availability

        clinic = self._make_clinic()
        tz = "Asia/Kuala_Lumpur"

        # All-day event has "date" key, not "dateTime"
        allday_event = {
            "start": {"date": "2026-05-01"},
            "end": {"date": "2026-05-02"},
        }

        mock_cal = MagicMock()
        mock_cal.events.return_value.list.return_value.execute.return_value = {
            "items": [allday_event]
        }

        with patch("app.get_calendar", return_value=mock_cal), \
             patch("app.now_local", return_value=datetime(2026, 5, 1, 10, 0, tzinfo=ZoneInfo(tz))):

            result = check_availability(
                service="scaling",
                date="2026-05-01",
                time="10:00",
                clinic=clinic,
            )

        assert result["ok"] is True, (
            f"All-day events must not block timed appointment slots, but got: {result}"
        )

    def test_get_available_slots_includes_12pm_after_11am_booking(self):
        """get_available_slots must include 12:00 when John's 11:00 filling ends at 12:00."""
        from app import get_available_slots

        clinic = self._make_clinic()
        tz = "Asia/Kuala_Lumpur"

        john_event = self._make_event(
            "2026-05-01T11:00:00+08:00",
            "2026-05-01T12:00:00+08:00",
        )

        mock_cal = MagicMock()
        mock_cal.events.return_value.list.return_value.execute.return_value = {
            "items": [john_event]
        }

        with patch("app.get_calendar", return_value=mock_cal), \
             patch("app.now_local", return_value=datetime(2026, 5, 1, 10, 0, tzinfo=ZoneInfo(tz))):

            result = get_available_slots(
                service="whitening",
                date="2026-05-01",
                clinic=clinic,
            )

        assert result["ok"] is True
        slot_times = [s["time"] for s in result["slots"]]
        assert "12:00" in slot_times, (
            f"12:00 should appear in available slots when John's filling ends at 12:00, "
            f"but got: {slot_times}"
        )
        # 11:00 and 11:30 must not appear — they overlap John
        assert "11:00" not in slot_times, "11:00 overlaps John's filling and must be excluded"
        assert "11:30" not in slot_times, "11:30 overlaps John's filling and must be excluded"
