import os
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event

from twilio.request_validator import RequestValidator

# Must be set before importing app.
os.environ.setdefault("OPENAI_API_KEY", "test-key")

import app as app_module
from app import (
    Clinic,
    ClinicDailyMetric,
    ConversationMessage,
    SessionLocal,
    app,
    dispatch_tool,
    run_ai,
    update_booking_state,
)


TZ_KL = ZoneInfo("Asia/Kuala_Lumpur")

CLINIC_A = {
    "id": 101,
    "name": "Clinic A",
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
    "google_calendar_id": "cal-A",
    "services": {"scaling": 60},
    "service_prices": {},
    "special_closures": [],
    "closure_notes": {},
}

CLINIC_B = {
    **CLINIC_A,
    "id": 202,
    "name": "Clinic B",
    "twilio_number": "whatsapp:+60222222222",
    "google_calendar_id": "cal-B",
}


def _post_whatsapp(client, body, from_phone="+60199999999", to_phone=None):
    """POST a correctly signed webhook (real Twilio signature validation runs)."""
    data = {
        "Body": body,
        "From": from_phone,
        "To": to_phone or CLINIC_A["twilio_number"],
    }
    signature = RequestValidator(app_module.TWILIO_AUTH_TOKEN).compute_signature(
        "http://localhost/whatsapp", data
    )
    return client.post("/whatsapp", data=data, headers={"X-Twilio-Signature": signature})


@pytest.fixture(autouse=True)
def _message_clock():
    # SQLAlchemy's default uses the real UTC clock; align new messages with the
    # mocked application clock so daily conversation counts use the same day.
    def set_timestamp(mapper, connection, message):
        if message.created_at is None:
            message.created_at = app_module.now_local().astimezone(
                ZoneInfo("UTC")
            ).replace(tzinfo=None)

    event.listen(ConversationMessage, "before_insert", set_timestamp)
    yield
    event.remove(ConversationMessage, "before_insert", set_timestamp)


def _fixed_local(hour=11):
    # A fixed Wednesday keeps working-hours assertions independent of Sundays.
    return datetime(2026, 4, 8, hour, 0, tzinfo=TZ_KL)


def _get_metric(clinic_id, metric_date):
    with SessionLocal() as db:
        return (
            db.query(ClinicDailyMetric)
            .filter(
                ClinicDailyMetric.clinic_id == clinic_id,
                ClinicDailyMetric.metric_date == metric_date,
            )
            .first()
        )


def _ensure_dashboard_clinic():
    # Clinic 1 is the shared demo clinic. Seed it the same way the app does
    # (with its services) so later test modules that rely on
    # get_default_clinic() are not left with a service-less clinic.
    app_module.ensure_demo_clinic_seeded()


def _fake_run_ai(user, message, clinic):
    app_module.append_history(user, "user", message, clinic_id=clinic["id"])
    app_module.append_history(user, "assistant", "ok", clinic_id=clinic["id"])
    return "ok"


def setup_function():
    with SessionLocal() as db:
        db.query(ClinicDailyMetric).delete()
        db.query(ConversationMessage).delete()
        db.commit()


def test_whatsapp_inbound_tracks_messages_and_conversations_once_per_day():
    fixed_day = _fixed_local(hour=11)
    with app.test_client() as client:
        with (
            patch("app.get_clinic_by_twilio_number", return_value=CLINIC_A),
            patch("app.is_human_escalation_request", return_value=False),
            patch("app.run_ai", side_effect=_fake_run_ai),
            patch("app.now_local", return_value=fixed_day),
        ):
            r1 = _post_whatsapp(client, "hello")
            r2 = _post_whatsapp(client, "i want to book")

    assert r1.status_code == 200
    assert r2.status_code == 200

    metric = _get_metric(CLINIC_A["id"], fixed_day.date())
    assert metric is not None
    assert metric.messages_handled == 2
    assert metric.conversations_handled == 1
    assert metric.after_hours_messages == 0


def test_whatsapp_after_hours_tracks_after_hours_metric():
    late_night = _fixed_local(hour=21)
    with app.test_client() as client:
        with (
            patch("app.get_clinic_by_twilio_number", return_value=CLINIC_A),
            patch("app.is_human_escalation_request", return_value=False),
            patch("app.run_ai", side_effect=_fake_run_ai),
            patch("app.now_local", return_value=late_night),
        ):
            response = _post_whatsapp(client, "hello after hours")

    assert response.status_code == 200
    metric = _get_metric(CLINIC_A["id"], late_night.date())
    assert metric is not None
    assert metric.after_hours_messages == 1


