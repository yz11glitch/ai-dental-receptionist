"""
Tests for Mandarin booking flow fixes:

1. test_name_required_before_booking_mandarin
   - dispatch_tool create_booking with no name → ok=False
2. test_normalize_service_mandarin
   - Common Mandarin dental terms map to correct English service keys
3. test_empty_response_no_loop
   - LLM returning empty string → safe fallback, no crash, no loop
4. test_duplicate_message_no_loop
   - Same assistant reply twice → second is NOT appended to history
"""

import sys
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, ".")


# ---------------------------------------------------------------------------
# 1. Python-level name guard in dispatch_tool
# ---------------------------------------------------------------------------

class TestNameRequiredBeforeBooking:
    """dispatch_tool must refuse create_booking when name is absent, regardless of language."""

    def _make_booking_state(self, availability_ok=True, name=None):
        return {
            "service": "scaling",
            "date": "2026-05-10",
            "time": "10:00",
            "name": name,
            "availability_ok": availability_ok,
        }

    def test_name_required_before_booking_no_name_arg(self):
        """create_booking with no name argument → ok=False with prompt to provide name."""
        from app import dispatch_tool

        with patch("app.get_booking_state", return_value=self._make_booking_state()), \
             patch("app.safe_tool_args", side_effect=lambda n, a: a):

            result = dispatch_tool(
                name="create_booking",
                args={"service": "scaling", "date": "2026-05-10", "time": "10:00"},
                user="+60111111111",
            )

        assert result["ok"] is False
        assert "name" in result["message"].lower()

    def test_name_required_before_booking_empty_name(self):
        """create_booking with empty string name → ok=False."""
        from app import dispatch_tool

        with patch("app.get_booking_state", return_value=self._make_booking_state()), \
             patch("app.safe_tool_args", side_effect=lambda n, a: a):

            result = dispatch_tool(
                name="create_booking",
                args={"name": "", "service": "scaling", "date": "2026-05-10", "time": "10:00"},
                user="+60111111111",
            )

        assert result["ok"] is False
        assert "name" in result["message"].lower()

    def test_name_required_before_booking_whitespace_only(self):
        """create_booking with whitespace-only name → ok=False."""
        from app import dispatch_tool

        with patch("app.get_booking_state", return_value=self._make_booking_state()), \
             patch("app.safe_tool_args", side_effect=lambda n, a: a):

            result = dispatch_tool(
                name="create_booking",
                args={"name": "   ", "service": "scaling", "date": "2026-05-10", "time": "10:00"},
                user="+60111111111",
            )

        assert result["ok"] is False
        assert "name" in result["message"].lower()

    def test_name_required_mandarin_name_accepted(self):
        """create_booking with a valid Chinese-character name passes the name guard."""
        from app import dispatch_tool

        with patch("app.get_booking_state", return_value=self._make_booking_state()), \
             patch("app.safe_tool_args", side_effect=lambda n, a: a), \
             patch("app.get_all_bookings", return_value=[]), \
             patch("app.update_booking_state"), \
             patch("app.create_booking", return_value={"ok": True, "event_id": "evt123",
                                                        "service": "scaling", "name": "陳大文",
                                                        "date": "2026-05-10", "time": "10:00",
                                                        "formatted_slot": "...", "short_notice": False}), \
             patch("app.reset_booking_state"), \
             patch("app.write_conversation_flag"):

            result = dispatch_tool(
                name="create_booking",
                args={"name": "陳大文", "service": "scaling", "date": "2026-05-10", "time": "10:00"},
                user="+60111111111",
            )

        # The Chinese name passes the name guard — create_booking result is returned.
        assert result["ok"] is True

    def test_availability_not_ok_returns_error_before_name_check(self):
        """availability_ok=False is checked before the name guard (existing guard intact)."""
        from app import dispatch_tool

        with patch("app.get_booking_state",
                   return_value=self._make_booking_state(availability_ok=False)), \
             patch("app.safe_tool_args", side_effect=lambda n, a: a):

            result = dispatch_tool(
                name="create_booking",
                args={"name": "陳大文", "service": "scaling", "date": "2026-05-10", "time": "10:00"},
                user="+60111111111",
            )

        assert result["ok"] is False
        assert "availability" in result["message"].lower()


