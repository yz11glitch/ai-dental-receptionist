from unittest.mock import patch

import app
from app import Clinic, FeedbackEntry, SessionLocal


def _ensure_test_clinic(clinic_id: int = 999) -> None:
    with SessionLocal() as db:
        existing = db.query(Clinic).filter(Clinic.id == clinic_id).first()
        if existing:
            return
        db.add(
            Clinic(
                id=clinic_id,
                name="Feedback Test Clinic",
                location="Kuala Lumpur",
                timezone="Asia/Kuala_Lumpur",
                open_hour=10,
                close_hour=18,
                hours_text="Mon-Sat 10:00-18:00",
            )
        )
        db.commit()


def _clear_feedback_rows() -> None:
    with SessionLocal() as db:
        db.query(FeedbackEntry).delete()
        db.commit()


def test_feedback_requires_dashboard_session():
    _clear_feedback_rows()
    with app.app.test_client() as c:
        res = c.post("/feedback", json={"category": "general", "message": "Hello"})
    assert res.status_code == 401
    assert res.get_json()["ok"] is False


def test_feedback_saves_and_notifies_telegram():
    _clear_feedback_rows()
    _ensure_test_clinic()

    with patch("app.send_telegram") as mock_tg:
        with app.app.test_client() as c:
            with c.session_transaction() as sess:
                sess["staff_clinic_id"] = 999
                sess["staff_id"] = 77
                sess["staff_name"] = "Test Staff"

            res = c.post(
                "/feedback",
                json={
                    "category": "bug",
                    "message": "The bookings filter is confusing on mobile.",
                },
            )

    assert res.status_code == 200
    assert res.get_json()["ok"] is True

    with SessionLocal() as db:
        row = db.query(FeedbackEntry).order_by(FeedbackEntry.id.desc()).first()
        assert row is not None
        assert row.clinic_id == 999
        assert row.staff_id == 77
        assert row.staff_name == "Test Staff"
        assert row.category == "Bug"
        assert row.message == "The bookings filter is confusing on mobile."

    mock_tg.assert_called_once()
    assert "Feedback Test Clinic" in mock_tg.call_args[0][0]
