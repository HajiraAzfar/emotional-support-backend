"""
"Know Your Week": the one generated paragraph in the weekly report.

The split matters. Everything countable is worked out in `weekly.py` and shown
on its own; the model is handed the finished figures and her own saved lines,
and asked only to put them into sentences. It never sees the database, never
decides a badge, and never produces a number — the check below throws the
whole narrative away if it does, and the report stands perfectly well without
one.
"""
import logging
import re

from app.core import enforcement, language, llm_client, md_loader

logger = logging.getLogger(__name__)

# Under this, a week is a handful of entries and a "reflection" on it would be
# the model talking rather than her week speaking.
MIN_ENTRIES = 2
# How many of each kind of thing the model is shown. Enough to see a shape;
# not so much that the prompt becomes the journal.
TOP_ITEMS = 4
MAX_LINES = 5
# A digit in the narrative is a statistic the model invented: every real number
# is already on the screen above it.
_DIGIT = re.compile(r"\d")


def _listed(rows: list[dict]) -> str:
    return ", ".join(row["name"] for row in rows[:TOP_ITEMS])


def _evidence(report: dict) -> str:
    """The verified week, as the only thing the model is allowed to write from."""
    sections = []
    journals = report["journals"]

    if report["feelings"]:
        sections.append("# Feelings she recorded\n" + _listed(report["feelings"]))
    if report["triggers"]:
        sections.append("# What she recorded alongside them\n" + _listed(report["triggers"]))
    if report["thinking_traps"]:
        sections.append("# Thinking patterns she picked out herself\n"
                        + _listed(report["thinking_traps"]))

    moments = [m for m in journals["savouring"]["moments"] if m["event"]]
    if moments:
        sections.append("# Moments she kept\n" + "\n".join(
            f"- {m['event']}" + (f" — why it mattered: {m['significance']}" if m["significance"] else "")
            for m in moments[:MAX_LINES]))

    balanced = [c for c in report["helped"] if c["kind"] == "revised_thought"]
    if balanced:
        sections.append("# Balanced thoughts she wrote\n"
                        + "\n".join(f"- {c['text']}" for c in balanced[:MAX_LINES]))

    learnings = [c for c in report["helped"] if c["kind"] == "learning"]
    if learnings:
        sections.append("# What she noticed after a step she tried\n"
                        + "\n".join(f"- {c['text']}" for c in learnings[:MAX_LINES]))

    if journals["exposure"]["attempted"]:
        sections.append("# She tried a step she had been avoiding this week.")
    if report["carried_in"]:
        sections.append("# What she said last week she wanted to carry into this one\n"
                        + report["carried_in"])
    return "\n\n".join(sections)


def _her_words(report: dict) -> list[str]:
    """Her own text this week, so the reply comes back in the language she used."""
    words = [card["text"] for card in report["helped"]]
    words += [m["event"] for m in report["journals"]["savouring"]["moments"] if m["event"]]
    return words


def write(report: dict) -> str | None:
    """
    The narrative, or None — too thin a week, a model that would not answer, or
    an answer that broke a rule. Every caller shows the report regardless.
    """
    if report["overview"]["entries"] < MIN_ENTRIES:
        return None
    evidence = _evidence(report)
    if not evidence:
        return None

    prompt = "\n\n".join([
        md_loader.load_weekly_report(),
        f"# Reply language\n{language.instruction(language.reply_language(_her_words(report)))}",
        evidence,
    ])
    text = llm_client.generate_closing(prompt)
    if not text:
        return None

    text = enforcement.limit_questions(enforcement.strip_card_labels(text), allowed=1)
    if not text.strip():
        return None
    if _DIGIT.search(text):
        # Not repairable: a wrong number is worse than no narrative, and the
        # figures it was reaching for are already on the screen.
        logger.info("Weekly narrative dropped: it contained a figure")
        return None
    return text.strip()
