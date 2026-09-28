"""
Check-in eval set — runs the real prompts against the configured model and
checks the SRS rules that can only be verified on actual output:

  FR-AIR-002  first message refers to something the user recorded
  FR-AIR-003  at most one question per reply
  FR-AIR-004  two minimal replies ("idk", "yeah") → session closes
  FR-AIR-009  asking to stop → session closes
  FR-AIR-010/011  closing messages contain no question
  FR-AIR-014/015  capture prompts request the right value, briefly
  FR-JRN-003  the extended free write: short replies, no replaying her message,
              a quiet shift is noticed, and the session tapers instead of stopping

Usage (from emotional-support-backend/, with OPENAI_API_KEY in .env):
    python -m evals.check_in_eval
Set LLM_MOCK=true to dry-run the harness without calling the API.
Rerun after every change to a prompt, skill or clinical file.
"""
from dataclasses import dataclass, field
from types import SimpleNamespace

from app.core import capture, context_builder, enforcement, language, llm_client
from app.core.config import settings

ACCOUNT = SimpleNamespace(work_issues=["overthinking"], life_vision="Sukoon se rehna", distress_baseline=6)
FOCUS = ["anxiety"]


@dataclass
class Case:
    name: str
    recorded: dict
    user_turns: list[str]
    must_mention: list[str]  # any one of these (case-insensitive) in the first reply
    expect_close_by: int | None = None  # user turn index (1-based) by which the session must close
    # (turn index, at least this many turns before closing): she moved here, so the
    # conversation should consolidate before it ends rather than stopping dead.
    taper_from: int | None = None
    results: list[str] = field(default_factory=list)


CASES = [
    Case(
        name="Roman Urdu throughout → replies stay in Roman Urdu",
        recorded={"mood": 2, "triggers": ["parents"], "trigger_note": "ammi se phir behas ho gayi shaadi ki baat pe",
                  "thinking_traps": ["should_statements"], "feelings": ["frustrated", "guilty"]},
        user_turns=["mujhe lagta hai main hi hamesha galat hoti hoon", "haan shayad woh bhi pareshan hain, sirf mera nahi socha"],
        must_mention=["ammi", "behas", "shaadi", "guilty", "frustrated", "should"],
    ),
    Case(
        name="low mood, work trigger, mind reading → user disengages",
        recorded={"mood": 2, "triggers": ["workload", "boss_or_teacher"], "trigger_note": "boss ne meeting mein ignore kiya",
                  "thinking_traps": ["mind_reading"], "feelings": ["hurt", "embarrassed"]},
        user_turns=["haan shayad woh mujh se naraz hai", "idk", "yeah"],
        must_mention=["boss", "meeting", "ignore", "mind reading", "naraz"],
        expect_close_by=3,
    ),
    Case(
        name="shift on turn 2 → consolidate, then close",
        recorded={"mood": 2, "triggers": ["boss_or_teacher"], "trigger_note": "boss ignored me in the meeting",
                  "thinking_traps": ["mind_reading"], "feelings": ["hurt"]},
        user_turns=["I think he's angry with me", "actually he was in back-to-back meetings all day, maybe it wasn't about me",
                    "yeah that does make more sense"],
        must_mention=["boss", "meeting", "ignored", "mind reading", "hurt"],
        expect_close_by=3,
        taper_from=2,
    ),
    Case(
        name="good day → user asks to stop",
        recorded={"mood": 5, "triggers": ["exams"], "trigger_note": "passed my exam!",
                  "thinking_traps": None, "feelings": ["proud", "relieved"]},
        user_turns=["it felt amazing honestly", "ok I have to go now, bye"],
        must_mention=["exam", "proud", "relieved", "passed"],
        expect_close_by=2,
    ),
    Case(
        name="overthinking loop repeated three times",
        recorded={"mood": 2, "triggers": ["a_decision"], "trigger_note": None,
                  "thinking_traps": ["what_if", "catastrophising"], "feelings": ["worried"]},
        user_turns=["what if I choose wrong", "but what if I choose the wrong one", "I just keep thinking what if it's wrong"],
        must_mention=["decision", "what if", "what-if", "worried", "choose"],
        expect_close_by=3,
    ),
]


def _language_ok(expected: str, got: str) -> bool:
    """Roman Urdu and a Roman Urdu/English mix are both fine replies to either."""
    if expected in (language.ROMAN_URDU, language.MIXED):
        return got in (language.ROMAN_URDU, language.MIXED)
    return got == expected


