"""
dashboard.py — Staff dashboard Blueprint for the AI WhatsApp Receptionist.

Provides login-gated views for clinic staff to review conversations,
manage flagged interactions, and browse booking activity.

All DB queries are scoped to session["staff_clinic_id"]. Clinic ID is
NEVER accepted from URL params or form fields.
"""

import logging
import os
import re
from datetime import date, datetime
from functools import wraps

import bcrypt
from flask import (
    Blueprint,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
    abort,
)
from sqlalchemy import func, or_

from app import (
    SessionLocal,
    BookingRecordModel,
    Clinic,
    ClinicDailyMetric,
    ClinicStaff,
    ConversationFlag,
    ConversationMessage,
    Customer,
    compute_billing_info,
    now_local,
    TIMEZONE,
)

logger = logging.getLogger("ai_receptionist.dashboard")

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

PAGE_SIZE = 30
# Comma-separated staff emails allowed into the admin billing pages.
# Empty by default: no one is an admin unless explicitly configured.
_RAW_ADMIN_EMAILS = os.environ.get("DASHBOARD_ADMIN_EMAILS", "")
_DASHBOARD_ADMIN_EMAILS = {
    email.strip().lower()
    for email in _RAW_ADMIN_EMAILS.split(",")
    if email.strip()
}


# -----------------------------------------------------------------------------
# Per-request clinic health injection
# -----------------------------------------------------------------------------

@dashboard_bp.before_request
def load_clinic_health():
    """Inject calendar health status and open flag count into Flask g for use in templates."""
    g.staff_is_admin = bool(session.get("staff_is_admin"))
    if "staff_clinic_id" not in session:
        g.calendar_healthy = True
        g.calendar_error = None
        g.open_flags_count = 0
        return
    try:
        with SessionLocal() as db:
            clinic = db.query(Clinic).filter(
                Clinic.id == session["staff_clinic_id"]
            ).first()
            g.calendar_healthy = clinic.calendar_healthy if clinic else True
            g.calendar_error = clinic.calendar_error if clinic else None
    except Exception:
        logger.exception("load_clinic_health: DB error; defaulting to healthy")
        g.calendar_healthy = True
        g.calendar_error = None

    try:
        with SessionLocal() as db:
            g.open_flags_count = (
                db.query(ConversationFlag)
                .filter(
                    ConversationFlag.clinic_id == session["staff_clinic_id"],
                    ConversationFlag.resolved_at == None,  # noqa: E711
                )
                .count()
            )
    except Exception:
        logger.exception("load_clinic_health: failed to count open flags; defaulting to 0")
        g.open_flags_count = 0


# -----------------------------------------------------------------------------
# Auth helpers
# -----------------------------------------------------------------------------


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "staff_clinic_id" not in session:
            return redirect(url_for("dashboard.login"))
        return f(*args, **kwargs)
    return decorated


def _is_staff_admin(staff: ClinicStaff) -> bool:
    if bool(getattr(staff, "is_admin", False)):
        return True
    staff_email = (staff.email or "").strip().lower()
    return staff_email in _DASHBOARD_ADMIN_EMAILS


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "staff_clinic_id" not in session:
            return redirect(url_for("dashboard.login"))
        if not session.get("staff_is_admin", False):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def _format_time_input(value):
    if not value:
        return ""
    if isinstance(value, str):
        value = value.strip()
        # Accept "HH:MM", "H:MM", and timestamp-like strings that start with time.
        match = re.match(r"^(\d{1,2}):(\d{2})", value)
        if not match:
            return ""
        hours = int(match.group(1))
        minutes = int(match.group(2))
        if not (0 <= hours <= 23 and 0 <= minutes <= 59):
            return ""
        return f"{hours:02d}:{minutes:02d}"
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    return ""


# -----------------------------------------------------------------------------
# Auth routes
# -----------------------------------------------------------------------------


@dashboard_bp.route("/login", methods=["GET"])
def login():
    return render_template("dashboard/login.html")


