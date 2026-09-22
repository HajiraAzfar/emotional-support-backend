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
# What happens after the last value: "offer" the AI conversation (default), or "close"
# the thread with one closing message and no conversation (savouring, FR-JRN-002).
ENDINGS: dict[str, str] = {name: f.get("ending", "offer") for name, f in _SCHEDULE_FILES.items()}
# Skills that always apply to a journal type, whatever the detected topic
# (e.g. savouring is always about a good moment → skills/positive.md).
JOURNAL_SKILLS: dict[str, list[str]] = {name: f.get("skills", []) for name, f in _SCHEDULE_FILES.items()}
LIBRARIES: dict[str, dict] = {
    p.stem: _read(p) for p in (_CONTENT_DIR / "libraries").glob("*.json")
}
SCALES: dict[str, list[dict]] = _read(_CONTENT_DIR / "scales.json")
FALLBACKS: dict = _read(_CONTENT_DIR / "capture_fallbacks.json")


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
        if ENDINGS[journal_type] == "close" and f"{journal_type}_close" not in FALLBACKS:
            raise RuntimeError(f"{journal_type}: no fallback closing message")


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


def has_schedule(journal_type: str) -> bool:
    return journal_type in SCHEDULES


def all_value_ids() -> tuple[str, ...]:
    return tuple(sorted({v["id"] for values in SCHEDULES.values() for v in values}))


def next_value(journal_type: str, recorded: set[str]) -> dict | None:
    """The first value in the schedule without a response or skip (FR-ENT-024)."""
    for spec in SCHEDULES[journal_type]:
        if spec["id"] not in recorded:
            return spec
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
    }
    return out


def summary(journal_type: str, recorded: dict[str, Any], extra: Extra = None) -> str:
    """
    Plain-language record of the entry for the model (FR-AIR-002).
    recorded maps value_id → stored value (None = skipped).
    """
    lines = []
    for spec in SCHEDULES[journal_type]:
        if spec["id"] not in recorded:
            continue
        value = recorded[spec["id"]]
        shown = "skipped" if value is None else display_text(spec, value, extra)
        if spec["control"] == "free_text" and value is not None:
            shown = f"<user_text>{value}</user_text>"
        lines.append(f"- {spec['id']}: {shown}")
    return "\n".join(lines) if lines else "- nothing recorded yet"