def test_whatsapp_escalation_tracks_human_escalations():
    fixed_day = _fixed_local(hour=11)
    with app.test_client() as client:
        with (
            patch("app.get_clinic_by_twilio_number", return_value=CLINIC_A),
            patch("app.is_human_escalation_request", return_value=True),
            patch("app.has_unresolved_human_flag", return_value=False),
            patch("app.write_conversation_flag"),
            patch("app.send_whatsapp_outbound"),
            patch("app.now_local", return_value=fixed_day),
        ):
            response = _post_whatsapp(client, "talk to human")

    assert response.status_code == 200
    metric = _get_metric(CLINIC_A["id"], fixed_day.date())
    assert metric is not None
    assert metric.human_escalations == 1


def test_repeated_same_day_escalation_does_not_overcount_conversations_handled():
    fixed_day = _fixed_local(hour=11)
    with app.test_client() as client:
        with (
            patch("app.get_clinic_by_twilio_number", return_value=CLINIC_A),
            patch("app.is_human_escalation_request", return_value=True),
            patch("app.has_unresolved_human_flag", side_effect=[False, True]),
            patch("app.write_conversation_flag"),
            patch("app.send_whatsapp_outbound"),
            patch("app.now_local", return_value=fixed_day),
        ):
            r1 = _post_whatsapp(client, "human please", from_phone="+60155555555")
            r2 = _post_whatsapp(client, "human please again", from_phone="+60155555555")

    assert r1.status_code == 200
    assert r2.status_code == 200

    metric = _get_metric(CLINIC_A["id"], fixed_day.date())
    assert metric is not None
    assert metric.messages_handled == 2
    assert metric.human_escalations == 2
    assert metric.conversations_handled == 1


def test_dispatch_tool_create_booking_tracks_bookings_created():
    fixed_day = _fixed_local(hour=11)
    booking_date = (fixed_day + timedelta(days=7)).strftime("%Y-%m-%d")
    user = "whatsapp:+60112223333"
    # Booking wizard state is scoped per clinic; dispatch_tool reads it with
    # the clinic's id, so it must be written with the same id.
    update_booking_state(
        user,
        clinic_id=CLINIC_A["id"],
        service="scaling",
        date=booking_date,
        time="11:00",
        availability_ok=True,
    )

    with (
        patch("app.get_all_bookings", return_value=[]),
        patch("app.create_booking", return_value={"ok": True, "event_id": "evt-123"}),
        patch("app.now_local", return_value=fixed_day),
    ):
        result = dispatch_tool(
            "create_booking",
            {"name": "Ali", "service": "scaling", "date": booking_date, "time": "11:00"},
            user=user,
            clinic=CLINIC_A,
        )

    assert result["ok"] is True
    metric = _get_metric(CLINIC_A["id"], fixed_day.date())
    assert metric is not None
    assert metric.bookings_created == 1


def test_metrics_are_isolated_per_clinic():
    fixed_day = _fixed_local(hour=11)
    clinic_map = {
        CLINIC_A["twilio_number"]: CLINIC_A,
        CLINIC_B["twilio_number"]: CLINIC_B,
    }

    with app.test_client() as client:
        with (
            patch("app.get_clinic_by_twilio_number", side_effect=lambda n: clinic_map.get(n)),
            patch("app.is_human_escalation_request", return_value=False),
            patch("app.run_ai", side_effect=_fake_run_ai),
            patch("app.now_local", return_value=fixed_day),
        ):
            r1 = _post_whatsapp(client, "hello clinic a", to_phone=CLINIC_A["twilio_number"])
            r2 = _post_whatsapp(client, "hello clinic b", to_phone=CLINIC_B["twilio_number"])

    assert r1.status_code == 200
    assert r2.status_code == 200

    metric_a = _get_metric(CLINIC_A["id"], fixed_day.date())
    metric_b = _get_metric(CLINIC_B["id"], fixed_day.date())
    assert metric_a is not None
    assert metric_b is not None
    assert metric_a.messages_handled == 1
    assert metric_b.messages_handled == 1


