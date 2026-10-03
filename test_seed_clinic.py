import os
import sqlite3
import sys
import importlib

DB_PATH = "test_seed_clinic.db"


def _load_seed_clinic():
    os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
    if "seed_clinic" in sys.modules:
        return importlib.reload(sys.modules["seed_clinic"])
    import seed_clinic  # pylint: disable=import-outside-toplevel
    return seed_clinic


def _prepare_db() -> None:
    try:
        os.remove(DB_PATH)
    except FileNotFoundError:
        pass

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE clinics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                location TEXT NOT NULL,
                timezone TEXT NOT NULL,
                open_hour INTEGER NOT NULL,
                close_hour INTEGER NOT NULL,
                hours_text TEXT NOT NULL,
                slot_minutes INTEGER NOT NULL,
                opening_message TEXT,
                promo_message TEXT,
                google_calendar_id TEXT,
                twilio_number TEXT,
                human_contact_number TEXT,
                is_active BOOLEAN NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE clinic_services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                clinic_id INTEGER NOT NULL,
                service_name TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL,
                price REAL,
                is_active BOOLEAN NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE clinic_closures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                clinic_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                note TEXT
            )
            """
        )
        conn.commit()


def _config(name: str, human_contact_number: str):
    return {
        "name": name,
        "location": "Kuala Lumpur",
        "timezone": "Asia/Kuala_Lumpur",
        "open_hour": 10,
        "close_hour": 18,
        "hours_text": "Monday to Saturday, 10:00 to 18:00. Closed Sunday.",
        "slot_minutes": 30,
        "opening_message": f"Welcome to {name}!",
        "promo_message": "",
        "google_calendar_id": "primary",
        "twilio_number": "+60111111111",
        "human_contact_number": human_contact_number,
        "services": {"scaling": {"duration": 30, "price": None}},
        "closures": {},
        "state": "Kuala Lumpur",
    }


def teardown_module():
    try:
        os.remove(DB_PATH)
    except FileNotFoundError:
        pass


def test_insert_clinic_stores_null_when_human_contact_blank():
    _prepare_db()
    seed_clinic = _load_seed_clinic()
    clinic_id = seed_clinic.insert_clinic(_config("Seed Clinic Null Contact", ""))

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT human_contact_number FROM clinics WHERE id = ?",
            (clinic_id,),
        ).fetchone()

    assert row is not None
    assert row[0] is None


def test_insert_clinic_stores_human_contact_when_provided():
    _prepare_db()
    seed_clinic = _load_seed_clinic()
    clinic_id = seed_clinic.insert_clinic(_config("Seed Clinic With Contact", "+60123456789"))

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT human_contact_number FROM clinics WHERE id = ?",
            (clinic_id,),
        ).fetchone()

    assert row is not None
    assert row[0] == "+60123456789"
