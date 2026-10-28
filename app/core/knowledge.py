"""
RAG Phase 1: the source registry (app/knowledge/sources.json) and the rule that
guards ingestion. Reading a publisher's page is not permission to copy it: a
source may be stored and embedded only when its licence is verified and both
uses are granted, with the written permission recorded. Anything else, an entry
with missing or malformed metadata included, fails closed.

- load_sources(): the registry's entries
- problems(): what is missing or malformed in one entry
- validate(): every problem in a registry, duplicate ids included
- cleared_for_ingestion() / ingestable_sources(): which sources may be ingested (none yet)
"""
import json
from collections import Counter
from datetime import date
from pathlib import Path

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "knowledge" / "sources.json"
# release_check.py's marker (it is a script, so it can't be imported): a knowledge card
# that still carries it is a draft. evals/knowledge_eval.py keeps the two the same.
DRAFT_MARKER = "DRAFT_NOT_CLINICALLY_APPROVED"

REQUIRED = ("id", "title", "publisher", "url", "format", "topics", "licence",
            "commercial_use", "permissions", "next_action")
LICENCE_STATUSES = {"verified", "unverified"}
COMMERCIAL_USE = {"permitted", "requires_permission", "requires_licence", "unknown", "not_applicable"}
USES = ("store", "embed", "adapt", "translate", "display")
PERMISSION_VALUES = {"granted", "not_granted"}
# What ingestion does to a text: keep a copy of it, and derive vectors from it.
INGESTION_USES = ("store", "embed")
# Who gave the written permission, when, and where it is kept.
EVIDENCE_FIELDS = ("granted_by", "granted_on", "reference")


def load_sources(path: Path = REGISTRY_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)["sources"]


def _is_date(value) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


def _documented(evidence) -> bool:
    return (isinstance(evidence, dict) and all(evidence.get(k) for k in EVIDENCE_FIELDS)
            and _is_date(evidence["granted_on"]))


def problems(source: dict) -> list[str]:
    """Missing or malformed metadata in one entry; [] when it is complete."""
    sid = source.get("id") or "<no id>"
    found = [f"{sid}: missing {key}" for key in REQUIRED if source.get(key) in (None, "", [], {})]
    # Present but possibly null: a source may show no date, and has no permission until given one.
    found += [f"{sid}: missing {key}" for key in ("published", "permission_evidence") if key not in source]

    if source.get("published") is not None and not _is_date(source["published"]):
        found.append(f"{sid}: published is not a YYYY-MM-DD date")
    if source.get("url") and not str(source["url"]).startswith("https://"):
        found.append(f"{sid}: url is not https")
    if source.get("commercial_use") and source["commercial_use"] not in COMMERCIAL_USE:
        found.append(f"{sid}: commercial_use must be one of {sorted(COMMERCIAL_USE)}")

    licence, permissions = source.get("licence") or {}, source.get("permissions") or {}
    for key, value in (("licence", licence), ("permissions", permissions)):
        if not isinstance(value, dict):
            found.append(f"{sid}: {key} must be an object")
    licence = licence if isinstance(licence, dict) else {}
    permissions = permissions if isinstance(permissions, dict) else {}

    if licence:
        if not licence.get("name"):
            found.append(f"{sid}: missing licence.name")
        if licence.get("status") not in LICENCE_STATUSES:
            found.append(f"{sid}: licence.status must be verified or unverified")
        elif licence["status"] == "verified" and not licence.get("verified_from"):
            found.append(f"{sid}: a verified licence names the pages it was read from")

    if permissions:
        found += [f"{sid}: permissions.{use} must be granted or not_granted"
                  for use in USES if permissions.get(use) not in PERMISSION_VALUES]
        if "granted" in permissions.values() and not _documented(source.get("permission_evidence")):
            found.append(f"{sid}: a granted use needs permission_evidence with {', '.join(EVIDENCE_FIELDS)}")
    return found


def validate(sources: list[dict]) -> list[str]:
    """Every problem in the registry; [] when it is sound."""
    found = [p for source in sources for p in problems(source)]
    counts = Counter(source.get("id") for source in sources)
    found += [f"{sid}: duplicate id" for sid, n in counts.items() if n > 1]
    return found


def cleared_for_ingestion(source: dict) -> bool:
    return (not problems(source)
            and source["licence"]["status"] == "verified"
            and all(source["permissions"][use] == "granted" for use in INGESTION_USES)
            and _documented(source["permission_evidence"]))


def ingestable_sources(sources: list[dict] | None = None) -> list[dict]:
    """Sources that may be stored and embedded. An id listed twice is never cleared."""
    sources = load_sources() if sources is None else sources
    counts = Counter(source.get("id") for source in sources)
    return [s for s in sources if counts[s.get("id")] == 1 and cleared_for_ingestion(s)]
