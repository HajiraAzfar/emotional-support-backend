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
# What happens after the last value: "offer" the AI conversation (default), "close"
# it with one closing message (savouring, FR-JRN-002), or "grounding" — a fixed
# grounding message and no conversation (thought trauma variant, FR-JRN-005/008).
ENDINGS: dict[str, str] = {name: f.get("ending", "offer") for name, f in _SCHEDULE_FILES.items()}
# An alternative schedule for users with particular focus areas (FR-JRN-005).
VARIANTS: dict[str, dict] = {name: f["variant"] for name, f in _SCHEDULE_FILES.items() if "variant" in f}
# Journals that end with a grounding message for users with the past-event focus
# area, whatever their ending (FR-JRN-008).
GROUNDING_JOURNALS = {"free_write", "thought"}
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


def _check_content() -> None:
    for journal_type, values in SCHEDULES.items():
        for spec in values:
            if spec["control"] == "multi_select" and spec["library"] not in LIBRARIES:
                raise RuntimeError(f"{journal_type}.{spec['id']}: unknown library {spec['library']}")
            if spec["control"] == "scale" and spec["scale"] not in SCALES:
                raise RuntimeError(f"{journal_type}.{spec['id']}: unknown scale {spec['scale']}")
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
        if name not in SCHEDULES:
            raise RuntimeError(f"conversation.json: {name} is not a journal type")
        if set(CONVERSATION[name]) - set(CONVERSATION["default"]) - {"_comment"}:
            raise RuntimeError(f"conversation.json: {name} sets a limit that does not exist")


_check_content()


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
    return tuple(sorted({v["id"] for values in SCHEDULES.values() for v in values}))


def _variant_applies(journal_type: str, focus_codes: list[str]) -> bool:
    variant = VARIANTS.get(journal_type)
    return bool(variant and set(focus_codes) & set(variant["when_focus"]))


def values_for(journal_type: str, focus_codes: list[str] | None = None) -> list[dict]:
    """
    The schedule this user gets. The trauma variant drops some values and makes
    others optional (FR-JRN-005); everyone else gets the base schedule.
    """
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
    "how strong was it?" makes no sense after she skipped the feelings.
    """
    depends_on = spec.get("depends_on")
    if depends_on is None:
        return True
    answer = recorded.get(depends_on)
    return answer is not None and answer != [] and answer != ""


def next_value(values: list[dict], recorded: dict[str, Any]) -> dict | None:
    """The first value in the schedule without a response or skip (FR-ENT-024)."""
    for spec in values:
        if spec["id"] not in recorded and _asked(spec, recorded):
            return spec
    return None


def scale_bounds(value_id: str) -> tuple[int, int] | None:
    """
    The lowest and highest a value can be recorded as, read from the schedule
    that records it — so a chart never hardcodes the ends of a scale.
    """
    for values in SCHEDULES.values():
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


def display_text(spec: dict, value: Any, extra: Extra = None) -> str:
    """How an answer appears as the user's message in the thread."""
    if value is None:
        return "Skipped"
    if spec["control"] == "scale":
        label = next(o["label"] for o in SCALES[spec["scale"]] if o["value"] == value)
        return f"{value} — {label}"
    if spec["control"] == "multi_select":
        names = _names(spec["library"], extra)
        return ", ".join(names.get(v, "(removed)") for v in value)
    return str(value)


def spec_out(spec: dict) -> dict:
    """What the client needs to render the control (FR-ENT-025)."""
    out = {
        "value_id": spec["id"],
        "control": spec["control"],
        "required": spec["required"],
        "library": spec.get("library"),
        "scale": SCALES[spec["scale"]] if spec["control"] == "scale" else None,
        "max_length": spec.get("max_length"),
        "prefer_valence": spec.get("prefer_valence"),
        # FR-JRN-003: free write is one value the user may send in several messages.
        "repeatable": bool(spec.get("repeatable")),
    }
    return out


def summary(values: list[dict], recorded: dict[str, Any], extra: Extra = None) -> str:
    """
    Plain-language record of the entry for the model (FR-AIR-002).
    recorded maps value_id → stored value (None = skipped).
    """
    lines = []
    for spec in values:
        if spec["id"] not in recorded:
            continue
        value = recorded[spec["id"]]
        shown = "skipped" if value is None else display_text(spec, value, extra)
        if spec["control"] == "free_text" and value is not None:
            shown = f"<user_text>{value}</user_text>"
        lines.append(f"- {spec['id']}: {shown}")
    return "\n".join(lines) if lines else "- nothing recorded yet"
