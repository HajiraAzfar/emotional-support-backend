"""
RAG Phase 2: the retrieval core. Not wired into AI Chat yet: no production code
calls it, and RAG_ENABLED is off by default.

- card_problems() / usable_cards(): a card needs its metadata, clinical approval,
  no release-gate draft marker anywhere in it, and a source the registry clears for
  ingestion (knowledge.py). Usable cards come back frozen: nobody can edit them.
- InMemoryIndex: exact cosine search over any embed(texts, timeout_seconds). A
  database index later implements the same search().
- TurnRisk / skip_reason(): retrieval serves only a turn whose triage ran and found
  no risk at all, with a valid safety check that agrees with it.
- evidence_for_turn(): the one entry point, and it never raises: any failure is no
  evidence. Before a card is returned it is checked again against the current card
  set and registry.
- Timeouts are the provider's job: search() gets timeout_seconds and must enforce it
  in its own client (an HTTP timeout, a statement timeout). Python cannot stop a
  provider call, or a generator's next(), that is already running: the caller waits
  for it to return, and a late result is then discarded.
- At most MAX_CONCURRENT_SEARCHES run at once, each in its caller's own thread; a
  request that finds every slot taken gets no evidence at once. The provider's
  results are read while the slot is held, at most MAX_RESULTS of them, and reading
  stops at the deadline; a generator is then closed, so it cannot resume. Nothing
  queues, and no provider work runs after its request has returned.
- Evidence.block() / enforce_citations(): [S1]-style citations, each tied to one card
  and its source locator; only ids retrieved for this turn survive.
"""
import logging
import math
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Callable, Iterable, Protocol

from app.core import crisis, knowledge
from app.core.config import settings
from app.core.crisis_check import TIER_ORDER
from app.core.llm_client import Safety

logger = logging.getLogger(__name__)

CARD_REQUIRED = ("id", "title", "source_id", "locator", "topics", "text", "clinical_status")
CARD_STRINGS = ("id", "title", "source_id", "locator", "text", "clinical_status")
MAX_TOP_K = 5
MAX_TIMEOUT_SECONDS = 5.0
# ponytail: a fixed cap below FastAPI's 40 request threads; size it from real chat
# concurrency when retrieval is wired in.
MAX_CONCURRENT_SEARCHES = 8
_slots = threading.BoundedSemaphore(MAX_CONCURRENT_SEARCHES)
# The most hits read from one search. A provider asked for top_k that sends more is
# misbehaving: the whole result is refused, and reading stops at MAX_RESULTS + 1.
MAX_RESULTS = 4 * MAX_TOP_K


@dataclass(frozen=True)
class RetrievalConfig:
    # No production threshold exists until a labelled evaluation set calibrates one:
    # without it every retrieval abstains. `calibration` says where the number came from.
    threshold: float | None = None
    calibration: str | None = None
    top_k: int = 3
    timeout_seconds: float = 2.0


PRODUCTION = RetrievalConfig()


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def config_problems(config) -> list[str]:
    if not isinstance(config, RetrievalConfig):
        return ["not a RetrievalConfig"]
    found = []
    if config.threshold is not None:
        if not (_number(config.threshold) and 0.0 <= config.threshold <= 1.0):
            found.append("threshold must be a number from 0 to 1")
        if not (isinstance(config.calibration, str) and config.calibration.strip()):
            found.append("a threshold needs its calibration")
    if not (isinstance(config.top_k, int) and not isinstance(config.top_k, bool) and 1 <= config.top_k <= MAX_TOP_K):
        found.append(f"top_k must be a whole number from 1 to {MAX_TOP_K}")
    if not (_number(config.timeout_seconds) and 0 < config.timeout_seconds <= MAX_TIMEOUT_SECONDS):
        found.append(f"timeout_seconds must be above 0 and at most {MAX_TIMEOUT_SECONDS}")
    return found