def test_dashboard_home_shows_today_metrics_summary():
    fixed_day = _fixed_local(hour=11)
    with SessionLocal() as db:
        db.add(
            ClinicDailyMetric(
                clinic_id=CLINIC_A["id"],
                metric_date=fixed_day.date(),
                conversations_handled=3,
                messages_handled=9,
                bookings_created=2,
                after_hours_messages=1,
                human_escalations=1,
            )
        )
        db.commit()

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["staff_clinic_id"] = CLINIC_A["id"]
            sess["staff_name"] = "Test Staff"
            sess["staff_id"] = 77
        with patch("dashboard.now_local", return_value=fixed_day):
            response = client.get("/dashboard/")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Metrics today" in body
    assert "Patients Assisted" in body
    assert "Messages Automated" in body
    assert "Appointments Booked" in body
    assert "After-Hours Enquiries" in body
    assert "Human Takeovers" in body


def test_dashboard_billing_shows_clinic_only_for_non_admin_staff():
    _ensure_dashboard_clinic()
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["staff_clinic_id"] = 1
            sess["staff_name"] = "Clinic Staff"
            sess["staff_id"] = 77
            sess["staff_is_admin"] = False
        response = client.get("/dashboard/billing")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Billing" in body
    assert "Admin Billing" not in body
    assert "/dashboard/admin/billing" not in body


def test_dashboard_admin_billing_allows_admin_staff_and_shows_nav_link():
    _ensure_dashboard_clinic()
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["staff_clinic_id"] = 1
            sess["staff_name"] = "Admin Staff"
            sess["staff_id"] = 88
            sess["staff_is_admin"] = True
        billing_response = client.get("/dashboard/admin/billing")
        home_response = client.get("/dashboard/")

    assert billing_response.status_code == 200
    billing_body = billing_response.get_data(as_text=True)
    assert "Admin Billing" in billing_body
    assert "/dashboard/admin/billing/1/update" in billing_body
    assert "/dashboard/admin/billing" in home_response.get_data(as_text=True)


def test_dashboard_home_shows_clinic_billing_link_for_non_admin():
    _ensure_dashboard_clinic()
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["staff_clinic_id"] = 1
            sess["staff_name"] = "Clinic Staff"
            sess["staff_id"] = 77
            sess["staff_is_admin"] = False
        home_response = client.get("/dashboard/")

    assert home_response.status_code == 200
    body = home_response.get_data(as_text=True)
    assert "/dashboard/billing" in body
    assert "/dashboard/admin/billing" not in body


def test_dashboard_admin_billing_blocks_non_admin_staff():
    _ensure_dashboard_clinic()
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["staff_clinic_id"] = 1
            sess["staff_name"] = "Clinic Staff"
            sess["staff_id"] = 77
            sess["staff_is_admin"] = False
        response = client.get("/dashboard/admin/billing")

    assert response.status_code == 403


def test_dashboard_admin_billing_update_fields():
    _ensure_dashboard_clinic()
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["staff_clinic_id"] = 1
            sess["staff_name"] = "Admin Staff"
            sess["staff_id"] = 88
            sess["staff_is_admin"] = True
        response = client.post(
            "/dashboard/admin/billing/1/update",
            data={
                "price_override": "199",
                "last_paid_date": "2026-04-01",
                "billing_cycle_days": "45",
                "billing_notes": "Updated by admin",
                "billing_paused": "1",
            },
        )

    assert response.status_code == 302
    with SessionLocal() as db:
        clinic = db.query(Clinic).filter(Clinic.id == 1).first()
        assert clinic is not None
        assert float(clinic.plan_price) == 199.0
        assert clinic.last_paid_date.isoformat() == "2026-04-01"
        assert clinic.billing_cycle_days == 45
        assert clinic.billing_status == "paused"
        assert clinic.billing_notes == "Updated by admin"


def test_dashboard_admin_billing_mark_paid_updates_date_and_state():
    _ensure_dashboard_clinic()
    with SessionLocal() as db:
        clinic = db.query(Clinic).filter(Clinic.id == 1).first()
        clinic.billing_status = "paused"
        db.commit()

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["staff_clinic_id"] = 1
            sess["staff_name"] = "Admin Staff"
            sess["staff_id"] = 88
            sess["staff_is_admin"] = True
        response = client.post("/dashboard/admin/billing/1/mark-paid")

    assert response.status_code == 302
    with SessionLocal() as db:
        clinic = db.query(Clinic).filter(Clinic.id == 1).first()
        assert clinic is not None
        assert clinic.billing_status == "paid"
        assert clinic.last_paid_date == date.today()


