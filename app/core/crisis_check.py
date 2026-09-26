import re

# Broad patterns that indicate possible crisis language.
# This list is a starting point for development/testing only —
# it must be reviewed and expanded by a clinical advisor before
# being used with real users.
CRISIS_PATTERNS = [
    r"\b(want to|going to|planning to|thinking about)\s+(kill myself|end my life|die)\b",
    r"\bsuicide\b",
    r"\bsuicidal\b",
    r"\bkill myself\b",
    r"\bend my life\b",
    r"\btake my (own )?life\b",
    r"\bself.?harm\b",
    r"\bcutting myself\b",
    r"\bno reason to (live|go on)\b",
    r"\bbetter off (dead|without me)\b",
    r"\bend it all\b",
    # Roman Urdu additions — starting set, expand as needed
    r"\bkhudkushi\b",
    r"\bmar jaun\b",
    r"\bzindagi khatam\b",
    r"\bjeena nahi chahta\b",
    r"\bjeena nahi chahti\b",
    r"\bjeene ka (koi )?(dil|mann|man|maza) nahi\b",
    r"\bjeene ki (koi )?(wajah|umeed|khwahish) nahi\b",
    r"\b(mar|marr) ?(jane|jaane) ko dil\b",
    r"\bmarne ka dil\b",
    r"\bab nahi jeena\b",
]

# Phrases that would otherwise false-positive against the patterns above.
# Checked first — if matched, the message is NOT treated as a crisis signal.
SAFE_PATTERNS = [
    r"\bdying to (see|watch|try|go|eat)\b",
    r"\bkilling it\b",
    r"\bdied laughing\b",
]

# Tiers (FR-CRIS-003). Draft split — the Clinical Advisor owns the real criteria.
# EMERGENCY: stated intent with timing, a plan, means, or an act already taken.
EMERGENCY_PATTERNS = [
    r"\b(going to|gonna|about to|decided to|will)\s+(kill myself|end my life|end it all|take my (own )?life)\b",
    r"\b(tonight|today|right now|abhi|aaj)\b.{0,40}\b(kill myself|end my life|end it all|khudkushi|mar jaun)\b",
    r"\b(kill myself|end my life|end it all|khudkushi|mar jaun)\b.{0,40}\b(tonight|today|right now|abhi|aaj)\b",
    r"\b(took|taken|swallowed|kha li(y[ae]n)?)\b.{0,30}\b(pills|tablets|goliyan|sleeping pills)\b",
    r"\boverdos(e|ed|ing)\b",
    r"\bi have a plan\b",
    r"\b(suicide|goodbye) (note|letter)\b",
]

# MILD: hopelessness without stated self-harm.
MILD_PATTERNS = [
    r"\bhopeless\b",
    r"\b(no|what'?s the|don'?t see the) point\b",
    r"\bcan'?t (go on|take (it|this) anymore)\b",
    r"\bnothing matters\b",
    r"\b(i'?m|i am) a burden\b",
    r"\bwant to disappear\b",
    r"\bkoi fayda nahi\b",
    r"\bumeed (nahi|khatam)\b",
    r"\bsab bekaar hai\b",
]

TIER_ORDER = ("clear", "mild", "danger", "emergency")

_CRISIS_REGEX = [re.compile(p, re.IGNORECASE) for p in CRISIS_PATTERNS]
_EMERGENCY_REGEX = [re.compile(p, re.IGNORECASE) for p in EMERGENCY_PATTERNS]
_MILD_REGEX = [re.compile(p, re.IGNORECASE) for p in MILD_PATTERNS]
_SAFE_REGEX = [re.compile(p, re.IGNORECASE) for p in SAFE_PATTERNS]


def higher_tier(a: str, b: str) -> str:
    return a if TIER_ORDER.index(a) >= TIER_ORDER.index(b) else b


def assess_lexical(text: str) -> str:
    """Stage one of detection (FR-CRIS-002): the local rule set. Returns a tier."""
    if not text:
        return "clear"
    for safe_pattern in _SAFE_REGEX:
        if safe_pattern.search(text):
            return "clear"
    if any(p.search(text) for p in _EMERGENCY_REGEX):
        return "emergency"
    if any(p.search(text) for p in _CRISIS_REGEX):
        return "danger"
    if any(p.search(text) for p in _MILD_REGEX):
        return "mild"
    return "clear"


def check_crisis(text: str) -> bool:
    """
    Deterministic, regex-based crisis check. Runs before any LLM call.

    Returns True if the message should be treated as a crisis signal —
    in which case the caller must short-circuit to crisis resources and
    skip the normal reflection flow entirely.

    This is intentionally biased toward false positives over false
    negatives: an unnecessary resource message costs nothing, a missed
    crisis signal could cost everything.
    """
    if not text:
        return False

    # Safe patterns are checked first so obvious non-crisis phrasing
    # (e.g. "dying to see this movie") doesn't get flagged.
    for safe_pattern in _SAFE_REGEX:
        if safe_pattern.search(text):
            return False

    for crisis_pattern in _CRISIS_REGEX:
        if crisis_pattern.search(text):
            return True

    return False