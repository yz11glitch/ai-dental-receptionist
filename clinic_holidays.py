"""
clinic_holidays.py — Malaysian public holiday templates for clinic onboarding.

All dates are hardcoded and deterministic. No AI inference.

Yearly update process:
  1. Define _NATIONAL_<YEAR> and _STATE_EXTRAS_<YEAR> for the new year.
  2. Add an entry to HOLIDAYS[<year>].
  3. Update ONBOARDING_YEAR in seed_clinic.py.

Moon-sighting dates (Hari Raya, Awal Muharram, etc.) must be verified against
the official JAKIM announcement or the Malaysian government gazette before
each year's data is finalised.

Supported states: see SUPPORTED_STATES below.
To add a new state, add its extras to _STATE_EXTRAS_<YEAR> for each year
and add the state name to SUPPORTED_STATES.
"""

SUPPORTED_STATES = ["Kuala Lumpur", "Selangor"]

# ---------------------------------------------------------------------------
# 2026
# ---------------------------------------------------------------------------

# Selangor 2026 — verified from official state gazette.
_SELANGOR_2026 = {
    "2026-01-01": "New Year's Day",
    "2026-02-01": "Thaipusam",
    "2026-02-02": "Thaipusam Holiday",
    "2026-02-17": "Chinese New Year",
    "2026-02-18": "Chinese New Year Holiday",
    "2026-03-07": "Nuzul Al-Quran",
    "2026-03-20": "Hari Raya Aidilfitri Holiday",
    "2026-03-21": "Hari Raya Aidilfitri",
    "2026-03-22": "Hari Raya Aidilfitri Holiday",
    "2026-03-23": "Hari Raya Aidilfitri Holiday",
    "2026-05-01": "Labour Day",
    "2026-05-27": "Hari Raya Haji",
    "2026-05-31": "Wesak Day",
    "2026-06-01": "Yang di-Pertuan Agong's Birthday",
    "2026-06-02": "Wesak Day Holiday",
    "2026-06-17": "Awal Muharram",
    "2026-08-25": "Prophet Muhammad's Birthday",
    "2026-08-31": "Merdeka Day",
    "2026-09-16": "Malaysia Day",
    "2026-11-08": "Deepavali",
    "2026-11-09": "Deepavali Holiday",
    "2026-12-11": "Sultan of Selangor's Birthday",
    "2026-12-25": "Christmas Day",
}

# Kuala Lumpur 2026 — PLACEHOLDER. Do not use until verified against the
# official Federal Territory gazette for 2026.
# KL shares most national holidays with Selangor but differs on:
#   - Federal Territory Day (1 Feb)
#   - No Sultan's Birthday (federal territory, not a state)
#   - Thaipusam date may differ
# Fill this in once the official KL 2026 gazette is published.
_KUALA_LUMPUR_2026: dict = {}  # TODO: populate before onboarding KL clinics

HOLIDAYS: dict = {
    2026: {
        "Selangor": _SELANGOR_2026,
        "Kuala Lumpur": _KUALA_LUMPUR_2026,
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_holidays(year: int, state: str) -> dict:
    """
    Return {date_str: note} for the given year and state.
    Returns an empty dict if the year or state has no data yet.
    """
    return HOLIDAYS.get(year, {}).get(state, {})


def has_holidays(year: int, state: str) -> bool:
    """Return True if holiday data exists and is non-empty for this year/state."""
    return bool(get_holidays(year, state))
