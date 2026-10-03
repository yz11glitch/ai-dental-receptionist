"""
pytest configuration — sets required environment variables before app is imported,
so unit tests work without real credentials.
"""
import os
from zoneinfo import ZoneInfo

import pytest

# Set required env vars before any test module imports app.
# These values are stubs only — they are never used in unit tests that mock
# external calls (OpenAI, Google Calendar, Twilio, DB).
_TEST_ENV = {
    "OPENAI_API_KEY": "test-key-not-real",
    "DATABASE_URL": "sqlite:///:memory:",
    "TWILIO_ACCOUNT_SID": "ACtest",
    "TWILIO_AUTH_TOKEN": "authtest",
    "TWILIO_WHATSAPP_FROM": "whatsapp:+14155238886",
    "GOOGLE_CALENDAR_ID": "primary",
}

for key, value in _TEST_ENV.items():
    os.environ.setdefault(key, value)


@pytest.fixture
def freeze_now(monkeypatch):
    """Return a function that pins app.now_local() to a fixed aware datetime.

    Booking code reads the current time exclusively through app.now_local()
    (booking._now and utils.resolve_relative_date import it lazily), so
    patching it makes tests that use literal calendar dates independent of
    the real clock.
    """
    def _freeze(moment):
        import app
        monkeypatch.setattr(
            app,
            "now_local",
            lambda clinic_tz=app.TIMEZONE: moment.astimezone(ZoneInfo(clinic_tz)),
        )
        return moment

    return _freeze
