"""
utils.py — Pure utility functions for the AI WhatsApp Dental Receptionist.

No DB access. No external API calls. Safe to import anywhere without side effects.

now_local() is intentionally NOT defined here — it lives in app.py so that
tests can patch it via @patch("app.now_local"). Functions in this module that
need the current time use a deferred import: `from app import now_local`.
"""

import json
import os
import re
import logging
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

logger = logging.getLogger("ai_receptionist")

TIMEZONE = "Asia/Kuala_Lumpur"


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def normalize_date(date_str: str) -> str:
    return datetime.strptime(date_str.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")


def normalize_time(time_str: str) -> str:
    return datetime.strptime(time_str.strip(), "%H:%M").strftime("%H:%M")


# ---------------------------------------------------------------------------
# Service normalisation
# ---------------------------------------------------------------------------

def normalize_service(service: Optional[str]) -> str:
    aliases = {
        # English aliases
        "cleaning": "scaling",
        "teeth cleaning": "scaling",
        "dental cleaning": "scaling",
        "teeth clean": "scaling",
        "tooth clean": "scaling",
        "scale": "scaling",
        "scaleing": "scaling",
        "scalling": "scaling",
        "sclaing": "scaling",
        "sacling": "scaling",
        "consultation": "braces consultation",
        "braces": "braces consultation",
        "brace consultation": "braces consultation",
        "brace": "braces consultation",
        "braces consult": "braces consultation",
        "braces check": "braces consultation",
        "whitning": "whitening",
        "whitenning": "whitening",
        "whiteing": "whitening",
        "teeth whitening": "whitening",
        "tooth whitening": "whitening",
        "teeth bleaching": "whitening",
        "bleaching": "whitening",
        "polshing": "polishing",
        "plishing": "polishing",
        "tooth filling": "filling",
        "teeth filling": "filling",
        "tooth fillings": "filling",
        "fillings": "filling",
        "fill": "filling",
        # BM / Manglish aliases
        "tampal": "filling",
        "tampal gigi": "filling",
        "cuci gigi": "scaling",
        "pembersihan gigi": "scaling",
        "pembersihan": "scaling",
        "scaler": "scaling",
        "gigi putih": "whitening",
        "memutihkan gigi": "whitening",
        "pemutihan gigi": "whitening",
        "pemutihan": "whitening",
        "pendakap": "braces consultation",
        "pendakap gigi": "braces consultation",
        "kawat gigi": "braces consultation",
        # Mandarin aliases
        "洗牙": "scaling",
        "洁牙": "scaling",
        "潔牙": "scaling",
        "補牙": "filling",
        "补牙": "filling",
        "蛀牙补": "filling",
        "蛀牙補": "filling",
        "美白": "whitening",
        "牙齒美白": "whitening",
        "牙齿美白": "whitening",
        "拔牙": "extraction",
        "脫牙": "extraction",
        "脱牙": "extraction",
        "箍牙": "braces consultation",
        "牙套": "braces consultation",
        "矯正": "braces consultation",
        "矫正": "braces consultation",
        "牙齒矯正": "braces consultation",
        "牙齿矫正": "braces consultation",
        "根管": "root canal",
        "根管治療": "root canal",
        "根管治疗": "root canal",
        "神经治疗": "root canal",
        "神經治療": "root canal",
        "檢查": "checkup",
        "检查": "checkup",
        "牙科檢查": "checkup",
        "牙科检查": "checkup",
    }
    key = (service or "").lower().strip()
    # Mandarin keys are not lowercased (they are already language-specific characters),
    # so also try the original-case stripped value before falling through.
    stripped = (service or "").strip()
    return aliases.get(key) or aliases.get(stripped) or key


# ---------------------------------------------------------------------------
# Date / time resolution
# ---------------------------------------------------------------------------

def next_weekday_from(base_dt: datetime, target_weekday: int) -> datetime:
    days_ahead = (target_weekday - base_dt.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return base_dt + timedelta(days=days_ahead)


def resolve_relative_date(date_text: str) -> Optional[str]:
    from app import now_local  # deferred: keeps app.now_local patchable in tests
    text = (date_text or "").strip().lower()
    now = now_local()

    weekdays = {
        "monday": 0, "mon": 0,
        "tuesday": 1, "tue": 1, "tues": 1,
        "wednesday": 2, "wed": 2,
        "thursday": 3, "thu": 3, "thurs": 3,
        "friday": 4, "fri": 4,
        "saturday": 5, "sat": 5,
        "sunday": 6, "sun": 6,
    }

    if text in {"today"}:
        return now.strftime("%Y-%m-%d")

    if text in {"tomorrow", "tmr", "tmrw"}:
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")

    if text in weekdays:
        target = next_weekday_from(now, weekdays[text])
        return target.strftime("%Y-%m-%d")

    # Handle "next week {weekday}" — Tuesday of the next calendar week
    # Handle "next {weekday}"      — next occurrence of that weekday
    for prefix, is_next_week in (("next week ", True), ("next ", False)):
        if text.startswith(prefix):
            remainder = text[len(prefix):]
            if remainder in weekdays:
                if is_next_week:
                    days_to_monday = (7 - now.weekday()) % 7 or 7
                    base = now + timedelta(days=days_to_monday)
                    return (base + timedelta(days=weekdays[remainder])).strftime("%Y-%m-%d")
                else:
                    return next_weekday_from(now, weekdays[remainder]).strftime("%Y-%m-%d")

    try:
        return normalize_date(text)
    except Exception:
        return None


_VAGUE_TIME_PHRASES = {
    # Time-of-day (English)
    "morning", "late morning", "mid morning", "mid-morning", "early morning",
    "afternoon", "early afternoon", "late afternoon", "midafternoon",
    "evening", "evening time", "late evening",
    "night", "tonight", "at night",
    "midday", "mid day", "mid-day", "noon time", "noontime", "around noon",
    "after lunch", "after dinner", "after breakfast",
    "lunchtime", "lunch time", "lunch",
    # Work/schedule anchors (English)
    "after work", "after office", "after office hours",
    "before work", "before lunch",
    "during lunch", "on my lunch break",
    # Vague / open-ended (English)
    "later", "later today", "sometime today", "some time today",
    "anytime", "any time", "whenever", "flexible", "free time",
    "soon", "asap", "as soon as possible",
    "not too early", "not too late",
    # BM / Manglish
    "pagi", "pagi-pagi", "awal pagi",
    "tengah hari", "waktu tengah hari",
    "petang", "petang nanti", "lewat petang",
    "malam", "malam nanti",
    "lepas kerja", "selepas kerja",
    "lepas lunch", "selepas lunch",
    "lepas makan", "selepas makan",
    "bila-bila masa", "masa lapang",
}


def resolve_time_text(time_text: str) -> Optional[str]:
    text = (time_text or "").strip().lower()

    # Vague time phrases cannot be resolved to a specific time.
    # Return None so the LLM is forced to ask for clarification.
    if text in _VAGUE_TIME_PHRASES:
        return None

    aliases = {
        "noon": "12:00",
    }
    if text in aliases:
        return aliases[text]

    # Normalise dot-notation times: "9.00am" → "9:00am", "9.30" → "9:30"
    text = re.sub(r'(\d+)\.(\d+)', r'\1:\2', text)
    text = text.replace(".", "").replace(" ", "")

    patterns = [
        (r"^(\d{1,2})am$", False),
        (r"^(\d{1,2})pm$", True),
        (r"^(\d{1,2}):(\d{2})am$", False),
        (r"^(\d{1,2}):(\d{2})pm$", True),
        (r"^(\d{1,2}):(\d{2})$", None),
    ]

    for pattern, is_pm in patterns:
        m = re.match(pattern, text)
        if not m:
            continue

        if len(m.groups()) == 1:
            hour = int(m.group(1))
            minute = 0
        else:
            hour = int(m.group(1))
            minute = int(m.group(2))

        if is_pm is True and hour < 12:
            hour += 12
        if is_pm is False and hour == 12:
            hour = 0

        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"

    try:
        return normalize_time(time_text)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Business hours
# ---------------------------------------------------------------------------

def is_within_business_hours(clinic: dict, start_dt: datetime, end_dt: datetime) -> bool:
    """
    Validate that both start and end times are within clinic business hours.

    BUG FIX #14: Now validates end_dt to prevent appointments that run past closing time.
    Example: 90-min appointment at 8pm when clinic closes at 9pm would end at 9:30pm.
    """
    if start_dt.weekday() == 6:
        return False

    open_dt = start_dt.replace(hour=clinic["open_hour"], minute=0, second=0, microsecond=0)
    close_dt = start_dt.replace(hour=clinic["close_hour"], minute=0, second=0, microsecond=0)

    # Both start AND end must be within business hours
    if not (start_dt >= open_dt and end_dt <= close_dt):
        return False

    lunch_start = clinic.get("lunch_start")
    lunch_end = clinic.get("lunch_end")
    if lunch_start and lunch_end:
        try:
            lunch_start_h, lunch_start_m = [int(x) for x in lunch_start.split(":", 1)]
            lunch_end_h, lunch_end_m = [int(x) for x in lunch_end.split(":", 1)]
            lunch_start_dt = start_dt.replace(
                hour=lunch_start_h, minute=lunch_start_m, second=0, microsecond=0
            )
            lunch_end_dt = start_dt.replace(
                hour=lunch_end_h, minute=lunch_end_m, second=0, microsecond=0
            )
            if lunch_start_dt < lunch_end_dt:
                overlaps_lunch = start_dt < lunch_end_dt and end_dt > lunch_start_dt
                if overlaps_lunch:
                    return False
        except Exception:
            # Invalid lunch data should never block valid booking flow.
            pass

    return True


# ---------------------------------------------------------------------------
# Human escalation detection
# ---------------------------------------------------------------------------

# English: single-word escalation targets
_ESCALATION_EN_KEYWORDS = [
    "human", "agent", "staff", "receptionist", "operator",
    "real person", "person", "someone", "doctor",
]

# English: intent phrases that must appear as substrings in the lowercased message
_ESCALATION_EN_PHRASES = [
    "talk to", "speak to", "speak with",
    "connect me", "call me",
    "get me a", "get me",
    "i want a", "i want",
    "i need a", "i need",
    "can i talk", "can i speak",
    "let me talk", "let me speak",
    "i wanna talk", "i wanna speak",
    "give me agent", "give me human", "give me staff",
    "want to talk", "want to speak",
    "need to talk", "need to speak",
    "talk to someone", "speak to someone", "talk to doctor",
]

# Mandarin: no word boundaries, check as-is (no lowercasing needed for CJK)
_ESCALATION_ZH = [
    "人工", "客服", "真人", "工作人员", "接线员",
    "人工服务", "转人工", "让我跟真人说话", "我要找人", "帮我转接",
]

# Bahasa Malaysia: single keywords and intent phrases
_ESCALATION_BM = [
    "manusia", "pekerja", "kakitangan", "ejen", "resepsionis", "orang sebenar",
    "bercakap dengan", "hubungi", "tolong hubungkan", "saya nak cakap dengan",
]


def is_human_escalation_request(message: str) -> bool:
    """Return True if the message contains any human-escalation intent.

    Uses substring matching so partial phrases like "can i talk to agent"
    or "i wanna talk to human" are caught reliably.

    Strategy:
    - English: match if the lowercased message contains BOTH an intent phrase
      AND an escalation keyword, OR contains a combined intent+keyword phrase.
    - Mandarin (CJK): direct substring match without lowercasing.
    - BM: substring match on lowercased message.
    """
    if not message:
        return False

    t = message.lower()

    # Mandarin: direct substring check (no lowercasing for CJK)
    if any(trigger in message for trigger in _ESCALATION_ZH):
        return True

    # BM: substring check on lowercased text
    if any(trigger in t for trigger in _ESCALATION_BM):
        return True

    # English: message must contain an intent phrase AND an escalation keyword
    has_intent = any(phrase in t for phrase in _ESCALATION_EN_PHRASES)
    has_target = any(kw in t for kw in _ESCALATION_EN_KEYWORDS)
    if has_intent and has_target:
        return True

    # English: also catch bare single-word escalation targets (entire normalized message),
    # including Malaysian polite suffixes like "pls", "please", "lah".
    t_stripped = t.strip()
    _ESCALATION_BARE = {
        "human", "agent", "staff", "receptionist", "operator",
        "call me", "real person",
        "agent pls", "agent please", "agent lah",
        "human pls", "human please", "human lah",
        "staff pls", "staff please", "staff lah",
        "receptionist pls", "receptionist please",
    }
    if t_stripped in _ESCALATION_BARE:
        return True

    return False


def is_human_request(text: str) -> bool:
    """Return True when the patient is explicitly asking to speak with a human."""
    t = normalize_text(text)
    exact = {"human", "agent", "staff", "receptionist"}
    phrases = {
        # Standard English
        "talk to human", "speak to human",
        "talk to staff", "speak to staff",
        "talk to agent", "speak to agent",
        "talk to receptionist", "speak to receptionist",
        # Informal English
        "connect me to someone", "connect me to staff",
        "connect me to reception", "connect me to receptionist",
        "i want to speak with someone", "i want to talk to someone",
        "can i speak with someone", "can i talk to someone",
        "i prefer to call", "let me call", "call the clinic",
        # Manglish / BM
        "nak cakap dengan staff", "nak cakap dengan orang",
        "nak cakap dengan receptionist",
        "boleh cakap dengan staff", "boleh cakap dengan orang",
        "boleh hubungi staff",
        "tolong sambungkan dengan staff",
    }
    return t in exact or t in phrases


# ---------------------------------------------------------------------------
# Reset command detection
# ---------------------------------------------------------------------------

def is_reset_command(text: str) -> bool:
    """Return True when the message is a standalone reset/restart intent.

    'cancel' only matches as the entire message to avoid intercepting
    legitimate cancellation requests like 'cancel my appointment'.
    """
    t = normalize_text(text)
    return t in {"reset", "/reset", "restart", "start over", "cancel"}


# ---------------------------------------------------------------------------
# Slot formatting
# ---------------------------------------------------------------------------

def format_slot(dt: datetime) -> str:
    return dt.strftime("%A, %d %B %Y at %I:%M %p").replace(" 0", " ")


def format_time_only(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").replace(" 0", " ")


def parse_slot(date_str: str, time_str: str) -> datetime:
    dt = datetime.strptime(
        f"{normalize_date(date_str)} {normalize_time(time_str)}",
        "%Y-%m-%d %H:%M"
    )
    return dt.replace(tzinfo=ZoneInfo(TIMEZONE))


# ---------------------------------------------------------------------------
# Conflicting date phrase detection
# ---------------------------------------------------------------------------

def detect_conflicting_date_phrases(text: str) -> Optional[str]:
    """
    BUG FIX #9: Detect when user provides multiple conflicting date phrases.

    Examples:
    - "tomorrow Friday" when tomorrow is Tuesday
    - "today Monday" when today is Wednesday
    - "next week Thursday" when that Thursday is this week

    Returns a message asking for clarification if conflict detected, None otherwise.
    """
    text_lower = text.lower()

    # Define date keyword groups
    relative_dates = ["today", "tomorrow", "tmr", "tmrw"]
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                "mon", "tue", "wed", "thu", "fri", "sat", "sun"]

    # Check if message contains both a relative date AND a weekday
    has_relative = any(word in text_lower for word in relative_dates)
    has_weekday = any(word in text_lower for word in weekdays)

    if has_relative and has_weekday:
        return (
            "I noticed you mentioned both a date (like 'today' or 'tomorrow') and a specific "
            "day of the week. Could you please clarify which day you meant?"
        )

    return None


# ---------------------------------------------------------------------------
# Multi-patient detection helpers
# ---------------------------------------------------------------------------

# Known dental service keywords — used to exclude "scaling and polishing" style
# phrases from the multi-patient name detector below.
_DENTAL_SERVICE_KEYWORDS = {
    "scaling", "polishing", "cleaning", "filling", "extraction", "whitening",
    "braces", "retainer", "root", "canal", "crown", "veneer", "implant",
    "xray", "x-ray", "consultation", "checkup", "check-up", "fluoride",
    "sealant", "denture", "bridge", "bleaching", "treatment", "procedure",
}


def _is_multi_patient_same_time_question(text: str) -> bool:
    """Return True when the user is asking whether multiple patients can share the same slot.

    Examples: "they can both be the same time?", "can both be at the same time",
    "can they book same time", "same time for both".
    """
    t = normalize_text(text)
    same_time_phrases = [
        "same time",
        "both be",
        "both at",
        "same slot",
        "same appointment",
    ]
    # A multi-patient same-time question needs at least one "same time" phrase
    # AND a plurality indicator ("both", "they", "we") or standalone "same time".
    plurality_words = {"both", "they", "we", "all", "together"}
    has_same_time = any(p in t for p in same_time_phrases)
    has_plurality = any(w in t.split() for w in plurality_words)
    return has_same_time and has_plurality


def _detect_multi_patient_booking_request(text: str):
    """Return (name1, name2) when the message looks like a booking request for two
    distinct patients joined by 'and'.  Returns (None, None) otherwise.

    Heuristic:
    - Must contain a booking-intent keyword.
    - Must contain ' and ' splitting the message into two short (<=4 word) segments
      where neither segment is a known dental service keyword.
    - If unsure, returns (None, None) so the message falls through to the LLM.
    """
    t = normalize_text(text)

    booking_intent_keywords = {
        "book", "appointment", "schedule", "slot", "reserve",
        "scaling", "cleaning", "polishing", "filling", "extraction",
        "whitening", "braces", "checkup", "check-up", "consultation",
        "root canal", "crown", "implant", "xray", "x-ray", "veneer",
        "fluoride", "sealant", "denture", "bridge", "bleaching",
    }

    has_booking_intent = any(kw in t for kw in booking_intent_keywords)
    if not has_booking_intent:
        return None, None

    if " and " not in t:
        return None, None

    # Split only on the first " and " to keep things simple.
    # We look for patterns like "book for Ali and Siti" or "scaling for John and Mary".
    # Extract the portion after common booking prepositions.
    # Try to find the names by looking at what follows "for" or starts at "book".
    name_segment = t
    for prefix in ("book for ", "appointment for ", "schedule for ", "slot for ", "booking for "):
        if prefix in name_segment:
            name_segment = name_segment.split(prefix, 1)[1]
            break

    if " and " not in name_segment:
        return None, None

    parts = name_segment.split(" and ", 1)
    left = parts[0].strip()
    right = parts[1].strip()

    # Strip trailing punctuation / short suffixes from the right side
    right = right.split()[0] if right.split() else right  # keep only first word of right side as name

    # Reject if either side is empty, too long (>4 words = likely a sentence, not a name),
    # or matches a dental service keyword.
    left_words = left.split()
    right_words = right.split()

    if not left_words or not right_words:
        return None, None
    if len(left_words) > 4 or len(right_words) > 4:
        return None, None
    if left_words[-1] in _DENTAL_SERVICE_KEYWORDS or right_words[0] in _DENTAL_SERVICE_KEYWORDS:
        return None, None

    # Capitalise for display.
    name1 = " ".join(w.capitalize() for w in left_words)
    name2 = " ".join(w.capitalize() for w in right_words)
    return name1, name2


# ---------------------------------------------------------------------------
# Reminder window helpers
# ---------------------------------------------------------------------------

def should_send_1d_reminder(start_dt: datetime, now_dt: datetime) -> bool:
    diff = start_dt - now_dt
    return timedelta(hours=20) <= diff <= timedelta(hours=28)


def should_send_2h_reminder(start_dt: datetime, now_dt: datetime) -> bool:
    diff = start_dt - now_dt
    return timedelta(minutes=90) <= diff <= timedelta(minutes=150)


# ---------------------------------------------------------------------------
# Telegram alert helper
# ---------------------------------------------------------------------------

def send_telegram_alert(message: str) -> None:
    """Send a Telegram message to the configured chat. Silently no-ops if not configured.

    Uses TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.
    Never raises — all failures are logged as warnings.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("send_telegram_alert: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set; skipping alert")
        return
    try:
        payload = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        logger.warning("send_telegram_alert: Telegram send failed — alert lost", exc_info=True)
