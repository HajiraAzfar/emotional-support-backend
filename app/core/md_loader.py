import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
_ECHO_LINE = re.compile(r'^Echo: "(.*)"\s*$', re.M)

FOLDERS = ("clinical_context", "skills", "journal_types")
# skills/ follows the Agent Skills layout: one folder per skill holding SKILL.md
# with name/description frontmatter. The other folders are plain .md files.
SKILL_FILE = "SKILL.md"

# The only domains the classifier may return. Each must have a skills/{name}/SKILL.md.
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


def _path(folder: str, name: str) -> Path:
    if folder == "skills":
        return APP_DIR / folder / name / SKILL_FILE
    return APP_DIR / folder / f"{name}.md"


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Returns (frontmatter fields, body). Only simple `key: value` lines are read."""
    if not text.startswith("---"):
        return {}, text
    _, raw, body = text.split("---", 2)
    fields = {}
    for line in raw.strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    return fields, body.strip()


def available(folder: str) -> frozenset[str]:
    """Names present in one of the content folders."""
    if folder not in FOLDERS:
        raise ValueError(f"Unknown content folder: {folder}")
    if folder == "skills":
        return frozenset(
            p.name for p in (APP_DIR / folder).iterdir() if (p / SKILL_FILE).is_file()
        )
    return frozenset(p.stem for p in (APP_DIR / folder).glob("*.md"))


def load(folder: str, name: str) -> str:
    """
    Reads the content file for `name`, without its frontmatter. The name is
    checked against what actually exists, so a user-supplied value (e.g.
    journal_type) can never turn into an arbitrary path. Not cached, so prompt
    edits apply without a server restart.
    """
    if name not in available(folder):
        raise FileNotFoundError(f"No {folder}/{name}")
    _, body = _split_frontmatter(_path(folder, name).read_text(encoding="utf-8").strip())
    return body


def example_replies(folder: str, name: str) -> list[str]:
    """The Echo lines of a file's examples: illustrations a reply must never copy."""
    return _ECHO_LINE.findall(load(folder, name))


def skill_descriptions() -> dict[str, str]:
    """
    name → description from each SKILL.md. The descriptions say when a skill
    applies, so they are also what the classifier chooses between — the skill
    files stay the single source of truth.
    """
    descriptions = {}
    for name in available("skills"):
        fields, _ = _split_frontmatter(_path("skills", name).read_text(encoding="utf-8"))
        descriptions[name] = fields.get("description", "")
    return descriptions


def load_base() -> str:
    return _split_frontmatter((APP_DIR / "base_instructions.md").read_text(encoding="utf-8").strip())[1]


def load_capture_instructions() -> str:
    return _split_frontmatter((APP_DIR / "capture_instructions.md").read_text(encoding="utf-8").strip())[1]


_missing = set(DOMAINS) - available("skills")
if _missing:
    raise RuntimeError(f"Missing skills/<name>/{SKILL_FILE} for domains: {sorted(_missing)}")
_undescribed = [n for n, d in skill_descriptions().items() if not d]
if _undescribed:
    raise RuntimeError(f"Skills without a description: {sorted(_undescribed)}")
