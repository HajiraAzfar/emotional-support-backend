"""
Knowledge registry check (RAG Phase 1). Offline only: no network, no database,
no model. The real registry first, then invented entries for each way a source
must be refused, then the release gate on a draft knowledge card.

    python -m evals.knowledge_eval
"""
import copy
import pathlib
import re
import subprocess
import sys
import tempfile

from app.core import knowledge as kn

MARKER = kn.DRAFT_MARKER
RELEASE_CHECK = pathlib.Path(kn.__file__).resolve().parents[2] / "release_check.py"


def cleared() -> dict:
    """An invented entry with everything documented: the one shape that may be ingested."""
    return {
        "id": "invented-source", "title": "An invented guide", "publisher": "Nobody",
        "url": "https://example.org/invented", "format": "web page", "topics": ["worry"],
        "published": "2024-01-31",
        "licence": {"name": "Invented licence", "status": "verified",
                    "verified_from": ["https://example.org/terms"], "notes": ""},
        "commercial_use": "permitted",
        "permissions": {use: "granted" for use in kn.USES},
        "permission_evidence": {"granted_by": "Invented Publisher", "granted_on": "2026-11-01",
                                "reference": "permissions/invented-source.pdf"},
        "next_action": "none",
    }


def changed(**edits) -> dict:
    entry = cleared()
    for key, value in edits.items():
        if "." in key:
            outer, inner = key.split(".")
            entry[outer][inner] = value
        else:
            entry[key] = value
    return entry


def refused(entry: dict) -> bool:
    return kn.ingestable_sources([entry]) == [] and not kn.cleared_for_ingestion(entry)


def release_gate(card_text: str) -> subprocess.CompletedProcess:
    """The real release_check.py, run on a throwaway app/ holding one knowledge card."""
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "release_check.py").write_text(RELEASE_CHECK.read_text(encoding="utf-8"), encoding="utf-8")
        card = root / "app" / "knowledge" / "cards" / "worry" / "worry-time.md"
        card.parent.mkdir(parents=True)
        card.write_text(card_text, encoding="utf-8")
        return subprocess.run([sys.executable, "-B", str(root / "release_check.py")],
                              capture_output=True, text=True, cwd=tmp)


def offline() -> None:
    # The real registry: sound, as audited, and nothing cleared.
    sources = kn.load_sources()
    assert kn.validate(sources) == [], kn.validate(sources)
    by_id = {s["id"]: s for s in sources}
    assert list(by_id) == ["who-dwm", "cci-lay", "nhs-emm-cbt", "beck-resources", "beck-permission",
                           "reframe-original"], list(by_id)
    # The app's own draft cards: original text, but ownership and licence not yet confirmed.
    assert (by_id["reframe-original"]["licence"]["status"], by_id["reframe-original"]["commercial_use"]) ==         ("unverified", "not_applicable")
    who, cci, nhs = by_id["who-dwm"], by_id["cci-lay"], by_id["nhs-emm-cbt"]
    assert (who["licence"]["name"], who["licence"]["status"], who["commercial_use"]) == \
        ("CC BY-NC-SA 3.0 IGO", "verified", "requires_permission")
    assert who["published"] == "2020-04-29"
    assert (cci["licence"]["status"], cci["commercial_use"]) == ("unverified", "unknown")
    assert nhs["commercial_use"] == "requires_licence"
    assert all(set(s["permissions"].values()) == {"not_granted"} and s["permission_evidence"] is None
               for s in sources), "no source has a documented permission yet"
    assert kn.ingestable_sources() == [], [s["id"] for s in kn.ingestable_sources()]
    assert not any(kn.cleared_for_ingestion(s) for s in sources)

    # A fully documented entry is cleared: the rule is not hard-wired to an empty list.
    assert kn.problems(cleared()) == [], kn.problems(cleared())
    assert kn.ingestable_sources([cleared()]) == [cleared()]

    # Missing metadata: reported, and never cleared even with every use granted.
    for key in kn.REQUIRED + ("published", "permission_evidence"):
        entry = cleared()
        del entry[key]
        assert any(f"missing {key}" in p for p in kn.problems(entry)), key
        assert refused(entry), key
    for entry, why in ((changed(topics=[]), "missing topics"), (changed(title=""), "missing title"),
                       (changed(**{"licence.name": ""}), "missing licence.name"),
                       (changed(published="29 April 2020"), "not a YYYY-MM-DD date"),
                       (changed(url="http://example.org/invented"), "not https"),
                       (changed(licence="CC BY"), "licence must be an object"),
                       (changed(commercial_use="maybe"), "commercial_use must be one of")):
        assert any(why in p for p in kn.problems(entry)), (why, kn.problems(entry))
        assert refused(entry), why

    # Unverified licence: a well-formed entry, still refused whatever it grants.
    entry = changed(**{"licence.status": "unverified"})
    assert kn.problems(entry) == [] and refused(entry)
    entry = changed(**{"licence.status": "assumed"})
    assert any("licence.status" in p for p in kn.problems(entry)) and refused(entry)
    entry = changed(**{"licence.verified_from": []})
    assert any("names the pages" in p for p in kn.problems(entry)) and refused(entry)

    # Disallowed ingestion: each ingestion use needs its own grant, and every grant its evidence.
    for use in kn.INGESTION_USES:
        entry = changed(**{f"permissions.{use}": "not_granted"})
        assert kn.problems(entry) == [] and refused(entry), use
    entry = changed(**{"permissions.adapt": "not_granted", "permissions.translate": "not_granted"})
    assert kn.ingestable_sources([entry]) == [entry], "store and embed are what ingestion needs"
    entry = changed(**{"permissions.embed": "yes"})
    assert any("permissions.embed" in p for p in kn.problems(entry)) and refused(entry)
    entry = changed(permission_evidence=None)
    assert any("needs permission_evidence" in p for p in kn.problems(entry)) and refused(entry)
    for field in kn.EVIDENCE_FIELDS:
        entry = cleared()
        entry["permission_evidence"][field] = ""
        assert refused(entry), field
    entry = changed(**{"permission_evidence.granted_on": "last week"})
    assert refused(entry)
    twice = [cleared(), cleared()]
    assert any("duplicate id" in p for p in kn.validate(twice)) and kn.ingestable_sources(twice) == []

    # The marker cards are checked for is the release gate's own, character for character.
    gate_marker = re.search(r'^MARKER = "([^"]+)"$', RELEASE_CHECK.read_text(encoding="utf-8"), re.M)
    assert gate_marker and gate_marker.group(1) == kn.DRAFT_MARKER, gate_marker

    # The release gate is unchanged and still blocks a draft knowledge card, until the marker goes.
    blocked = release_gate(f"---\nid: worry-time\n---\n{MARKER}\n\nInvented card text.\n")
    assert blocked.returncode == 1 and "app/knowledge/cards/worry/worry-time.md" in blocked.stdout, blocked.stdout
    approved = release_gate("---\nid: worry-time\n---\nInvented card text.\n")
    assert approved.returncode == 0, approved.stdout

    print("offline: registry, ingestion gate and release gate checks passed")


if __name__ == "__main__":
    offline()
