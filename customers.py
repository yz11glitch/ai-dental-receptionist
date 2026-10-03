"""
customers.py — Customer CRM helpers for the AI WhatsApp Dental Receptionist.

Functions here write to the 'customers' table to track first/last contact
and appointment history per clinic. All operations are wrapped in try/except
so they never raise or crash the webhook.

Imports SessionLocal and Customer model from app.py. This works because app.py
imports this module at its bottom, after all model/session definitions are complete.
"""

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger("ai_receptionist")


def upsert_customer(clinic_id: int, phone: str, name: Optional[str] = None) -> None:
    """Insert or update a customer row for (clinic_id, phone).

    On first contact: INSERT with first_contact_at=now, last_contact_at=now.
    On repeat contact: UPDATE last_contact_at=now. If name was NULL and is now
    known, set it.

    Wrapped in try/except — must never raise or crash the webhook.
    """
    # Deferred import to avoid circular import at module load time.
    from app import SessionLocal, Customer

    if clinic_id is None or not phone:
        return
    try:
        now = datetime.utcnow()
        with SessionLocal() as db:
            row = db.query(Customer).filter(
                Customer.clinic_id == clinic_id,
                Customer.phone == phone,
            ).first()
            if not row:
                row = Customer(
                    clinic_id=clinic_id,
                    phone=phone,
                    name=name,
                    first_contact_at=now,
                    last_contact_at=now,
                )
                db.add(row)
            else:
                row.last_contact_at = now
                if name and not row.name:
                    row.name = name
            db.commit()
    except Exception:
        logger.exception(
            "upsert_customer: error for clinic_id=%s phone=%s", clinic_id, phone
        )


def record_booking_for_customer(
    clinic_id: int,
    phone: str,
    appointment_dt: datetime,
    name: Optional[str] = None,
) -> None:
    """Upsert the customer row and increment total_bookings + set last_appointment_at.

    Called after a booking is successfully created in Google Calendar.
    Wrapped in try/except — must never raise or crash the webhook.
    """
    # Deferred import to avoid circular import at module load time.
    from app import SessionLocal, Customer

    if clinic_id is None or not phone:
        return
    try:
        upsert_customer(clinic_id, phone, name=name)
        with SessionLocal() as db:
            row = db.query(Customer).filter(
                Customer.clinic_id == clinic_id,
                Customer.phone == phone,
            ).first()
            if row:
                row.total_bookings = (row.total_bookings or 0) + 1
                row.last_appointment_at = appointment_dt
                if name and not row.name:
                    row.name = name
                db.commit()
    except Exception:
        logger.exception(
            "record_booking_for_customer: error for clinic_id=%s phone=%s", clinic_id, phone
        )
