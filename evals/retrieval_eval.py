"""
Retrieval core check (RAG Phase 2, hardened). Offline only: a deterministic fake
embedder, synthetic cards (no clinical or source text), an invented cleared
source; no network, database or model.

    python -m evals.retrieval_eval
"""
import copy
import hashlib
import math
import pathlib
import re
import threading
import time
from types import SimpleNamespace

from app.core import crisis, retrieval as rt
from app.core.config import Settings, settings
from app.core.knowledge import DRAFT_MARKER
from app.core.llm_client import LLMError, Safety
from evals.knowledge_eval import cleared as cleared_source

# A threshold for these fixtures only. A production one waits for a labelled evaluation set.
TEST = rt.RetrievalConfig(threshold=0.3, calibration="synthetic test fixtures only", top_k=3, timeout_seconds=1.0)
WIDE = rt.RetrievalConfig(threshold=0.0, calibration="synthetic test fixtures only", top_k=2, timeout_seconds=1.0)

NO_HARM = Safety(harm_type="none", timing="none", danger_now="no", about="self",
                 discloses=False, distress_now=False, asks_for_help=False)


def safety(**fields) -> Safety:
    return NO_HARM.model_copy(update=fields)


def risk(tier="clear", safety_check=NO_HARM) -> rt.TurnRisk:
    """A TurnRisk as add_message would build it: the real triage, run on these inputs."""
    return rt.TurnRisk(tier, crisis.triage(tier, safety_check), safety_check)


def fake_embed(texts: list[str], timeout_seconds: float | None = None) -> list[list[float]]:
    """Hashed bag of words: the same text always gives the same vector, on every run."""
    vectors = []
    for text in texts:
        v = [0.0] * 64
        for word in re.findall(r"[a-z]+", text.lower()):
            v[int(hashlib.sha256(word.encode()).hexdigest(), 16) % 64] += 1.0
        vectors.append(v)
    return vectors


def card(cid, topics, text, **extra) -> dict:
    return {"id": cid, "title": f"Test card {cid}", "source_id": "invented-source", "locator": f"section {cid}",
            "topics": topics, "text": f"Synthetic fixture text. {text}", "clinical_status": "approved", **extra}


def sources() -> list[dict]:
    uncleared = cleared_source()
    uncleared.update(id="invented-uncleared", permissions={**uncleared["permissions"], "embed": "not_granted"})
    return [cleared_source(), uncleared]


CARDS = [
    card("fx-worry", ["overthinking"], "worry night sleep racing"),
    card("fx-thought", ["overthinking", "low_self_esteem"], "notice thought evidence balanced"),
    card("fx-activity", ["low_mood"], "small activity plan walk morning"),
    card("fx-draft", ["overthinking"], "worry night sleep", clinical_status="draft"),
    card("fx-uncleared", ["overthinking"], "worry night sleep", source_id="invented-uncleared"),
    card("fx-broken", ["overthinking"], "worry night sleep", locator=""),
]
USABLE = ["fx-worry", "fx-thought", "fx-activity"]


class Spy:
    """A search provider that records its calls, and can fail, stall, or ignore its timeout on purpose."""

    def __init__(self, inner=None, raises=None, sleep=0.0, hits=None, honours_timeout=True):
        self.inner, self.raises, self.sleep, self.hits, self.honours_timeout = inner, raises, sleep, hits, honours_timeout
        self.calls = self.active = self.peak = 0
        self.lock = threading.Lock()

    def search(self, query, top_k, topics, timeout_seconds):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.sleep:
                if self.honours_timeout and self.sleep > timeout_seconds:
                    time.sleep(timeout_seconds)
                    raise TimeoutError("the provider gave up at its budget")
                time.sleep(self.sleep)
            if self.raises:
                raise self.raises
            return self.hits if self.hits is not None else self.inner.search(query, top_k, topics, timeout_seconds)
        finally:
            with self.lock:
                self.active -= 1


class Lazy:
    """A provider whose results are a generator: its work happens only as they are read."""

    def __init__(self, inner=None, delay=0.0, first_delay=None, endless=False, fail_after=None, hits=None):
        self.inner, self.delay, self.first_delay, self.endless = inner, delay, first_delay, endless
        self.fail_after, self.hits = fail_after, hits
        self.calls = self.pulled = self.active = self.peak = self.closed = 0
        self.lock = threading.Lock()
        self.generators = []

    def search(self, query, top_k, topics, timeout_seconds):
        with self.lock:
            self.calls += 1
        source = self.hits if self.hits is not None else self.inner.search(query, top_k, topics, timeout_seconds)

        def produce():
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                i = 0
                while self.endless or i < len(source):
                    time.sleep(self.first_delay if i == 0 and self.first_delay is not None else self.delay)
                    if i == self.fail_after:
                        raise RuntimeError("the provider broke mid-stream")
                    with self.lock:
                        self.pulled += 1
                    yield source[i % len(source)]
                    i += 1
            finally:
                with self.lock:
                    self.active -= 1
                    self.closed += 1

        generator = produce()
        self.generators.append(generator)
        return generator