@dashboard_bp.route("/login", methods=["POST"])
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    password = (request.form.get("password") or "")

    if not email or not password:
        flash("Email and password are required.")
        return render_template("dashboard/login.html"), 400

    with SessionLocal() as db:
        staff = (
            db.query(ClinicStaff)
            .filter(ClinicStaff.email == email, ClinicStaff.is_active == True)  # noqa: E712
            .first()
        )

        if not staff:
            logger.warning("dashboard.login: failed attempt for email=%s (not found)", email)
            flash("Invalid email or password.")
            return render_template("dashboard/login.html"), 401

        password_matches = bcrypt.checkpw(
            password.encode("utf-8"),
            staff.password_hash.encode("utf-8"),
        )

        if not password_matches:
            logger.warning(
                "dashboard.login: failed attempt for email=%s clinic_id=%s (bad password)",
                email, staff.clinic_id,
            )
            flash("Invalid email or password.")
            return render_template("dashboard/login.html"), 401

        # Success
        session["staff_id"] = staff.id
        session["staff_clinic_id"] = staff.clinic_id
        session["staff_name"] = staff.full_name
        session["staff_email"] = staff.email
        session["staff_is_admin"] = _is_staff_admin(staff)

        staff.last_login_at = datetime.utcnow()
        db.commit()

        logger.info(
            "dashboard.login: staff_id=%s clinic_id=%s logged in",
            staff.id, staff.clinic_id,
        )

    return redirect(url_for("dashboard.home"))


@dashboard_bp.route("/logout", methods=["POST"])
def logout():
    staff_id = session.get("staff_id")
    session.clear()
    logger.info("dashboard.logout: staff_id=%s", staff_id)
    return redirect(url_for("dashboard.login"))


# -----------------------------------------------------------------------------
# Home / stats
# -----------------------------------------------------------------------------


@dashboard_bp.route("/")
@login_required
def home():
    clinic_id = session["staff_clinic_id"]
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    with SessionLocal() as db:
        clinic = db.query(Clinic).filter(Clinic.id == clinic_id).first()
        clinic_tz = clinic.timezone if clinic else TIMEZONE
        today_local = now_local(clinic_tz).date()

        bookings_this_month = (
            db.query(func.count(BookingRecordModel.event_id))
            .filter(
                BookingRecordModel.clinic_id == clinic_id,
                BookingRecordModel.created_at >= month_start,
            )
            .scalar()
        ) or 0

        open_flags = (
            db.query(func.count(ConversationFlag.id))
            .filter(
                ConversationFlag.clinic_id == clinic_id,
                ConversationFlag.resolved_at == None,  # noqa: E711
            )
            .scalar()
        ) or 0

        total_conversations = (
            db.query(func.count(func.distinct(ConversationMessage.user)))
            .filter(ConversationMessage.clinic_id == clinic_id)
            .scalar()
        ) or 0

        metric_today = (
            db.query(ClinicDailyMetric)
            .filter(
                ClinicDailyMetric.clinic_id == clinic_id,
                ClinicDailyMetric.metric_date == today_local,
            )
            .first()
        )

    with SessionLocal() as db:
        clinic_row = db.query(Clinic).filter(Clinic.id == clinic_id).first()
    billing = compute_billing_info(clinic_row) if clinic_row else None

    return render_template(
        "dashboard/home.html",
        bookings_this_month=bookings_this_month,
        open_flags=open_flags,
        total_conversations=total_conversations,
        metric_today=metric_today,
        billing=billing,
    )


# -----------------------------------------------------------------------------
# Conversations list
# -----------------------------------------------------------------------------