def run_capture_checks() -> list[str]:
    failures = []
    recorded: dict = {}
    previous = None
    values = capture.values_for("check_in", FOCUS)
    for spec in values:
        prompt = context_builder.build_capture_prompt(FOCUS, "check_in", capture.summary(values, recorded), spec["id"], previous)
        result = llm_client.generate_capture_prompt(prompt, spec["id"])
        if result is None:
            failures.append(f"capture {spec['id']}: no usable prompt (fallback would be used)")
        else:
            text = result.message_text
            if enforcement.count_questions(text) > 1:
                failures.append(f"capture {spec['id']}: more than one question: {text!r}")
            if len(text.split()) > 40:
                failures.append(f"capture {spec['id']}: too long ({len(text.split())} words)")
            # A scale prompt may name its range, but only the real one (FR-INS-009
            # added a 0-10 value next to the 1-5 mood scale, and the model guessed).
            if spec["control"] == "scale":
                options = capture.SCALES[spec["scale"]]
                low, high = options[0]["value"], options[-1]["value"]
                wrong_low = 1 if low == 0 else 0
                said = text.lower()
                if any(f"{wrong_low} {word} {high}" in said for word in ("to", "se")) or f"{wrong_low}-{high}" in said:
                    failures.append(f"capture {spec['id']}: states the wrong range for a {low}-{high} scale: {text!r}")
        value = {"mood": 2, "triggers": ["workload"], "trigger_note": "busy day", "thinking_traps": [],
                 "feelings": ["tired"], "feeling_intensity": 7}[spec["id"]]
        recorded[spec["id"]] = value or None
        previous = f"{spec['id']} = {capture.display_text(spec, value or None)}"
    return failures


def run_case(case: Case) -> list[str]:
    failures = []
    summary = capture.summary(capture.values_for("check_in", FOCUS), case.recorded)
    history: list[tuple[str, str]] = []
    closed_at = None
    stage = None

    turns = [None] + case.user_turns  # None = Echo speaks first
    for i, text in enumerate(turns):
        domains = llm_client.classify(history, text or summary, ACCOUNT.work_issues).domains or ["general"]
        close_reason = enforcement.required_closure(history, text, settings.CONVERSATION_CONTAINMENT_TURNS, stage)
        free_text = [case.recorded["trigger_note"]] if case.recorded.get("trigger_note") else []
        user_texts = free_text + [c for role, c in history if role == "user"] + ([text] if text else [])
        expected_lang = language.reply_language(user_texts)
        prompt = context_builder.build_system_prompt(
            ACCOUNT, FOCUS, domains, "check_in", summary, close_reason, expected_lang
        )
        outcome = enforcement.enforce(llm_client.generate_reflection(prompt, history, text), close_reason)
        stage = enforcement.CONFIRMING if outcome.shift_noticed else enforcement.EXPLORING
        reply = outcome.reply_text

        if i == 0 and not any(k.lower() in reply.lower() for k in case.must_mention):
            failures.append(f"first reply mentions nothing recorded (FR-AIR-002): {reply!r}")
        if enforcement.count_questions(reply) > 1:
            failures.append(f"turn {i}: more than one question (FR-AIR-003): {reply!r}")
        tum = language.TUM_WORDS.findall(reply)
        if tum:
            failures.append(f"turn {i}: informal address {sorted(set(w.lower() for w in tum))}, must be 'aap': {reply!r}")
        got_lang = language.detect(reply)
        if got_lang and not _language_ok(expected_lang, got_lang):
            failures.append(f"turn {i}: expected a {expected_lang} reply, got {got_lang}: {reply!r}")
        if outcome.session_end and enforcement.count_questions(reply):
            failures.append(f"turn {i}: closing message has a question (FR-AIR-010): {reply!r}")

        if case.taper_from is not None and i == case.taper_from and outcome.session_end:
            failures.append(f"closed abruptly on the turn she moved, with no consolidating reply: {reply!r}")

        if text is not None:
            history.append(("user", text))
        history.append(("ai", reply))
        if outcome.session_end:
            closed_at = i
            break

    if case.expect_close_by and (closed_at is None or closed_at > case.expect_close_by):
        failures.append(f"session not closed by user turn {case.expect_close_by} (closed at {closed_at})")
    return failures


SAVOURING_CASES = [
    ({"event": "My little brother called just to say he missed me", "significance": "we don't talk much lately",
      "feelings": ["loved", "grateful"]}, ["brother", "called", "missed"]),
    ({"event": "Ammi ne meri banayi chai ki tareef ki", "significance": "unse tareef kam milti hai",
      "feelings": ["proud"]}, ["ammi", "chai", "tareef"]),
]


def run_savouring_checks() -> list[str]:
    """FR-JRN-002 / FR-AIR-011: closing refers to the moment, no question, user's language, 'aap'."""
    failures = []
    for recorded, keywords in SAVOURING_CASES:
        texts = [recorded["event"], recorded["significance"]]
        lang = language.reply_language(texts)
        prompt = context_builder.build_closing_prompt(
            FOCUS, "savouring", capture.summary(capture.values_for("savouring", FOCUS), recorded), lang,
            capture.JOURNAL_SKILLS["savouring"]
        )
        text = llm_client.generate_closing(prompt)
        if text is None:
            failures.append(f"savouring {recorded['event'][:25]!r}: no usable closing")
            continue
        text = enforcement.limit_questions(text, allowed=0)
        if enforcement.count_questions(text):
            failures.append(f"savouring closing has a question: {text!r}")
        if not any(k in text.lower() for k in keywords):
            failures.append(f"savouring closing doesn't mention the moment: {text!r}")
        got = language.detect(text)
        if got and not _language_ok(lang, got):
            failures.append(f"savouring closing expected {lang}, got {got}: {text!r}")
        if language.TUM_WORDS.search(text):
            failures.append(f"savouring closing uses 'tum': {text!r}")
    return failures


