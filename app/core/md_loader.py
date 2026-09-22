from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent

FOLDERS = ("clinical_context", "skills", "journal_types")

# The only domains the classifier may return. Each must have a skills/{name}.md file.
DOMAINS = (
    "low_mood",
    "low_self_esteem",
    "relationship_issues",
    "distraction",
    "lack_of_self_control",
    "overwhelmed",
    "overthinking",
    "grief",
    "positive",
    "general",
)


def available(folder: str) -> frozenset[str]:
    """File names (without .md) present in one of the content folders."""
    if folder not in FOLDERS:
        raise ValueError(f"Unknown content folder: {folder}")
    return frozenset(p.stem for p in (APP_DIR / folder).glob("*.md"))


def load(folder: str, name: str) -> str:
    """
    Reads app/{folder}/{name}.md. The name is checked against the files that
    actually exist, so a user-supplied value (e.g. journal_type) can never
    turn into an arbitrary path. Not cached, so prompt edits apply without a
    server restart.
    """
    if name not in available(folder):
        raise FileNotFoundError(f"No {folder}/{name}.md")
    return (APP_DIR / folder / f"{name}.md").read_text(encoding="utf-8").strip()


def load_base() -> str:
    return (APP_DIR / "base_instructions.md").read_text(encoding="utf-8").strip()


def load_capture_instructions() -> str:
    return (APP_DIR / "capture_instructions.md").read_text(encoding="utf-8").strip()


_missing = set(DOMAINS) - available("skills")
if _missing:
    raise RuntimeError(f"Missing skill files for domains: {sorted(_missing)}")
