"""
RAG end to end on the app's own knowledge cards (app/knowledge/cards): the real loader and gate,
then retrieval, abstention, citations and failures on a small labelled query set.

  python -m evals.rag_eval               offline: FAKE hashed bag-of-words vectors. Checks the plumbing
                                         only; its scores say nothing about semantic quality.
  python -m evals.rag_eval --local NAME  genuine embeddings from a sentence-transformers model already on
                                         disk (local_files_only: nothing is downloaded)
  python -m evals.rag_eval --live        genuine embeddings via llm_client.embed (OPENAI_EMBEDDING_MODEL):
                                         paid API calls, so only with approval
  ... --rewrite                          each query first turned into a short English query (llm_client.search_query)

The cards are drafts from a source that is not cleared, so the real gate refuses every one. Scores come
from test-only approved copies, made in memory under the invented cleared test source; nothing is
written. The threshold found here is a measurement on 24 queries, not a production setting.
The held-out queries are the fairer measure: the main set's wording overlaps the cards' search_terms.
"""
import functools
import sys
import time

from app.core import knowledge as kn, llm_client, md_loader, retrieval as rt
from app.core.config import settings
from app.core.knowledge import DRAFT_MARKER
from evals import retrieval_eval as rx

# (query, the card that should come first, or None when nothing should be cited)
QUERIES = [
    ("How do I write down a thought and check the evidence for it?", "thought-record"),
    ("I always assume the worst thing will happen", "catastrophizing"),
    ("What if I lose my job and everything falls apart", "catastrophizing"),
    ("My friend didn't reply, she must hate me", "mind-reading"),
    ("If it's not perfect then I'm a total failure", "all-or-nothing"),
    ("I'm panicking and feel disconnected, how do I come back to the present", "grounding-54321"),
    ("My heart is racing, how should I breathe to calm down", "slow-breathing"),
    ("I have no energy and stay in bed all day doing nothing", "behavioural-activation"),
    ("I can't stop worrying, all day and at night too", "worry-time"),
    ("I have so many problems and don't know where to start", "problem-solving"),
    ("I must never make mistakes, I should be better than this", "should-statements"),
    ("mujhe kabhi rona nahi chahiye, mujhe strong hona chahiye", "should-statements"),
    ("How do I do a check-in?", "app-check-in"),
    ("thought journal kaise use karoon?", "app-thought-journal"),
    ("Where can I download my weekly report?", "app-insights-reports"),
    ("apna account kaise delete karoon", "app-settings-privacy"),
    ("mujhe lagta hai sab kuch kharab ho jaye ga", "catastrophizing"),
    ("raat bhar pareshani aur fikr rehti hai, neend nahi aati", "worry-time"),
    ("ghabrahat ho rahi hai, saans tez chal rahi hai", "slow-breathing"),
    ("kuch karne ka dil nahi karta, bistar se uthne ki himmat nahi", "behavioural-activation"),
    ("itne masle hain samajh nahi aata kahan se shuru karoon", "problem-solving"),
    ("woh mujhse naraz hogi, zaroor mere baare mein bura soch rahi hai", "mind-reading"),
    ("What's the capital of France?", None),
    ("Can you give me a good biryani recipe?", None),
    ("kal cricket match kitne baje hai?", None),
    ("Help me fix a bug in my Python code", None),
    ("What will the weather be like tomorrow?", None),
    ("mujhe naya phone lena hai, kaunsa acha hai?", None),
    ("Tell me a joke", None),
    ("How much is a flight to Dubai?", None),
]
# Written after the cards' search_terms, in other words than theirs; the threshold is never
# calibrated on these, so they show how well it holds up on phrasing the cards were not tuned to.
HELD_OUT = [
    ("dil ghabra raha hai aur theek se saans nahi aa rahi", "slow-breathing"),
    ("mujhe darr hai exam mein fail ho kar meri zindagi barbaad ho jaye gi", "catastrophizing"),
    ("subah se bistar par padi hoon, koi kaam shuru hi nahi hota", "behavioural-activation"),
    ("har waqt dimagh mein baatein ghoomti rehti hain, chain nahi milta", "worry-time"),
    ("meri behan ne baat nahi ki, pakka mujhse tang aa gayi hai", "mind-reading"),
    ("ek number kam aaya to lagta hai main kisi kaam ki nahi", "all-or-nothing"),
    ("zindagi mein itni mushkilat hain, samajh nahi aa raha kya hal nikalun", "problem-solving"),
    ("lagta hai main yahan hoon hi nahi, sab kuch ajeeb sa lag raha hai", "grounding-54321"),
    ("Everyone at work thinks I'm stupid, I can tell", "mind-reading"),
    ("I should be able to handle everything on my own", "should-statements"),
    ("subah ka mood app mein kahan record karna hai?", "app-check-in"),
    ("How do I make a fear ladder in the app?", "app-exposure-journal"),
    ("purani chats kahan milengi?", "app-history"),
    ("app mein saans wali exercise kahan hai", "app-calm-tools"),
    ("aaj khane mein kya banaun?", None),
    ("is saal Eid kis din hai?", None),
]
# For genuine embeddings only: below these, the cards or the model are not good enough yet.
TARGETS = {"top3": 0.8, "top1": 0.7, "abstain": 0.75}