# One extended free-write session, run turn by turn the way the router runs it.
FREE_WRITE_ENTRY = (
    "aaj phir wohi din tha. subah se sab theek chal raha tha phir ammi ka phone aaya aur shaadi ki "
    "baat pe behes ho gayi. mujhe samajh nahi aata main hi hamesha ghalat kyun hoti hoon. gaari mein "
    "bees minute baith kar roti rahi phir office chali gayi jaise kuch hua hi nahi."
)
FREE_WRITE_TURNS = [
    "pata nahi shayad main over-react karti hoon",
    # Several messages sent before Echo replies, as the app allows.
    "unhon ne kaha main khud-gharz hoon\n\nki main sirf apni sochti hoon\n\naur ye ke unhon ne meri khatir sab qurban kiya",
    # A quiet shift: she reads the other side. This must set shift_noticed.
    "shayad wo bhi dari hoi hain ke main door chali jaungi",
    "haan ab thora halka lag raha hai",
]
# A reply longer than this is summarising rather than reflecting.
FREE_WRITE_MAX_WORDS = 55


def run_free_write_checks() -> list[str]:
    """FR-JRN-003 with FR-AIR-003/009/011: the extended session, end to end."""
    failures: list[str] = []
    limits = capture.conversation_limits("free_write")
    summary = f"- account: <user_text>{FREE_WRITE_ENTRY}</user_text>"
    history: list[tuple[str, str]] = []
    stage = None
    shift_turn = closed_turn = None

    for index, text in enumerate([None] + FREE_WRITE_TURNS):
        close_reason = enforcement.required_closure(
            history, text, limits["containment_turns"], stage, minimal_replies=limits["minimal_replies"]
        )
        prompt = context_builder.build_system_prompt(
            ACCOUNT, FOCUS, ["relationship_issues"], "free_write", summary,
            close_reason=close_reason, reply_language=language.ROMAN_URDU,
            repeat_limit=limits["repeat_limit"],
        )
        outcome = enforcement.enforce(
            llm_client.generate_reflection(prompt, history, text),
            forced_closure=close_reason, repeat_limit=limits["repeat_limit"],
        )
        stage = enforcement.CONFIRMING if outcome.shift_noticed else enforcement.EXPLORING
        reply = outcome.reply_text

        if enforcement.count_questions(reply) > 1:
            failures.append(f"free write turn {index}: more than one question: {reply!r}")
        if len(reply.split()) > FREE_WRITE_MAX_WORDS:
            failures.append(f"free write turn {index}: {len(reply.split())} words — summarising: {reply!r}")
        if language.TUM_WORDS.search(reply):
            failures.append(f"free write turn {index} uses 'tum': {reply!r}")
        if outcome.shift_noticed and shift_turn is None:
            shift_turn = index
        if outcome.session_end and closed_turn is None:
            closed_turn = index
            if enforcement.count_questions(reply):
                failures.append(f"free write closing has a question: {reply!r}")
            break

        if text is not None:
            history.append(("user", text))
        history.append(("ai", reply))

    if shift_turn is None:
        failures.append("free write: her change of view was never noticed (shift_noticed stayed false)")
    if closed_turn is None:
        failures.append("free write: the session never closed, even after she said she felt lighter")
    elif shift_turn is not None and closed_turn <= shift_turn:
        failures.append(f"free write: closed on the shift turn ({closed_turn}) instead of tapering")
    return failures


def main() -> None:
    mode = "MOCK (harness dry-run)" if settings.LLM_MOCK else f"{settings.OPENAI_RESPONSE_MODEL} / {settings.OPENAI_CLASSIFIER_MODEL}"
    print(f"Check-in eval — {mode}\n")
    total_fail = 0

    fails = run_capture_checks()
    print(f"[{'PASS' if not fails else 'FAIL'}] capture prompts")
    for f in fails:
        print("    -", f)
    total_fail += len(fails)

    fails = run_savouring_checks()
    print(f"[{'PASS' if not fails else 'FAIL'}] savouring closing messages")
    for f in fails:
        print("    -", f)
    total_fail += len(fails)

    try:
        fails = run_free_write_checks()
    except llm_client.LLMError as exc:
        fails = [f"model call failed: {exc}"]
    print(f"[{'PASS' if not fails else 'FAIL'}] extended free-write session")
    for f in fails:
        print("    -", f)
    total_fail += len(fails)

    for case in CASES:
        try:
            fails = run_case(case)
        except llm_client.LLMError as exc:
            fails = [f"model call failed: {exc}"]
        print(f"[{'PASS' if not fails else 'FAIL'}] {case.name}")
        for f in fails:
            print("    -", f)
        total_fail += len(fails)

    print(f"\n{'All checks passed' if not total_fail else f'{total_fail} failure(s)'}")


if __name__ == "__main__":
    main()