class Topics:
    """A provider that records the topic filter it was given, may try to change it, and returns what it likes."""

    def __init__(self, returns, meddle=None):
        self.returns, self.meddle, self.seen = returns, meddle, []

    def search(self, query, top_k, topics, timeout_seconds):
        self.seen.append(topics)
        if self.meddle:
            self.meddle(topics)
        return list(self.returns)


def blocked(change) -> bool:
    """True when the attempted change was refused."""
    try:
        change()
        return False
    except (TypeError, AttributeError):
        return True


_DEFAULT = object()


def turn(text, search, domains=_DEFAULT, turn_risk=_DEFAULT, config=TEST, srcs=None, cards=_DEFAULT) -> rt.Evidence:
    """One turn with sound defaults; any argument passed, None included, is used as given."""
    return rt.evidence_for_turn(text, ["general"] if domains is _DEFAULT else domains,
                                risk() if turn_risk is _DEFAULT else turn_risk, search,
                                CARDS if cards is _DEFAULT else cards, config, sources() if srcs is None else srcs)


def cited(ev: rt.Evidence) -> list[str]:
    return [c.card["id"] for c in ev.items]


def offline() -> None:
    index = rt.InMemoryIndex(CARDS, fake_embed, sources())
    assert [c["id"] for c in index.cards] == USABLE, [c["id"] for c in index.cards]
    settings.RAG_ENABLED = True
    try:
        _checks(index)
    finally:
        settings.RAG_ENABLED = False
    print("offline: gate, malformed and inconsistent risk, permissions, draft marker, revalidation, ranking, "
          "abstention, config bounds, timeouts and bursts, lazy providers, topic constraints, frozen index and "
          "results, citations and crisis bypass checks passed")


def _checks(index) -> None:
    _permissions(index)
    _draft_marker(index)
    _revalidation(index)
    _ranking(index)
    _failures(index)
    _malformed_risk(index)
    _inconsistent_risk(index)
    _crisis_bypass(index)
    _config_bounds(index)
    _frozen_results(index)
    _citations(index)
    _production_untouched(index)
    _topic_constraints(index)
    _index_immutability(index)
    _timeouts_and_bursts(index)  # last: these sleep
    _lazy_providers(index)


def _topic_constraints(index) -> None:
    worry, activity = index.cards[0], index.cards[2]  # topics: overthinking / low_mood
    probe = Topics([rt.Hit(activity, 0.9)])
    assert cited(turn("x", probe, domains=["low_mood"])) == ["fx-activity"]
    assert probe.seen == [frozenset({"low_mood"})] and type(probe.seen[0]) is frozenset, probe.seen
    assert turn("x", Topics([rt.Hit(worry, 0.9)]), domains=["general"]).ids == ["S1"], "general: no topic filter"
    # It cannot clear or widen the filter it was given...
    for meddle in (lambda t: t.clear(), lambda t: t.add("overthinking"), lambda t: t.update({"overthinking"})):
        meddler = Topics([rt.Hit(worry, 0.9)], meddle)
        ev = turn("x", meddler, domains=["low_mood"])
        assert ev.items == () and ev.reason == "error: AttributeError", ev
        assert meddler.seen == [frozenset({"low_mood"})]
    # ...and whatever it sends back is checked against the topics that were asked for.
    assert turn("x", Topics([rt.Hit(worry, 0.9)]), domains=["low_mood"]).reason == "abstain: nothing qualified"
    assert cited(turn("x", Topics([rt.Hit(worry, 0.95), rt.Hit(activity, 0.9)]), domains=["low_mood"])) == ["fx-activity"]
    assert cited(turn("x", Topics([rt.Hit(worry, 0.95), rt.Hit(activity, 0.9)]),
                      domains=["low_mood", "overthinking"])) == ["fx-worry", "fx-activity"], "any shared topic matches"
    # Malformed topic metadata on a card fails closed.
    for bad in ("low_mood", [1], [], None, ["low_mood", None]):
        cards = [dict(c, topics=bad) if c["id"] == "fx-activity" else c for c in CARDS]
        assert turn("x", Topics([rt.Hit(activity, 0.9)]), domains=["low_mood"], cards=cards).items == (), bad