def test_copies(cards) -> list[dict]:
    return [{**c, "clinical_status": "approved", "source_id": "invented-source"} for c in cards]


def gate(cards) -> list[dict]:
    """The real cards, the real gate. Returns the test-only approved copies."""
    ids = [c.get("id") for c in cards]
    assert len(ids) >= 20 and len(set(ids)) == len(ids), ids
    # The real registry: no clinical card passes; only app-help cards may, once the owner approves them.
    assert all(c["source_id"] == "reframe-app-help" for c in rt.usable_cards(cards)), "a clinical card passed the real gate"
    # That approval, simulated in memory: every app-help card passes, every clinical card is still refused.
    app = next(s for s in kn.load_sources() if s["id"] == "reframe-app-help")
    granted = {**app, "licence": {**app["licence"], "status": "verified", "verified_from": ["https://example.invalid/approval"]},
               "permissions": {use: "granted" for use in app["permissions"]},
               "permission_evidence": {"granted_by": "rag_eval", "granted_on": "2026-10-28", "reference": "simulation"}}
    registry = [s for s in kn.load_sources() if s["id"] != "reframe-app-help"] + [granted]
    approved = [{**c, "clinical_status": "approved"} if c["source_id"] == "reframe-app-help" else c for c in cards]
    passed = {c["id"] for c in rt.usable_cards(approved, registry)}
    assert passed == {c["id"] for c in cards if c["source_id"] == "reframe-app-help"} and len(passed) >= 12, passed
    copies = test_copies(cards)
    # Draft status and an uncleared source are each card's only problems.
    assert {c["id"] for c in rt.usable_cards(copies, rx.sources())} == set(ids)
    one = copies[0]
    for broken in ({**one, "clinical_status": DRAFT_MARKER}, {**one, "source_id": "reframe-original"},
                   {**one, "source_id": "invented-uncleared"}, {**one, "text": ""}, {**one, "topics": "overthinking"},
                   {k: v for k, v in one.items() if k != "locator"}, {**one, "text": one["text"] + " " + DRAFT_MARKER}):
        assert rt.usable_cards([broken], rx.sources()) == [], broken
    return copies


def calibrate(rows) -> float:
    """The threshold that gets the most queries right: right card first and above it, or nothing above it."""
    def right(t):
        return sum((want is None and score < t) or (want is not None and ids[:1] == [want] and score >= t)
                   for _, want, ids, score in rows)
    scores = sorted({s for *_, s in rows})
    candidates = [(a + b) / 2 for a, b in zip(scores, scores[1:])] or scores
    return max(candidates, key=lambda t: (right(t), t))


def run(index, queries) -> list[tuple]:
    rows = []
    for text, want in queries:
        try:
            hits = index.search(text, 3, None, rt.MAX_TIMEOUT_SECONDS)
        except Exception as exc:  # a provider failure is scored as a miss, as AI Chat would get no evidence
            print(f"  provider failed ({type(exc).__name__}) on: {text[:60]}")
            hits = []
        rows.append((text, want, [h.card["id"] for h in hits], hits[0].score if hits else 0.0))
    return rows


