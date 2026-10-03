"""
Tests for the date-validation-order fix.

Covers:
- check_date_available: Sunday detection + next_open_days suggestion
- check_date_available: special closure detection + next_open_days suggestion
- check_date_available: open day returns ok=True
- find_next_open_days: skips Sundays and special closures correctly
- dispatch_tool routes check_date_available without crashing
- System prompt does not contain self-correction phrases
"""
import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ["DATABASE_URL"] = "sqlite:///test_date_validation.db"

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import (
    check_date_available,
    find_next_open_days,
    dispatch_tool,
    build_system_prompt,
    TOOLS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_clinic(special_closures=None, closure_notes=None):
    return {
        "id": 1,
        "name": "Test Dental",
        "location": "KL",
        "timezone": "Asia/Kuala_Lumpur",
        "open_hour": 10,
        "close_hour": 18,
        "hours_text": "Monday to Saturday, 10:00 to 18:00. Closed Sunday.",
        "slot_minutes": 30,
        "opening_message": "",
        "promo_message": "",
        "google_calendar_id": "primary",
        "twilio_number": None,
        "services": {"scaling": 60},
        "service_prices": {"scaling": 80.0},
        "special_closures": special_closures or [],
        "closure_notes": closure_notes or {},
    }


# The tests below use literal April 2026 dates (e.g. 2026-04-12 is a Sunday).
# check_date_available rejects past dates, so pin "now" to just before them;
# otherwise these tests start failing once the real clock passes April 2026.
FROZEN_NOW = datetime(2026, 4, 8, 10, 0, tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))  # Wednesday


@pytest.fixture(autouse=True)
def _frozen_clock(freeze_now):
    freeze_now(FROZEN_NOW)


# ---------------------------------------------------------------------------
# find_next_open_days
# ---------------------------------------------------------------------------

class TestFindNextOpenDays:
    def test_skips_sunday(self):
        # Find next 2 days from a Saturday — Sunday must be skipped.
        # Find the next Saturday in the near future to avoid flakiness.
        from datetime import date
        # Use a known Saturday: 2026-04-11 (Saturday)
        result = find_next_open_days("2026-04-11", make_clinic(), count=2)
        # 2026-04-12 is Sunday (skip), 2026-04-13 is Monday, 2026-04-14 is Tuesday
        assert "2026-04-13" in result
        assert "2026-04-14" in result
        assert "2026-04-12" not in result  # Sunday must not appear

    def test_skips_special_closures(self):
        clinic = make_clinic(special_closures=["2026-04-13", "2026-04-14"])
        # From 2026-04-11 (Sat): skip Sun 12, skip Mon 13, skip Tue 14, land on Wed 15 and Thu 16
        result = find_next_open_days("2026-04-11", clinic, count=2)
        assert "2026-04-13" not in result
        assert "2026-04-14" not in result
        assert "2026-04-15" in result
        assert "2026-04-16" in result

    def test_returns_requested_count(self):
        result = find_next_open_days("2026-04-08", make_clinic(), count=3)
        assert len(result) == 3

    def test_invalid_from_date_falls_back_gracefully(self):
        # Should not raise; falls back to today and still returns dates
        result = find_next_open_days("not-a-date", make_clinic(), count=2)
        assert len(result) == 2

    def test_does_not_include_from_date_itself(self):
        # From date must be excluded — only future dates returned
        from_date = "2026-04-09"
        result = find_next_open_days(from_date, make_clinic(), count=2)
        assert from_date not in result


# ---------------------------------------------------------------------------
# check_date_available — Sunday
# ---------------------------------------------------------------------------