def _frozen(value):
    """
    A read-only deep copy: mappings become read-only views, lists tuples, sets
    frozensets. Leaves are immutable already (card_problems refuses any other kind).
    """
    if isinstance(value, Mapping):
        return MappingProxyType({k: _frozen(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_frozen(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_frozen(v) for v in value)
    return value


# The only leaf values a card may hold: all immutable, so a frozen card shares nothing editable.
_LEAVES = (str, int, float, bool, type(None), bytes, date)


def _immutable_leaves(value) -> bool:
    if isinstance(value, Mapping):
        return all(isinstance(k, str) and _immutable_leaves(v) for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return all(_immutable_leaves(v) for v in value)
    return isinstance(value, _LEAVES)


def _plain(value):
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def card_problems(card, cleared_ids: set[str]) -> list[str]:
    if not isinstance(card, Mapping):
        return ["<not a card>: not a mapping"]
    cid = card.get("id") if isinstance(card.get("id"), str) and card.get("id") else "<no id>"
    found = [f"{cid}: missing {key}" for key in CARD_REQUIRED if card.get(key) in (None, "", [], (), {})]
    found += [f"{cid}: {key} must be text" for key in CARD_STRINGS
              if card.get(key) not in (None, "") and not isinstance(card[key], str)]
    topics = card.get("topics")
    if topics and not (isinstance(topics, (list, tuple)) and all(isinstance(t, str) for t in topics)):
        found.append(f"{cid}: topics must be a list of strings")
    if not _immutable_leaves(card):
        found.append(f"{cid}: holds a value of a kind a card may not carry")
    # The release gate's own marker: a card still carrying it is a draft, whatever its status says.
    if knowledge.DRAFT_MARKER in repr(_plain(card)):
        found.append(f"{cid}: contains the draft marker {knowledge.DRAFT_MARKER}")
    if card.get("clinical_status") not in (None, "") and card["clinical_status"] != "approved":
        found.append(f"{cid}: not clinically approved")
    if isinstance(card.get("source_id"), str) and card["source_id"] not in cleared_ids:
        found.append(f"{cid}: source {card['source_id']} is not cleared for ingestion")
    return found


def _cleared(sources: list[dict] | None) -> dict[str, dict]:
    return {s["id"]: s for s in knowledge.ingestable_sources(sources)}


def _usable(cards, cleared_ids: set[str]) -> list[MappingProxyType]:
    ids = [c.get("id") if isinstance(c, Mapping) else None for c in cards]
    return [_frozen(c) for c, cid in zip(cards, ids)
            if not card_problems(c, cleared_ids) and ids.count(cid) == 1]


def usable_cards(cards, sources: list[dict] | None = None) -> list[MappingProxyType]:
    """Frozen copies of the cards that pass card_problems(); an id listed twice is never usable."""
    return _usable(cards, set(_cleared(sources)))


def _cosine(a: list[float], b: list[float]) -> float:
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / norm if norm else 0.0


@dataclass(frozen=True)
class Hit:
    card: Mapping
    score: float


class Search(Protocol):
    def search(self, query: str, top_k: int, topics: frozenset[str] | None, timeout_seconds: float) -> Iterable[Hit]:
        """
        Must give up by itself after timeout_seconds (raise): nobody else can stop it.
        `topics` is read-only; whatever comes back is checked against it again.
        """


class InMemoryIndex:
    """Exact cosine search over usable cards only. `embed` is pluggable: a fake in tests."""

    def __init__(self, cards, embed: Callable[[list[str], float], list[list[float]]],
                 sources: list[dict] | None = None, timeout_seconds: float = MAX_TIMEOUT_SECONDS):
        self._cards = tuple(usable_cards(cards, sources))
        self._embed = embed
        # `search_terms` (optional: other words people use, Roman Urdu too) help retrieval only; the model sees title and text.
        texts = [f"{c['title']}\n{c['text']}" + (f"\n{c['search_terms']}" if c.get("search_terms") else "") for c in self._cards]
        vectors = embed(texts, timeout_seconds) if self._cards else []
        self._vectors = tuple(tuple(v) for v in vectors)

    @property
    def cards(self) -> tuple:
        """Frozen copies, in a tuple: nobody can add, swap or edit an indexed card."""
        return self._cards

    def search(self, query: str, top_k: int, topics: frozenset[str] | None, timeout_seconds: float) -> list[Hit]:
        if not self.cards:
            return []
        [q] = self._embed([query], timeout_seconds)
        hits = [Hit(c, _cosine(q, v)) for c, v in zip(self.cards, self._vectors)
                if topics is None or topics & set(c["topics"])]
        # Ties go to the card id, so the same query always ranks the same way.
        return sorted(hits, key=lambda h: (-h.score, h.card["id"]))[:top_k]


@dataclass(frozen=True)
class Cited:
    id: str  # "S1", in rank order
    card: Mapping  # read-only
    source: Mapping  # read-only
    score: float


@dataclass(frozen=True)
class Evidence:
    items: tuple[Cited, ...] = ()
    reason: str = ""  # why there is none; content-free, safe to log

    @property
    def ids(self) -> list[str]:
        return [c.id for c in self.items]

    def citations(self, ids=None) -> dict[str, dict]:
        """Each citation id (or only those in `ids`) -> the exact card and source locator it stands for."""
        wanted = None if ids is None else set(ids)
        return {c.id: {"card_id": c.card["id"], "source_id": c.source["id"], "locator": c.card["locator"],
                       "source_title": c.source["title"], "source_url": c.source["url"]}
                for c in self.items if wanted is None or c.id in wanted}

    def block(self) -> str | None:
        """The prompt section for this turn's evidence, or None when there is none."""
        if not self.items:
            return None
        parts = ["# Evidence for this reply\nCite a card as [S1] where you use it. Nothing else here is a source."]
        parts += [f"[{c.id}] {c.card['title']} ({c.source['title']}, {c.card['locator']})\n{c.card['text']}"
                  for c in self.items]
        return "\n\n".join(parts)


@dataclass(frozen=True)
class TurnRisk:
    """What the crisis screen decided for this turn, as add_message holds it."""
    tier: str  # crisis.assess(): rules and model, higher wins; "clear" when nothing was found
    level: str | None  # crisis.triage(tier, safety): None means it ran and found nothing
    safety: Safety | None  # the classifier's safety check; None when the model stage was down


def _checked_safety(safety) -> Safety | None:
    """A Safety whose every field is one the classifier can return, or None."""
    if not isinstance(safety, Safety):
        return None
    try:
        return Safety.model_validate(safety.model_dump())
    except Exception:
        return None


def skip_reason(risk) -> str | None:
    """Retrieval serves only a turn with no risk at all; anything else, or anything unclear, is a reason not to."""
    if not isinstance(risk, TurnRisk):
        return "no triage"
    if risk.level == crisis.TIER1:
        return "tier1"
    if risk.level == crisis.CLARIFY:
        return "clarify"
    if risk.tier not in TIER_ORDER:
        return "no triage"
    safety = _checked_safety(risk.safety)
    if safety is None:
        return "no safety check"
    if safety.harm_type != "none":
        return "harm"
    if safety.danger_now != "no":
        return "danger signal"
    if safety.distress_now:
        return "distress now"
    if safety.timing != "none" or safety.discloses:
        return "inconsistent safety"  # the classifier gives timing "none" and no disclosure without harm
    if risk.tier != "clear":
        return f"risk tier ({risk.tier})"
    if risk.level is not None:
        return f"risk ({risk.level})"
    if crisis.triage(risk.tier, safety) is not None:
        return "inconsistent triage"
    return None


def evidence_for_turn(text: str, domains: list[str] | None, risk: TurnRisk, search: Search, cards,
                      config: RetrievalConfig = PRODUCTION, sources: list[dict] | None = None) -> Evidence:
    """
    This turn's evidence, or none with the reason. Never raises. `cards` is the
    current card set: every hit is checked against it and the registry before use.
    """
    if not settings.RAG_ENABLED:
        return Evidence(reason="disabled")
    try:
        return _evidence(text, domains, risk, search, cards, config, sources)
    except Exception as exc:  # any failure is simply no evidence, never the crisis fallback
        logger.warning("retrieval failed: %s", type(exc).__name__)
        return Evidence(reason=f"error: {type(exc).__name__}")


def _evidence(text, domains, risk, search, cards, config, sources) -> Evidence:
    reason = skip_reason(risk)
    if reason:
        return Evidence(reason=f"skipped: {reason}")
    if config_problems(config):
        return Evidence(reason="invalid config")
    if config.threshold is None:
        return Evidence(reason="no calibrated threshold")
    if not (isinstance(text, str) and text.strip()):
        return Evidence(reason="no query")
    if domains is not None and not (isinstance(domains, (list, tuple)) and all(isinstance(d, str) for d in domains)):
        return Evidence(reason="invalid domains")
    # Read-only, and kept here: whatever the provider does, results are checked against these.
    topics = frozenset(d for d in domains or () if d != "general") or None

    cleared = _cleared(sources)
    current = {c["id"]: c for c in _usable(list(cards), set(cleared))}
    if not current:
        return Evidence(reason="abstain: no usable cards")

    if not _slots.acquire(blocking=False):
        return Evidence(reason="busy")
    try:
        deadline = time.monotonic() + config.timeout_seconds
        hits, refused = _read(search.search(text, config.top_k, topics, config.timeout_seconds), deadline)
    finally:
        _slots.release()
    if refused:
        return Evidence(reason=refused)

    best: dict[str, tuple[Mapping, float]] = {}
    for hit in hits:
        card = _current(hit, current)
        if card is None or hit.score < config.threshold or (topics and not topics & set(card["topics"])):
            continue
        if card["id"] not in best or hit.score > best[card["id"]][1]:  # a card returned twice is cited once
            best[card["id"]] = (card, hit.score)
    ranked = sorted(best.values(), key=lambda cs: (-cs[1], cs[0]["id"]))[:config.top_k]
    if not ranked:
        return Evidence(reason="abstain: nothing qualified")
    return Evidence(tuple(Cited(f"S{i}", card, _frozen(cleared[card["source_id"]]), score)
                          for i, (card, score) in enumerate(ranked, 1)))


def _read(results, deadline: float) -> tuple[list, str | None]:
    """
    The provider's hits, read now, while the caller still holds its slot: at most
    MAX_RESULTS, and nothing after the deadline. Returns (hits, None), or ([], the
    reason they were refused). Whatever was returned is closed when reading stops,
    so a generator can never resume after the request is over. A next() already
    running when the deadline passes still runs to its end: Python cannot stop it.
    """
    hits = []
    try:
        for hit in results:
            if time.monotonic() > deadline:
                return [], "timeout: the provider overran its budget"
            if len(hits) == MAX_RESULTS:
                return [], f"error: more than {MAX_RESULTS} results"
            hits.append(hit)
    finally:
        close = getattr(results, "close", None)
        if callable(close):
            close()
    if time.monotonic() > deadline:  # search() itself, or running out, took too long
        return [], "timeout: the provider overran its budget"
    return hits, None


def _current(hit, current: dict) -> Mapping | None:
    """The hit's card as the current card set has it now, or None if it is not the same usable card."""
    if not (isinstance(hit, Hit) and _number(hit.score) and isinstance(hit.card, Mapping)):
        return None
    cid = hit.card.get("id")
    card = current.get(cid) if isinstance(cid, str) else None
    # Changed since it was indexed (its vector is stale), withdrawn, or never usable: not this turn's evidence.
    return card if card is not None and _plain(hit.card) == _plain(card) else None


# Citations. The one supported form is [S1] (or [S1, S2]). Its near misses ([S 1], (S1),
# [s1]) are rewritten as [S1] when the id was retrieved this turn; any other S-id goes.
_S_IDS = r"[\[(]\s*S\s*\d+(?:\s*,\s*S\s*\d+)*\s*[\])]"
# Invented forms that mean nothing else in a reply, removed. Two kinds of lookalike are
# left alone because they can't be told apart from ordinary text: bracketed numbers
# ([2], [0-10], arr[2]) and narrative author-years ("Beck (2011) showed").
_NAME = r"[A-Z][A-Za-z'’-]+"
_AUTHOR_YEAR = (rf"{_NAME}(?:\s+et\s+al\.?|\s*(?:&|and)\s*{_NAME})?,?\s+(?:19|20)\d{{2}}[a-z]?"
                rf"(?:,\s*pp?\.\s*\d+(?:[-–]\d+)?)?")
_INVENTED = (
    r"\[\^\w{1,20}\]"  # footnote [^1]
    r"|【\s*\d{1,3}\s*】"  # 【1】
    r"|\[\s*(?:sources?|refs?|references?|citations?|cite)\b[^\[\]\n]{0,60}?\d[^\[\]\n]{0,60}?\]"  # [source 4]
    r"|\[\s*(?:sources?|refs?|references?|citations?|cite)(?:\s+needed)?\s*\]"  # [Citation needed]
    rf"|(?-i:\(\s*{_AUTHOR_YEAR}(?:\s*;\s*{_AUTHOR_YEAR})*\s*\))"  # (Beck, 2011), (Beck et al., 2019)
)
_MARKER = re.compile(rf"(?P<ids>{_S_IDS})|(?P<invented>{_INVENTED})", re.IGNORECASE)
# Never touched, whatever brackets they hold: fenced and inline code, and URLs.
_PROTECTED = re.compile(r"```.*?```|`[^`\n]*`|(?:https?://|www\.)\S+", re.DOTALL | re.IGNORECASE)


def _ids(marker: str) -> list[str]:
    return [re.sub(r"\s+", "", i).upper() for i in re.findall(r"S\s*\d+", marker, re.IGNORECASE)]


def enforce_citations(text: str, cited, evidence: Evidence) -> tuple[str, list[str]]:
    """
    The reply text with this turn's citations resolved and invented ones removed,
    plus the ids that survive (from the text, then the reply's own list), in order.
    Their sources come from evidence.citations(ids): nothing is added to the text.
    Only the forms above are touched; code, URLs and all other wording and spacing
    stay exactly as they were, apart from the one space a removed marker takes with it.
    """
    known = set(evidence.ids)
    protected = [m.span() for m in _PROTECTED.finditer(text)]
    found, out, last = [], [], 0
    for m in _MARKER.finditer(text):
        start, end = m.span()
        if any(s < end and start < e for s, e in protected):
            continue
        ids = [i for i in dict.fromkeys(_ids(m.group("ids"))) if i in known] if m.group("ids") else []
        found += ids
        new = f"[{', '.join(ids)}]" if ids else ""
        if not new:  # the one space that goes with it: before it, or after it at the start of a line
            if start > last and text[start - 1] in " \t":
                start -= 1
            elif (start == 0 or text[start - 1] == "\n") and text[end:end + 1] in (" ", "\t"):
                end += 1
        out += [text[last:start], new]
        last = end
    clean = "".join(out) + text[last:]
    listed = [re.sub(r"\s+", "", str(c)).upper() for c in (cited or [])]
    return clean, [i for i in dict.fromkeys(found + listed) if i in known]
_OLD_IDS = re.compile(rf" ?{_S_IDS}", re.IGNORECASE)


def without_ids(text: str) -> str:
    """An earlier reply without its [S1]-style ids: they named that turn's cards, not this one's."""
    return _OLD_IDS.sub("", text)


class LazyIndex:
    """Builds its index on the first search, once; a build that fails is tried again on the next search."""

    def __init__(self, build: Callable[[], Search]):
        self._build, self._index, self._lock = build, None, threading.Lock()

    def search(self, query: str, top_k: int, topics: frozenset[str] | None, timeout_seconds: float):
        # ponytail: the first search waits for every card to be embedded; warm it at startup once RAG is on.
        with self._lock:
            if self._index is None:
                self._index = self._build()
        return self._index.search(query, top_k, topics, timeout_seconds)


class RewrittenQuery:
    """
    Searches `inner` with rewrite(message, timeout) instead of the raw message (Roman Urdu -> English).
    It is a search provider, so it only runs for a turn that passed the risk gate, within the same budget.
    """

    def __init__(self, inner: Search, rewrite: Callable[[str, float], str]):
        self._inner, self._rewrite = inner, rewrite

    def search(self, query: str, top_k: int, topics: frozenset[str] | None, timeout_seconds: float):
        deadline = time.monotonic() + timeout_seconds
        rewritten = self._rewrite(query, timeout_seconds)
        if not rewritten.strip():
            return []  # nothing to search for: abstain
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("query rewrite used the whole budget")
        return self._inner.search(rewritten, top_k, topics, left)