def scores(rows, index, copies, config, label: str) -> dict:
    # Through evidence_for_turn, as AI Chat calls it: a clear turn, no topic filter.
    settings.RAG_ENABLED = True
    try:
        right = {True: [], False: []}
        for text, want, *_ in rows:
            evidence = rt.evidence_for_turn(text, ["general"], rx.risk(), index, copies, config, rx.sources())
            right[want is not None].append((evidence.items[0].card["id"] if evidence.items else None) == want)
    finally:
        settings.RAG_ENABLED = False
    for text, want, ids, score in rows:
        ok = (want is None and score < config.threshold) or (want is not None and ids[:1] == [want] and score >= config.threshold)
        print(f"  {'ok  ' if ok else 'MISS'} {score:.3f} {str(want):24} got {ids[:1]}  {text[:60]}")
    relevant = [r for r in rows if r[1]]
    result = {"top1": sum(r[2][:1] == [r[1]] for r in relevant) / len(relevant),
              "top3": sum(r[1] in r[2] for r in relevant) / len(relevant),
              "abstain": sum(right[False]) / len(right[False]),
              "cited_right": sum(right[True]) / len(right[True])}
    print(f"{label}: " + ", ".join(f"{k} {v:.3f}" for k, v in result.items()))
    return result


def evaluate(embed, mode: str, rewrite=None) -> list[dict]:
    copies = gate(md_loader.knowledge_cards())
    index = rt.InMemoryIndex(copies, embed, rx.sources())
    search = rt.RewrittenQuery(index, rewrite) if rewrite else index
    rows, held = run(search, QUERIES), run(search, HELD_OUT)
    threshold = round(calibrate(rows), 4)  # the main set only; the held-out set is scored with it
    config = rt.RetrievalConfig(threshold=threshold, calibration=f"rag_eval {mode}: {len(rows)} labelled queries, not production",
                                top_k=3, timeout_seconds=rt.MAX_TIMEOUT_SECONDS)
    print(f"{mode}: threshold {threshold}, worst relevant {min(r[3] for r in rows if r[1]):.3f}, "
          f"best irrelevant {max(r[3] for r in rows if not r[1]):.3f}")
    return [scores(rows, search, copies, config, f"{mode}, main set"), scores(held, search, copies, config, f"{mode}, held-out set")]


