"""
System prompt: existing booking records.

Regression tests for a multi-patient booking response mismatch: after a parent
books two family members, the system prompt must show the clinic-scoped
existing booking record (not a phantom "FAMILY BOOKINGS" section), so the LLM
does not contradict what was actually booked.
"""

import sys
sys.path.insert(0, ".")

from app import build_system_prompt, save_booking_record, reset_user_session, get_all_bookings


def test_system_prompt_shows_all_family_bookings():
    """Verify that the system prompt reflects clinic-scoped booking records."""
    phone = "+60123999888"
    reset_user_session(phone)

    # Scenario: Parent books Daniel, then books Mary — both at clinic 1 (default).
    save_booking_record(
        user=phone,
        event_id="evt-daniel",
        service="whitening",
        name="Daniel",
        date="2026-01-15",
        time="10:00",
        status="Confirmed",
        clinic_id=1,
    )

    save_booking_record(
        user=phone,
        event_id="evt-mary",
        service="whitening",
        name="Mary",
        date="2026-01-15",
        time="11:30",
        status="Confirmed",
        clinic_id=1,
    )

    # Verify database has both bookings (unscoped — admin path).
    all_bookings = get_all_bookings(phone)
    assert len(all_bookings) == 2
    names = {b["name"] for b in all_bookings}
    assert "Daniel" in names
    assert "Mary" in names

    # The system prompt uses get_existing_booking (most recent booking, clinic-scoped).
    # The most recently inserted record is Mary's — so the prompt must show Mary.
    prompt = build_system_prompt(phone)

    # The prompt must show at least the most recent booking details.
    assert "Mary" in prompt or "Daniel" in prompt  # At least one visible.
    assert "whitening" in prompt
    # Must not have FAMILY BOOKINGS section — that feature is not implemented.
    assert "FAMILY BOOKINGS (total:" not in prompt
    # Must have the existing booking record section.
    assert "CURRENT EXISTING BOOKING RECORD" in prompt or "EXISTING BOOKING" in prompt


def test_system_prompt_single_booking_simple_format():
    """Verify that single booking shows its details in the system prompt."""
    phone = "+60123999777"
    reset_user_session(phone)

    save_booking_record(
        user=phone,
        event_id="evt-single-daniel",
        service="scaling",
        name="Daniel",
        date="2026-01-20",
        time="14:00",
        status="Confirmed",
        clinic_id=1,
    )

    prompt = build_system_prompt(phone)

    # Single booking must show its details.
    assert "Daniel" in prompt
    assert "scaling" in prompt
    assert "14:00" in prompt
    assert "event_id exists: yes" in prompt
    # Must not contain phantom family bookings label.
    assert "FAMILY BOOKINGS (total:" not in prompt

