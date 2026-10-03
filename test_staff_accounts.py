"""
Staff account provisioning and admin access.

- seed_staff.py has no default credentials: details come from env vars or
  prompts, and the password must meet a minimum length.
- The dashboard admin allow-list (DASHBOARD_ADMIN_EMAILS) is empty unless
  configured, so a staff login is not an admin by default.
"""
import bcrypt
import pytest

from app import ClinicStaff, SessionLocal, app  # import app before dashboard (circular import)
import dashboard
import seed_staff
from seed_staff import MIN_PASSWORD_LENGTH, SeedStaffError, collect_staff_details

GOOD_PASSWORD = "x" * MIN_PASSWORD_LENGTH


def _no_prompt(*_args, **_kwargs):
    raise AssertionError("should not prompt")


# ---------------------------------------------------------------------------
# seed_staff.collect_staff_details
# ---------------------------------------------------------------------------

def test_seed_staff_has_no_default_credentials():
    assert not hasattr(seed_staff, "DEFAULT_PASSWORD")
    assert not hasattr(seed_staff, "DEFAULT_EMAIL")


def test_collect_staff_details_from_env():
    env = {
        "STAFF_EMAIL": " Owner@Example.com ",
        "STAFF_FULL_NAME": "Clinic Owner",
        "STAFF_CLINIC_ID": "3",
        "STAFF_PASSWORD": GOOD_PASSWORD,
    }
    details = collect_staff_details(env=env, input_fn=_no_prompt, getpass_fn=_no_prompt, interactive=False)
    assert details == ("owner@example.com", "Clinic Owner", 3, GOOD_PASSWORD)


def test_collect_staff_details_requires_password_when_not_interactive():
    env = {"STAFF_EMAIL": "owner@example.com", "STAFF_FULL_NAME": "Clinic Owner"}
    with pytest.raises(SeedStaffError, match="STAFF_PASSWORD"):
        collect_staff_details(env=env, input_fn=_no_prompt, getpass_fn=_no_prompt, interactive=False)


def test_collect_staff_details_rejects_short_password():
    env = {
        "STAFF_EMAIL": "owner@example.com",
        "STAFF_FULL_NAME": "Clinic Owner",
        "STAFF_PASSWORD": "x" * (MIN_PASSWORD_LENGTH - 1),
    }
    with pytest.raises(SeedStaffError, match="at least"):
        collect_staff_details(env=env, input_fn=_no_prompt, getpass_fn=_no_prompt, interactive=False)


def test_collect_staff_details_blank_prompt_password_is_rejected():
    # Pressing Enter at the password prompt must not fall back to any default.
    answers = iter(["owner@example.com", "Clinic Owner", ""])
    with pytest.raises(SeedStaffError, match="at least"):
        collect_staff_details(
            env={},
            input_fn=lambda _p: next(answers),
            getpass_fn=lambda _p: "",
            interactive=True,
        )


def test_collect_staff_details_prompt_password_must_match_confirmation():
    answers = iter(["owner@example.com", "Clinic Owner", ""])
    passwords = iter([GOOD_PASSWORD, GOOD_PASSWORD + "typo"])
    with pytest.raises(SeedStaffError, match="do not match"):
        collect_staff_details(
            env={},
            input_fn=lambda _p: next(answers),
            getpass_fn=lambda _p: next(passwords),
            interactive=True,
        )


# ---------------------------------------------------------------------------
# Dashboard admin allow-list
# ---------------------------------------------------------------------------

STAFF_EMAIL = "staff-admin-test@example.com"


@pytest.fixture
def staff_account():
    with SessionLocal() as db:
        db.query(ClinicStaff).filter(ClinicStaff.email == STAFF_EMAIL).delete()
        db.add(ClinicStaff(
            clinic_id=1,
            email=STAFF_EMAIL,
            password_hash=bcrypt.hashpw(GOOD_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode(),
            full_name="Test Staff",
            is_active=True,
        ))
        db.commit()
    yield STAFF_EMAIL
    with SessionLocal() as db:
        db.query(ClinicStaff).filter(ClinicStaff.email == STAFF_EMAIL).delete()
        db.commit()


def _login(client, email):
    return client.post("/dashboard/login", data={"email": email, "password": GOOD_PASSWORD})


def test_admin_allow_list_is_empty_by_default():
    # conftest does not set DASHBOARD_ADMIN_EMAILS, so this is the unconfigured default.
    assert dashboard._DASHBOARD_ADMIN_EMAILS == set()


def test_staff_login_is_not_admin_by_default(staff_account):
    with app.test_client() as client:
        response = _login(client, staff_account)
        assert response.status_code == 302
        with client.session_transaction() as sess:
            assert sess["staff_is_admin"] is False
        assert client.get("/dashboard/admin/billing").status_code == 403


def test_staff_login_in_allow_list_is_admin(staff_account, monkeypatch):
    monkeypatch.setattr(dashboard, "_DASHBOARD_ADMIN_EMAILS", {staff_account})
    with app.test_client() as client:
        response = _login(client, staff_account)
        assert response.status_code == 302
        with client.session_transaction() as sess:
            assert sess["staff_is_admin"] is True
        assert client.get("/dashboard/admin/billing").status_code == 200
