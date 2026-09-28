"""
The learning library (SRS 4.11): short articles she can read, search, and open
from a thinking pattern in her insights (FR-INS-012).

Articles are Markdown files under content/library/ with a few frontmatter
fields, for the same reason the prompts are: the Clinical Advisor can write and
revise them without reading code, and every one of them currently says in its
own frontmatter that it is a draft awaiting review.
"""
from dataclasses import dataclass, field
from pathlib import Path

from app.core.md_loader import _split_frontmatter

_LIBRARY_DIR = Path(__file__).resolve().parent.parent / "content" / "library"


@dataclass
class Article:
    slug: str
    title: str
    summary: str
    category: str
    minutes: int
    featured: bool
    # Thinking patterns this article covers, for the link from insights.
    traps: list[str] = field(default_factory=list)
    body: str = ""

    def listed(self, favourite: bool = False) -> dict:
        """What a list needs: everything except the body."""
        return {
            "slug": self.slug,
            "title": self.title,
            "summary": self.summary,
            "category": self.category,
            "minutes": self.minutes,
            "featured": self.featured,
            "favourite": favourite,
        }

    def full(self, favourite: bool = False) -> dict:
        return {**self.listed(favourite), "body": self.body, "traps": self.traps}


def _read(path: Path) -> Article:
    fields, body = _split_frontmatter(path.read_text(encoding="utf-8").strip())
    return Article(
        slug=path.stem,
        title=fields.get("title", path.stem.replace("-", " ").capitalize()),
        summary=fields.get("summary", ""),
        category=fields.get("category", "General"),
        minutes=int(fields.get("minutes", "3") or 3),
        featured=fields.get("featured", "").lower() == "true",
        traps=[t.strip() for t in fields.get("traps", "").split(",") if t.strip()],
        body=body,
    )


ARTICLES: dict[str, Article] = {
    path.stem: _read(path) for path in sorted(_LIBRARY_DIR.glob("*.md"))
}


def _check_content() -> None:
    for article in ARTICLES.values():
        if not article.summary:
            raise RuntimeError(f"library/{article.slug}: no summary to show on the card")
        if not article.body:
            raise RuntimeError(f"library/{article.slug}: no body")


_check_content()

CATEGORIES: list[str] = sorted({article.category for article in ARTICLES.values()})


def get(slug: str) -> Article | None:
    return ARTICLES.get(slug)


def search(query: str | None = None, trap: str | None = None, category: str | None = None) -> list[Article]:
    """
    Whole-library search over title, summary and body. Plain substring matching:
    the library is small, and a ranked index would be a lot of machinery for
    six articles.
    """
    found = list(ARTICLES.values())
    if trap:
        found = [a for a in found if trap in a.traps]
    if category:
        found = [a for a in found if a.category.lower() == category.lower()]
    if query:
        needle = query.strip().lower()
        found = [
            a for a in found
            if needle in a.title.lower() or needle in a.summary.lower() or needle in a.body.lower()
        ]
    # Featured first, then alphabetically, so the order never wanders.
    return sorted(found, key=lambda a: (not a.featured, a.title))


def for_trap(trap: str) -> Article | None:
    """FR-INS-012: the article to open from a thinking pattern, if there is one."""
    matches = search(trap=trap)
    return matches[0] if matches else None