class TestCheckDateAvailableSunday:
    # 2026-04-12 is a Sunday
    SUNDAY = "2026-04-12"

    def test_sunday_returns_not_ok_and_closed_true(self):
        result = check_date_available(self.SUNDAY, make_clinic())
        assert result["ok"] is False
        assert result["closed"] is True
        assert result["reason"] == "sunday"

    def test_sunday_message_mentions_closed(self):
        result = check_date_available(self.SUNDAY, make_clinic())
        assert "closed" in result["message"].lower()

    def test_sunday_includes_next_open_days(self):
        result = check_date_available(self.SUNDAY, make_clinic())
        assert "next_open_days" in result
        assert len(result["next_open_days"]) == 2
        # Neither suggestion should be a Sunday
        for d in result["next_open_days"]:
            dt = datetime.strptime(d, "%Y-%m-%d")
            assert dt.weekday() != 6, f"{d} is a Sunday and should not be suggested"

    def test_sunday_message_mentions_next_open_days(self):
        result = check_date_available(self.SUNDAY, make_clinic())
        # Message should mention at least one suggested day
        assert any(d[:10] in result["message"] or
                   datetime.strptime(d, "%Y-%m-%d").strftime("%A") in result["message"]
                   for d in result["next_open_days"])

    def test_relative_phrase_tmr_on_saturday_resolves_to_sunday(self):
        # If today were Saturday 2026-04-11, "tmr" → Sunday 2026-04-12.
        # Patch now_local so resolve_relative_date returns the known Sunday.
        saturday = datetime(2026, 4, 11, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Kuala_Lumpur"))
        with patch("app.now_local", return_value=saturday):
            result = check_date_available("tmr", make_clinic())
        assert result["ok"] is False
        assert result["closed"] is True
        assert result["date"] == "2026-04-12"


# ---------------------------------------------------------------------------
# check_date_available — special closure
# ---------------------------------------------------------------------------

class TestCheckDateAvailableSpecialClosure:
    CLOSED_DATE = "2026-04-15"
    CLOSURE_NOTE = "Staff training day."

    def test_special_closure_returns_not_ok(self):
        clinic = make_clinic(
            special_closures=[self.CLOSED_DATE],
            closure_notes={self.CLOSED_DATE: self.CLOSURE_NOTE},
        )
        result = check_date_available(self.CLOSED_DATE, clinic)
        assert result["ok"] is False
        assert result["closed"] is True
        assert result["reason"] == "special_closure"

    def test_special_closure_message_includes_note(self):
        clinic = make_clinic(
            special_closures=[self.CLOSED_DATE],
            closure_notes={self.CLOSED_DATE: self.CLOSURE_NOTE},
        )
        result = check_date_available(self.CLOSED_DATE, clinic)
        assert self.CLOSURE_NOTE in result["message"]

    def test_special_closure_includes_next_open_days(self):
        clinic = make_clinic(special_closures=[self.CLOSED_DATE])
        result = check_date_available(self.CLOSED_DATE, clinic)
        assert "next_open_days" in result
        assert len(result["next_open_days"]) == 2
        assert self.CLOSED_DATE not in result["next_open_days"]


# ---------------------------------------------------------------------------
# check_date_available — open day
# ---------------------------------------------------------------------------

class TestCheckDateAvailableOpenDay:
    # 2026-04-09 is a Thursday (open)
    THURSDAY = "2026-04-09"

    def test_open_day_returns_ok_true(self):
        result = check_date_available(self.THURSDAY, make_clinic())
        assert result["ok"] is True
        assert result["closed"] is False

    def test_open_day_returns_correct_date(self):
        result = check_date_available(self.THURSDAY, make_clinic())
        assert result["date"] == self.THURSDAY

    def test_open_day_includes_weekday(self):
        result = check_date_available(self.THURSDAY, make_clinic())
        assert result["weekday"] == "Thursday"

    def test_monday_relative_phrase(self):
        # Patch now_local to a known Thursday so "next monday" resolves predictably
        thursday = datetime(2026, 4, 9, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Kuala_Lumpur"))
        with patch("app.now_local", return_value=thursday):
            result = check_date_available("monday", make_clinic())
        assert result["ok"] is True
        assert result["weekday"] == "Monday"


# ---------------------------------------------------------------------------
# check_date_available — bad input
# ---------------------------------------------------------------------------

class TestCheckDateAvailableBadInput:
    def test_unresolvable_phrase_returns_not_ok(self):
        result = check_date_available("blarg123", make_clinic())
        assert result["ok"] is False
        assert result["closed"] is False  # Not closed — just unparseable

    def test_empty_string_returns_not_ok(self):
        result = check_date_available("", make_clinic())
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# dispatch_tool routing for check_date_available
# ---------------------------------------------------------------------------

class TestDispatchToolCheckDateAvailable:
    def test_dispatch_routes_to_check_date_available(self):
        clinic = make_clinic()
        result = dispatch_tool(
            "check_date_available",
            {"date_text": "2026-04-12"},  # Sunday
            user="whatsapp:+60123456789",
            clinic=clinic,
        )
        assert result["ok"] is False
        assert result["closed"] is True

    def test_dispatch_open_day(self):
        clinic = make_clinic()
        result = dispatch_tool(
            "check_date_available",
            {"date_text": "2026-04-09"},  # Thursday
            user="whatsapp:+60123456789",
            clinic=clinic,
        )
        assert result["ok"] is True

    def test_dispatch_unknown_tool_still_works(self):
        clinic = make_clinic()
        result = dispatch_tool("unknown_tool_xyz", {}, user="whatsapp:+60123456789", clinic=clinic)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# check_date_available is registered in TOOLS
# ---------------------------------------------------------------------------

class TestToolsRegistration:
    def test_check_date_available_in_tools_list(self):
        names = {t["name"] for t in TOOLS}
        assert "check_date_available" in names

    def test_check_date_available_has_date_text_param(self):
        tool = next(t for t in TOOLS if t["name"] == "check_date_available")
        assert "date_text" in tool["parameters"]["properties"]
        assert "date_text" in tool["parameters"]["required"]


# ---------------------------------------------------------------------------
# System prompt: no self-correction phrases
# ---------------------------------------------------------------------------

SELF_CORRECTION_PHRASES = [
    "i made a mistake",
    "i was wrong",
    "i apologize for the confusion",
]


class TestSystemPromptNonSelfCorrection:
    def _get_prompt(self):
        from unittest.mock import patch, MagicMock
        mock_clinic = make_clinic()
        with patch("app.get_default_clinic", return_value=mock_clinic), \
             patch("app.get_booking_state", return_value={"service": None, "date": None, "time": None, "name": None, "availability_ok": False}), \
             patch("app.get_existing_booking", return_value=None):
            return build_system_prompt("whatsapp:+60123456789", mock_clinic).lower()

    def test_i_made_a_mistake_only_inside_prohibition(self):
        # The phrase appears at most twice: once in "Never say 'I made a mistake'" and
        # once in the example ("instead of 'I made a mistake — ...'"). Both occurrences
        # are inside the prohibition rule. Anything beyond 2 would mean it was added
        # as a positive instruction.
        prompt = self._get_prompt()
        count = prompt.count("i made a mistake")
        assert count <= 2, \
            f"'I made a mistake' appears {count} times — expected at most 2 (only inside the prohibition rule)"
        # The prohibition must actually be present
        assert "never say" in prompt

    def test_i_was_wrong_only_inside_prohibition(self):
        prompt = self._get_prompt()
        count = prompt.count("i was wrong")
        assert count <= 1, \
            f"'I was wrong' appears {count} times — expected at most 1 (only inside the prohibition rule)"

    def test_apologize_for_confusion_only_inside_prohibition(self):
        prompt = self._get_prompt()
        count = prompt.count("apologize for the confusion")
        assert count <= 1, \
            f"'apologize for the confusion' appears {count} times — expected at most 1 (only inside the prohibition rule)"

    def test_never_say_instruction_present(self):
        prompt = self._get_prompt()
        # The prohibition instruction itself must be present
        assert "never say" in prompt, \
            "System prompt must contain a 'never say' self-correction prohibition"

    def test_path_a_calls_check_date_available_before_time(self):
        prompt = self._get_prompt()
        # check_date_available must appear before asking for time in Path A instructions
        idx_check = prompt.find("check_date_available")
        idx_time = prompt.find("ask for the preferred time")
        assert idx_check != -1, "System prompt must mention check_date_available"
        assert idx_time != -1, "System prompt must instruct asking for time after date check"
        assert idx_check < idx_time, \
            "check_date_available must appear before 'ask for the preferred time' in the prompt"

    def test_closed_day_rule_in_strict_booking_rules(self):
        prompt = self._get_prompt()
        assert "always call check_date_available" in prompt, \
            "STRICT BOOKING RULES must contain the eager date check instruction"
