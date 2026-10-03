"""
Fail-closed configuration.

- /whatsapp requires a valid Twilio signature; unsigned requests are only
  accepted with the explicit ALLOW_UNSIGNED_WEBHOOKS flag.
- /tasks/* endpoints require REMINDER_SECRET via the X-Reminder-Secret header
  and reject everything when it is unset.
"""
from unittest.mock import patch

import pytest
from twilio.request_validator import RequestValidator

import app as app_module
from app import app

WEBHOOK_DATA = {"Body": "hello", "From": "+60123456789", "To": "+60100000000"}
TASK_ROUTES = [
    "/tasks/process-reminders",
    "/tasks/daily-summary",
    "/tasks/calendar-health-check",
]


@pytest.fixture
def stub_tasks():
    """Stub the work behind /tasks/* so only the auth gate is exercised."""
    with patch("app.process_reminders", return_value={"checked": 0}), \
         patch("app.run_calendar_health_checks", return_value={"ok": True}), \
         patch("app.send_telegram"):
        yield


# ---------------------------------------------------------------------------
# Twilio webhook signature
# ---------------------------------------------------------------------------

def test_webhook_accepts_valid_signature():
    signature = RequestValidator(app_module.TWILIO_AUTH_TOKEN).compute_signature(
        "http://localhost/whatsapp", WEBHOOK_DATA
    )
    with patch("app.get_clinic_by_twilio_number", return_value=None), \
         app.test_client() as c:
        response = c.post("/whatsapp", data=WEBHOOK_DATA, headers={"X-Twilio-Signature": signature})
    assert response.status_code == 200


def test_webhook_rejects_missing_signature_when_token_set():
    with app.test_client() as c:
        response = c.post("/whatsapp", data=WEBHOOK_DATA)
    assert response.status_code == 403


def test_webhook_rejects_unsigned_when_token_missing():
    with patch("app.TWILIO_AUTH_TOKEN", None), \
         patch("app.ALLOW_UNSIGNED_WEBHOOKS", False), \
         app.test_client() as c:
        response = c.post("/whatsapp", data=WEBHOOK_DATA)
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# /tasks/* shared secret
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", TASK_ROUTES)
def test_task_routes_accept_secret_header(path, stub_tasks):
    with app.test_client() as c:
        response = c.post(path, headers={"X-Reminder-Secret": app_module.REMINDER_SECRET})
    assert response.status_code == 200


@pytest.mark.parametrize("path", TASK_ROUTES)
def test_task_routes_reject_wrong_or_missing_secret(path, stub_tasks):
    with app.test_client() as c:
        assert c.post(path).status_code == 403
        assert c.post(path, headers={"X-Reminder-Secret": "wrong"}).status_code == 403


@pytest.mark.parametrize("path", TASK_ROUTES)
def test_task_routes_no_longer_accept_secret_in_query_string(path, stub_tasks):
    # Secrets in URLs end up in access logs; only the header is accepted.
    secret = app_module.REMINDER_SECRET
    with app.test_client() as c:
        assert c.get(f"{path}?secret={secret}").status_code == 403
        assert c.get(f"{path}?token={secret}").status_code == 403


@pytest.mark.parametrize("path", TASK_ROUTES)
def test_task_routes_reject_everything_when_secret_unset(path, stub_tasks):
    with patch("app.REMINDER_SECRET", None), app.test_client() as c:
        assert c.post(path).status_code == 403
        assert c.post(path, headers={"X-Reminder-Secret": ""}).status_code == 403
        assert c.post(path, headers={"X-Reminder-Secret": "change-me"}).status_code == 403