# ---------------------------------------------------------------------------
# 2. Mandarin service aliases in normalize_service
# ---------------------------------------------------------------------------

class TestNormalizeServiceMandarin:
    """normalize_service must map common Mandarin dental terms to English service keys."""

    @pytest.mark.parametrize("input_term,expected", [
        # Scaling
        ("洗牙", "scaling"),
        ("洁牙", "scaling"),
        ("潔牙", "scaling"),
        # Filling
        ("補牙", "filling"),
        ("补牙", "filling"),
        ("蛀牙补", "filling"),
        ("蛀牙補", "filling"),
        # Whitening
        ("美白", "whitening"),
        ("牙齒美白", "whitening"),
        ("牙齿美白", "whitening"),
        # Extraction
        ("拔牙", "extraction"),
        ("脫牙", "extraction"),
        ("脱牙", "extraction"),
        # Braces consultation
        ("箍牙", "braces consultation"),
        ("牙套", "braces consultation"),
        ("矯正", "braces consultation"),
        ("矫正", "braces consultation"),
        ("牙齒矯正", "braces consultation"),
        ("牙齿矫正", "braces consultation"),
        # Root canal
        ("根管", "root canal"),
        ("根管治療", "root canal"),
        ("根管治疗", "root canal"),
        ("神经治疗", "root canal"),
        ("神經治療", "root canal"),
        # Checkup
        ("檢查", "checkup"),
        ("检查", "checkup"),
        ("牙科檢查", "checkup"),
        ("牙科检查", "checkup"),
    ])
    def test_mandarin_term_maps_to_correct_service(self, input_term, expected):
        from app import normalize_service
        assert normalize_service(input_term) == expected

    def test_english_aliases_unaffected(self):
        """Existing English aliases must still work after adding Mandarin ones."""
        from app import normalize_service
        assert normalize_service("cleaning") == "scaling"
        assert normalize_service("tampal") == "filling"
        assert normalize_service("gigi putih") == "whitening"

    def test_unknown_term_passthrough(self):
        """Unknown terms should pass through unchanged (let the service validation catch them)."""
        from app import normalize_service
        assert normalize_service("unknown_service") == "unknown_service"
        assert normalize_service("某種手術") == "某種手術"

    def test_none_input(self):
        """normalize_service(None) must not raise."""
        from app import normalize_service
        result = normalize_service(None)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# 3. Empty/None response does not loop
# ---------------------------------------------------------------------------

class TestEmptyResponseNoLoop:
    """When the LLM returns an empty string, run_ai must return a safe fallback without crashing."""

    def _make_clinic(self):
        return {
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
        }

    def test_empty_response_returns_safe_fallback(self):
        """LLM returning empty output_text → safe fallback message returned, no exception."""
        from app import run_ai

        clinic = self._make_clinic()

        mock_response = MagicMock()
        mock_response.output = []  # no tool calls
        mock_response.output_text = ""  # empty

        with patch("app.get_default_clinic", return_value=clinic), \
             patch("app.get_direct_reply", return_value=None), \
             patch("app.build_system_prompt", return_value="system prompt"), \
             patch("app.append_history"), \
             patch("app.get_history", return_value=[{"role": "user", "content": "洗牙"}]), \
             patch("app.client") as mock_client:

            mock_client.responses.create.return_value = mock_response

            reply = run_ai(user="+60111111111", message="洗牙", clinic=clinic)

        # Must return a non-empty, safe fallback message
        assert reply
        assert isinstance(reply, str)
        assert len(reply) > 0

    def test_none_response_text_returns_safe_fallback(self):
        """LLM returning None output_text → safe fallback returned, not None propagated."""
        from app import run_ai

        clinic = self._make_clinic()

        mock_response = MagicMock()
        mock_response.output = []
        mock_response.output_text = None

        with patch("app.get_default_clinic", return_value=clinic), \
             patch("app.get_direct_reply", return_value=None), \
             patch("app.build_system_prompt", return_value="system prompt"), \
             patch("app.append_history"), \
             patch("app.get_history", return_value=[{"role": "user", "content": "補牙"}]), \
             patch("app.client") as mock_client:

            mock_client.responses.create.return_value = mock_response

            reply = run_ai(user="+60111111111", message="補牙", clinic=clinic)

        assert reply
        assert isinstance(reply, str)