def _index_immutability(index) -> None:
    assert type(index.cards) is tuple
    first = index.cards[0]
    assert blocked(lambda: index.cards.__setitem__(0, index.cards[1]))
    assert blocked(lambda: index.cards.append(first))
    assert blocked(lambda: setattr(index, "cards", ()))
    assert blocked(lambda: index.cards[0].__setitem__("text", "changed"))
    assert blocked(lambda: index.cards[0]["topics"].append("x"))
    assert index.cards[0] is first and [c["id"] for c in index.cards] == USABLE

    # Nested sets and mappings are copied and frozen, so the caller's originals are not shared.
    tags = {"calm", "sleep"}
    original = card("fx-tagged", ["overthinking"], "worry night sleep racing",
                    tags=tags, meta={"notes": ["a"], "kinds": {"b"}})
    snapshot = copy.deepcopy(original)
    own = rt.InMemoryIndex([original], fake_embed, sources())
    tags.add("changed")
    original["meta"]["notes"].append("changed")
    original["meta"]["kinds"].add("changed")
    frozen = own.cards[0]
    assert frozen["tags"] == frozenset({"calm", "sleep"}) and frozen["meta"]["kinds"] == frozenset({"b"})
    assert frozen["meta"]["notes"] == ("a",)
    assert blocked(lambda: frozen["tags"].add("x")) and blocked(lambda: frozen["meta"]["kinds"].add("x"))
    assert blocked(lambda: frozen["meta"].__setitem__("notes", ()))
    ev = turn("worry at night", own, cards=[snapshot])
    assert cited(ev) == ["fx-tagged"] and blocked(lambda: ev.items[0].card["tags"].add("x")), ev
    assert ev.items[0].card["tags"] == frozenset({"calm", "sleep"})
    assert turn("worry at night", own, cards=[original]).items == (), "the edited original no longer matches the index"
    # A value that could not be frozen makes the card unusable.
    for odd in (bytearray(b"x"), object(), [bytearray(b"x")], {"k": object()}, {1: "non-text key"}):
        assert rt.usable_cards([card("fx-odd", ["overthinking"], "worry", extra=odd)], sources()) == [], odd


