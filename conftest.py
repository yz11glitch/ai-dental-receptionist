"""
pytest configuration — sets required environment variables before app is imported,
so unit tests work without real credentials.
"""
import os

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