@dashboard_bp.route("/conversations")
@login_required
def conversations():
    clinic_id = session["staff_clinic_id"]
    q = request.args.get("q", "").strip()
    attention_only = request.args.get("attention") == "1"
    page = max(1, request.args.get("page", 1, type=int))
    offset = (page - 1) * PAGE_SIZE

    with SessionLocal() as db:
        # Each distinct user with their last message time and message count.
        subq = (
            db.query(
                ConversationMessage.user,
                func.max(ConversationMessage.created_at).label("last_message_at"),
                func.count(ConversationMessage.id).label("message_count"),
            )
            .filter(ConversationMessage.clinic_id == clinic_id)
            .group_by(ConversationMessage.user)
        )

        if q:
            # Match phones directly OR phones that have a booking with a matching name.
            matching_phones_by_name = (
                db.query(BookingRecordModel.user)
                .filter(
                    BookingRecordModel.clinic_id == clinic_id,
                    BookingRecordModel.name.ilike(f"%{q}%"),
                )
                .subquery()
            )
            subq = subq.filter(
                or_(
                    ConversationMessage.user.ilike(f"%{q}%"),
                    ConversationMessage.user.in_(matching_phones_by_name),
                )
            )

        if attention_only:
            open_flag_phones = (
                db.query(ConversationFlag.phone)
                .filter(
                    ConversationFlag.clinic_id == clinic_id,
                    ConversationFlag.resolved_at == None,  # noqa: E711
                )
                .subquery()
            )
            subq = subq.filter(ConversationMessage.user.in_(open_flag_phones))

        subq = subq.order_by(func.max(ConversationMessage.created_at).desc())

        total_rows = subq.count()
        rows = subq.offset(offset).limit(PAGE_SIZE).all()

        # Build phone -> patient name map from the most recent booking per phone.
        phones = [r.user for r in rows]
        name_map = {}
        if phones:
            bookings = (
                db.query(BookingRecordModel.user, BookingRecordModel.name)
                .filter(
                    BookingRecordModel.clinic_id == clinic_id,
                    BookingRecordModel.user.in_(phones),
                )
                .order_by(BookingRecordModel.created_at.desc())
                .all()
            )
            for b in bookings:
                if b.user not in name_map:
                    name_map[b.user] = b.name

    total_pages = max(1, (total_rows + PAGE_SIZE - 1) // PAGE_SIZE)

    conversations_list = [
        {
            "phone": r.user,
            "patient_name": name_map.get(r.user, ""),
            "last_message_at": r.last_message_at,
            "message_count": r.message_count,
        }
        for r in rows
    ]

    return render_template(
        "dashboard/conversations.html",
        conversations=conversations_list,
        page=page,
        total_pages=total_pages,
        q=q,
        attention_only=attention_only,
    )


# -----------------------------------------------------------------------------
# Conversation detail
# -----------------------------------------------------------------------------


@dashboard_bp.route("/conversations/<path:user_phone>")
@login_required
def conversation_detail(user_phone):
    clinic_id = session["staff_clinic_id"]

    with SessionLocal() as db:
        # Data isolation check: at least one message must exist for this phone
        # in this clinic before we show anything.
        guard = (
            db.query(ConversationMessage.id)
            .filter(
                ConversationMessage.user == user_phone,
                ConversationMessage.clinic_id == clinic_id,
            )
            .first()
        )
        if not guard:
            abort(404)

        messages = (
            db.query(ConversationMessage)
            .filter(
                ConversationMessage.user == user_phone,
                ConversationMessage.clinic_id == clinic_id,
            )
            .order_by(ConversationMessage.created_at.asc())
            .all()
        )

        open_flags = (
            db.query(ConversationFlag)
            .filter(
                ConversationFlag.clinic_id == clinic_id,
                ConversationFlag.phone == user_phone,
                ConversationFlag.resolved_at == None,  # noqa: E711
            )
            .order_by(ConversationFlag.created_at.desc())
            .all()
        )

        bookings = (
            db.query(BookingRecordModel)
            .filter(
                BookingRecordModel.clinic_id == clinic_id,
                BookingRecordModel.user == user_phone,
            )
            .order_by(BookingRecordModel.created_at.desc())
            .all()
        )

    return render_template(
        "dashboard/conversation_detail.html",
        user_phone=user_phone,
        messages=messages,
        open_flags=open_flags,
        bookings=bookings,
    )


# -----------------------------------------------------------------------------
# Needs Attention (flags)
# -----------------------------------------------------------------------------


@dashboard_bp.route("/attention")
@login_required
def attention():
    clinic_id = session["staff_clinic_id"]
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "unresolved")

    with SessionLocal() as db:
        query = (
            db.query(ConversationFlag)
            .filter(ConversationFlag.clinic_id == clinic_id)
        )

        if status == "unresolved":
            query = query.filter(ConversationFlag.resolved_at == None)  # noqa: E711
        elif status == "resolved":
            query = query.filter(ConversationFlag.resolved_at != None)  # noqa: E711
        # status == "all": no filter

        if q:
            query = query.filter(ConversationFlag.phone.ilike(f"%{q}%"))

        flags = query.order_by(ConversationFlag.created_at.desc()).all()

    return render_template(
        "dashboard/attention.html",
        flags=flags,
        q=q,
        status=status,
    )


