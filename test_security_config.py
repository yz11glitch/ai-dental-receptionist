"""
Fail-closed configuration.

- /whatsapp requires a valid Twilio signature; unsigned requests are only
  accepted with the explicit ALLOW_UNSIGNED_WEBHOOKS flag.
- /tasks/* endpoints require REMINDER_SECRET via the X-Reminder-Secret header
  and reject everything when it is unset.
- /chat (LLM + calendar test page) is limited to development mode or staff.
- DASHBOARD_SECRET_KEY is required outside development.
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


def test_webhook_rejects_unsigned_when_token_missing_even_in_development():
    # Development mode alone is not enough — unsigned webhooks need the explicit flag.
    with patch("app.TWILIO_AUTH_TOKEN", None), \
         patch("app.DEV_MODE", True), \
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


# ---------------------------------------------------------------------------
# /chat test page
# ---------------------------------------------------------------------------

def test_chat_forbidden_without_login_in_production():
    with patch("app.run_ai") as mock_run_ai, app.test_client() as c:
        assert c.get("/chat").status_code == 403
        assert c.post("/chat", data={"msg": "book scaling tomorrow"}).status_code == 403
        assert c.post("/chat/reset").status_code == 403
    mock_run_ai.assert_not_called()


def test_chat_allowed_for_logged_in_staff():
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["staff_clinic_id"] = 1
            sess["staff_id"] = 77
        assert c.get("/chat").status_code == 200


def test_chat_allowed_in_development_mode():
    with patch("app.DEV_MODE", True), app.test_client() as c:
        assert c.get("/chat").status_code == 200


# ---------------------------------------------------------------------------
# Dashboard session secret key
# ---------------------------------------------------------------------------

def test_app_uses_configured_dashboard_secret_key():
    assert app.secret_key == "test-dashboard-secret-key"


def test_dashboard_secret_key_required_outside_development(monkeypatch):
    monkeypatch.delenv("DASHBOARD_SECRET_KEY", raising=False)
    monkeypatch.setattr(app_module, "DEV_MODE", False)
    with pytest.raises(RuntimeError, match="DASHBOARD_SECRET_KEY"):
        app_module._load_dashboard_secret_key()


def test_dashboard_secret_key_is_random_in_development(monkeypatch):
    monkeypatch.delenv("DASHBOARD_SECRET_KEY", raising=False)
    monkeypatch.setattr(app_module, "DEV_MODE", True)
    first = app_module._load_dashboard_secret_key()
    second = app_module._load_dashboard_secret_key()
    assert len(first) >= 32
    assert first != second
    assert first != "dev-secret-change-in-prod"
