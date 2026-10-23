"""
Capture schedules (FR-ENT-001): which values a journal type records, in what
order, and with which control. Wording is never stored here — the model words
each prompt, and capture_fallbacks.json is used only when it can't (FR-ENT-023).
"""
import json
from pathlib import Path
from typing import Any

_CONTENT_DIR = Path(__file__).resolve().parent.parent / "content"


def _read(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_SCHEDULE_FILES = {p.stem: _read(p) for p in (_CONTENT_DIR / "capture_schedules").glob("*.json")}
SCHEDULES: dict[str, list[dict]] = {name: f["values"] for name, f in _SCHEDULE_FILES.items()}
# The check-in's three daily slots (core/slots.py), each with its own questions.
# They are check-ins: same journal type, same fallbacks, same ending.
_SLOT_FILES = {p.stem: _read(p) for p in (_CONTENT_DIR / "capture_schedules" / "check_in_slots").glob("*.json")}
SLOT_SCHEDULES: dict[str, list[dict]] = {slot: f["values"] for slot, f in _SLOT_FILES.items()}


def _all_schedules() -> list[tuple[str, list[dict]]]:
    """(journal type, values) for every schedule, slot schedules included."""
    return list(SCHEDULES.items()) + [("check_in", values) for values in SLOT_SCHEDULES.values()]
# What happens after the last value: "offer" the AI conversation (default), "close"
# it with one closing message (savouring, FR-JRN-002), or "grounding" — a fixed
# grounding message and no conversation (thought trauma variant, FR-JRN-005/008).
ENDINGS: dict[str, str] = {name: f.get("ending", "offer") for name, f in _SCHEDULE_FILES.items()}
# An alternative schedule for users with particular focus areas (FR-JRN-005).
VARIANTS: dict[str, dict] = {name: f["variant"] for name, f in _SCHEDULE_FILES.items() if "variant" in f}
# Journals that end with a grounding message for users with the past-event focus
# area, whatever their ending (FR-JRN-008).
GROUNDING_JOURNALS = {"free_write", "thought"}
# Journal types that are a conversation with no capture schedule (the AI Chat tab).
CONVERSATION_ONLY = {"chat"}
GROUNDING_FOCUS = "trauma_ptsd"
GROUNDING: dict = _read(_CONTENT_DIR / "grounding.json")
# Skills that always apply to a journal type, whatever the detected topic
# (e.g. savouring is always about a good moment → skills/positive.md).
JOURNAL_SKILLS: dict[str, list[str]] = {name: f.get("skills", []) for name, f in _SCHEDULE_FILES.items()}
LIBRARIES: dict[str, dict] = {
    p.stem: _read(p) for p in (_CONTENT_DIR / "libraries").glob("*.json")
}
SCALES: dict[str, list[dict]] = _read(_CONTENT_DIR / "scales.json")
FALLBACKS: dict = _read(_CONTENT_DIR / "capture_fallbacks.json")
# How long a conversation may run, per journal type (FR-AIR-009, FR-AIR-013).
CONVERSATION: dict = _read(_CONTENT_DIR / "conversation.json")

# FR-JRN-007: a fixed notice shown before the first value, for users with the
# past-event focus area. The schedule names which notice; the text is content.
NOTICES: dict[str, str | None] = {name: f.get("notice") for name, f in _SCHEDULE_FILES.items()}
NOTICE_FOCUS = "trauma_ptsd"
NOTICE_TEXTS: dict = _read(_CONTENT_DIR / "exposure_notice.json")

def _library_items(name: str) -> list[dict]:
    lib = LIBRARIES[name]
    if "categories" in lib:
        return [item for cat in lib["categories"] for item in cat["items"]]
    return lib["items"]


# id → display name, per library
LIBRARY_NAMES: dict[str, dict[str, str]] = {
    name: {item["id"]: item["name"] for item in _library_items(name)} for name in LIBRARIES
}


# A schedule says what to collect, never what to say: wording comes from the model
# or capture_fallbacks.json. These are the only keys a value may have.
VALUE_TYPES = {"scale", "single_select", "multi_select", "text", "rating_set"}
SPEC_KEYS = {"id", "value_type", "control", "required", "library", "scale", "max_length", "depends_on",
             "min_mood", "max_mood", "prefer_valence", "valence_follows_mood", "repeatable",
             "pause_after", "voice", "rates",
             # Asked only when depends_on was answered with exactly this value (a Yes).
             "when",
             # Worded exactly as written in capture_fallbacks.json, never by the
             # model: a published questionnaire item must not be paraphrased.
             "fixed",
             # Part of a weekly questionnaire (core/assessments.py); left out of
             # the schedule unless that questionnaire is due.
             "assessment"}
# A rating_set rates each item of an earlier multi_select on its own, 0-100:
# one number for several feelings at once never says which one moved.
RATING_MIN, RATING_MAX = 0, 100
SCHEDULE_KEYS = {"journal_type", "ending", "skills", "notice", "variant", "values", "_comment"}


def _check_no_wording(name: str, schedule: dict) -> None:
    if set(schedule) - SCHEDULE_KEYS:
        raise RuntimeError(f"{name}: unknown schedule keys {sorted(set(schedule) - SCHEDULE_KEYS)}")
    for spec in schedule["values"]:
        if set(spec) - SPEC_KEYS:
            raise RuntimeError(f"{name}.{spec['id']}: keys a schedule may not hold {sorted(set(spec) - SPEC_KEYS)}")
        if spec["value_type"] not in VALUE_TYPES:
            raise RuntimeError(f"{name}.{spec['id']}: value_type must be one of {sorted(VALUE_TYPES)}")


def _check_content() -> None:
    for name, schedule in _SCHEDULE_FILES.items():
        _check_no_wording(name, schedule)
    for slot, schedule in _SLOT_FILES.items():
        _check_no_wording(f"check_in:{slot}", schedule)
        if schedule.get("journal_type") != "check_in" or "variant" in schedule or "ending" in schedule:
            raise RuntimeError(f"check_in:{slot}: a slot schedule is a plain check-in schedule")
    for journal_type, values in _all_schedules():
        ids = [v["id"] for v in values]
        if len(ids) != len(set(ids)):
            raise RuntimeError(f"{journal_type}: a value id appears twice")
        for spec in values:
            if "when" in spec and "depends_on" not in spec:
                raise RuntimeError(f"{journal_type}.{spec['id']}: `when` needs a depends_on")
            if spec.get("assessment") and not spec.get("fixed"):
                raise RuntimeError(f"{journal_type}.{spec['id']}: a questionnaire item must be fixed wording")
            if spec["control"] == "rating_set" and "scale" in spec and spec["scale"] not in SCALES:
                raise RuntimeError(f"{journal_type}.{spec['id']}: unknown scale {spec['scale']}")
            # A mood condition can only be judged once mood is recorded.
            if ("min_mood" in spec or "max_mood" in spec) and "mood" not in [v["id"] for v in values[:values.index(spec)]]:
                raise RuntimeError(f"{journal_type}.{spec['id']}: mood condition but no earlier mood value")
            if spec["control"] == "multi_select" and spec["library"] not in LIBRARIES:
                raise RuntimeError(f"{journal_type}.{spec['id']}: unknown library {spec['library']}")
            if spec["control"] == "scale" and spec["scale"] not in SCALES:
                raise RuntimeError(f"{journal_type}.{spec['id']}: unknown scale {spec['scale']}")
            if spec["control"] == "rating_set":
                rated = next((v for v in values[:values.index(spec)] if v["id"] == spec.get("rates")), None)
                if rated is None or rated["control"] != "multi_select":
                    raise RuntimeError(
                        f"{journal_type}.{spec['id']}: rates must name an earlier multi_select"
                    )
                if spec.get("depends_on") != spec["rates"]:
                    raise RuntimeError(
                        f"{journal_type}.{spec['id']}: a rating_set must depend_on what it rates"
                    )
            if spec["id"] not in FALLBACKS.get(journal_type, {}):
                raise RuntimeError(f"{journal_type}.{spec['id']}: no fallback wording")
            if spec.get("pause_after") and spec is values[-1]:
                raise RuntimeError(f"{journal_type}.{spec['id']}: pauses after the last value")
            # A dependency must come earlier, or it could never be answered in time.
            depends_on = spec.get("depends_on")
            if depends_on is not None:
                earlier = [v["id"] for v in values[:values.index(spec)]]
                if depends_on not in earlier:
                    raise RuntimeError(
                        f"{journal_type}.{spec['id']}: depends_on {depends_on} does not come before it"
                    )
        if ENDINGS[journal_type] == "close" and f"{journal_type}_close" not in FALLBACKS:
            raise RuntimeError(f"{journal_type}: no fallback closing message")
        variant = VARIANTS.get(journal_type, {})
        unknown = (set(variant.get("omit", [])) | set(variant.get("optional", []))) - {v["id"] for v in values}
        if unknown:
            raise RuntimeError(f"{journal_type} variant names values that do not exist: {sorted(unknown)}")

    for name in CONVERSATION:
        if name.startswith("_") or name == "default":
            continue
        if name not in SCHEDULES and name not in CONVERSATION_ONLY:
            raise RuntimeError(f"conversation.json: {name} is not a journal type")
        if set(CONVERSATION[name]) - set(CONVERSATION["default"]) - {"_comment"}:
            raise RuntimeError(f"conversation.json: {name} sets a limit that does not exist")


_check_content()


# ---------- daily check-in (the app runs the steps; one submit at the end) ----------

CHECKIN: dict = _read(_CONTENT_DIR / "checkin_factors.json")
CHECKIN_ITEMS: dict[str, dict] = {item["id"]: item for item in CHECKIN["items"]}
MOODS = range(1, 6)


def _check_checkin() -> None:
    if len(CHECKIN_ITEMS) != len(CHECKIN["items"]):
        raise RuntimeError("checkin_factors.json: duplicate chip ids")
    categories = {c["id"] for c in CHECKIN["categories"]}
    for item in CHECKIN["items"]:
        if item["category"] not in categories or not set(item["moods"]) <= set(MOODS):
            raise RuntimeError(f"checkin_factors.json: {item['id']} has an unknown category or mood")
    # No mood may ever show an empty category.
    for category in categories:
        for mood in MOODS:
            if not any(i["category"] == category and mood in i["moods"] for i in CHECKIN["items"]):
                raise RuntimeError(f"checkin_factors.json: no {category} chip for mood {mood}")


_check_checkin()
# The chips are stored as the check-in's `triggers` value, so Insights names them
# from that library; old-only ids keep their old names.
LIBRARY_NAMES["triggers"] = {**LIBRARY_NAMES["triggers"], **{i: item["label"] for i, item in CHECKIN_ITEMS.items()}}


def mood_band(mood: int) -> str:
    return "high" if mood >= 4 else "mid" if mood == 3 else "low"


def checkin_factors(mood: int, user_terms: list[dict]) -> dict:
    """
    The chips for this mood, grouped and ordered, plus the app-owned wording of the
    step. Her own words come first, at every mood, because only she knows what they mean.
    """
    categories = [
        {
            **{k: c[k] for k in ("id", "label", "label_ur", "label_roman")},
            "items": [
                {k: i[k] for k in ("id", "label", "label_ur", "label_roman")}
                for i in sorted(CHECKIN["items"], key=lambda i: i["order"])
                if i["category"] == c["id"] and mood in i["moods"]
            ],
        }
        for c in sorted(CHECKIN["categories"], key=lambda c: c["order"])
    ]
    if user_terms:
        mine = [{"id": t["id"], "label": t["name"], "label_ur": t["name"], "label_roman": t["name"]} for t in user_terms]
        categories = [{"id": "my_words", "label": "My words", "label_ur": "میرے الفاظ",
                       "label_roman": "Mere alfaaz", "items": mine}] + categories
    return {"prompt": CHECKIN["prompts"][mood_band(mood)], "note_prompt": CHECKIN["prompts"]["note"],
            "categories": categories}


def checkin_fallback_closing(mood: int, avoid: list[str]) -> str:
    """Stored closing for when the model can't write one: never the line she saw last."""
    lines = CHECKIN["closings"][mood_band(mood)]
    return next((line for line in lines if line not in avoid), lines[0])


# FR-PICK-006: libraries a user may add her own terms to.
EXTENDABLE_LIBRARIES = {"feelings", "triggers"}
USER_TERM_MAX_LENGTH = 20

# extra: library → {item id → name} for the user's own terms.
Extra = dict[str, dict[str, str]] | None


class InvalidAnswer(ValueError):
    pass


def _names(library: str, extra: Extra) -> dict[str, str]:
    return {**LIBRARY_NAMES[library], **((extra or {}).get(library, {}))}


def library_for_user(name: str, focus_codes: list[str], user_terms: list[dict], prefer_valence: str | None = None) -> dict:
    """
    The library as one user sees it: her own terms first as "My words", then
    categories matching prefer_valence (e.g. positive feelings in a savouring
    entry), then categories linked to her focus areas, then the rest
    (FR-PICK-005, FR-PICK-006). Every item stays reachable; only the order changes.
    """
    lib = LIBRARIES[name]
    if "categories" not in lib:
        return lib
    focus = set(focus_codes)
    categories = sorted(
        lib["categories"],
        key=lambda c: (
            0 if prefer_valence and c.get("valence") == prefer_valence else 1,
            0 if focus & set(c.get("focus_areas", [])) else 1,
        ),  # stable sort
    )
    if user_terms:
        categories = [{"id": "my_words", "name": "My words", "items": user_terms}] + categories
    return {**lib, "categories": categories, "extendable": name in EXTENDABLE_LIBRARIES}


def conversation_limits(journal_type: str) -> dict:
    """
    When a conversation must close, for this journal (FR-AIR-009, FR-AIR-013).
    A journal with nothing of its own gets the default; free write runs longer
    because being heard, not resolving something, is the point of it.
    """
    limits = {**CONVERSATION["default"], **CONVERSATION.get(journal_type, {})}
    return {k: limits[k] for k in ("repeat_limit", "minimal_replies", "containment_turns")}


def has_schedule(journal_type: str) -> bool:
    return journal_type in SCHEDULES


def all_value_ids() -> tuple[str, ...]:
    return tuple(sorted({v["id"] for _, values in _all_schedules() for v in values}))


def _variant_applies(journal_type: str, focus_codes: list[str]) -> bool:
    variant = VARIANTS.get(journal_type)
    return bool(variant and set(focus_codes) & set(variant["when_focus"]))


def values_for(
    journal_type: str,
    focus_codes: list[str] | None = None,
    slot: str | None = None,
    assessments: frozenset[str] | set[str] = frozenset(),
) -> list[dict]:
    """
    The schedule this user gets. The trauma variant drops some values and makes
    others optional (FR-JRN-005); everyone else gets the base schedule. A
    check-in with a slot gets that slot's questions, with each weekly
    questionnaire included only when it is among `assessments` (due).
    """
    if journal_type == "check_in" and slot in SLOT_SCHEDULES:
        return [v for v in SLOT_SCHEDULES[slot] if not v.get("assessment") or v["assessment"] in assessments]
    values = SCHEDULES[journal_type]
    if not _variant_applies(journal_type, focus_codes or []):
        return values
    variant = VARIANTS[journal_type]
    omit, optional = set(variant.get("omit", [])), set(variant.get("optional", []))
    return [
        {**spec, "required": False} if spec["id"] in optional else spec
        for spec in values
        if spec["id"] not in omit
    ]


def ending_for(journal_type: str, focus_codes: list[str] | None = None) -> str:
    if _variant_applies(journal_type, focus_codes or []):
        return VARIANTS[journal_type].get("ending", ENDINGS[journal_type])
    return ENDINGS[journal_type]


def needs_grounding(journal_type: str, focus_codes: list[str] | None) -> bool:
    """FR-JRN-008: the thread's final message for past-event focus users."""
    return journal_type in GROUNDING_JOURNALS and GROUNDING_FOCUS in (focus_codes or [])

def notice_for(journal_type: str, focus_codes: list[str] | None) -> str | None:
    """The scope notice this user must see first, if any (FR-JRN-007)."""
    notice = NOTICES.get(journal_type)
    if not notice or NOTICE_FOCUS not in (focus_codes or []):
        return None
    return NOTICE_TEXTS[notice]


def pauses_after(spec: dict) -> bool:
    """
    FR-JRN-006: the plan is made now, and what happened comes hours or days
    later. The entry stops after this value and waits for her, rather than
    asking how something went that has not happened yet.
    """
    return bool(spec.get("pause_after"))


def pause_message() -> str:
    return NOTICE_TEXTS["pause"]


def carried_values(journal_type: str) -> list[str]:
    """Values a further cycle reuses from the cycle it continues (FR-JRN-006)."""
    return ["feared_outcome"] if journal_type == "exposure" else []


def grounding_message() -> str:
    return GROUNDING["message"]


def _asked(spec: dict, recorded: dict[str, Any]) -> bool:
    """
    Whether this value is worth asking. A value with depends_on follows up on
    another one, so it is only asked when that one was actually answered —
    "how strong was it?" makes no sense after she skipped the feelings. A
    min_mood / max_mood condition asks it only within that mood range.
    """
    depends_on = spec.get("depends_on")
    if depends_on is not None:
        answer = recorded.get(depends_on)
        if answer is None or answer == [] or answer == "":
            return False
        # A follow-up to a Yes is not asked after a No.
        if "when" in spec and answer != spec["when"]:
            return False
    mood = recorded.get("mood")
    if mood is not None and not (spec.get("min_mood", mood) <= mood <= spec.get("max_mood", mood)):
        return False
    return True


def next_value(values: list[dict], recorded: dict[str, Any]) -> dict | None:
    """The first value in the schedule without a response or skip (FR-ENT-024)."""
    for spec in values:
        if spec["id"] not in recorded and _asked(spec, recorded):
            return spec
    return None


def not_asked(values: list[dict], recorded: dict[str, Any]) -> list[str]:
    """
    Values the schedule's conditions passed over before the next one to ask (or
    before the end). They are stored as not asked, never as skipped.
    """
    passed = []
    for spec in values:
        if spec["id"] in recorded:
            continue
        if _asked(spec, recorded):
            break
        passed.append(spec["id"])
    return passed


def scale_bounds(value_id: str) -> tuple[int, int] | None:
    """
    The lowest and highest a value can be recorded as, read from the schedule
    that records it — so a chart never hardcodes the ends of a scale.
    """
    for _, values in _all_schedules():
        for spec in values:
            if spec["id"] == value_id and spec["control"] == "scale":
                points = [option["value"] for option in SCALES[spec["scale"]]]
                return min(points), max(points)
    return None


def fallback_wording(journal_type: str, value_id: str) -> str:
    return FALLBACKS[journal_type][value_id]


def validate(spec: dict, value: Any, skipped: bool, extra: Extra = None) -> Any:
    """Returns the typed value to store (None when skipped). Raises InvalidAnswer."""
    if skipped:
        if spec["required"]:
            raise InvalidAnswer(f"{spec['id']} cannot be skipped")
        return None

    control = spec["control"]
    if control == "scale":
        allowed = {opt["value"] for opt in SCALES[spec["scale"]]}
        if isinstance(value, bool) or not isinstance(value, int) or value not in allowed:
            raise InvalidAnswer(f"{spec['id']} must be one of {sorted(allowed)}")
        return value

    if control == "multi_select":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise InvalidAnswer(f"{spec['id']} must be a list of ids")
        names = _names(spec["library"], extra)
        unknown = [v for v in value if v not in names]
        if unknown:
            raise InvalidAnswer(f"Unknown {spec['library']} ids: {unknown}")
        # FR-PICK-004: an empty selection is a skip, not an error.
        return list(dict.fromkeys(value)) or None

    if control == "rating_set":
        # {item id: 0-100} for the items she chose. Rating only some of them is
        # fine; rating something she did not choose is not.
        if not isinstance(value, dict):
            raise InvalidAnswer(f"{spec['id']} must be a mapping of id to rating")
        chosen = spec.get("_rated_ids")
        low, high = rating_range(spec)
        bad = [k for k, v in value.items()
               if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high]
        if bad:
            raise InvalidAnswer(f"{spec['id']} ratings must be whole numbers {low}-{high}: {bad}")
        if chosen is not None:
            unknown = [k for k in value if k not in chosen]
            if unknown:
                raise InvalidAnswer(f"{spec['id']} rates something not chosen: {unknown}")
        return value or None

    if control == "free_text":
        if not isinstance(value, str):
            raise InvalidAnswer(f"{spec['id']} must be text")
        text = value.strip()
        if len(text) > spec.get("max_length", 2000):
            raise InvalidAnswer(f"{spec['id']} is too long")
        if not text and spec["required"]:
            raise InvalidAnswer(f"{spec['id']} cannot be empty")
        return text or None

    raise InvalidAnswer(f"Unsupported control {control}")


def display_text(spec: dict, value: Any, extra: Extra = None) -> str | None:
    """How an answer appears as the user's message in the thread. An empty answer has no message."""
    if value is None:
        return None
    if spec["control"] == "scale":
        label = next(o["label"] for o in SCALES[spec["scale"]] if o["value"] == value)
        return f"{value} — {label}"
    if spec["control"] == "multi_select":
        names = _names(spec["library"], extra)
        return ", ".join(names.get(v, "(removed)") for v in value)
    if spec["control"] == "rating_set":
        names = _names(_rated_library(spec), extra)
        return ", ".join(f"{names.get(k, '(removed)')} {v}" for k, v in value.items())
    return str(value)


def rating_range(spec: dict) -> tuple[int, int]:
    """A rating_set's range: its own scale's ends when it names one, else 0-100."""
    if spec.get("scale") in SCALES:
        points = [option["value"] for option in SCALES[spec["scale"]]]
        return min(points), max(points)
    return RATING_MIN, RATING_MAX


def _rated_library(spec: dict) -> str:
    """The library whose items a rating_set rates, found from the value it rates."""
    for _, values in _all_schedules():
        rated = next((v for v in values if v["id"] == spec.get("rates")), None)
        if rated is not None:
            return rated["library"]
    raise KeyError(spec.get("rates"))


def rating_items(spec: dict, recorded: dict[str, Any], extra: Extra = None) -> list[dict]:
    """
    What the app puts a rating against: the items she actually chose, in the
    order she chose them, with their names. Empty if she chose none — and then
    the schedule never asks, because a rating_set depends_on what it rates.
    """
    names = _names(_rated_library(spec), extra)
    chosen = recorded.get(spec.get("rates")) or []
    return [{"id": i, "name": names.get(i, "(removed)")} for i in chosen]


# Which end of a library she is most likely to be reaching for, by the mood she
# just recorded. The list is the same either way and every item stays reachable;
# only the order changes. A mixed mood leads nowhere in particular, so it leads
# nowhere: she is not sorted into a half she did not choose.
POSITIVE_FROM_MOOD = 4
NEGATIVE_UP_TO_MOOD = 2


def valence_for(spec: dict, recorded: dict[str, Any]) -> str | None:
    if not spec.get("valence_follows_mood"):
        return spec.get("prefer_valence")
    mood = recorded.get("mood")
    if mood is None:
        return spec.get("prefer_valence")
    if mood >= POSITIVE_FROM_MOOD:
        return "positive"
    if mood <= NEGATIVE_UP_TO_MOOD:
        return "negative"
    return None


def spec_out(
    spec: dict,
    recorded: dict[str, Any] | None = None,
    values: list[dict] | None = None,
    extra: Extra = None,
) -> dict:
    """What the client needs to render the control (FR-ENT-025)."""
    recorded = recorded or {}
    out = {
        "value_id": spec["id"],
        "control": spec["control"],
        "required": spec["required"],
        "library": spec.get("library"),
        "scale": SCALES[spec["scale"]] if spec["control"] == "scale" else None,
        # A published questionnaire item: the app shows it as worded, no rewording.
        "fixed": bool(spec.get("fixed")),
        "max_length": spec.get("max_length"),
        "prefer_valence": valence_for(spec, recorded),
        # FR-JRN-003: free write is one value the user may send in several messages.
        "repeatable": bool(spec.get("repeatable")),
        # She may speak this answer instead of typing it.
        "voice": bool(spec.get("voice")),
        # A rating_set rates what she already chose: the items, and the ends of
        # the range, so the app never hardcodes either.
        "rates": (
            {"items": rating_items(spec, recorded, extra),
             "min": rating_range(spec)[0], "max": rating_range(spec)[1],
             "carried": recorded.get(_carries_from(spec)) or None}
            if spec["control"] == "rating_set" else None
        ),
    }
    if values is not None:
        out["step"], out["total"] = _progress(values, recorded, spec)
    return out


# emotions_after starts from the ratings she already gave, so she adjusts rather
# than rates from nothing. Rating a feeling again is not a fresh question.
_CARRIES = {"emotions_after": "emotions_before"}


def _carries_from(spec: dict) -> str:
    return _CARRIES.get(spec["id"], "")


def _ruled_out(spec: dict, recorded: dict[str, Any]) -> bool:
    """
    Whether this value is settled as never being asked. Not the same as "not
    asked yet": a rating waits on feelings she has not reached, and counting it
    out would make the total jump upwards when she gets there. Only an answered
    dependency that came back empty, or a mood outside the range, settles it.
    """
    depends_on = spec.get("depends_on")
    if depends_on is not None and depends_on in recorded:
        answer = recorded.get(depends_on)
        if answer is None or answer == [] or answer == "":
            return True
        if "when" in spec and answer != spec["when"]:
            return True
    mood = recorded.get("mood")
    if mood is not None and not (spec.get("min_mood", mood) <= mood <= spec.get("max_mood", mood)):
        return True
    return False


def _progress(values: list[dict], recorded: dict[str, Any], spec: dict) -> tuple[int, int]:
    """
    Which step she is on, out of how many this entry will ask. Counted, never
    hardcoded: a schedule branches and the trauma variant asks fewer. The total
    can shrink as her answers rule questions out; it never grows, so it never
    promises fewer steps than are coming.
    """
    coming = [v["id"] for v in values if v["id"] in recorded or not _ruled_out(v, recorded)]
    return (coming.index(spec["id"]) + 1 if spec["id"] in coming else len(coming)), len(coming)


def summary(values: list[dict], recorded: dict[str, Any], extra: Extra = None) -> str:
    """
    Plain-language record of the entry for the model (FR-AIR-002).
    recorded maps value_id → stored value (None = left empty or not asked).
    Empty values are left out: nothing may comment on them.
    """
    lines = []
    for spec in values:
        value = recorded.get(spec["id"])
        if value is None:
            continue
        shown = display_text(spec, value, extra)
        if spec["control"] == "free_text":
            shown = f"<user_text>{value}</user_text>"
        lines.append(f"- {spec['id']}: {shown}")
    return "\n".join(lines) if lines else "- nothing recorded yet"
