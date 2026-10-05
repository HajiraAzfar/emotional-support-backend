"""
Accuracy of the NLP that classifies each message, against a labelled set
(evals/data/classification_set.csv: text, risk, topics, language):

  Crisis detection (FR-CRIS-002), three ways:
    lexical   the rule set alone (crisis_check.assess_lexical)
    model     the classifier's risk_tier alone (llm_client.classify)
    combined  what the app uses: the higher of the two (crisis.assess)
    → tier accuracy, and precision / recall / F1 for "needs crisis content"
      (danger or emergency) and for "any risk" (mild and above)
  Topic (skill) classification: the classifier's first topic, and any of its
    topics, against the acceptable topics (clear and mild messages only)
  Language identification (language.detect), strict and with Roman Urdu and
    mixed counted as one

The labels are a first draft and should be reviewed (the crisis ones by a
clinician). A labelled set this size measures the pipeline, not real-world
accuracy; say so wherever the numbers are reported.

Usage (from emotional-support-backend/, with OPENAI_API_KEY in .env):
    python -m evals.accuracy_eval
Per-message predictions are written to evals/results/accuracy_predictions.csv.
"""
import csv
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.core import crisis, crisis_check, language, llm_client

DATA = Path(__file__).parent / "data" / "classification_set.csv"
OUT = Path(__file__).parent / "results" / "accuracy_predictions.csv"
TIERS = ("clear", "mild", "danger", "emergency")
CRISIS = {"danger", "emergency"}


def prf(rows, predicted_key, positive):
    """Precision, recall, F1 for a binary question: is the tier in `positive`?"""
    tp = sum(1 for r in rows if r[predicted_key] in positive and r["risk"] in positive)
    fp = sum(1 for r in rows if r[predicted_key] in positive and r["risk"] not in positive)
    fn = sum(1 for r in rows if r[predicted_key] not in positive and r["risk"] in positive)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1, fn


def pct(n, d):
    return f"{100 * n / d:5.1f}%  ({n}/{d})" if d else "  n/a"


def main() -> None:
    with open(DATA, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # The classifier is the only API call; a few in parallel keeps the run short.
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda row: llm_client.classify([], row["text"], None), rows))

    for row, result in zip(rows, results):
        row["acceptable_topics"] = set(row["topics"].split("|"))
        row["lexical"] = crisis_check.assess_lexical(row["text"])
        row["model"] = result.risk_tier or "unavailable"
        row["combined"] = crisis.assess(row["text"], result.risk_tier)
        row["predicted_topics"] = result.domains or []
        row["predicted_language"] = language.detect(row["text"]) or "none"

    n = len(rows)
    print(f"\n=== Crisis detection — {n} messages "
          f"({', '.join(f'{t} {sum(r['risk'] == t for r in rows)}' for t in TIERS)})\n")
    unavailable = sum(r["model"] == "unavailable" for r in rows)
    print(f"Classifier returned nothing for {unavailable} message(s) "
          "(blocked by the content filter or failed) — the lexical tier stands alone there.\n")

    print(f"{'':10} {'tier accuracy':>22}   {'crisis precision':>16} {'recall':>8} {'F1':>6}   {'any-risk recall':>15}")
    for key in ("lexical", "model", "combined"):
        exact = sum(r[key] == r["risk"] for r in rows)
        p, rcl, f1, _ = prf(rows, key, CRISIS)
        _, any_recall, _, _ = prf(rows, key, CRISIS | {"mild"})
        print(f"{key:10} {pct(exact, n):>22}   {p:16.2f} {rcl:8.2f} {f1:6.2f}   {any_recall:15.2f}")

    print("\nConfusion matrix, combined (rows = true tier, columns = predicted):")
    print(f"{'':12}" + "".join(f"{t:>11}" for t in TIERS))
    for true in TIERS:
        counts = Counter(r["combined"] for r in rows if r["risk"] == true)
        print(f"{true:12}" + "".join(f"{counts.get(t, 0):>11}" for t in TIERS))

    missed = [r for r in rows if r["risk"] in CRISIS and r["combined"] not in CRISIS]
    print(f"\nCrisis messages the app would NOT treat as a crisis ({len(missed)}):")
    for r in missed:
        print(f"  [{r['risk']} → lexical {r['lexical']}, model {r['model']}] {r['text']}")
    false_alarms = [r for r in rows if r["risk"] not in CRISIS and r["combined"] in CRISIS]
    print(f"\nFalse alarms: non-crisis messages treated as a crisis ({len(false_alarms)}):")
    for r in false_alarms:
        print(f"  [{r['risk']} → lexical {r['lexical']}, model {r['model']}] {r['text']}")

    topic_rows = [r for r in rows if r["risk"] in ("clear", "mild") and r["predicted_topics"]]
    scored = [r for r in rows if r["risk"] in ("clear", "mild")]
    first = sum(r["predicted_topics"][0] in r["acceptable_topics"] for r in topic_rows)
    any_hit = sum(bool(set(r["predicted_topics"]) & r["acceptable_topics"]) for r in topic_rows)
    print(f"\n=== Topic classification — {len(scored)} clear/mild messages, "
          f"{len(scored) - len(topic_rows)} with no topic returned\n")
    print(f"first topic acceptable      {pct(first, len(scored))}")
    print(f"any returned topic accept.  {pct(any_hit, len(scored))}")
    print("\nPer topic (recall of the first acceptable topic):")
    for topic in sorted({t for r in scored for t in r["acceptable_topics"]}):
        group = [r for r in scored if r["topics"].split("|")[0] == topic]
        if group:
            hits = sum(bool(set(r["predicted_topics"]) & r["acceptable_topics"]) for r in group)
            print(f"  {topic:22} {pct(hits, len(group))}")

    lang_rows = [r for r in rows if r["language"] != "any"]
    strict = sum(r["predicted_language"] == r["language"] for r in lang_rows)
    loose = {"roman_urdu": "urdu_latin", "mixed": "urdu_latin"}
    lenient = sum(loose.get(r["predicted_language"], r["predicted_language"])
                  == loose.get(r["language"], r["language"]) for r in lang_rows)
    print(f"\n=== Language identification — {len(lang_rows)} messages\n")
    print(f"strict                      {pct(strict, len(lang_rows))}")
    print(f"Roman Urdu = mixed          {pct(lenient, len(lang_rows))}")
    labels = ("english", "roman_urdu", "mixed", "urdu_script", "none")
    print(f"\n{'':12}" + "".join(f"{t:>12}" for t in labels))
    for true in labels[:-1]:
        counts = Counter(r["predicted_language"] for r in lang_rows if r["language"] == true)
        print(f"{true:12}" + "".join(f"{counts.get(t, 0):>12}" for t in labels))

    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["text", "risk", "lexical", "model", "combined",
                         "topics", "predicted_topics", "language", "predicted_language"])
        for r in rows:
            writer.writerow([r["text"], r["risk"], r["lexical"], r["model"], r["combined"],
                             r["topics"], "|".join(r["predicted_topics"]),
                             r["language"], r["predicted_language"]])
    print(f"\nPredictions written to {OUT}")


if __name__ == "__main__":
    main()