def safety_paths() -> None:
    """Embedder-independent: risky turns, citations, old ids, provider and build failures."""
    copies = test_copies(md_loader.knowledge_cards())
    index = rt.InMemoryIndex(copies, rx.fake_embed, rx.sources())
    wide = rt.RetrievalConfig(threshold=0.0, calibration="rag_eval: any hit", top_k=3, timeout_seconds=1.0)
    query = "I can't stop worrying at night"
    settings.RAG_ENABLED = True
    try:
        evidence = rt.evidence_for_turn(query, ["overthinking"], rx.risk(), index, copies, wide, rx.sources())
        assert evidence.ids == ["S1", "S2", "S3"], evidence.reason

        # Risky turns never search.
        for risk in (rx.risk("danger"), rx.risk("mild"), rx.risk("clear", rx.safety(harm_type="self_harm", timing="ongoing", danger_now="yes")),
                     rx.risk("clear", None)):
            spy = rx.Spy(index)
            skipped = rt.evidence_for_turn(query, ["overthinking"], risk, spy, copies, wide, rx.sources())
            assert not skipped.items and skipped.reason.startswith("skipped") and not spy.calls, skipped.reason

        # Provider errors and a failing build are simply no evidence; a good build happens once.
        assert rt.evidence_for_turn(query, None, rx.risk(), rx.Spy(index, raises=RuntimeError("down")), copies, wide,
                                    rx.sources()).reason == "error: RuntimeError"
        builds = []

        def build(fail):
            builds.append(fail)
            if fail:
                raise llm_client.LLMError("no embedding model is configured")
            return index

        failing = rt.LazyIndex(lambda: build(True))
        for _ in range(2):
            assert rt.evidence_for_turn(query, None, rx.risk(), failing, copies, wide, rx.sources()).reason == "error: LLMError"
        lazy = rt.LazyIndex(lambda: build(False))
        for _ in range(3):
            assert rt.evidence_for_turn(query, None, rx.risk(), lazy, copies, wide, rx.sources()).ids == evidence.ids
        assert builds == [True, True, False], builds

        # The English rewrite is a search provider: a risky turn never reaches it, and its failures are no evidence.
        rewrites = []

        def rewrite(text, timeout_seconds):
            rewrites.append(text)
            return "worry at night, cannot sleep"

        def broken(text, timeout_seconds):
            raise llm_client.LLMError("search query failed")

        def slow(text, timeout_seconds):
            time.sleep(timeout_seconds + 0.05)
            return "worry"

        for risk in (rx.risk("danger"), rx.risk("mild"), rx.risk("clear", None)):
            assert not rt.evidence_for_turn(query, None, risk, rt.RewrittenQuery(index, rewrite), copies, wide, rx.sources()).items
        assert rewrites == [], rewrites
        assert rt.evidence_for_turn(query, None, rx.risk(), rt.RewrittenQuery(index, rewrite), copies, wide, rx.sources()).items
        assert rewrites == [query], rewrites
        assert rt.evidence_for_turn(query, None, rx.risk(), rt.RewrittenQuery(index, lambda t, s: " "), copies, wide,
                                    rx.sources()).reason == "abstain: nothing qualified"
        assert rt.evidence_for_turn(query, None, rx.risk(), rt.RewrittenQuery(index, broken), copies, wide,
                                    rx.sources()).reason == "error: LLMError"
        fast = rt.RetrievalConfig(threshold=0.0, calibration="rag_eval: any hit", top_k=3, timeout_seconds=0.1)
        assert rt.evidence_for_turn(query, None, rx.risk(), rt.RewrittenQuery(index, slow), copies, fast,
                                    rx.sources()).reason == "error: TimeoutError"
    finally:
        settings.RAG_ENABLED = False

    # Only this turn's ids survive; old ids are hidden from the model.
    clean, kept = rt.enforce_citations("Try a worry period [S1], or this [S7] (Beck, 2011).", ["S1", "S7"], evidence)
    assert clean == "Try a worry period [S1], or this." and kept == ["S1"], (clean, kept)
    assert rt.without_ids("Try a worry period [S1]. Or that (S2) [s3].") == "Try a worry period. Or that."
    assert rt.without_ids("No ids [here], [0-10] stays") == "No ids [here], [0-10] stays"
    if not settings.OPENAI_EMBEDDING_MODEL:
        try:
            llm_client.embed(["x"], 1.0)
            raise AssertionError("embed ran with no embedding model configured")
        except llm_client.LLMError:
            pass
        try:
            llm_client.search_query("x", 1.0)
            raise AssertionError("search_query ran with no embedding model configured")
        except llm_client.LLMError:
            pass
    print("safety paths: risky turns skipped, errors give no evidence, lazy index built once, citations checked")


def local_embedder(name: str):
    from sentence_transformers import SentenceTransformer  # only for --local; not a requirement of the app

    model = SentenceTransformer(name, local_files_only=True)
    return lambda texts, timeout_seconds=None: model.encode(texts, normalize_embeddings=True).tolist()


if __name__ == "__main__":
    safety_paths()
    if "--live" in sys.argv or "--local" in sys.argv:
        mode = "live" if "--live" in sys.argv else f"local {sys.argv[sys.argv.index('--local') + 1]}"
        embed = llm_client.embed if mode == "live" else local_embedder(mode.split(" ", 1)[1])
        # Cached: each query is rewritten once, though it is searched twice (ranking, then evidence_for_turn).
        rewrite = functools.lru_cache(maxsize=None)(llm_client.search_query) if "--rewrite" in sys.argv else None
        results = evaluate(embed, mode + (" + English rewrite" if rewrite else ""), rewrite)
        below = [f"{k} ({name})" for name, result in zip(("main", "held-out"), results)
                 for k, target in TARGETS.items() if result[k] < target]
        print(f"genuine embeddings ({mode}): " + (f"BELOW TARGET on {below}" if below else "targets met"))
        sys.exit(1 if below else 0)
    evaluate(rx.fake_embed, "offline FAKE embedder")
    print("offline: plumbing passed. FAKE vectors: these scores do not measure semantic retrieval")