@dashboard_bp.route("/attention/<int:flag_id>/resolve", methods=["POST"])
@login_required
def resolve_flag(flag_id):
    clinic_id = session["staff_clinic_id"]
    staff_id = session["staff_id"]

    with SessionLocal() as db:
        flag = db.query(ConversationFlag).filter(ConversationFlag.id == flag_id).first()

        if not flag:
            abort(404)

        # Multi-tenant guard: flag must belong to the logged-in clinic.
        if flag.clinic_id != clinic_id:
            logger.warning(
                "dashboard.resolve_flag: staff_id=%s clinic_id=%s attempted to resolve "
                "flag_id=%s belonging to clinic_id=%s — denied",
                staff_id, clinic_id, flag_id, flag.clinic_id,
            )
            abort(403)

        flag.resolved_at = datetime.utcnow()
        flag.resolved_by_staff_id = staff_id
        db.commit()

        logger.info(
            "dashboard.resolve_flag: flag_id=%s resolved by staff_id=%s clinic_id=%s",
            flag_id, staff_id, clinic_id,
        )

    return redirect(url_for("dashboard.attention"))


# -----------------------------------------------------------------------------
# Booking activity log
# -----------------------------------------------------------------------------


@dashboard_bp.route("/bookings")
@login_required
def bookings():
    clinic_id = session["staff_clinic_id"]
    page = max(1, request.args.get("page", 1, type=int))
    q = request.args.get("q", "").strip()
    period = request.args.get("period", "all")

    offset = (page - 1) * PAGE_SIZE

    # today_str and now_str are in Malaysia time for comparing against the stored
    # date/time strings (which are stored as local-time strings, e.g. "2026-04-09").
    now_kl = now_local(TIMEZONE)
    today_str = now_kl.strftime("%Y-%m-%d")
    now_dt_str = now_kl.strftime("%Y-%m-%d %H:%M")

    with SessionLocal() as db:
        query = (
            db.query(BookingRecordModel)
            .filter(BookingRecordModel.clinic_id == clinic_id)
        )

        if q:
            query = query.filter(
                or_(
                    BookingRecordModel.name.ilike(f"%{q}%"),
                    BookingRecordModel.user.ilike(f"%{q}%"),
                )
            )

        if period == "today":
            query = query.filter(BookingRecordModel.date == today_str)
        elif period == "upcoming":
            # date > today OR (date == today AND time >= now)
            query = query.filter(
                or_(
                    BookingRecordModel.date > today_str,
                    (BookingRecordModel.date == today_str),
                )
            ).filter(
                or_(
                    BookingRecordModel.date > today_str,
                    func.concat(BookingRecordModel.date, " ", BookingRecordModel.time) >= now_dt_str,
                )
            )
        elif period == "past":
            query = query.filter(
                or_(
                    BookingRecordModel.date < today_str,
                    func.concat(BookingRecordModel.date, " ", BookingRecordModel.time) < now_dt_str,
                )
            )

        total_rows = query.count()

        booking_rows = (
            query
            .order_by(BookingRecordModel.created_at.desc())
            .offset(offset)
            .limit(PAGE_SIZE)
            .all()
        )

    total_pages = max(1, (total_rows + PAGE_SIZE - 1) // PAGE_SIZE)

    return render_template(
        "dashboard/bookings.html",
        bookings=booking_rows,
        page=page,
        total_pages=total_pages,
        q=q,
        period=period,
    )


# -----------------------------------------------------------------------------
# Customers
# -----------------------------------------------------------------------------


@dashboard_bp.route("/customers")
@login_required
def customers():
    clinic_id = session["staff_clinic_id"]
    page = max(1, request.args.get("page", 1, type=int))
    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "recent")

    offset = (page - 1) * PAGE_SIZE

    with SessionLocal() as db:
        query = (
            db.query(Customer)
            .filter(Customer.clinic_id == clinic_id)
        )

        if q:
            query = query.filter(
                or_(
                    Customer.phone.ilike(f"%{q}%"),
                    Customer.name.ilike(f"%{q}%"),
                )
            )

        if sort == "bookings":
            query = query.order_by(Customer.total_bookings.desc())
        elif sort == "name":
            query = query.order_by(Customer.name.asc().nullslast())
        else:  # "recent" is the default
            query = query.order_by(Customer.last_contact_at.desc())

        total = query.count()
        rows = query.offset(offset).limit(PAGE_SIZE).all()

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    return render_template(
        "dashboard/customers.html",
        customers=rows,
        page=page,
        total_pages=total_pages,
        total=total,
        q=q,
        sort=sort,
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dashboard_bp.route("/updates")
@login_required
def updates():
    return render_template("dashboard/updates.html")


@dashboard_bp.route("/billing")
@login_required
def billing_clinic():
    clinic_id = session["staff_clinic_id"]
    with SessionLocal() as db:
        clinic = db.query(Clinic).filter(
            Clinic.id == clinic_id,
            Clinic.is_active == True,
        ).first()
    if not clinic:
        abort(404)
    billing = compute_billing_info(clinic)
    return render_template("dashboard/billing_clinic.html", clinic=clinic, billing=billing)


@dashboard_bp.route("/admin/billing")
@admin_required
def billing_overview():
    with SessionLocal() as db:
        clinics = db.query(Clinic).filter(Clinic.is_active == True).order_by(Clinic.id).all()
    rows = [
        {"clinic": c, "billing": compute_billing_info(c)}
        for c in clinics
    ]
    return render_template("dashboard/billing.html", rows=rows)


@dashboard_bp.route("/admin/billing/<int:clinic_id>/update", methods=["POST"])
@admin_required
def admin_billing_update(clinic_id):
    from app import BILLING_PLANS
    form = request.form

    # Primary fields
    billing_plan_raw = (form.get("billing_plan") or "").strip().lower() or None
    if billing_plan_raw not in {*BILLING_PLANS.keys(), None}:
        flash("Invalid plan.", "error")
        return redirect(url_for("dashboard.billing_overview"))

    start_date_raw = (form.get("billing_start_date") or "").strip()
    if start_date_raw:
        try:
            billing_start_date = date.fromisoformat(start_date_raw)
        except ValueError:
            flash("Invalid billing start date.", "error")
            return redirect(url_for("dashboard.billing_overview"))
    else:
        billing_start_date = None

    # Advanced overrides
    price_override_raw = (form.get("price_override") or "").strip()
    try:
        price_override = float(price_override_raw) if price_override_raw else None
        if price_override is not None and price_override < 0:
            raise ValueError
    except ValueError:
        flash("Price override must be a non-negative number.", "error")
        return redirect(url_for("dashboard.billing_overview"))

    cycle_days_raw = (form.get("billing_cycle_days") or "").strip()
    try:
        billing_cycle_days = int(cycle_days_raw) if cycle_days_raw else 30
        if billing_cycle_days <= 0:
            raise ValueError
    except ValueError:
        flash("Billing cycle days must be a positive whole number.", "error")
        return redirect(url_for("dashboard.billing_overview"))

    last_paid_raw = (form.get("last_paid_date") or "").strip()
    if last_paid_raw:
        try:
            last_paid_date = date.fromisoformat(last_paid_raw)
        except ValueError:
            flash("Invalid last paid date.", "error")
            return redirect(url_for("dashboard.billing_overview"))
    else:
        last_paid_date = None

    billing_paused = form.get("billing_paused") == "1"
    billing_notes = (form.get("billing_notes") or "").strip() or None

    with SessionLocal() as db:
        clinic = db.query(Clinic).filter(
            Clinic.id == clinic_id,
            Clinic.is_active == True,
        ).first()
        if not clinic:
            abort(404)
        clinic.billing_plan = billing_plan_raw
        clinic.billing_start_date = billing_start_date
        clinic.plan_price = price_override
        clinic.billing_cycle_days = billing_cycle_days
        if last_paid_raw:
            clinic.last_paid_date = last_paid_date
        clinic.billing_status = "paused" if billing_paused else "paid"
        clinic.billing_notes = billing_notes
        db.commit()

    flash("Billing updated.", "success")
    return redirect(url_for("dashboard.billing_overview"))


@dashboard_bp.route("/admin/billing/<int:clinic_id>/mark-paid", methods=["POST"])
@admin_required
def admin_billing_mark_paid(clinic_id):
    with SessionLocal() as db:
        clinic = db.query(Clinic).filter(
            Clinic.id == clinic_id,
            Clinic.is_active == True,
        ).first()
        if not clinic:
            abort(404)
        clinic.last_paid_date = date.today()
        clinic.billing_status = "paid"
        db.commit()

    flash("Marked as paid.", "success")
    return redirect(url_for("dashboard.billing_overview"))


@dashboard_bp.route("/settings", methods=["GET"])
@login_required
def settings():
    clinic_id = session["staff_clinic_id"]
    from app import ClinicPromotion, ClinicService, _VALID_TONES

    with SessionLocal() as db:
        clinic = db.query(Clinic).filter(Clinic.id == clinic_id).first()
        if not clinic:
            abort(404)
        promos = (
            db.query(ClinicPromotion)
            .filter(ClinicPromotion.clinic_id == clinic_id)
            .order_by(ClinicPromotion.created_at.desc())
            .all()
        )
        active_promos = [promo for promo in promos if promo.is_active]
        services = (
            db.query(ClinicService)
            .filter(ClinicService.clinic_id == clinic_id, ClinicService.is_active == True)
            .order_by(ClinicService.id.asc())
            .all()
        )
    return render_template(
        "dashboard/settings.html",
        clinic=clinic,
        current_tone=clinic.tone or "professional",
        welcome_message=clinic.opening_message or "",
        clinic_hours={
            "open_hour": clinic.open_hour,
            "close_hour": clinic.close_hour,
            "lunch_start": _format_time_input(clinic.lunch_start),
            "lunch_end": _format_time_input(clinic.lunch_end),
            "hours_text": clinic.hours_text,
        },
        promotions=promos,
        active_promotions=active_promos,
        valid_tones=sorted(_VALID_TONES),
        services=services,
    )


@dashboard_bp.route("/settings", methods=["POST"])
@login_required
def settings_post():
    clinic_id = session["staff_clinic_id"]
    form = request.form
    from app import _VALID_TONES

    # Validate tone
    raw_tone = (form.get("tone") or "").strip().lower()
    tone = raw_tone if raw_tone in _VALID_TONES else "professional"

    # Backward-compatible support for both key names.
    welcome_message = (
        (form.get("welcome_message") if "welcome_message" in form else form.get("opening_message")) or ""
    ).strip() or None
    hours_text = (form.get("hours_text") or "").strip()

    open_hour = None
    close_hour = None
    include_open_hour = "open_hour" in form
    include_close_hour = "close_hour" in form
    if include_open_hour or include_close_hour:
        # Sanitise hours only when provided: integers in [0, 23], open < close
        try:
            open_hour = int((form.get("open_hour") or "").strip())
            close_hour = int((form.get("close_hour") or "").strip())
            open_hour = max(0, min(23, open_hour))
            close_hour = max(0, min(23, close_hour))
            if open_hour >= close_hour:
                flash("Opening hour must be before closing hour.", "error")
                return redirect(url_for("dashboard.settings"))
        except (ValueError, TypeError):
            flash("Invalid hours value.", "error")
            return redirect(url_for("dashboard.settings"))

    def _to_minutes(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    lunch_start_raw = (form.get("lunch_start") or "").strip()
    lunch_end_raw = (form.get("lunch_end") or "").strip()
    lunch_start = None
    lunch_end = None
    if lunch_start_raw or lunch_end_raw:
        if not lunch_start_raw or not lunch_end_raw:
            flash("Please set both lunch start and lunch end, or leave both empty.", "error")
            return redirect(url_for("dashboard.settings"))
        try:
            lunch_start = datetime.strptime(lunch_start_raw, "%H:%M").strftime("%H:%M")
            lunch_end = datetime.strptime(lunch_end_raw, "%H:%M").strftime("%H:%M")
        except ValueError:
            flash("Invalid lunch break time format.", "error")
            return redirect(url_for("dashboard.settings"))
        if _to_minutes(lunch_start) >= _to_minutes(lunch_end):
            flash("Lunch start must be before lunch end.", "error")
            return redirect(url_for("dashboard.settings"))

    with SessionLocal() as db:
        clinic = db.query(Clinic).filter(Clinic.id == clinic_id).first()
        if not clinic:
            abort(404)
        effective_open = open_hour if (include_open_hour and include_close_hour) else clinic.open_hour
        effective_close = close_hour if (include_open_hour and include_close_hour) else clinic.close_hour
        if lunch_start and lunch_end:
            if _to_minutes(lunch_start) < (effective_open * 60) or _to_minutes(lunch_end) > (effective_close * 60):
                flash("Lunch break must be within opening hours.", "error")
                return redirect(url_for("dashboard.settings"))

        clinic.tone = tone
        if include_open_hour and include_close_hour:
            clinic.open_hour = open_hour
            clinic.close_hour = close_hour
        clinic.lunch_start = lunch_start
        clinic.lunch_end = lunch_end
        clinic.opening_message = welcome_message
        if hours_text:
            clinic.hours_text = hours_text

        db.commit()

    flash("Settings updated", "success")
    return redirect(url_for("dashboard.settings"))


@dashboard_bp.route("/settings/promotions/add", methods=["POST"])
@login_required
def promotions_add():
    clinic_id = session["staff_clinic_id"]
    from app import ClinicPromotion

    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip() or None

    if not title:
        flash("Promotion title is required.", "error")
        return redirect(url_for("dashboard.settings"))

    with SessionLocal() as db:
        promo = ClinicPromotion(
            clinic_id=clinic_id,
            title=title,
            description=description,
            is_active=True,
        )
        db.add(promo)
        db.commit()

    flash("Promotion added", "success")
    return redirect(url_for("dashboard.settings"))


@dashboard_bp.route("/settings/promotions/<int:promo_id>/toggle", methods=["POST"])
@login_required
def promotions_toggle(promo_id):
    clinic_id = session["staff_clinic_id"]
    from app import ClinicPromotion

    with SessionLocal() as db:
        promo = db.query(ClinicPromotion).filter(
            ClinicPromotion.id == promo_id,
            ClinicPromotion.clinic_id == clinic_id,
        ).first()
        if not promo:
            abort(404)
        promo.is_active = not promo.is_active
        db.commit()
    return redirect(url_for("dashboard.settings"))


@dashboard_bp.route("/settings/promotions/<int:promo_id>/delete", methods=["POST"])
@login_required
def promotions_delete(promo_id):
    clinic_id = session["staff_clinic_id"]
    from app import ClinicPromotion

    with SessionLocal() as db:
        promo = db.query(ClinicPromotion).filter(
            ClinicPromotion.id == promo_id,
            ClinicPromotion.clinic_id == clinic_id,
        ).first()
        if not promo:
            abort(404)
        db.delete(promo)
        db.commit()
    flash("Promotion deleted.", "success")
    return redirect(url_for("dashboard.settings"))


@dashboard_bp.route("/settings/services/add", methods=["POST"])
@login_required
def services_add():
    clinic_id = session["staff_clinic_id"]
    from app import ClinicService

    name = request.form.get("name", "").strip()
    duration_raw = request.form.get("duration_minutes", "").strip()
    price_raw = request.form.get("price", "").strip()

    if not name:
        flash("Service name is required.", "error")
        return redirect(url_for("dashboard.settings"))
    try:
        duration_minutes = int(duration_raw)
        if duration_minutes <= 0:
            raise ValueError
    except (ValueError, TypeError):
        flash("Duration must be a positive whole number of minutes.", "error")
        return redirect(url_for("dashboard.settings"))
    price = None
    if price_raw:
        try:
            price = float(price_raw)
            if price < 0:
                raise ValueError
        except (ValueError, TypeError):
            flash("Price must be a non-negative number.", "error")
            return redirect(url_for("dashboard.settings"))

    with SessionLocal() as db:
        db.add(ClinicService(
            clinic_id=clinic_id,
            service_name=name,
            duration_minutes=duration_minutes,
            price=price,
            is_active=True,
        ))
        db.commit()

    flash("Service added.", "success")
    return redirect(url_for("dashboard.settings"))


@dashboard_bp.route("/settings/services/<int:service_id>/update", methods=["POST"])
@login_required
def services_update(service_id):
    clinic_id = session["staff_clinic_id"]
    from app import ClinicService

    name = request.form.get("name", "").strip()
    duration_raw = request.form.get("duration_minutes", "").strip()
    price_raw = request.form.get("price", "").strip()

    if not name:
        flash("Service name is required.", "error")
        return redirect(url_for("dashboard.settings"))
    try:
        duration_minutes = int(duration_raw)
        if duration_minutes <= 0:
            raise ValueError
    except (ValueError, TypeError):
        flash("Duration must be a positive whole number of minutes.", "error")
        return redirect(url_for("dashboard.settings"))
    price = None
    if price_raw:
        try:
            price = float(price_raw)
            if price < 0:
                raise ValueError
        except (ValueError, TypeError):
            flash("Price must be a non-negative number.", "error")
            return redirect(url_for("dashboard.settings"))

    with SessionLocal() as db:
        svc = db.query(ClinicService).filter(
            ClinicService.id == service_id,
            ClinicService.clinic_id == clinic_id,
        ).first()
        if not svc:
            abort(404)
        svc.service_name = name
        svc.duration_minutes = duration_minutes
        svc.price = price
        db.commit()

    flash("Service updated.", "success")
    return redirect(url_for("dashboard.settings"))


@dashboard_bp.route("/settings/services/<int:service_id>/delete", methods=["POST"])
@login_required
def services_delete(service_id):
    clinic_id = session["staff_clinic_id"]
    from app import ClinicService

    with SessionLocal() as db:
        svc = db.query(ClinicService).filter(
            ClinicService.id == service_id,
            ClinicService.clinic_id == clinic_id,
        ).first()
        if not svc:
            abort(404)
        svc.is_active = False
        db.commit()

    flash("Service removed.", "success")
    return redirect(url_for("dashboard.settings"))
