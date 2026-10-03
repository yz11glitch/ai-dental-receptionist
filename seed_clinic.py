"""
seed_clinic.py — Interactive operator onboarding script for a new clinic.

Usage:
    DATABASE_URL="postgresql://..." python3 seed_clinic.py

Prompts for all clinic details step-by-step, shows a summary,
and inserts into the production PostgreSQL database on confirmation.
"""

import os
import sys

# Year used to load the public holiday template.
# Update this when onboarding clinics for a new calendar year.
ONBOARDING_YEAR = 2026

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL environment variable is not set.")
    print("Set it to your Railway PostgreSQL connection string and retry:")
    print("  DATABASE_URL=postgresql://... python3 seed_clinic.py")
    sys.exit(1)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from clinic_holidays import SUPPORTED_STATES, get_holidays, has_holidays


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def prompt(label, default=None, required=True):
    """Prompt for a string value. Shows default if provided."""
    suffix = f" [{default}]" if default is not None else ""
    while True:
        value = input(f"  {label}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        if not required:
            return ""
        print("  This field is required.")


def prompt_int(label, default=None):
    """Prompt for an integer value."""
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"  {label}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        try:
            value = int(raw)
            return value
        except ValueError:
            print("  Please enter a whole number (e.g. 9, 18, 30).")


def confirm(label):
    """Ask a y/n question. Returns True for yes."""
    while True:
        raw = input(f"  {label} (y/n): ").strip().lower()
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  Please enter y or n.")


def section(title):
    print(f"\n{'─' * 50}")
    print(f"  {title}")
    print(f"{'─' * 50}")


# ---------------------------------------------------------------------------
# Collection steps
# ---------------------------------------------------------------------------

def collect_clinic_details():
    section("Clinic Details")
    name = prompt("Clinic name")
    location = prompt("Location (city or area shown to patients)")
    timezone = prompt("Timezone", default="Asia/Kuala_Lumpur")
    open_hour = prompt_int("Opening hour (24h, e.g. 9 for 9:00 AM)", default=9)
    close_hour = prompt_int("Closing hour (24h, e.g. 18 for 6:00 PM)", default=18)
    hours_text = prompt(
        "Hours description (shown to patients)",
        default=f"Monday to Saturday, {open_hour}:00 to {close_hour}:00. Closed Sunday."
    )
    return {
        "name": name,
        "location": location,
        "timezone": timezone,
        "open_hour": open_hour,
        "close_hour": close_hour,
        "hours_text": hours_text,
    }


def collect_integration_details():
    section("Integration Details")
    google_calendar_id = prompt("Google Calendar ID")
    twilio_number = prompt("Twilio WhatsApp number (e.g. +601XXXXXXXXX)")
    human_contact_number = prompt(
        "Human contact number for escalations (optional)",
        required=False,
    )
    return {
        "google_calendar_id": google_calendar_id,
        "twilio_number": twilio_number,
        "human_contact_number": human_contact_number,
    }


def collect_optional_details(clinic_name):
    section("Optional Details")
    slot_minutes = prompt_int("Slot duration in minutes (minimum booking unit)", default=30)
    promo_message = prompt("Current promo or announcement (leave blank to skip)", required=False)
    return {
        "slot_minutes": slot_minutes,
        "opening_message": f"Welcome to {clinic_name}! How may we help you today?",
        "promo_message": promo_message,
    }


def collect_services():
    section("Services")
    print("  Add each service the clinic offers.")
    services = {}
    while True:
        name = prompt("Service name (e.g. scaling, filling, polishing)")
        duration = prompt_int("Duration (minutes)")
        price_str = prompt("Price in RM (e.g. 80, 150.50) — press Enter to skip", required=False)
        price = None
        if price_str:
            try:
                price = float(price_str)
            except ValueError:
                print("  Invalid price — skipping (will be set to no price).")
        services[name.lower()] = {"duration": duration, "price": price}
        if not confirm("Add another service?"):
            break
    return services


def collect_state():
    section("Clinic State / Region")
    print("  Select the state this clinic is located in:")
    for i, state in enumerate(SUPPORTED_STATES, start=1):
        print(f"    {i}. {state}")
    while True:
        raw = input("  Enter number: ").strip()
        try:
            choice = int(raw)
            if 1 <= choice <= len(SUPPORTED_STATES):
                return SUPPORTED_STATES[choice - 1]
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {len(SUPPORTED_STATES)}.")


def collect_closures_with_template(state):
    section("Closure Dates")
    closures = {}

    template = get_holidays(ONBOARDING_YEAR, state)

    if not template:
        print(f"  No {ONBOARDING_YEAR} holiday template available for {state} yet.")
        print("  You can add closure dates manually below.")
    else:
        print(f"\n  {ONBOARDING_YEAR} Public Holiday Template — {state} ({len(template)} dates):")
        dates = sorted(template.keys())
        for date_str in dates:
            print(f"    {date_str}  {template[date_str]}")

        if confirm(f"\n  Apply this template as closure dates?"):
            closures = dict(sorted(template.items()))
            print(f"  {len(closures)} holiday dates loaded.")

            if confirm("  Remove any dates this clinic stays open on?"):
                print("  Enter the numbers of dates to remove (space-separated), or press Enter to skip:")
                date_list = sorted(closures.keys())
                for i, date_str in enumerate(date_list, start=1):
                    print(f"    {i:>2}. {date_str}  {closures[date_str]}")
                raw = input("  Numbers to remove: ").strip()
                if raw:
                    to_remove = []
                    for token in raw.split():
                        try:
                            idx = int(token) - 1
                            if 0 <= idx < len(date_list):
                                to_remove.append(date_list[idx])
                        except ValueError:
                            pass
                    for date_str in to_remove:
                        removed = closures.pop(date_str, None)
                        if removed:
                            print(f"    Removed: {date_str}  {removed}")
                    print(f"  {len(closures)} dates remaining after removal.")
        else:
            print("  Template skipped. No holiday dates loaded.")

    if confirm("  Add any extra clinic-specific closure dates (staff training, renovation, etc.)?"):
        print("  Enter closure dates. Format: YYYY-MM-DD")
        while True:
            date_str = prompt("Date (YYYY-MM-DD)")
            note = prompt("Reason shown to patients (e.g. Closed for staff training)")
            closures[date_str] = note
            if not confirm("Add another closure date?"):
                break

    return dict(sorted(closures.items()))


# ---------------------------------------------------------------------------
# Summary and confirmation
# ---------------------------------------------------------------------------

def print_summary(config):
    section("Summary — Please Review")
    print(f"\n  Clinic:        {config['name']}")
    print(f"  Location:      {config['location']}")
    print(f"  State:         {config['state']}")
    print(f"  Timezone:      {config['timezone']}")
    print(f"  Hours:         {config['open_hour']}:00 – {config['close_hour']}:00")
    print(f"  Hours text:    {config['hours_text']}")
    print(f"  Slot minutes:  {config['slot_minutes']}")
    print(f"  Calendar ID:   {config['google_calendar_id']}")
    print(f"  Twilio number: {config['twilio_number']}")
    print(f"  Human contact: {config['human_contact_number'] or '(none)'}")
    if config["promo_message"]:
        print(f"  Promo:         {config['promo_message']}")

    print(f"\n  Services:")
    for name, info in config["services"].items():
        price_str = f", RM {info['price']:.2f}" if info["price"] is not None else ""
        print(f"    - {name.title()} ({info['duration']} min{price_str})")

    if config["closures"]:
        print(f"\n  Closure dates:")
        for date_str, note in config["closures"].items():
            print(f"    - {date_str}: {note}")
    else:
        print(f"\n  Closure dates: none")


# ---------------------------------------------------------------------------
# Database insertion (unchanged logic from original)
# ---------------------------------------------------------------------------

def insert_clinic(config):
    engine = create_engine(DATABASE_URL, future=True)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        existing = db.execute(
            text("SELECT id FROM clinics WHERE name = :name"),
            {"name": config["name"]}
        ).fetchone()

        if existing:
            print(f"\nERROR: A clinic named '{config['name']}' already exists (id={existing[0]}).")
            print("Re-run and use a different name, or remove the existing row first.")
            sys.exit(1)

        db.execute(text("""
            INSERT INTO clinics
                (name, location, timezone, open_hour, close_hour, hours_text,
                 slot_minutes, opening_message, promo_message,
                 google_calendar_id, twilio_number, human_contact_number, is_active)
            VALUES
                (:name, :location, :timezone, :open_hour, :close_hour, :hours_text,
                 :slot_minutes, :opening_message, :promo_message,
                 :google_calendar_id, :twilio_number, :human_contact_number, true)
        """), {
            "name": config["name"],
            "location": config["location"],
            "timezone": config["timezone"],
            "open_hour": config["open_hour"],
            "close_hour": config["close_hour"],
            "hours_text": config["hours_text"],
            "slot_minutes": config["slot_minutes"],
            "opening_message": config["opening_message"],
            "promo_message": config["promo_message"],
            "google_calendar_id": config["google_calendar_id"],
            "twilio_number": config["twilio_number"],
            "human_contact_number": config["human_contact_number"] or None,
        })

        clinic_id = db.execute(
            text("SELECT id FROM clinics WHERE name = :name"),
            {"name": config["name"]}
        ).scalar()

        for service_name, info in config["services"].items():
            db.execute(text("""
                INSERT INTO clinic_services (clinic_id, service_name, duration_minutes, price, is_active)
                VALUES (:clinic_id, :service_name, :duration_minutes, :price, true)
            """), {
                "clinic_id": clinic_id,
                "service_name": service_name,
                "duration_minutes": info["duration"],
                "price": info["price"],
            })

        for date_str, note in config["closures"].items():
            db.execute(text("""
                INSERT INTO clinic_closures (clinic_id, date, note)
                VALUES (:clinic_id, :date, :note)
            """), {
                "clinic_id": clinic_id,
                "date": date_str,
                "note": note,
            })

        db.commit()

    return clinic_id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("\n=== AI Receptionist — Clinic Onboarding ===")

    config = {}
    config.update(collect_clinic_details())
    config.update(collect_integration_details())
    config.update(collect_optional_details(config["name"]))
    config["services"] = collect_services()
    config["state"] = collect_state()
    config["closures"] = collect_closures_with_template(config["state"])

    print_summary(config)

    print()
    if not confirm("Confirm insert into production database?"):
        print("\n  Aborted. Nothing was inserted.")
        sys.exit(0)

    print("\n  Inserting...")
    clinic_id = insert_clinic(config)

    print(f"\n✓ Clinic '{config['name']}' created (id={clinic_id}).")
    print(f"  {len(config['services'])} service(s) inserted.")
    print(f"  {len(config['closures'])} closure date(s) inserted.")
    print()
    print("Next steps:")
    print("  1. Ask the clinic to share their Google Calendar with your service account email.")
    print("  2. Confirm the Twilio number points to /whatsapp in the Twilio console.")
    print("  3. Send 'hi' to the WhatsApp number — confirm the bot responds with this clinic's message.")


if __name__ == "__main__":
    main()