def _lazy_providers(index) -> None:
    quick = rt.RetrievalConfig(0.3, "test", 3, 0.1)

    # A slow generator: its result arrives after the deadline and is refused.
    slow = Lazy(index, delay=0.3)
    start = time.perf_counter()
    ev = turn("worry at night", slow, config=quick)
    took = time.perf_counter() - start
    assert ev.items == () and ev.reason == "timeout: the provider overran its budget" and took < 0.5, (ev, took)
    assert slow.pulled == 1 and slow.closed == 1, (slow.pulled, slow.closed)

    # Fast first, then slow: no partial evidence once the deadline has passed.
    stalls = Lazy(index, first_delay=0.0, delay=0.3)
    ev = turn("worry thought evidence night", stalls, config=rt.RetrievalConfig(0.0, "test", 3, 0.1))
    assert ev.items == () and ev.reason == "timeout: the provider overran its budget" and stalls.pulled == 2, ev

    # An endless generator: reading stops at MAX_RESULTS + 1 and the whole result is refused.
    endless = Lazy(index, endless=True)
    ev = turn("worry at night", endless)
    assert ev.reason == f"error: more than {rt.MAX_RESULTS} results", ev
    assert endless.pulled == rt.MAX_RESULTS + 1 and endless.closed == 1, endless.pulled

    # Every generator is closed when reading stops: none can resume after its request returned.
    for provider in (slow, stalls, endless):
        pulled = provider.pulled
        assert next(provider.generators[-1], "closed") == "closed" and provider.pulled == pulled

    # Broken or malformed streams: no evidence, generator closed.
    broken = Lazy(index, fail_after=1)
    assert turn("worry at night", broken).reason == "error: RuntimeError" and broken.closed == 1
    junk = Lazy(hits=["junk", None, rt.Hit(None, 0.9)])
    assert turn("worry at night", junk).reason == "abstain: nothing qualified" and junk.closed == 1

    # A burst of lazy providers: their work happens inside the slots, so the cap holds.
    lazy = Lazy(index, delay=0.05)
    n = rt.MAX_CONCURRENT_SEARCHES * 3
    barrier, reasons, lock = threading.Barrier(n), [], threading.Lock()

    def request():
        barrier.wait()
        reason = turn("worry thought evidence night", lazy, config=WIDE).reason or "evidence"
        with lock:
            reasons.append(reason)

    before = threading.active_count()
    threads = [threading.Thread(target=request) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pulled_when_done = lazy.pulled
    assert lazy.peak <= rt.MAX_CONCURRENT_SEARCHES, lazy.peak
    assert reasons.count("evidence") == lazy.calls and reasons.count("busy") == n - lazy.calls > 0, reasons
    time.sleep(0.3)
    assert lazy.pulled == pulled_when_done and lazy.active == 0 and lazy.closed == lazy.calls, \
        (lazy.pulled, pulled_when_done, lazy.active, lazy.closed, lazy.calls)
    assert threading.active_count() == before

    # Every slot is free again after all of it.
    taken = [rt._slots.acquire(blocking=False) for _ in range(rt.MAX_CONCURRENT_SEARCHES)]
    assert all(taken) and not rt._slots.acquire(blocking=False)
    for _ in taken:
        rt._slots.release()


def _permissions(index) -> None:
    assert [c["id"] for c in rt.usable_cards(CARDS, sources())] == USABLE
    problems = {c["id"]: rt.card_problems(c, {"invented-source"}) for c in CARDS}
    assert any("not clinically approved" in p for p in problems["fx-draft"])
    assert any("not cleared for ingestion" in p for p in problems["fx-uncleared"])
    assert any("missing locator" in p for p in problems["fx-broken"])
    assert [c["id"] for c in rt.usable_cards(CARDS + [copy.deepcopy(CARDS[0])], sources())] == USABLE[1:], \
        "a duplicated id is dropped"
    assert rt.usable_cards(CARDS) == [], "the real registry clears no source yet, so no card is usable"
    assert rt.InMemoryIndex(CARDS, fake_embed).cards == ()
    stale = Spy(hits=[rt.Hit(index.cards[0], 0.99)])  # a provider still holding a card from an uncleared source
    ev = turn("worry night", stale, srcs=[])
    assert ev.items == () and ev.reason == "abstain: no usable cards" and stale.calls == 0, ev
    revoked = Spy(hits=[rt.Hit(c, 0.99) for c in CARDS[3:]])
    assert turn("worry night", revoked).reason == "abstain: nothing qualified"
    assert turn("worry", index, srcs=[{"id": "broken"}]).reason == "abstain: no usable cards"


def _draft_marker(index) -> None:
    # A card still carrying the release gate's marker is a draft, whatever clinical_status says.
    for field in ("text", "title", "locator"):
        marked = card("fx-marked", ["overthinking"], "worry night")
        marked[field] = f"{marked[field]} {DRAFT_MARKER}"
        assert any("draft marker" in p for p in rt.card_problems(marked, {"invented-source"})), field
        assert rt.usable_cards([marked], sources()) == [], field
    marked = card("fx-marked", ["overthinking", DRAFT_MARKER], "worry night")
    assert rt.usable_cards([marked], sources()) == [], "in a topic too"
    # Marked after it was indexed: the index still has it, the current card set does not.
    now_marked = [dict(c, text=f"{DRAFT_MARKER}\n{c['text']}") if c["id"] == "fx-worry" else c for c in CARDS]
    assert "fx-worry" not in cited(turn("worry at night, cannot sleep", index, cards=now_marked))


def _revalidation(index) -> None:
    query = "worry at night, cannot sleep"
    assert cited(turn(query, index)) == ["fx-worry"]
    withdrawn = [dict(c, clinical_status="draft") if c["id"] == "fx-worry" else c for c in CARDS]
    assert "fx-worry" not in cited(turn(query, index, cards=withdrawn)), "approval is checked again before use"
    assert "fx-worry" not in cited(turn(query, index, cards=[c for c in CARDS if c["id"] != "fx-worry"]))
    edited = [dict(c, text=c["text"] + " edited") if c["id"] == "fx-worry" else c for c in CARDS]
    assert "fx-worry" not in cited(turn(query, index, cards=edited)), "a card changed since indexing is stale"
    assert turn(query, index, srcs=sources()[1:]).items == (), "permission revoked: only the uncleared source is left"


def _ranking(index) -> None:
    hits = index.search("worry at night, cannot sleep", 3, None, 1.0)
    assert hits[0].card["id"] == "fx-worry" and hits == sorted(hits, key=lambda h: -h.score), hits
    first, again = turn("worry at night, cannot sleep", index), turn("worry at night, cannot sleep", index)
    assert first.ids == ["S1"] and cited(first) == ["fx-worry"], first
    assert [(c.id, c.card["id"]) for c in first.items] == [(c.id, c.card["id"]) for c in again.items]
    assert {h.card["id"] for h in index.search("worry thought activity", 3, {"low_mood"}, 1.0)} == {"fx-activity"}
    assert cited(turn("small walk in the morning", index, domains=["low_mood"])) == ["fx-activity"]
    assert turn("worry night sleep racing", index, domains=["low_mood"]).items == (), "off-topic card never cited"
    assert len(turn("worry thought activity walk", index, config=WIDE).items) == 2
    flood = Spy(hits=[rt.Hit(index.cards[0], 0.9), rt.Hit(index.cards[0], 0.8), rt.Hit(index.cards[1], 0.7)])
    assert cited(turn("x", flood)) == ["fx-worry", "fx-thought"], "a card returned twice is cited once"
    assert turn("completely unrelated words here", index).reason == "abstain: nothing qualified"
    assert turn("worry", rt.InMemoryIndex([], fake_embed, sources()), cards=[]).reason == "abstain: no usable cards"
    spy = Spy(index)
    assert turn("worry at night", spy, config=rt.PRODUCTION).reason == "no calibrated threshold" and spy.calls == 0
    assert rt.PRODUCTION.threshold is None, "no production threshold before a labelled evaluation set"


def _failures(index) -> None:
    # Every failure is no evidence, never an exception that could reach the crisis fallback.
    for error in (RuntimeError("boom"), LLMError("down"), TimeoutError(), ValueError("bad vector")):
        ev = turn("worry", Spy(raises=error))
        assert ev.items == () and ev.reason == f"error: {type(error).__name__}", ev
    bad_hits = ["not a hit", rt.Hit({"id": ["fx-worry"]}, 0.9), rt.Hit(index.cards[0], math.nan),
                rt.Hit(index.cards[0], "0.9"), rt.Hit(index.cards[0], True), rt.Hit(None, 0.9)]
    assert turn("worry", Spy(hits=bad_hits)).reason == "abstain: nothing qualified"
    assert turn("worry", SimpleNamespace(search=lambda *args: None)).reason == "error: TypeError", "a provider returning None"
    for text in (None, "", "   ", 42):
        assert turn(text, index).reason == "no query", text
    for domains in (5, "overthinking", [None], {"a": 1}):
        assert turn("worry", index, domains=domains).reason == "invalid domains", domains
    for cards in (None, 5, [None, 7, "x"]):
        ev = turn("worry", index, cards=cards)
        assert ev.items == () and (ev.reason.startswith("error:") or ev.reason.startswith("abstain")), (cards, ev)
    assert turn("worry", object()).reason == "error: AttributeError", "a provider without search()"


def _malformed_risk(index) -> None:
    broken = Safety.model_construct(harm_type="banana", timing="none", danger_now="no", about="self",
                                    discloses=False, distress_now=False, asks_for_help=False)
    for bad in (None, "clear", {"tier": "clear", "level": None, "safety": NO_HARM},
                SimpleNamespace(tier="clear", level=None, safety=NO_HARM),
                rt.TurnRisk("clear", None, None), rt.TurnRisk("clear", None, {"harm_type": "none"}),
                rt.TurnRisk("clear", None, SimpleNamespace(harm_type="none", danger_now="no")),
                rt.TurnRisk("clear", None, broken), rt.TurnRisk("clear", None, Safety.model_construct()),
                rt.TurnRisk(None, None, NO_HARM), rt.TurnRisk("unknown", None, NO_HARM),
                rt.TurnRisk(["clear"], None, NO_HARM), rt.TurnRisk("clear", ["x"], NO_HARM), rt.TurnRisk("clear", "", NO_HARM)):
        spy = Spy(index)
        ev = turn("worry at night", spy, turn_risk=bad)  # must not raise
        assert ev.items == () and spy.calls == 0 and ev.reason.startswith("skipped:"), (bad, ev)


def _inconsistent_risk(index) -> None:
    # Each is a turn a missing or mismatched triage would wave through; none may retrieve.
    for bad, why in (
        (rt.TurnRisk("clear", None, safety(danger_now="yes")), "danger signal"),
        (rt.TurnRisk("clear", None, safety(danger_now="unclear")), "danger signal"),
        (risk("clear", safety(danger_now="unclear")), "danger signal"),  # triage itself says nothing here
        (rt.TurnRisk("clear", None, safety(distress_now=True)), "distress now"),
        (rt.TurnRisk("clear", None, safety(timing="ongoing")), "inconsistent safety"),
        (rt.TurnRisk("clear", None, safety(discloses=True)), "inconsistent safety"),
        (rt.TurnRisk("danger", None, NO_HARM), "risk tier (danger)"),  # triage would say tier 1
        (rt.TurnRisk("mild", None, NO_HARM), "risk tier (mild)"),
        (rt.TurnRisk("clear", crisis.TIER2, NO_HARM), "risk (tier2)"),
    ):
        spy = Spy(index)
        ev = turn("worry at night", spy, turn_risk=bad)
        assert spy.calls == 0 and ev.items == () and ev.reason == f"skipped: {why}", (bad, ev)
    assert cited(turn("worry at night", index, turn_risk=risk("clear", safety(asks_for_help=True)))) == ["fx-worry"]


def _crisis_bypass(index) -> None:
    # Real crisis.assess / crisis.triage outcomes: the provider is never asked.
    lexical = crisis.assess("I want to kill myself", None)
    for turn_risk, why in (
        (risk(lexical, None), "tier1"),
        (risk("clear", safety(harm_type="self_harm", timing="ongoing", danger_now="yes", discloses=True)), "tier1"),
        (risk("clear", safety(harm_type="physical_violence", timing="unclear", danger_now="unclear", discloses=True)), "clarify"),
        (risk("clear", safety(harm_type="emotional_abuse", timing="past", discloses=True)), "harm"),
        (risk("mild", NO_HARM), "risk tier (mild)"),
        (risk("clear", None), "no safety check"),
    ):
        spy = Spy(index)
        ev = turn("worry at night", spy, turn_risk=turn_risk)
        assert spy.calls == 0 and ev.items == () and ev.reason == f"skipped: {why}", (turn_risk, ev)
    assert crisis.triage("clear", NO_HARM) is None, "a turn with no risk has no triage level: the only one served"
    settings.RAG_ENABLED = False
    spy = Spy(index)
    assert turn("worry at night", spy).reason == "disabled" and spy.calls == 0
    settings.RAG_ENABLED = True


def _config_bounds(index) -> None:
    assert rt.config_problems(TEST) == [] and rt.config_problems(WIDE) == [] and rt.config_problems(rt.PRODUCTION) == []
    C = rt.RetrievalConfig
    bad = [None, "TEST", SimpleNamespace(threshold=0.3, calibration="x", top_k=3, timeout_seconds=1.0),
           C(-0.1, "t"), C(1.5, "t"), C(math.nan, "t"), C(math.inf, "t"), C("0.3", "t"), C(True, "t"),
           C(0.3, None), C(0.3, "   "),
           C(0.3, "t", 0), C(0.3, "t", -1), C(0.3, "t", rt.MAX_TOP_K + 1), C(0.3, "t", True), C(0.3, "t", 2.5), C(0.3, "t", "3"),
           C(0.3, "t", 3, 0), C(0.3, "t", 3, -1), C(0.3, "t", 3, 60), C(0.3, "t", 3, None), C(0.3, "t", 3, math.nan),
           C(0.3, "t", 3, True)]
    for config in bad:
        spy = Spy(index)
        ev = turn("worry at night", spy, config=config)
        assert rt.config_problems(config) and ev.reason == "invalid config" and spy.calls == 0, (config, ev)
    assert rt.config_problems(C(None, None, 0)), "bounds hold without a threshold too"


def _frozen_results(index) -> None:
    original = index.cards[0]["text"]
    errors, results = [], {}
    n = rt.MAX_CONCURRENT_SEARCHES  # all at once, none turned away as busy
    barrier = threading.Barrier(n)

    def request(n):
        query = "worry at night, cannot sleep" if n % 2 else "small walk in the morning"
        barrier.wait()
        ev = turn(query, index)
        results[n] = (query, cited(ev))
        for attempt in (lambda: ev.items[0].card.__setitem__("text", "changed"),
                        lambda: ev.items[0].card["topics"].append("x"),
                        lambda: ev.items[0].source.__setitem__("title", "changed")):
            try:
                attempt()
                errors.append(f"request {n} changed its evidence")
            except (TypeError, AttributeError):
                pass

    threads = [threading.Thread(target=request, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(results) == n, (errors, results)
    assert all(ids == (["fx-worry"] if "worry" in q else ["fx-activity"]) for q, ids in results.values()), results
    assert index.cards[0]["text"] == original and CARDS[0]["text"].endswith("racing")
    mine = copy.deepcopy(CARDS)
    own_index = rt.InMemoryIndex(mine, fake_embed, sources())
    mine[0]["text"] = "edited after indexing"
    assert own_index.cards[0]["text"] != "edited after indexing", "the index keeps its own copy"


def _citations(index) -> None:
    ev = turn("worry thought evidence night", index, config=WIDE)
    assert ev.ids == ["S1", "S2"], ev
    text, kept = rt.enforce_citations("Try a worry period [S1]. A study proves it [S7]. See [S2, S9] and [s2].",
                                      ["S1", "S7", "S9"], ev)
    assert text == "Try a worry period [S1]. A study proves it. See [S2] and [S2].", text
    assert kept == ["S1", "S2"], kept
    clean = lambda raw: rt.enforce_citations(raw, [], ev)[0]  # noqa: E731

    # Ids retrieved this turn: near misses become [S1]; anything else in that shape goes.
    for raw, want in (("Try it (S1).", "Try it [S1]."), ("Try [S 1].", "Try [S1]."), ("Try [s2].", "Try [S2]."),
                      ("Both [S1, S9].", "Both [S1]."), ("Try this [S9].", "Try this."), ("Try [S 9].", "Try."),
                      ("Try it (S9).", "Try it."), ("Try [S0] or [S01].", "Try or.")):
        assert clean(raw) == want, (raw, clean(raw))

    # Invented citation formats that mean nothing else in a reply are removed, with the one space before them.
    for raw, want in (("As noted [source 4].", "As noted."), ("As noted [Ref: WHO 2020].", "As noted."),
                      ("As noted [Citation needed].", "As noted."), ("[Citation needed] maybe.", "maybe."),
                      ("CBT helps[^1].", "CBT helps."), ("CBT helps [^note].", "CBT helps."), ("CBT helps【1】.", "CBT helps."),
                      ("This is well known (Beck, 2011).", "This is well known."),
                      ("Studies agree (Beck et al., 2019).", "Studies agree."),
                      ("Studies agree (Beck & Emery, 1985).", "Studies agree."),
                      ("Studies agree (Clark and Wells 1995).", "Studies agree."),
                      ("Guidance says so (WHO, 2020).", "Guidance says so."),
                      ("See (Beck, 2011, p. 45) and (Beck, 2011; Clark, 1986).", "See and.")):
        assert clean(raw) == want, (raw, clean(raw))

    # Ordinary text is left exactly as it was: scales, options, code, URLs, asides, Roman Urdu spacing.
    ordinary = ("Rate your mood [0-10] tonight.", "Option [2] felt easier.", "Code: arr[2] = x", "I [laughs] felt better.",
                "Pick [a] or [b].", "(Source of stress: work)", "[Source of stress: work]", "My S9 phone broke.",
                "Do step 1 (see step 2).", "Rate it 1-10.", "Use [brackets] freely.", "Season 2 (2019) was good.",
                "Visit https://example.org/[1]/page now.", "See https://example.org/a?b[2]=c&d=[S9] here",
                "Open www.example.org/notes_(S9) later", "Use `arr[S9]` or `x[^1]` in code.",
                "```\ny = data[S9] + note[^1]\n```", "Ramzan (2024 mein) sab badla.",
                "Kya aap theek hain ?  Main  yahin hun .", "Acha , phir kya hua ?\nBatayein  na .")
    for text in ordinary:
        assert rt.enforce_citations(text, [], ev) == (text, []), (text, clean(text))
        assert rt.enforce_citations(text, [], rt.Evidence()) == (text, []), text

    # Removing a marker changes nothing else: no global whitespace or punctuation rewriting.
    for raw, want in (("Kya aap theek hain ? [S9] Main  yahin hun .", "Kya aap theek hain ? Main  yahin hun ."),
                      ("A  B [S9] .", "A  B ."), ("[S9] Start here.", "Start here."),
                      ("Line one.\n[S9] Line two.", "Line one.\nLine two."), ("  Indented [S9] text", "  Indented text"),
                      ("word[S9] next", "word next"), ("Trailing [S9]", "Trailing")):
        assert clean(raw) == want, (raw, clean(raw))
    assert clean("Nothing to change here  .") == "Nothing to change here  ."

    # Only markers in the reply's own prose count: ids inside code or URLs are neither resolved nor cited.
    assert rt.enforce_citations("See https://example.org/[S1] and `[S2]`.", [], ev) == \
        ("See https://example.org/[S1] and `[S2]`.", [])

    # Known trade-offs, pinned so a change is deliberate:
    # bracketed numbers can't be told from scales and code, so an invented "[1]" stays;
    assert clean("CBT works for everyone [1].") == "CBT works for everyone [1]."
    assert clean("Steps help [2, 3] and [4-6].") == "Steps help [2, 3] and [4-6]."
    # a narrative author-year citation can't be cut without breaking the sentence;
    assert clean("Beck (2011) showed this.") == "Beck (2011) showed this."
    # a name and a year in brackets look like a citation, even when they are a place and a year;
    assert clean("We met there (Lahore, 2020).") == "We met there."
    # and source brackets with no number or "needed" are left, invented or not.
    assert clean("As noted [Sources: WHO].") == "As noted [Sources: WHO]."
    assert clean("As noted (beck, 2011).") == "As noted (beck, 2011).", "lower-case names are not matched"

    # Ids: from the text first, then the reply's own list; only this turn's; metadata kept apart from the text.
    assert rt.enforce_citations("As shown [S1].", ["S1"], rt.Evidence()) == ("As shown.", [])
    assert rt.enforce_citations("No markers at all.", [None, 5, " s1 "], ev) == ("No markers at all.", ["S1"])
    text, kept = rt.enforce_citations("First [S2], then (S1).", ["S1", "S9"], ev)
    assert (text, kept) == ("First [S2], then [S1].", ["S2", "S1"]), (text, kept)
    assert set(ev.citations(kept)) == {"S1", "S2"} and set(ev.citations(["S2"])) == {"S2"}
    assert ev.citations([]) == {} and ev.citations(["S9"]) == {}
    assert "example.org" not in text, "source metadata never enters the reply text"
    meta = ev.citations()
    assert meta["S1"]["card_id"] == ev.items[0].card["id"] and meta["S1"]["source_id"] == "invented-source"
    assert meta["S1"]["locator"] == ev.items[0].card["locator"] and meta["S1"]["source_url"] == "https://example.org/invented"
    block = ev.block()
    assert block.startswith("# Evidence for this reply") and "[S1] Test card" in block and "section fx-" in block
    assert rt.Evidence().block() is None


def _production_untouched(index) -> None:
    # On by default since 28 Oct 2026 (the owner's decision); the gate still uses only approved cards from cleared sources.
    assert Settings.model_fields["RAG_ENABLED"].default is True
    assert not hasattr(rt, "_POOL"), "no background thread pool"
    app_dir = pathlib.Path(rt.__file__).resolve().parents[1]

    def imports_retrieval(text: str) -> bool:
        # One-line and parenthesised "from app.core import (...)" lists alike.
        names = re.findall(r"^\s*from app\.core import (\([^)]*\)|[^\n]*)", text, re.M)
        return (any(re.search(r"\bretrieval\b", n) for n in names)
                or bool(re.search(r"^\s*(from app\.core\.retrieval|import app\.core\.retrieval)", text, re.M)))

    importers = sorted(p.relative_to(app_dir).as_posix() for p in app_dir.rglob("*.py")
                       if p.name != "retrieval.py" and imports_retrieval(p.read_text(encoding="utf-8")))
    # RAG Step A wires retrieval into AI Chat at one place only.
    assert importers == ["routers/entries.py"], f"retrieval may be imported only by app/routers/entries.py: {importers}"


def _timeouts_and_bursts(index) -> None:
    quick = rt.RetrievalConfig(0.3, "test", 3, 0.1)
    before = threading.active_count()
    start = time.perf_counter()
    ev = turn("worry", Spy(index, sleep=0.5), config=quick)  # honours its timeout: gives up at 0.1 s
    took = time.perf_counter() - start
    assert ev.reason == "error: TimeoutError" and took < 0.35, (ev, took)
    late = turn("worry", Spy(index, sleep=0.25, honours_timeout=False), config=quick)
    assert late.items == () and late.reason == "timeout: the provider overran its budget", late
    assert threading.active_count() == before, "retrieval starts no threads of its own"

    # A burst: at most MAX_CONCURRENT_SEARCHES searches at once, the rest refused at once, nothing queued.
    slow = Spy(index, sleep=0.2)
    n = rt.MAX_CONCURRENT_SEARCHES * 3
    barrier, reasons = threading.Barrier(n), []
    lock = threading.Lock()

    def request():
        barrier.wait()
        reason = turn("worry at night", slow).reason or "evidence"
        with lock:
            reasons.append(reason)

    start = time.perf_counter()
    threads = [threading.Thread(target=request) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    took = time.perf_counter() - start
    calls_when_done = slow.calls
    assert slow.peak <= rt.MAX_CONCURRENT_SEARCHES, slow.peak
    assert reasons.count("evidence") == calls_when_done and reasons.count("busy") == n - calls_when_done > 0, reasons
    assert took < 1.0, f"refused requests did not wait: {took:.2f}s"
    time.sleep(0.4)
    assert slow.calls == calls_when_done, "no search ran after its request had returned"
    assert threading.active_count() == before
    # Every slot is free again after successes, refusals, errors and timeouts.
    taken = [rt._slots.acquire(blocking=False) for _ in range(rt.MAX_CONCURRENT_SEARCHES)]
    assert all(taken) and not rt._slots.acquire(blocking=False)
    for _ in taken:
        rt._slots.release()


if __name__ == "__main__":
    offline()