# ---------------------------------------------------------------------------
# 4. Duplicate consecutive assistant message is not re-appended
# ---------------------------------------------------------------------------

class TestDuplicateMessageNoLoop:
    """When the LLM returns the same message as the last assistant message, it must not be appended again."""

    def _make_clinic(self):
        return {
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
        }

    def test_duplicate_reply_not_appended(self):
        """If the LLM returns an identical reply to the last assistant message, do not append."""
        from app import run_ai

        clinic = self._make_clinic()
        repeated_reply = "Could you please tell me your full name?"

        mock_response = MagicMock()
        mock_response.output = []
        mock_response.output_text = repeated_reply

        # History already ends with the same assistant message.
        existing_history = [
            {"role": "user", "content": "我想洗牙"},
            {"role": "assistant", "content": repeated_reply},
            {"role": "user", "content": "下星期三"},
        ]

        with patch("app.get_default_clinic", return_value=clinic), \
             patch("app.get_direct_reply", return_value=None), \
             patch("app.build_system_prompt", return_value="system prompt"), \
             patch("app.append_history") as mock_append, \
             patch("app.get_history", return_value=existing_history), \
             patch("app.client") as mock_client:

            mock_client.responses.create.return_value = mock_response

            reply = run_ai(user="+60111111111", message="下星期三", clinic=clinic)

        # The reply is still returned to the caller (user sees it)
        assert reply == repeated_reply

        # But it must NOT have been appended to history as an assistant message
        # (the user message append in run_ai uses role="user", so we check no
        # assistant append with the duplicate content happened)
        assistant_appends = [
            c for c in mock_append.call_args_list
            if c.args[1] == "assistant" and c.args[2] == repeated_reply
        ]
        assert len(assistant_appends) == 0, (
            f"Duplicate assistant message was appended {len(assistant_appends)} time(s) — expected 0"
        )

    def test_different_reply_is_appended_normally(self):
        """A reply that differs from the last assistant message is still appended normally."""
        from app import run_ai

        clinic = self._make_clinic()
        old_reply = "Could you please tell me your full name?"
        new_reply = "Great, I have your name. Let me check availability."

        mock_response = MagicMock()
        mock_response.output = []
        mock_response.output_text = new_reply

        existing_history = [
            {"role": "user", "content": "我想洗牙"},
            {"role": "assistant", "content": old_reply},
            {"role": "user", "content": "陳大文"},
        ]

        with patch("app.get_default_clinic", return_value=clinic), \
             patch("app.get_direct_reply", return_value=None), \
             patch("app.build_system_prompt", return_value="system prompt"), \
             patch("app.append_history") as mock_append, \
             patch("app.get_history", return_value=existing_history), \
             patch("app.client") as mock_client:

            mock_client.responses.create.return_value = mock_response

            reply = run_ai(user="+60111111111", message="陳大文", clinic=clinic)

        assert reply == new_reply

        # The new (different) reply must have been appended
        assistant_appends = [
            c for c in mock_append.call_args_list
            if c.args[1] == "assistant" and c.args[2] == new_reply
        ]
        assert len(assistant_appends) == 1, (
            f"Expected 1 assistant append for new reply, got {len(assistant_appends)}"
        )