def test_dashboard_admin_billing_update_blocked_for_non_admin():
    _ensure_dashboard_clinic()
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["staff_clinic_id"] = 1
            sess["staff_name"] = "Clinic Staff"
            sess["staff_id"] = 77
            sess["staff_is_admin"] = False
        response = client.post(
            "/dashboard/admin/billing/1/update",
            data={"billing_paused": "1"},
        )

    assert response.status_code == 403


def test_whatsapp_escalation_message_falls_back_when_no_human_number():
    clinic_no_number = {**CLINIC_A, "human_contact_number": "", "twilio_number": ""}
    with app.test_client() as client:
        with (
            patch("app.get_clinic_by_twilio_number", return_value=clinic_no_number),
            patch("app.is_human_escalation_request", return_value=True),
            patch("app.has_unresolved_human_flag", return_value=False),
            patch("app.write_conversation_flag"),
            patch("app.send_whatsapp_outbound") as mock_send,
            patch("app.now_local", return_value=_fixed_local(hour=11)),
        ):
            response = _post_whatsapp(client, "human please")

    assert response.status_code == 200
    body = mock_send.call_args.kwargs["body"]
    assert body == "Sure — I'll notify our clinic team. Someone will assist you shortly."


def test_run_ai_does_not_add_hint_on_first_message():
    fixed_day = _fixed_local(hour=11)

    class _Resp:
        def __init__(self, text):
            self.output = []
            self.output_text = text
            self.id = "resp-1"

    with patch("app.build_system_prompt", return_value="system"), patch(
        "app.client.responses.create",
        return_value=_Resp("How can I help you today?"),
    ), patch("app.now_local", return_value=fixed_day):
        first = run_ai("whatsapp:+60116667777", "hello", clinic=CLINIC_A)

    assert "If you'd like to speak to our receptionist at any point, just type 'human'." not in first
    assert "If you'd prefer a receptionist to assist, just type 'human'." not in first


def test_run_ai_adds_soft_hint_once_for_long_conversation():
    fixed_day = _fixed_local(hour=11)

    class _Resp:
        def __init__(self, text):
            self.output = []
            self.output_text = text
            self.id = "resp-1"

    with patch("app.build_system_prompt", return_value="system"), patch(
        "app.client.responses.create",
        return_value=_Resp("How can I help you today?"),
    ), patch("app.now_local", return_value=fixed_day):
        replies = []
        for i in range(7):
            replies.append(run_ai("whatsapp:+60116667778", f"message {i}", clinic=CLINIC_A))

    assert "If you'd like to speak to our receptionist at any point, just type 'human'." in replies[5]
    assert "If you'd like to speak to our receptionist at any point, just type 'human'." not in replies[6]


def test_run_ai_adds_stronger_hint_for_confusion():
    fixed_day = _fixed_local(hour=11)

    class _Resp:
        def __init__(self, text):
            self.output = []
            self.output_text = text
            self.id = "resp-1"

    with patch("app.build_system_prompt", return_value="system"), patch(
        "app.client.responses.create",
        return_value=_Resp("Could you share the date and time you prefer?"),
    ), patch("app.now_local", return_value=fixed_day):
        run_ai("whatsapp:+60116667779", "hello", clinic=CLINIC_A)
        confused = run_ai("whatsapp:+60116667779", "huh", clinic=CLINIC_A)

    assert "If you'd prefer a receptionist to assist, just type 'human'." in confused


def test_run_ai_adds_stronger_hint_for_repeated_unavailable_friction():
    fixed_day = _fixed_local(hour=11)

    class _Resp:
        def __init__(self, text):
            self.output = []
            self.output_text = text
            self.id = "resp-1"

    responses = [
        _Resp("That time is unavailable. Here are other options."),
        _Resp("That slot is still unavailable."),
        _Resp("Please choose another time."),
    ]

    with patch("app.build_system_prompt", return_value="system"), patch(
        "app.client.responses.create",
        side_effect=responses,
    ), patch("app.now_local", return_value=fixed_day):
        run_ai("whatsapp:+60116667780", "book 10am", clinic=CLINIC_A)
        run_ai("whatsapp:+60116667780", "book 11am", clinic=CLINIC_A)
        third = run_ai("whatsapp:+60116667780", "book 12pm", clinic=CLINIC_A)

    assert "If you'd prefer a receptionist to assist, just type 'human'." in third
