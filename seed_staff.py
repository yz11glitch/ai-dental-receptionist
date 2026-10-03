"""
seed_staff.py — Create (or update) a staff login account for the dashboard.

Usage (interactive — prompts for anything not set in the environment):
    DATABASE_URL="postgresql://..." python3 seed_staff.py

Usage (non-interactive, e.g. in a deploy shell):
    DATABASE_URL="postgresql://..." \\
    STAFF_EMAIL="owner@example.com" \\
    STAFF_FULL_NAME="Clinic Owner" \\
    STAFF_CLINIC_ID=1 \\
    STAFF_PASSWORD="<a long random password>" \\
    python3 seed_staff.py

There is deliberately no default email or password. To give an account access
to the admin billing pages, add its email to DASHBOARD_ADMIN_EMAILS.
Never commit real credentials to version control.
"""

import getpass
import os
import sys
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL environment variable is not set.")
    print("Set it to your PostgreSQL connection string and retry:")
    print("  DATABASE_URL=postgresql://... python3 seed_staff.py")
    sys.exit(1)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

try:
    import bcrypt
except ImportError:
    print("ERROR: bcrypt is not installed. Run: pip install bcrypt")
    sys.exit(1)

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DEFAULT_CLINIC_ID = 1
MIN_PASSWORD_LENGTH = 12


class SeedStaffError(ValueError):
    """Raised when the supplied staff details are missing or invalid."""


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def collect_staff_details(env=None, input_fn=input, getpass_fn=getpass.getpass, interactive=None):
    """Return (email, full_name, clinic_id, password) from env vars or prompts.

    Values come from STAFF_EMAIL / STAFF_FULL_NAME / STAFF_CLINIC_ID /
    STAFF_PASSWORD when set; anything missing is prompted for when running
    interactively, otherwise a SeedStaffError is raised. There are no default
    credentials.
    """
    env = os.environ if env is None else env
    if interactive is None:
        interactive = sys.stdin.isatty()

    def _value(var, prompt, secret=False):
        value = (env.get(var) or "").strip()
        if value:
            return value
        if not interactive:
            raise SeedStaffError(f"{var} is not set and no terminal is available to prompt for it.")
        return (getpass_fn(prompt) if secret else input_fn(prompt)).strip()

    email = _value("STAFF_EMAIL", "  Email: ").lower()
    if not email or "@" not in email:
        raise SeedStaffError("A valid staff email is required.")

    full_name = _value("STAFF_FULL_NAME", "  Full name: ")
    if not full_name:
        raise SeedStaffError("A staff full name is required.")

    clinic_id_raw = (env.get("STAFF_CLINIC_ID") or "").strip()
    if not clinic_id_raw and interactive:
        clinic_id_raw = input_fn(f"  Clinic ID [{DEFAULT_CLINIC_ID}]: ").strip()
    try:
        clinic_id = int(clinic_id_raw) if clinic_id_raw else DEFAULT_CLINIC_ID
    except ValueError:
        raise SeedStaffError("Clinic ID must be a whole number.") from None

    password = env.get("STAFF_PASSWORD") or ""
    if not password:
        if not interactive:
            raise SeedStaffError("STAFF_PASSWORD is not set and no terminal is available to prompt for it.")
        password = getpass_fn(f"  Password (min {MIN_PASSWORD_LENGTH} chars): ")
        if password != getpass_fn("  Confirm password: "):
            raise SeedStaffError("Passwords do not match.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SeedStaffError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    return email, full_name, clinic_id, password


def main():
    print("\n=== AI Receptionist — Staff Account Seed ===\n")

    try:
        email, full_name, clinic_id, password = collect_staff_details()
    except SeedStaffError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    password_hash = hash_password(password)

    engine = create_engine(DATABASE_URL, future=True)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        existing = db.execute(
            text("SELECT id FROM clinic_staff WHERE email = :email"),
            {"email": email},
        ).fetchone()

        if existing:
            db.execute(
                text(
                    """
                    UPDATE clinic_staff
                    SET clinic_id = :clinic_id,
                        password_hash = :password_hash,
                        full_name = :full_name,
                        is_active = TRUE
                    WHERE email = :email
                    """
                ),
                {
                    "clinic_id": clinic_id,
                    "email": email,
                    "password_hash": password_hash,
                    "full_name": full_name,
                },
            )
        else:
            db.execute(
                text(
                    """
                    INSERT INTO clinic_staff (clinic_id, email, password_hash, full_name, is_active, created_at)
                    VALUES (:clinic_id, :email, :password_hash, :full_name, TRUE, :created_at)
                    """
                ),
                {
                    "clinic_id": clinic_id,
                    "email": email,
                    "password_hash": password_hash,
                    "full_name": full_name,
                    "created_at": datetime.utcnow(),
                },
            )
        db.commit()

        row = db.execute(
            text("SELECT id FROM clinic_staff WHERE email = :email"),
            {"email": email},
        ).fetchone()

    print(f"\nStaff account saved (id={row[0]}).")
    print(f"  Email:     {email}")
    print(f"  Name:      {full_name}")
    print(f"  Clinic ID: {clinic_id}")
    print()
    print("Next steps:")
    print("  1. Log in at /dashboard/login with this email and the password you entered.")
    print("  2. For admin billing access, add the email to DASHBOARD_ADMIN_EMAILS.")


if __name__ == "__main__":
    main()
