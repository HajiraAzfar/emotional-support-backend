import os, pathlib, sys, uuid
os.environ.update(DATABASE_URL="sqlite://", JWT_SECRET="x", RESEND_API_KEY="x")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from sqlalchemy import create_engine, JSON
from sqlalchemy.types import ARRAY
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

import app.models  # noqa
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
for t in Base.metadata.tables.values():
    for col in t.columns:
        if isinstance(col.type, ARRAY):
            col.type = JSON()
eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(eng)
S = sessionmaker(bind=eng)

from app.main import app
from app.models.account import Account
from app.models.entry import Entry
from app.models.message import Message
from app.models.captured_value import CapturedValue
from app.models.crisis_event import CrisisEvent
from app.models.focus_area import FocusArea
from app.models.user_term import UserTerm
from app.core import capture
from app.core import llm_client as lc
from app.core.config import settings
from app.core.crisis_check import assess_lexical, check_crisis

db = S()
acct = Account(email="t@x.com", work_issues=["overthinking"], life_vision="sukoon", distress_baseline=6)
db.add(acct); db.commit(); db.refresh(acct)
db.add(FocusArea(account_id=acct.id, code="anxiety")); db.commit()


def get_test_db():
    s = S()
    try:
        yield s
    finally:
        s.close()


app.dependency_overrides[get_db] = get_test_db
app.dependency_overrides[get_current_user] = lambda: S().get(Account, acct.id)
c = TestClient(app)
ok = 0


def check(cond, label):
    global ok
    assert cond, label
    ok += 1
    print("  ✓", label)


def post(url, body):
    r = c.post(url, json=body)
    assert r.status_code < 300, (url, r.status_code, r.json())
    return r.json()


def answer_check_in(u, **answers):
    """
    Answer whatever the check-in asks next, in whatever order it asks. The
    schedule decides which values come up at a given mood, so a helper that
    hardcoded the order would be testing the helper.
    """
    given = {"mood": 2, "feelings": ["tired"], "feeling_intensity": 6,
             "triggers": ["workload"], "trigger_note": "", "thinking_traps": [],
             "good_note": ""}
    given.update(answers)
    asked, state, replies = [], None, {}
    while True:
        r = c.get(u).json() if state is None else state
        spec = r.get("next_capture")
        if not spec:
            return r, asked, replies
        value_id = spec["value_id"]
        value = given.get(value_id)
        asked.append(value_id)
        state = post(u + "/captures", {"value_id": value_id, "value": value,
                                       "skipped": value in (None, "", [])})
        replies[value_id] = state


def complete_check_in(note="", traps=None):
    j = post("/entries", {"journal_type": "check_in"}); u = f"/entries/{j['id']}"
    j, _, replies = answer_check_in(u, mood=2, trigger_note=note, thinking_traps=traps or [])
    return u, j, replies["trigger_note"]


settings.OPENAI_API_KEY = None
print("--- lexical tiers")
for text, want in [("aaj acha din tha", "clear"), ("honestly what's the point", "mild"), ("I want to kill myself", "danger"),
                   ("I'm going to kill myself tonight", "emergency"), ("took all the sleeping pills", "emergency"),
                   ("dying to see this movie", "clear"), ("zindagi khatam, jeena nahi chahti", "danger"), ("mujhe ab jeene ka koi dil nahi karta", "danger"), ("aaj jeene ka maza aa gaya", "clear")]:
    check(assess_lexical(text) == want, f"{text!r} -> {want}")
check(check_crisis("I want to kill myself") and not check_crisis("what's the point"), "check_crisis() unchanged for danger, false for mild")

print("--- A: basic capture + validation (no key -> fallback wording)")
j = post("/entries", {"journal_type": "check_in"}); u = f"/entries/{j['id']}"
check(c.post(u + "/captures", json={"value_id": "triggers", "value": []}).status_code == 409, "out-of-order value -> 409")
check(c.post(u + "/captures", json={"value_id": "mood", "skipped": True}).status_code == 422, "required mood can't be skipped")
check(c.post(u + "/captures", json={"value_id": "mood", "value": 9}).status_code == 422, "mood outside scale -> 422")
u, j, _ = complete_check_in(note="work was a lot")
check(j["status"] == "completed" and j["conversation_status"] == "offered" and j["support_note"] is None, "clean entry completes with offer, no support note")

j = post("/entries", {"journal_type": "check_in"}); uf = f"/entries/{j['id']}"
j, asked, _ = answer_check_in(uf, mood=3, feelings=[])
check(j["status"] == "completed" and j["next_capture"] is None and "feeling_intensity" not in asked,
      "no feelings named -> no intensity question, entry completes (FR-INS-009)")

print("--- B: mild tier (FR-CRIS-005)")
u, j, last = complete_check_in(note="honestly what's the point")
check(last["crisis_event"]["tier"] == "mild" and last["crisis_event"]["text"] is None, "mild event carries no interrupting content")
check(not any(m["kind"] == "crisis" for m in j["messages"]), "no crisis message in thread for mild")
check(j["conversation_status"] == "offered" and j["support_note"], "offer still shown + support note at the end")

print("--- C: danger tier during capture (FR-CRIS-004/006)")
settings.LLM_MOCK = True
u, j, last = complete_check_in(note="I want to kill myself")
check(last["crisis_event"]["tier"] == "danger" and last["crisis_event"]["variant"] == "full", "danger event, full content first time")
check(j["status"] == "completed" and j["crisis_tier"] == "danger", "entry still completes (FR-CRIS-004)")
check(j["conversation_status"] == "suppressed" and not any(m["kind"] == "offer" for m in j["messages"]), "no conversation offer after danger")
check(c.post(u + "/conversation", json={"choice": "continue"}).status_code == 409, "continue refused after danger")
check(c.post(u + "/messages", json={"content": "hi"}).status_code == 409, "messages refused after danger")
u2, j2, last2 = complete_check_in(note="i want to kill myself")
check(last2["crisis_event"]["variant"] == "abbreviated", "second danger within 7 days -> abbreviated (FR-CRIS-012)")
s = S()
for ev in s.query(CrisisEvent).filter(CrisisEvent.tier == "danger").all():
    ev.created_at = datetime.utcnow() - timedelta(days=10)
s.commit()
u3, j3, last3 = complete_check_in(note="i want to kill myself")
check(last3["crisis_event"]["variant"] == "full", "danger after 10 days -> full again")
cols = {col.name for col in CrisisEvent.__table__.columns}
check(cols == {"id", "account_id", "entry_id", "field", "tier", "variant", "created_at"}, "detection record has no text column (FR-CRIS-013)")

print("--- D: emergency tier in free write")
j = post("/entries", {"journal_type": "free_write"}); uf = f"/entries/{j['id']}"
j = post(uf + "/captures", {"value_id": "account", "value": "I'm going to kill myself tonight"})
check(j["crisis_event"]["tier"] == "emergency" and "emergency number" in j["crisis_event"]["text"], "emergency event with fixed emergency content")
check(j["conversation_status"] == "suppressed", "no AI offered after emergency")

print("--- E: conversation (mock) + chat crisis")
u, j, _ = complete_check_in(note="long day")
post(u + "/conversation", {"choice": "continue"})
j = post(u + "/messages", {"content": "he always does this"})
check(j["conversation_status"] == "active", "conversation active")
j = post(u + "/messages", {"content": "sab khatam, I want to kill myself"})
check(j["crisis_event"]["tier"] == "danger" and j["conversation_status"] == "suppressed", "chat danger suppresses conversation")
check(j["messages"][-1]["kind"] == "crisis", "last message is fixed crisis content")
check(c.post(u + "/messages", json={"content": "hello?"}).status_code == 409, "no more AI turns after chat danger")

print("--- F: model stage + no generative calls once suppressed (mocked OpenAI)")
settings.LLM_MOCK = False; settings.OPENAI_API_KEY = "test"
calls = []
NO_HARM = lc.Safety(harm_type="none", timing="none", danger_now="no", about="self", discloses=False, distress_now=False, asks_for_help=False)
model = {"risk": "clear", "reflection_crisis": False, "shift": False, "safety": NO_HARM}


def parse(**kw):
    calls.append(kw["text_format"].__name__)
    model["instructions"] = kw.get("instructions")
    fmt = kw["text_format"]
    if fmt is lc.CapturePrompt:
        want = kw["instructions"].split("# Value to request\n")[1].strip()
        return SimpleNamespace(output_parsed=lc.CapturePrompt(value_id=want, message_text=f"AI asks {want}"))
    if fmt is lc.DomainResult:
        return SimpleNamespace(output_parsed=lc.DomainResult(safety=model["safety"], domains=["overthinking"], risk_tier=model["risk"]))
    if fmt is lc.ChatReply:
        # Scripted replies, in order, when a test sets them; otherwise a fixed one.
        text = model["replies"].pop(0) if model.get("replies") else "Model reply here."
        return SimpleNamespace(output_parsed=lc.ChatReply(crisis_indicators_noticed=model["reflection_crisis"], response_text=text, referral_flag=False))
    return SimpleNamespace(output_parsed=lc.Reflection(same_concern_count=1, shift_noticed=model.get("shift", False), response_text="Model reply here.", session_end=False, closure_reason="none",
                                                       referral_flag=False, crisis_indicators_noticed=model["reflection_crisis"]))


fake = MagicMock(); fake.responses.parse.side_effect = parse
with patch.object(lc, "_get_client", return_value=fake):
    model["risk"] = "danger"
    # A low mood, so the thinking patterns still come after the note.
    j = post("/entries", {"journal_type": "check_in"}); u = f"/entries/{j['id']}"
    for value_id, value in (("mood", 2), ("feelings", []), ("triggers", [])):
        post(u + "/captures", {"value_id": value_id, "value": value})
    calls.clear()
    j = post(u + "/captures", {"value_id": "trigger_note", "value": "sab theek hai bas"})  # lexical clear, model danger
    check(j["crisis_event"]["tier"] == "danger", "model tier wins when higher than rules (FR-CRIS-002)")
    check(calls == ["DomainResult"], f"after danger, no capture-prompt generation (calls: {calls})")
    check(j["messages"][-1]["content"] == capture.fallback_wording("check_in", "thinking_traps"), "next prompt uses stored wording")
    calls.clear()
    j = post(u + "/captures", {"value_id": "thinking_traps", "value": []})
    check(j["status"] == "completed", "the entry still finishes after a danger tier")
    check(calls == [], "zero generative calls for the rest of a suppressed entry")

    model["risk"] = "clear"
    u, j, _ = complete_check_in(note="work")
    post(u + "/conversation", {"choice": "continue"})
    model["reflection_crisis"] = True
    j = post(u + "/messages", {"content": "whatever"})
    check(j["messages"][-1]["kind"] == "crisis" and "Model reply" not in j["messages"][-1]["content"], "model-noticed risk: reply replaced by fixed content")
    check(j["conversation_status"] == "suppressed", "and conversation suppressed")
    model["reflection_crisis"] = False

print("--- F2: AI Chat keeps talking through a disclosure; the card is its own field under the reply")
from app.core import enforcement, md_loader  # noqa: E402


def safety(harm, timing, danger="no", distress=False, discloses=True):
    return lc.Safety(harm_type=harm, timing=timing, danger_now=danger, about="self", discloses=discloses,
                     distress_now=distress, asks_for_help=False)


def last_ai(j):
    return [m for m in j["messages"] if m["role"] == "ai"][-1]


LABELS = {"none", "soft", "prominent", "urgent"}

with patch.object(lc, "_get_client", return_value=fake):
    # N1–N5 from the 10 Oct test, replayed in order.
    model.update(risk="clear", reflection_crisis=False, safety=safety("emotional_abuse", "past"),
                 replies=["Living with that kind of abuse wears a person down. Are you safe now, and is he still in contact with you?"])
    u = f"/entries/{post('/entries', {'journal_type': 'chat'})['id']}"
    j = post(u + "/messages", {"content": "my husband was really abusive"})
    check(last_ai(j)["card"] is None and "Ask once, gently" in model["instructions"], "N1: one safety question, no card for past abuse")
    check("# Skill: abuse and violence" in model["instructions"], "the safety check loaded the abuse skill")
    # A follow-up, even one the classifier still files under violence, gets no card.
    model.update(safety=safety("physical_violence", "past", discloses=False), replies=["Getting yourself out of that marriage took real courage."])
    j = post(u + "/messages", {"content": "I am safe now I am out of that marrigge"})
    check(last_ai(j)["card"] is None, "N2: no card for a follow-up")
    check(not any(m["content"].strip().lower() in LABELS for m in j["messages"]) and "support" not in [m["kind"] for m in j["messages"]],
          "N3: no card label in the text, no separate card message")
    model.update(safety=NO_HARM, replies=["soft"])  # a reply that is nothing but a label
    j = post(u + "/messages", {"content": "Why u wrote soft"})
    check(last_ai(j)["content"].strip().lower() not in LABELS, "N4: a reply that is only a card label never reaches her")
    check("glitch on your" in model["instructions"], "N4: the honesty rule is in the prompt")
    copied = "That's a heavy thing to have lived with, and it was never on you."
    check(copied in md_loader.example_replies("skills", "abuse")[1], "the copied line really is a skill example")
    model.update(safety=safety("physical_violence", "past"),
                 replies=[f"{copied} Are you safe now, and out of that situation?",
                          "What he did to you was never okay, and none of it was your fault. Are you safe now?"])
    calls.clear()
    j = post(u + "/messages", {"content": "Ok he use to beat me"})
    n5 = last_ai(j)
    earlier = [m["content"] for m in j["messages"] if m["role"] == "ai" and m["id"] != n5["id"]]
    seen = earlier + md_loader.example_replies("journal_types", "chat") + md_loader.example_replies("skills", "abuse")
    check(calls.count("ChatReply") == 2, "N5: a draft copying a skill example is written again")
    check(not enforcement.repeated_sentences(n5["content"], seen), "N5: no full sentence from the examples or earlier replies")
    check("safe" not in n5["content"].lower() and "don't ask about safety" in model["instructions"], "N5: no second safety question")
    check(n5["card"] == "soft" and j["conversation_status"] == "active", "N5: soft card under the reply, chat open")
    model.update(safety=safety("physical_violence", "past"))
    j = post(u + "/messages", {"content": "he is not in my life now"})
    check(last_ai(j)["card"] is None, "once per chat: no second card")
    model.update(safety=safety("physical_violence", "ongoing", "unclear"))
    j = post(u + "/messages", {"content": "He beats me"})
    check(last_ai(j)["card"] == "prominent", "present violence: the same card again, prominent")

    # She says she is safe before Echo asks: no safety question on any later turn.
    model.update(safety=safety("physical_violence", "past"), replies=["Leaving took real strength. Are you safe now?"])
    u = f"/entries/{post('/entries', {'journal_type': 'chat'})['id']}"
    j = post(u + "/messages", {"content": "I am safe now, I left him last year"})
    check(not enforcement.count_questions(last_ai(j)["content"]), "safe-now: not asked on the same turn")
    model.update(safety=safety("physical_violence", "ongoing", "unclear"), replies=["That sounds unsettling. Are you safe when he does?"])
    j = post(u + "/messages", {"content": "he still messages me sometimes"})
    check("safe" not in last_ai(j)["content"].lower(), "safe-now: not asked on a later turn either")

    model.update(risk="mild", safety=safety("emotional_abuse", "past"))
    u = f"/entries/{post('/entries', {'journal_type': 'chat'})['id']}"
    j = post(u + "/messages", {"content": "hey i am was in an abusive marriage and I am going through ptsd now"})
    check(last_ai(j)["card"] is None and j["support_note"] is None, "R5: no card and no note on the first reply")
    model.update(risk="clear", safety=safety("emotional_abuse", "past", distress=True, discloses=False))
    j = post(u + "/messages", {"content": "No I just get scaredd"})
    check(last_ai(j)["card"] == "soft" and "ONE small, gentle idea" in model["instructions"], "scaredd: coping earned, soft card now")

    model.update(safety=NO_HARM, reflection_crisis=True)
    u = f"/entries/{post('/entries', {'journal_type': 'chat'})['id']}"
    j = post(u + "/messages", {"content": "whatever"})
    check(last_ai(j)["content"] == "Model reply here." and last_ai(j)["card"] == "prominent", "chat: model-noticed risk keeps the reply, card turns prominent")
    check(j["conversation_status"] == "active" and post(u + "/messages", {"content": "ok"})["conversation_status"] == "active", "and the chat goes on")
    model.update(reflection_crisis=False)

print("--- G: resume, list, delete")
settings.OPENAI_API_KEY = None
j = post("/entries", {"journal_type": "check_in"}); ud = f"/entries/{j['id']}"
post(ud + "/captures", {"value_id": "mood", "value": 4})
drafts = c.get("/entries", params={"status": "in_progress", "since_days": 7}).json()
check(drafts[0]["id"] == j["id"] and drafts[0]["mood"] == 4, "draft listed for resumption with mood")
g = c.get(ud).json()
check(g["next_capture"]["value_id"] == "feelings" and len(g["messages"]) == 3, "reopened draft resumes at the first unrecorded value")
done = c.get("/entries", params={"status": "completed"}).json()
check(all(e["status"] == "completed" for e in done) and done == sorted(done, key=lambda e: e["completed_at"], reverse=True), "completed list, newest first")
victim = done[0]["id"]
check(c.delete(f"/entries/{victim}").status_code == 204, "delete -> 204")
s = S(); vid = uuid.UUID(victim)
check(s.get(Entry, vid) is None and not s.query(Message).filter(Message.entry_id == vid).count()
      and not s.query(CapturedValue).filter(CapturedValue.entry_id == vid).count(), "entry, messages, values all gone (FR-ENT-008)")
check(c.get(f"/entries/{victim}").status_code == 404, "deleted entry is 404")

print("--- H: user terms + focus ordering")
lib = c.get("/libraries/triggers").json()
order = [cat["id"] for cat in lib["categories"]]
check(order[:3] == ["work_study", "money_practical", "inner"], f"anxiety-linked categories first: {order}")
check(sum(len(cat["items"]) for cat in lib["categories"]) == 40, "all 40 still reachable")
check(c.post("/libraries/thinking_traps/terms", json={"name": "x"}).status_code == 404, "traps library not extendable")
check(c.post("/libraries/feelings/terms", json={"name": "x" * 21}).status_code == 422, "21-char term refused")
t = post("/libraries/triggers/terms", {"name": "Rishtedaar"})
check(t["term"]["id"].startswith("u_") and t["crisis_event"] is None, "term added, clear")
t2 = post("/libraries/triggers/terms", {"name": "rishtedaar"})
check(t2["term"]["id"] == t["term"]["id"], "duplicate (case-insensitive) returns existing term")
lib = c.get("/libraries/triggers").json()
check(lib["categories"][0]["id"] == "my_words" and lib["categories"][0]["items"][0]["name"] == "Rishtedaar", "'My words' shown first")
bad = post("/libraries/feelings/terms", {"name": "want to die"})
check(bad["crisis_event"]["tier"] == "danger", "user term screened for crisis (FR-PICK-007)")
j = post("/entries", {"journal_type": "check_in"}); u = f"/entries/{j['id']}"
post(u + "/captures", {"value_id": "mood", "value": 3})
post(u + "/captures", {"value_id": "feelings", "value": []})
j = post(u + "/captures", {"value_id": "triggers", "value": [t["term"]["id"], "money"]})
check(j["messages"][-2]["content"] == "Rishtedaar, Money", "own term selectable and shown by name")

j = post("/entries", {"journal_type": "check_in"}); u = f"/entries/{j['id']}"
post(u + "/captures", {"value_id": "mood", "value": 1})
t3 = post("/libraries/triggers/terms", {"name": "end my life", "entry_id": j["id"]})
g = c.get(u).json()
check(t3["crisis_event"]["tier"] == "danger" and g["crisis_tier"] == "danger", "danger term added inside an entry raises that entry's tier")
check(g["messages"][-1]["kind"] == "crisis", "and puts fixed crisis content in the thread")
g, _, _ = answer_check_in(u, mood=1, feelings=[], triggers=[], trigger_note="", thinking_traps=[])
check(g["conversation_status"] == "suppressed", "so the entry ends without an AI offer")
other = S().query(UserTerm).filter(UserTerm.name == "Rishtedaar").one()
check(other.account_id == acct.id, "terms belong to the account that added them")

print("--- I: savouring journal (FR-JRN-002)")
settings.OPENAI_API_KEY = None; settings.LLM_MOCK = False
j = post("/entries", {"journal_type": "savouring"}); us = f"/entries/{j['id']}"
check(j["next_capture"]["value_id"] == "event" and j["next_capture"]["required"], "starts with the event (required free text)")
check(c.post(us + "/captures", json={"value_id": "event", "value": None, "skipped": True}).status_code == 422, "event can't be skipped")
j = post(us + "/captures", {"value_id": "event", "value": "Ammi ne meri chai ki tareef ki"})
check(j["next_capture"]["value_id"] == "significance" and not j["next_capture"]["required"], "then significance (optional)")
j = post(us + "/captures", {"value_id": "significance", "value": None, "skipped": True})
check(j["next_capture"]["library"] == "feelings" and j["next_capture"]["prefer_valence"] == "positive", "then feelings, positive first")
lib = c.get("/libraries/feelings", params={"prefer": "positive"}).json()
check([cat["valence"] for cat in lib["categories"] if cat["id"] != "my_words"][:2] == ["positive", "positive"], "library honours prefer=positive")
j = post(us + "/captures", {"value_id": "feelings", "value": ["proud", "loved"]})
last = j["messages"][-1]
check(j["status"] == "completed" and j["conversation_status"] == "closed", "completes and closes — no conversation")
check(last["kind"] == "closing" and last["content"].startswith("Thank you for noticing"), "fallback closing message without AI")
check(not any(m["kind"] == "offer" for m in j["messages"]), "no talk-it-through offer")
check(c.post(us + "/conversation", json={"choice": "continue"}).status_code == 409, "conversation refused")

settings.OPENAI_API_KEY = "test"
closing_calls = []


def parse_savour(**kw):
    fmt = kw["text_format"]
    if fmt is lc.CapturePrompt:
        want = kw["instructions"].split("# Value to request\n")[1].strip()
        return SimpleNamespace(output_parsed=lc.CapturePrompt(value_id=want, message_text=f"AI asks {want}"))
    if fmt is lc.DomainResult:
        return SimpleNamespace(output_parsed=lc.DomainResult(safety=NO_HARM, domains=["positive"], risk_tier="clear"))
    if fmt is lc.Closing:
        closing_calls.append(kw["instructions"])
        return SimpleNamespace(output_parsed=lc.Closing(message_text="Ammi ki tareef aapne sambhal li. Ek lamha ruk kar usay mehsoos kijiye. Kya aur kuch acha hua?"))
    raise AssertionError(fmt)


fake2 = MagicMock(); fake2.responses.parse.side_effect = parse_savour
with patch.object(lc, "_get_client", return_value=fake2):
    j = post("/entries", {"journal_type": "savouring"}); us2 = f"/entries/{j['id']}"
    post(us2 + "/captures", {"value_id": "event", "value": "Ammi ne meri banayi chai ki tareef ki"})
    post(us2 + "/captures", {"value_id": "significance", "value": "unse tareef kam milti hai"})
    j = post(us2 + "/captures", {"value_id": "feelings", "value": ["proud"]})
    last = j["messages"][-1]["content"]
    check("?" not in last and last.startswith("Ammi ki tareef"), f"AI closing kept, its question removed: {last!r}")
    check("savouring" in closing_calls[0].lower() and "Roman Urdu" in closing_calls[0] and "# Skill: positive moments" in closing_calls[0], "closing prompt has savouring rules, positive skill and Roman Urdu instruction")

settings.OPENAI_API_KEY = None
j = post("/entries", {"journal_type": "savouring"}); us3 = f"/entries/{j['id']}"
j = post(us3 + "/captures", {"value_id": "event", "value": "nothing good, I want to kill myself"})
check(j["crisis_event"]["tier"] == "danger", "crisis in the event is caught")
post(us3 + "/captures", {"value_id": "significance", "value": None, "skipped": True})
j = post(us3 + "/captures", {"value_id": "feelings", "value": []})
check(j["conversation_status"] == "suppressed" and not any(m["kind"] == "closing" for m in j["messages"]), "suppressed: saved-notice instead of AI closing")

print("--- L: winding down after a shift")
settings.LLM_MOCK = False; settings.OPENAI_API_KEY = "test"
with patch.object(lc, "_get_client", return_value=fake):
    model["risk"] = "clear"; model["shift"] = False
    u, j, _ = complete_check_in(note="boss ne ignore kiya")
    post(u + "/conversation", {"choice": "continue"})
    j = post(u + "/messages", {"content": "shayad woh mujh se naraz hai"})
    check(j["conversation_status"] == "active", "still exploring while she is stuck")
    model["shift"] = True
    j = post(u + "/messages", {"content": "haan shayad woh bhi pareshan the, sirf mera nahi socha"})
    check(j["conversation_status"] == "active", "she moved: Echo consolidates first, session not cut off")
    e = S().get(Entry, uuid.UUID(u.split("/")[-1]))
    check(e.conversation_stage == "confirming", "entry marked as confirming the shift")
    model["shift"] = False
    j = post(u + "/messages", {"content": "haan ab thoda halka lag raha hai"})
    check(j["conversation_status"] == "closed", "the very next reply closes the conversation")
    check("?" not in j["messages"][-1]["content"], "and the closing message has no question")
    e = S().get(Entry, uuid.UUID(u.split("/")[-1]))
    check(e.closure_reason == "revised_appraisal", f"closed as revised_appraisal (got {e.closure_reason})")
settings.OPENAI_API_KEY = None

print("--- L2: the extended free-write session")
from app.core import capture as cap
check(cap.conversation_limits("free_write") == {"repeat_limit": 5, "minimal_replies": 3, "containment_turns": 60},
      "free write runs longer than the default")
check(cap.conversation_limits("check_in") == {"repeat_limit": 3, "minimal_replies": 2, "containment_turns": None},
      "other journals keep the default limits")

settings.LLM_MOCK = False; settings.OPENAI_API_KEY = "test"
with patch.object(lc, "_get_client", return_value=fake):
    model["risk"] = "clear"; model["shift"] = False
    j = post("/entries", {"journal_type": "free_write"}); ue = f"/entries/{j['id']}"
    post(ue + "/captures", {"value_id": "account", "value": "bohot kuch chal raha hai andar"})
    post(ue + "/conversation", {"choice": "continue"})

    calls.clear()
    j = post(ue + "/messages", {"content": "mujhe samajh nahi aa raha kahan se shuru karon", "more": True})
    check(j["conversation_status"] == "active" and "Reflection" not in calls,
          f"'still writing' stores the message without a reply (calls: {calls})")
    check(j["messages"][-1]["role"] == "user", "her message is the last one in the thread")

    j = post(ue + "/messages", {"content": "bas sab ikattha ho gaya hai", "more": True})
    check(j["messages"][-1]["role"] == "user" and "Reflection" not in calls, "a second message also waits")

    j = post(ue + "/messages", {"content": "ab bata sakti hoon"})
    check(j["messages"][-1]["role"] == "ai" and "Reflection" in calls, "then Echo answers all of it at once")
    sent = [m for m in j["messages"] if m["role"] == "user" and m["kind"] == "chat"]
    check(len(sent) == 3, f"all three of her messages are in the thread ({len(sent)})")

    # FR-AIR-009: two "hmm"s end a check-in, but not an extended free write.
    j = post(ue + "/messages", {"content": "hmm"})
    j = post(ue + "/messages", {"content": "ok"})
    check(j["conversation_status"] == "active", "two minimal replies do not close a free write")
    j = post(ue + "/messages", {"content": "idk"})
    check(j["conversation_status"] == "closed", "the third one does")
    e = S().get(Entry, uuid.UUID(ue.split("/")[-1]))
    check(e.closure_reason == "minimal_replies", f"closed as minimal_replies (got {e.closure_reason})")
settings.OPENAI_API_KEY = None

print("--- J: thought journal (FR-JRN-004)")
settings.OPENAI_API_KEY = None; settings.LLM_MOCK = False
j = post("/entries", {"journal_type": "thought"}); ut = f"/entries/{j['id']}"
order = []
answers = {"name": "Meeting ke baad", "situation": "Team meeting mein sab ke saamne", "automatic_thought": "sab samajhte hain main nalayak hoon",
           "thinking_traps": ["mind_reading", "labelling"], "feelings": ["ashamed"],
           "emotions_before": {"ashamed": 90}, "emotions_after": {"ashamed": 50}, "supporting_evidence": "kisi ne meri baat nahi maani",
           "contradicting_evidence": "lead ne baad mein mera idea use kiya", "revised_thought": "shayad sab aisa nahi sochte"}
while j["next_capture"]:
    vid = j["next_capture"]["value_id"]; order.append(vid)
    j = post(ut + "/captures", {"value_id": vid, "value": answers[vid]})
check(order == ["name", "situation", "automatic_thought", "feelings", "emotions_before", "thinking_traps",
                "supporting_evidence", "contradicting_evidence", "revised_thought", "emotions_after"],
      f"the seven CBT stages in order, feelings before the patterns: {order}")
check(j["status"] == "completed" and j["conversation_status"] == "offered", "completes and offers the conversation")
check(not any(m["kind"] == "grounding" for m in j["messages"]), "no grounding message for a non-trauma user")
listed = [e for e in c.get("/entries", params={"status": "completed"}).json() if e["id"] == j["id"]][0]
check(listed["name"] == "Meeting ke baad", "entry name shown in the list (FR-ENT-009)")

j = post("/entries", {"journal_type": "thought"}); u61 = f"/entries/{j['id']}"
j = post(u61 + "/captures", {"value_id": "name", "value": None, "skipped": True})
check(j["next_capture"]["value_id"] == "situation" and j["next_capture"]["required"], "name is skippable, situation is required")
check(c.post(u61 + "/captures", json={"value_id": "situation", "value": None, "skipped": True}).status_code == 422, "situation can't be skipped normally")
check(c.post(u61 + "/captures", json={"value_id": "situation", "value": "x" * 2001}).status_code == 422, "too-long text refused")
s = S(); s.add(FocusArea(account_id=acct.id, code="trauma_ptsd")); s.commit()

print("--- K: thought trauma variant + grounding (FR-JRN-005, FR-JRN-008)")
j = c.get(u61).json()
check(j["next_capture"]["value_id"] == "situation" and not j["next_capture"]["required"], "with trauma focus, situation becomes optional")
order = []
j = post(u61 + "/captures", {"value_id": "situation", "value": None, "skipped": True})
while j["next_capture"]:
    vid = j["next_capture"]["value_id"]; order.append(vid)
    j = post(u61 + "/captures", {"value_id": vid, "value": answers[vid]})
check("supporting_evidence" not in order, f"supporting evidence is not requested: {order}")
check(j["conversation_status"] == "closed" and not any(m["kind"] == "offer" for m in j["messages"]), "trauma variant ends without a conversation")
check(j["messages"][-1]["kind"] == "grounding" and "five things" in j["messages"][-1]["content"], "fixed grounding message is the final message")

jf = post("/entries", {"journal_type": "free_write"}); uf2 = f"/entries/{jf['id']}"
jf = post(uf2 + "/captures", {"value_id": "account", "value": "kal raat phir wohi khayal aaye"})
check(jf["conversation_status"] == "offered" and not any(m["kind"] == "grounding" for m in jf["messages"]), "free write still offers the conversation first")
jf = post(uf2 + "/conversation", {"choice": "stop"})
check(jf["messages"][-1]["kind"] == "grounding", "grounding closes the free write thread too (FR-JRN-008)")
s = S(); s.query(FocusArea).filter(FocusArea.code == "trauma_ptsd").delete(); s.commit()

print("--- M: exposure journal (FR-JRN-006)")
settings.OPENAI_API_KEY = None; settings.LLM_MOCK = False
j = post("/entries", {"journal_type": "exposure"}); ue = f"/entries/{j['id']}"
check(j["pending_notice"] is None, "no scope notice for a user without the past-event focus")
check(j["next_capture"]["value_id"] == "feared_outcome" and j["next_capture"]["required"], "starts with the feared outcome")
j = post(ue + "/captures", {"value_id": "feared_outcome", "value": "lift mein phans jaungi aur saans ruk jayegi"})
spec = j["next_capture"]
check(spec["value_id"] == "distress_before" and [o["value"] for o in spec["scale"]] == list(range(11)), "then a 0-10 distress scale")
check(c.post(ue + "/captures", json={"value_id": "distress_before", "value": 11}).status_code == 422, "11 is refused")
j = post(ue + "/captures", {"value_id": "distress_before", "value": 0})
check(j["messages"][-2]["content"] == "0 — No distress at all", "0 is a valid answer, shown with its label")
order = ["planned_activity", "post_account", "distress_during", "distress_after", "learning"]
answers = {"planned_activity": "kal office ki lift lunga, do floor", "post_account": "thoda ghabrai lekin kar liya",
           "distress_during": 7, "distress_after": 3, "learning": "ghabrahat thi lekin utni nahi jitni socha tha"}
got = []
while j["next_capture"]:
    vid = j["next_capture"]["value_id"]; got.append(vid)
    j = post(ue + "/captures", {"value_id": vid, "value": answers[vid]})
    # FR-JRN-006: the entry stops after the plan and waits for her to go and do it.
    if j["pending_resume"]:
        check(got == ["planned_activity"], f"it waits right after the plan: {got}")
        check(j["messages"][-1]["kind"] == "pause", "with the fixed 'come back afterwards' message")
        check(c.post(ue + "/captures", json={"value_id": "post_account", "value": "x"}).status_code == 409,
              "and records nothing until she is back")
        j = post(ue + "/resume", {})
check(got == order, f"remaining steps in SRS order: {got}")
check(j["status"] == "completed" and j["conversation_status"] == "offered", "completes and offers the conversation")

print("--- N: a further cycle (FR-JRN-006)")
j2 = post("/entries", {"journal_type": "exposure", "parent_entry_id": ue.split("/")[-1]})
u2e = f"/entries/{j2['id']}"
check(j2["parent_entry_id"] == ue.split("/")[-1], "new cycle links to the completed one")
check(j2["messages"][0]["value_id"] == "feared_outcome" and "lift" in j2["messages"][0]["content"], "feared outcome carried over")
check(j2["next_capture"]["value_id"] == "distress_before", "so the new cycle starts at the distress, not the fear")
r = c.post("/entries", json={"journal_type": "exposure", "parent_entry_id": u2e.split("/")[-1]})
check(r.status_code == 409, "cannot continue a cycle that is not finished")
r = c.post("/entries", json={"journal_type": "check_in", "parent_entry_id": ue.split("/")[-1]})
check(r.status_code == 409, "cannot continue an exposure as a check-in")

print("--- O: scope notice for past-event focus (FR-JRN-007)")
s = S(); s.add(FocusArea(account_id=acct.id, code="trauma_ptsd")); s.commit()
j = post("/entries", {"journal_type": "exposure"}); un = f"/entries/{j['id']}"
check(j["pending_notice"] and "not for going back over a past traumatic event" in j["pending_notice"], "notice returned for this user")
check(j["messages"][0]["kind"] == "notice" and j["next_capture"] is None, "notice is the first message, no value requested yet")
r = c.post(un + "/captures", json={"value_id": "feared_outcome", "value": "x"})
check(r.status_code == 409 and "acknowledged" in r.json()["detail"], "capture refused before acknowledgement")
j = post(un + "/acknowledge", {})
check(j["pending_notice"] is None and j["next_capture"]["value_id"] == "feared_outcome", "after acknowledging, capture starts")
check(c.post(un + "/acknowledge", json={}).status_code == 409, "acknowledging twice is refused")
j = post(un + "/captures", {"value_id": "feared_outcome", "value": "bus mein log ghoor ke dekhenge"})
check(j["next_capture"]["value_id"] == "distress_before", "capture proceeds normally after that")
check(not capture.needs_grounding("exposure", ["trauma_ptsd"]), "exposure is not a grounding journal (FR-JRN-008)")
s = S(); s.query(FocusArea).filter(FocusArea.code == "trauma_ptsd").delete(); s.commit()

print("--- P: free write in several messages (FR-JRN-003)")
settings.OPENAI_API_KEY = None; settings.LLM_MOCK = False
j = post("/entries", {"journal_type": "free_write"}); uw = f"/entries/{j['id']}"
check(j["next_capture"]["repeatable"], "free write is marked repeatable")
j = post(uw + "/captures", {"value_id": "account", "value": "aaj subah se ajeeb lag raha tha", "more": True})
check(j["next_capture"] and j["next_capture"]["value_id"] == "account" and j["status"] == "in_progress",
      "after one message the same value stays open")
check(not any(m["kind"] == "offer" for m in j["messages"]), "no offer yet")
j = post(uw + "/captures", {"value_id": "account", "value": "phir ammi ka phone aaya", "more": True})
j = post(uw + "/captures", {"value_id": "account", "value": "ab thoda behtar hoon", "more": True})
sent = [m["content"] for m in j["messages"] if m["kind"] == "capture_answer"]
check(len(sent) == 3 and sent[-1] == "ab thoda behtar hoon", f"each message appears separately in the thread: {len(sent)}")
j = post(uw + "/captures", {"value_id": "account", "value": "", "more": False})
check(j["status"] == "completed" and j["conversation_status"] == "offered", "'done' finishes the entry and offers the conversation")
stored = S().query(CapturedValue).filter(CapturedValue.key == "account").order_by(CapturedValue.created_at.desc()).first()
import json as _json
check(_json.loads(stored.value).count("\n\n") == 2 and "ammi ka phone" in _json.loads(stored.value),
      "all three messages are stored as one account")
j = post("/entries", {"journal_type": "free_write"}); uw2 = f"/entries/{j['id']}"
r = c.post(uw2 + "/captures", json={"value_id": "account", "value": "", "more": False})
check(r.status_code == 422, "'done' with nothing written is refused (the account is required)")
j = post(uw2 + "/captures", {"value_id": "account", "value": "ek hi message kaafi hai"})
check(j["status"] == "completed", "a single message still completes the entry in one go")

print("--- Q: history shows drafts too, and is searchable")
j = post("/entries", {"journal_type": "free_write"}); uq = f"/entries/{j['id']}"
post(uq + "/captures", {"value_id": "account", "value": "chacha ke ghar shaadi ka hungama tha", "more": True})
listed = c.get("/entries").json()
mine = [e for e in listed if e["id"] == uq.split("/")[-1]]
check(bool(mine), "an unfinished entry appears in the history")
check(mine[0]["status"] == "in_progress" and mine[0]["preview"].startswith("chacha ke ghar"), "listed as a draft, with a preview of what she wrote")
check(listed[0]["id"] == uq.split("/")[-1], "most recently touched entry comes first")
completed_only = c.get("/entries", params={"status": "completed"}).json()
check(all(e["status"] == "completed" for e in completed_only), "status=completed still filters to finished entries")
found = c.get("/entries", params={"q": "shaadi ka hungama"}).json()
check([e["id"] for e in found] == [uq.split("/")[-1]], "search finds it by what she wrote")
found = c.get("/entries", params={"q": "boss ne ignore"}).json()
check(found and all(e["id"] != uq.split("/")[-1] for e in found), "search matches other entries by their text")
check(c.get("/entries", params={"q": "zzzznothingmatches"}).json() == [], "no matches gives an empty list")
found = c.get("/entries", params={"q": "SHAADI KA HUNGAMA"}).json()
check([e["id"] for e in found] == [uq.split("/")[-1]], "search ignores case")

print("--- R: insights (SRS 4.10)")
settings.OPENAI_API_KEY = None; settings.LLM_MOCK = False
s = S()
# a clean slate: this section counts entries, so it needs to know exactly what is there
s.query(Message).delete(); s.query(CapturedValue).delete(); s.query(Entry).delete(); s.commit()


def finished_check_in(mood, when, traps=(), feelings=("tired",), triggers=("workload",), intensity=6):
    j = post("/entries", {"journal_type": "check_in"}); u = f"/entries/{j['id']}"
    answer_check_in(u, mood=mood, triggers=list(triggers), trigger_note="likha hua note",
                    thinking_traps=list(traps), feelings=list(feelings), feeling_intensity=intensity)
    e = S().get(Entry, uuid.UUID(j["id"]))
    with S() as w:
        row = w.get(Entry, e.id); row.completed_at = when; w.commit()
    return j["id"]


now = datetime.utcnow()
a = finished_check_in(2, now - timedelta(days=1), traps=["mind_reading"], triggers=["workload", "money"], intensity=8)
b = finished_check_in(4, now - timedelta(days=3), traps=["mind_reading", "catastrophising"], feelings=["hurt"], intensity=3)
cc = finished_check_in(3, now - timedelta(days=40), traps=["mind_reading"])  # outside 30 days
draft = post("/entries", {"journal_type": "check_in"})
post(f"/entries/{draft['id']}/captures", {"value_id": "mood", "value": 1})

j = c.get("/insights").json()
check(j["period"] == "30d" and j["from"] and j["to"], "defaults to 30 days with a from/to (FR-INS-001/002)")
check(j["completed_entries"] == 2, f"only completed entries inside the period count (FR-INS-023): {j['completed_entries']}")
check([p["value"] for p in j["mood"]["points"]] == [4, 2], "mood points in date order, one per entry (FR-INS-003)")
check(all(p["entry_id"] for p in j["mood"]["points"]), "each point names its entry so it can be opened (FR-INS-005)")
check(j["mood"]["needed"] == 1, f"placeholder counts how many more are needed (FR-INS-021): {j['mood']['needed']}")
ints = j["feeling_intensity"]
check([p["value"] for p in ints["points"]] == [3, 8], f"intensity points in date order (FR-INS-009): {[p['value'] for p in ints['points']]}")
check(all(p["entry_id"] and p["journal_type"] for p in ints["points"]), "each intensity point names its entry and journal")
check((ints["min"], ints["max"]) == (0, 10), f"intensity ends come from the scale, not the chart: {ints['min']}-{ints['max']}")
check(ints["needed"] == 1, f"intensity placeholder counts what is missing (FR-INS-021): {ints['needed']}")
# One of the two check-ins was at mood 4, where neither triggers nor thinking
# patterns are asked at all, so only the low-mood one contributes either.
triggers = {t["id"]: t for t in j["triggers"]}
check(triggers["workload"]["count"] == 1 and triggers["money"]["count"] == 1,
      f"trigger counts, from the entries that were asked (FR-INS-006): {j['triggers']}")
check(all("count" in t and "%" not in str(t) for t in j["triggers"]), "counts of entries, no percentages (FR-INS-007)")
check(triggers["workload"]["name"] == "Workload", "items are labelled by name, not id")
traps = {t["id"]: t for t in j["thinking_traps"]}
check(traps["mind_reading"]["count"] == 2 and traps["catastrophising"]["count"] == 1,
      f"thinking trap counts, from every entry that wrote a note (FR-INS-010): {j['thinking_traps']}")
check("change" not in traps["mind_reading"], "no change shown when the previous period has too few entries (FR-INS-011)")
days = j["calendar"]["days"]
check(len(days) == 2, f"calendar marks the days with a completed entry (FR-INS-015): {days}")
check(all(set(d) == {"date", "mood", "entries"} for d in days), f"each day carries its mood and count: {days[0]}")
check([d["mood"] for d in days] == [4, 2], f"the day's mood colours the calendar: {[d['mood'] for d in days]}")
st = j["streak"]
check(st["total_entries"] >= 2 and st["entries_this_month"] >= 1,
      f"the ring counts entries, not days: {st['total_entries']} total, {st['entries_this_month']} this month")
check(j["journal_types"] == [{"journal_type": "check_in", "count": 2}], "entries counted by type (FR-INS-016)")

j7 = c.get("/insights", params={"period": "7d"}).json()
jall = c.get("/insights", params={"period": "all"}).json()
check(j7["completed_entries"] == 2 and jall["completed_entries"] == 3, "the period changes every view (FR-INS-001)")
check(jall["from"] is None, "'all' has no start date")
check(c.get("/insights", params={"period": "weekly"}).status_code == 422, "an unknown period is refused")

# enough history in the previous period, so the change appears. Low moods,
# because that is where the thinking patterns are asked for at all.
for day in (35, 37, 39):
    finished_check_in(2, now - timedelta(days=day), traps=["mind_reading"])
traps = {t["id"]: t for t in c.get("/insights").json()["thinking_traps"]}
check(traps["mind_reading"].get("change") == -2, f"change against the previous period (FR-INS-011): {traps['mind_reading'].get('change')}")

print("--- T: streak and local days (module 4 + FR-INS-015)")
s = S(); s.query(Message).delete(); s.query(CapturedValue).delete(); s.query(Entry).delete(); s.commit()

def entry_on(when):
    """A completed check-in whose completed_at is exactly `when` (UTC)."""
    j = post("/entries", {"journal_type": "check_in"}); u = f"/entries/{j['id']}"
    answer_check_in(u, mood=3, feelings=[], triggers=[], trigger_note="")
    with S() as w:
        row = w.get(Entry, uuid.UUID(j["id"])); row.completed_at = when; w.commit()
    return j["id"]

PKT = 300  # minutes east of UTC
today_utc = datetime.utcnow()
# three days in a row, ending today, in her timezone
for back in (0, 1, 2):
    entry_on(today_utc - timedelta(days=back))
j = c.get("/insights", params={"tz_offset": PKT}).json()
st = j["streak"]
check(st["current"] == 3, f"three days in a row is a streak of 3 (got {st['current']})")
check(st["longest"] >= 3, f"longest is at least as long as the current one ({st['longest']})")
check(st["today"] is True, "today counts as written")
check(st["weekly_goal"] == 3, f"her weekly goal comes back with it ({st['weekly_goal']})")
check(st["days_this_week"] >= 1, "this week's days are counted")

# a gap of one whole day breaks it
entry_on(today_utc - timedelta(days=5))
j = c.get("/insights", params={"tz_offset": PKT}).json()
check(j["streak"]["current"] == 3, "an older entry beyond a gap does not extend the streak")

# grace: nothing today, but yesterday counts
s = S(); s.query(Message).delete(); s.query(CapturedValue).delete(); s.query(Entry).delete(); s.commit()
entry_on(today_utc - timedelta(days=1))
j = c.get("/insights", params={"tz_offset": PKT}).json()
check(j["streak"]["current"] == 1 and j["streak"]["today"] is False,
      f"yesterday alone keeps the streak alive, today is still unwritten ({j['streak']})")

# the local-day fix itself: 10pm UTC is already tomorrow in Karachi
s = S(); s.query(Message).delete(); s.query(CapturedValue).delete(); s.query(Entry).delete(); s.commit()
late = datetime.utcnow().replace(hour=22, minute=0, second=0, microsecond=0) - timedelta(days=1)
entry_on(late)
utc_day = [d["date"] for d in c.get("/insights", params={"tz_offset": 0}).json()["calendar"]["days"]]
pkt_day = [d["date"] for d in c.get("/insights", params={"tz_offset": PKT}).json()["calendar"]["days"]]
check(utc_day != pkt_day, f"the same entry falls on a different day in each timezone: {utc_day} vs {pkt_day}")
check(pkt_day == [(late + timedelta(minutes=PKT)).date().isoformat()], f"and her day is the local one ({pkt_day})")
check(c.get("/insights", params={"tz_offset": 900}).status_code == 422, "an impossible offset is refused")

print("--- U: trends over time (mental trends)")
s = S(); s.query(Message).delete(); s.query(CapturedValue).delete(); s.query(Entry).delete(); s.commit()
PKT = 300

def check_in_on(when, mood, traps=(), triggers=()):
    j = post("/entries", {"journal_type": "check_in"}); u = f"/entries/{j['id']}"
    answer_check_in(u, mood=mood, triggers=list(triggers), trigger_note="likha hua note",
                    thinking_traps=list(traps), feelings=[])
    with S() as w:
        row = w.get(Entry, uuid.UUID(j["id"])); row.completed_at = when; w.commit()
    return j["id"]

now = datetime.utcnow()
# two weeks: the older one worse and full of mind reading, the newer one better
# Moods 3 and up are never asked for a thinking pattern, so the newer week has
# none whatever is passed here; 3 still gets the triggers, 5 does not.
for back, mood, traps in [(16, 2, ["mind_reading"]), (15, 2, ["mind_reading", "catastrophising"]), (14, 2, ["mind_reading"]),
                          (3, 3, ["mind_reading"]), (2, 3, []), (1, 5, [])]:
    check_in_on(now - timedelta(days=back), mood, traps, triggers=["workload"])

j = c.get("/insights", params={"period": "3m", "tz_offset": PKT}).json()

trend = j["mood_trend"]
check(len(trend["points"]) >= 2, f"one point per week she wrote in ({len(trend['points'])})")
check(trend["points"][0]["value"] < trend["points"][-1]["value"],
      f"the weekly average moved with her mood: {[p['value'] for p in trend['points']]}")
check(sum(p["entries"] for p in trend["points"]) == 6, "every entry counts towards exactly one week")
check((trend["min"], trend["max"]) == (1, 5), "the trend carries the ends of the mood scale")

dist = {row["value"]: row["count"] for row in j["mood_distribution"]}
check(dist == {1: 0, 2: 3, 3: 2, 4: 0, 5: 1}, f"the distribution counts every point of the scale: {dist}")
check([r["label"] for r in j["mood_distribution"]][0] == "Very low", "each bar is labelled from the scale")

traps = j["trap_trend"]
check(len(traps["weeks"]) >= 2, f"the trap trend spans the weeks she wrote in ({traps['weeks']})")
mind = next(s for s in traps["series"] if s["id"] == "mind_reading")
check(mind["name"] == "Mind reading", "series are labelled by name, not id")
check(len(mind["counts"]) == len(traps["weeks"]), "one count per week, so the lines line up")
check(mind["counts"][0] > mind["counts"][-1], f"mind reading fell across the weeks: {mind['counts']}")
check(len(traps["series"]) <= 3, "at most three lines, or the chart is unreadable")

times = {row["part"]: row["count"] for row in j["writing_times"]}
check(sum(times.values()) == 6, f"every entry lands in exactly one part of the day: {times}")
check([r["label"] for r in j["writing_times"]] == ["Morning", "Afternoon", "Evening", "Night"], "in day order")

# FR-INS-011 now applies to triggers and feelings too, not only traps.
# The week before this one needs enough entries to compare against.
for back in (8, 9, 10):
    check_in_on(now - timedelta(days=back), 3, triggers=["workload", "money"])
week = c.get("/insights", params={"period": "7d", "tz_offset": PKT}).json()
workload = next(row for row in week["triggers"] if row["id"] == "workload")
check(workload["change"] == -1, f"workload: 2 this week and 3 the week before ({workload})")
check(all("change" in row for row in week["feelings"]) or not week["feelings"], "feelings carry it too")
gone = [row for row in week["triggers"] if row["id"] == "money"]
check(not gone, "a trigger she stopped noting simply drops out of this period")

print("--- V: the wellbeing questionnaire (FR-INS-017/018/019)")
from app.models.questionnaire_response import QuestionnaireResponse
from app.core import questionnaire as Q
s = S(); s.query(QuestionnaireResponse).delete(); s.commit()

spec = c.get("/insights/questionnaire").json()
check(len(spec["items"]) == 5 and len(spec["scale"]) == 4, "the form comes back with its items and scale")
check(not any(k in spec for k in ("score", "max_score", "bands", "condition")),
      f"nothing in the form reveals scoring or what is measured: {sorted(spec)}")

j = c.get("/insights").json()
check(j["questionnaire"]["due"] is True, "with no history it is offered")

r = c.post("/insights/questionnaire", json={"answers": {"q1": 1, "q2": 2, "q3": 0, "q4": 3, "q5": 1}})
check(r.status_code == 201 and r.json() == {"recorded": True}, f"answers are accepted ({r.status_code})")
check("score" not in r.text, "FR-INS-018: the score never comes back to the client")
row = S().query(QuestionnaireResponse).first()
check(row.score == 7 and row.max_score == 15, f"but it is stored ({row.score}/{row.max_score})")
check(row.outcome == "completed" and row.version == Q.VERSION, "recorded against the instrument version")

j = c.get("/insights").json()
check(j["questionnaire"]["due"] is False, "and it is not offered again straight away")
check(j["questionnaire"]["next_due"], f"the next offer has a date ({j['questionnaire']['next_due']})")

bad = c.post("/insights/questionnaire", json={"answers": {"q1": 9, "q2": 0, "q3": 0, "q4": 0, "q5": 0}})
check(bad.status_code == 422, "a value outside the scale is refused")
missing = c.post("/insights/questionnaire", json={"answers": {"q1": 1}})
check(missing.status_code == 422, "so is a half-answered form")

# FR-INS-019: the trend is a position between 0 and 1, never the score
with S() as w:
    row2 = w.get(QuestionnaireResponse, row.id); row2.created_at = datetime.utcnow() - timedelta(days=30); w.commit()
c.post("/insights/questionnaire", json={"answers": {"q1": 0, "q2": 0, "q3": 1, "q4": 0, "q5": 0}})
tr = c.get("/insights", params={"period": "3m"}).json()["questionnaire"]["trend"]
check(len(tr["points"]) == 2, f"two completions, two points ({len(tr['points'])})")
check(all(0 <= p["position"] <= 1 for p in tr["points"]), "each point is a position between 0 and 1")
check(all("score" not in p and "value" not in p for p in tr["points"]), "no score reaches the client")
check(tr["points"][0]["position"] > tr["points"][1]["position"], "the line moves the way the answers did")

# a decline delays the next offer just as a completion does
s = S(); s.query(QuestionnaireResponse).delete(); s.commit()
r = c.post("/insights/questionnaire/decline", json={})
check(r.status_code == 201, "declining is accepted")
j = c.get("/insights").json()
check(j["questionnaire"]["due"] is False, "and it is not offered again for the same wait")
check(S().query(QuestionnaireResponse).first().score is None, "a decline stores no score")

print("--- W: the learning library (SRS 4.11, FR-INS-012)")
from app.models.library_favourite import LibraryFavourite
from app.core import learning as LIB
s = S(); s.query(LibraryFavourite).delete(); s.commit()

lib = c.get("/library").json()
check(len(lib["articles"]) == len(LIB.ARTICLES), f"every article is listed ({len(lib['articles'])})")
check(all(a["featured"] for a in lib["featured"]), "the featured ones are marked as such")
check(lib["articles"][0]["featured"], "and they come first")
check(all(set(a) >= {"slug", "title", "summary", "category", "minutes", "favourite"} for a in lib["articles"]),
      "each card has what it needs to be drawn")
check(all("body" not in a for a in lib["articles"]), "the list does not carry the whole text")
check(lib["categories"], f"categories come back for filtering: {lib['categories']}")

found = c.get("/library", params={"q": "distress"}).json()["articles"]
check(any(a["slug"] == "managing-distress" for a in found), "search finds an article by its title")
deep = c.get("/library", params={"q": "cold water"}).json()["articles"]
check([a["slug"] for a in deep] == ["managing-distress"], f"and by something only in the body: {[a['slug'] for a in deep]}")
check(c.get("/library", params={"q": "zzzznothing"}).json()["articles"] == [], "a search with no match returns nothing")

# FR-INS-012: a thinking pattern opens its article
trap = c.get("/library", params={"trap": "mind_reading"}).json()["articles"]
check(trap and trap[0]["slug"] == "understanding-thinking-traps", f"a trap finds its article: {trap}")
check(c.get("/library", params={"trap": "not_a_trap"}).json()["articles"] == [], "an unknown trap finds none")

one = c.get("/library/managing-distress").json()
check(one["body"].startswith("Distress is uncomfortable"), "the article itself comes back with its body")
check(one["favourite"] is False, "and is not saved yet")
check(c.get("/library/nope").status_code == 404, "an unknown slug is a 404")

r = c.post("/library/managing-distress/favourite", json={})
check(r.status_code == 201 and r.json() == {"favourite": True}, "saving an article works")
check(c.get("/library/managing-distress").json()["favourite"] is True, "and it comes back saved")
c.post("/library/managing-distress/favourite", json={})
check(S().query(LibraryFavourite).count() == 1, "saving twice keeps one row")
check(c.get("/library", params={"saved_only": True}).json()["articles"][0]["slug"] == "managing-distress",
      "the saved filter shows only what she kept")
check(c.post("/library/nope/favourite", json={}).status_code == 404, "an unknown slug cannot be saved")

c.delete("/library/managing-distress/favourite")
check(S().query(LibraryFavourite).count() == 0, "unsaving removes it")
check(c.get("/library", params={"saved_only": True}).json()["articles"] == [], "and the saved list is empty again")

print("--- S: insights, exposure cycles (FR-INS-013/014)")
def finished_exposure(outcome, before, during, after, when):
    j = post("/entries", {"journal_type": "exposure"}); u = f"/entries/{j['id']}"
    post(u + "/captures", {"value_id": "feared_outcome", "value": outcome})
    post(u + "/captures", {"value_id": "distress_before", "value": before})
    post(u + "/captures", {"value_id": "planned_activity", "value": "lift lena hai"})
    post(u + "/resume", {})  # she went and did it, then came back (FR-JRN-006)
    post(u + "/captures", {"value_id": "post_account", "value": "kar liya"})
    post(u + "/captures", {"value_id": "distress_during", "value": during})
    post(u + "/captures", {"value_id": "distress_after", "value": after})
    post(u + "/captures", {"value_id": "learning", "value": "utna bura nahi tha"})
    with S() as w:
        row = w.get(Entry, uuid.UUID(j["id"])); row.completed_at = when; w.commit()
    return j["id"]


finished_exposure("lift mein phans jaungi", 8, 7, 4, now - timedelta(days=5))
finished_exposure("lift mein phans jaungi", 6, 5, 2, now - timedelta(days=2))
finished_exposure("call pe awaz kaanp jayegi", 7, 6, 5, now - timedelta(days=4))
groups = {g["feared_outcome"]: g for g in c.get("/insights").json()["exposure"]}
lift = groups["lift mein phans jaungi"]
check(lift["enough"] and [cy["cycle"] for cy in lift["cycles"]] == [1, 2], "cycles numbered in the order they were completed")
check([(cy["before"], cy["during"], cy["after"]) for cy in lift["cycles"]] == [(8, 7, 4), (6, 5, 2)],
      "all three distress values per cycle (FR-INS-013)")
check(not groups["call pe awaz kaanp jayegi"]["enough"], "a single cycle is not a comparison (FR-INS-014)")

print("--- daily check-in: app-owned steps, one submit, model writes only the closing")
import json  # noqa: E402
settings.OPENAI_API_KEY = None; settings.LLM_MOCK = True
with S() as w:
    me = Account(email="ci@x.com", distress_baseline=4); w.add(me); w.commit(); w.refresh(me); me_id = me.id
app.dependency_overrides[get_current_user] = lambda: S().get(Account, me_id)
STRESSORS = {"workload", "deadline", "argument", "feeling_ignored", "loneliness", "money", "sleep", "illness", "comparison"}
POSITIVE = {"friends", "achievement", "rest", "nature", "hobby", "calm", "gratitude", "kindness", "good_food"}


def chips(mood):
    return {i["id"] for cat in c.get(f"/entries/check_in/factors?mood={mood}").json()["categories"] for i in cat["items"]}


def check_in(mood, factors=(), note="", client_id=None):
    r = c.post("/entries/check_in", json={"client_id": str(client_id or uuid.uuid4()), "mood": mood,
                                          "factors": list(factors), "note": note})
    assert r.status_code < 300, r.json()
    return r.json()


for m in range(1, 6):
    cats = c.get(f"/entries/check_in/factors?mood={m}").json()["categories"]
    check(all(cat["items"] for cat in cats), f"mood {m}: every category has chips")
check(not chips(5) & STRESSORS and not chips(4) & STRESSORS, "T2: no stressor chips at mood 4-5")
check(not chips(1) & POSITIVE and not chips(2) & POSITIVE, "no positive chips at mood 1-2")
f5 = c.get("/entries/check_in/factors?mood=5").json()
check(f5["prompt"]["en"] == "What was part of this mood?" and "trigger" not in json.dumps(f5).lower(),
      "fixed wording per mood, no 'triggers' anywhere")
check(c.get("/entries/check_in/factors?mood=6").status_code == 422, "mood outside 1-5 -> 422")

r = check_in(5)
with S() as w:
    rows = w.query(CapturedValue).filter(CapturedValue.entry_id == uuid.UUID(r["entry_id"])).all()
    msgs = w.query(Message).filter(Message.entry_id == uuid.UUID(r["entry_id"])).order_by(Message.sequence).all()
check([x.key for x in rows] == ["mood"] and not any(x.skipped for x in rows), "T1: empty steps save nothing, no skip status")
check([(x.role, x.kind) for x in msgs] == [("user", "capture_answer"), ("ai", "closing")], "T1: no bubble for an empty step")
low = r["closing"].lower()
check(r["closing"] and not any(w_ in low for w_ in ("skip", "trigger", "didn't", "nothing")), "T1: warm closing, says nothing about empty steps")
check(r["card"] is None and r["crisis_event"] is None, "T1: no card on an ordinary check-in")

r2 = check_in(5)
check(r2["closing"] != r["closing"], "T9: same check-in twice -> different wording")
r4 = check_in(2)
check("!" not in r4["closing"] and r4["card"] is None, "T4: mood 2, all empty: gentle closing, no card")

cid = uuid.uuid4()
first = check_in(3, ["workload"], "busy day", client_id=cid)
again = check_in(3, ["workload"], "busy day", client_id=cid)
with S() as w:
    n = w.query(Entry).filter(Entry.client_id == cid).count()
check(again["entry_id"] == first["entry_id"] and again["closing"] == first["closing"] and n == 1, "retry with same client_id saves once")
check(c.post("/entries/check_in", json={"client_id": str(uuid.uuid4()), "mood": 3, "factors": ["nope"]}).status_code == 422,
      "unknown chip id -> 422")
check(capture.LIBRARY_NAMES["triggers"]["illness"] == "Health worries" and capture.LIBRARY_NAMES["triggers"]["in_laws"] == "In-laws",
      "Insights names new chips, old-only ids keep their names")

post("/libraries/triggers/terms", {"name": "Cricket"})
check(c.get("/entries/check_in/factors?mood=4").json()["categories"][0]["id"] == "my_words", "her own words come first, at any mood")

for _ in range(3):
    last = check_in(1)
check(last["suggest"] == "chat", "three low check-ins in a row -> soft pointer to Chat")
check(check_in(4)["suggest"] is None, "streak broken -> no pointer")

settings.LLM_MOCK = False; settings.OPENAI_API_KEY = "test"
ci = {"risk": "clear", "safety": NO_HARM, "closing": "Model closing."}


def ci_parse(**kw):
    fmt = kw["text_format"]
    ci["instructions"] = kw.get("instructions")
    ci.setdefault("calls", []).append(fmt.__name__)
    if fmt is lc.DomainResult:
        return SimpleNamespace(output_parsed=lc.DomainResult(safety=ci["safety"], domains=["general"], risk_tier=ci["risk"]))
    return SimpleNamespace(output_parsed=lc.Closing(message_text=ci["closing"]))


fake = MagicMock(); fake.responses.parse.side_effect = ci_parse
with patch.object(lc, "_get_client", return_value=fake):
    ci["risk"] = "danger"
    ci["safety"] = lc.Safety(harm_type="self_harm", timing="ongoing", danger_now="unclear", about="self",
                             discloses=True, distress_now=True, asks_for_help=False)
    ci["closing"] = "I'm really glad you told me. You matter, and you don't have to hold this alone."
    ci["calls"] = []
    r = check_in(1, note="I don't want to be here")
    check(r["crisis_event"]["tier"] == "danger" and r["card"] == "prominent", "T3: danger flow fires, prominent card")
    check(ci["calls"] == ["DomainResult"], f"T3: danger in a journal -> zero generation calls (calls: {ci['calls']})")
    check(r["closing"] == r["crisis_event"]["text"] and r["closing"] != ci["closing"], "T3: fixed crisis content, never a model reply")

    ci["risk"], ci["safety"] = "clear", NO_HARM
    ci["closing"] = "Work and sleep, that's a lot. What helped? Did anything else? card: soft"
    r = check_in(2, ["workload", "sleep"], note="long day")
    check(r["closing"].count("?") == 1 and "card" not in r["closing"], "at most one question; card labels stripped")
    check("What was part of it: Workload, Sleep" in ci["instructions"] and "triggers" not in ci["instructions"].split("# What she recorded")[1],
          "model reads plain labels, never field names")
    ci["closing"] = "A really good day, nice to hear. Hope some of it carries into tomorrow."
    r = check_in(5)
    check(r["closing"] != ci["closing"], "a closing copied from the skill examples is replaced")
settings.OPENAI_API_KEY = None; settings.LLM_MOCK = False
app.dependency_overrides[get_current_user] = lambda: S().get(Account, acct.id)

print("--- Phase B: shared journal engine")
from app.core import crisis as crisis_mod  # noqa: E402
GENERATION = {"CapturePrompt", "Closing", "ChatReply", "Reflection"}


def new_account(email, focus=None):
    with S() as w:
        a = Account(email=email, distress_baseline=4); w.add(a); w.commit(); w.refresh(a)
        if focus:
            w.add(FocusArea(account_id=a.id, code=focus)); w.commit()
        return a.id


def as_user(account_id):
    app.dependency_overrides[get_current_user] = lambda: S().get(Account, account_id)


def default_answer(spec):
    if spec["control"] == "scale":
        return spec["scale"][len(spec["scale"]) // 2]["value"]
    if spec["control"] == "multi_select":
        return []
    return "it was a day" if spec["required"] else ""


def drive(journal_type, answer=default_answer, until=None):
    """Answers every step of one journal, the way the app does, until it completes."""
    j = post("/entries", {"journal_type": journal_type}); u = f"/entries/{j['id']}"
    for _ in range(40):
        if j["status"] == "completed" or (until and j["next_capture"] and j["next_capture"]["value_id"] == until):
            return u, j
        if j["pending_notice"]:
            j = post(u + "/acknowledge", {}); continue
        if j["pending_resume"]:
            j = post(u + "/resume", {}); continue
        spec = j["next_capture"]
        j = post(u + "/captures", {"value_id": spec["value_id"], "value": answer(spec)})
    raise AssertionError(f"{journal_type} never completed")


def rows(u):
    with S() as w:
        return {r.key: r for r in w.query(CapturedValue).filter(CapturedValue.entry_id == uuid.UUID(u.split("/")[-1])).all()}


plain, trauma = new_account("b1@x.com"), new_account("b2@x.com", "trauma_ptsd")
JOURNALS = ["check_in", "savouring", "thought", "exposure", "free_write"]

# Schedules hold no wording.
for name, sched in capture._SCHEDULE_FILES.items():
    capture._check_no_wording(name, sched)
check(True, "every schedule holds only structure, no wording")
try:
    capture._check_no_wording("bad", {"values": [{"id": "x", "value_type": "text", "control": "free_text", "prompt": "Hi?"}]})
    check(False, "a schedule with wording is rejected")
except RuntimeError:
    check(True, "a schedule with wording is rejected at startup")

# Model down: every journal, both variants, completes on stored wording.
settings.OPENAI_API_KEY = "test"; settings.LLM_MOCK = False
down = MagicMock(); down.responses.parse.side_effect = lc.openai.OpenAIError("service down")
with patch.object(lc, "_get_client", return_value=down):
    for who, label in ((plain, "standard"), (trauma, "past-event focus")):
        as_user(who)
        for jt in JOURNALS:
            u, j = drive(jt)
            check(j["status"] == "completed", f"model down: {jt} ({label}) completes")
            prompts = [m for m in j["messages"] if m["kind"] == "capture_prompt"]
            check(all(m["content"] == capture.fallback_wording(jt, m["value_id"]) for m in prompts),
                  f"model down: {jt} ({label}) used stored wording only")
settings.OPENAI_API_KEY = None

# Danger in any journal's free text: zero generation calls from then on.
b = {"calls": [], "risk": "clear", "safety": None, "prompt": None}


def b_parse(**kw):
    fmt = kw["text_format"]
    b["calls"].append(fmt.__name__)
    if fmt is lc.DomainResult:
        return SimpleNamespace(output_parsed=lc.DomainResult(safety=b["safety"] or NO_HARM, domains=["general"], risk_tier=b["risk"]))
    if fmt is lc.CapturePrompt:
        want = kw["instructions"].split("# Value to request\n")[1].strip()
        text, vid = b["prompt"] or (f"Tell me about {want.split('_')[0]}.", want)
        return SimpleNamespace(output_parsed=lc.CapturePrompt(value_id=vid, message_text=text))
    if fmt is lc.ChatReply:
        return SimpleNamespace(output_parsed=lc.ChatReply(crisis_indicators_noticed=False, response_text="Chat model reply.", referral_flag=False))
    if fmt is lc.Closing:
        return SimpleNamespace(output_parsed=lc.Closing(message_text="A closing line."))
    return SimpleNamespace(output_parsed=lc.Reflection(same_concern_count=1, shift_noticed=False, response_text="Reflection.",
                                                       session_end=False, closure_reason="none", referral_flag=False,
                                                       crisis_indicators_noticed=False))


settings.OPENAI_API_KEY = "test"
fake_b = MagicMock(); fake_b.responses.parse.side_effect = b_parse
with patch.object(lc, "_get_client", return_value=fake_b):
    as_user(plain)
    for jt in JOURNALS:
        first_text = next(v["id"] for v in capture.values_for(jt) if v["control"] == "free_text")
        u, j = drive(jt, until=first_text)
        b["calls"].clear()
        j = post(u + "/captures", {"value_id": first_text, "value": "I want to kill myself"})
        check(j["crisis_event"]["tier"] == "danger" and not set(b["calls"]) & GENERATION,
              f"{jt}: danger text -> zero generation calls (calls: {b['calls']})")
        while j["status"] != "completed":
            if j["pending_resume"]:
                j = post(u + "/resume", {}); continue
            spec = j["next_capture"]
            j = post(u + "/captures", {"value_id": spec["value_id"], "value": default_answer(spec)})
        check(not set(b["calls"]) & GENERATION and j["conversation_status"] == "suppressed",
              f"{jt}: the rest of the entry makes no generation call, no offer")
        fixed = {v["english"] for v in crisis_mod.CONTENT["danger"].values()}
        crisis_msgs = [m["content"] for m in j["messages"] if m["kind"] == "crisis"]
        check(len(crisis_msgs) == 1 and crisis_msgs[0] in fixed, f"{jt}: fixed crisis content in the thread, never generated")

    # A user-added term inside a journal is screened the same way.
    u, j = drive("check_in", until="triggers")
    b["calls"].clear()
    t = post("/libraries/triggers/terms", {"name": "kill myself", "entry_id": u.split("/")[-1]})
    check(t["crisis_event"]["tier"] == "danger" and not set(b["calls"]) & GENERATION, "danger term -> zero generation calls")

    # The model's wording: wrong value_id or a leaked label -> stored wording.
    b["prompt"] = ("How strong is it?", "feeling_intensity")
    u, j = drive("savouring", until="event")
    check(j["messages"][-1]["content"] == capture.fallback_wording("savouring", "event"), "wrong value_id -> stored fallback wording")
    b["prompt"] = ("card: soft What happened that was good?", "event")
    j = post("/entries", {"journal_type": "savouring"})
    check(j["messages"][-1]["content"] == "What happened that was good?", "card label stripped from capture wording")
    b["prompt"] = ("Anything about thinking_traps or triggers today?", "event")
    j = post("/entries", {"journal_type": "savouring"})
    check(j["messages"][-1]["content"] == capture.fallback_wording("savouring", "event"), "internal words in the wording -> stored fallback")
    b["prompt"] = None

    # Old threads read back unchanged after the model's wording changes.
    u, j = drive("savouring")
    before = c.get(u).json()["messages"]
    b["prompt"] = ("Completely different wording now.", "event")
    after = c.get(u).json()["messages"]
    check(before == after, "an old thread is identical after a model change")
    b["prompt"] = None

    # Abuse disclosed in a journal: not danger. Normal flow, soft card.
    b["risk"] = "danger"
    b["safety"] = lc.Safety(harm_type="physical_violence", timing="ongoing", danger_now="no", about="self",
                            discloses=True, distress_now=False, asks_for_help=False)
    u, j = drive("thought", until="situation")
    j = post(u + "/captures", {"value_id": "situation", "value": "he hits me when he is angry"})
    check(j["crisis_tier"] in (None, "clear") and not any(m["kind"] == "crisis" for m in j["messages"]),
          "abuse disclosure in a journal is not danger tier")
    check(j["messages"][-1]["kind"] == "capture_prompt" and j["messages"][-1]["card"] == "soft" and j["next_capture"],
          "the journal goes on, with a soft helplines card under the next message")
    b["safety"] = b["safety"].model_copy(update={"danger_now": "yes"})
    j = post(u + "/captures", {"value_id": "automatic_thought", "value": "he will kill me tonight"})
    check(j["crisis_event"]["tier"] == "danger", "a threat to life now stays danger")
    b["risk"], b["safety"] = "clear", None

    # Regression: AI Chat still answers danger-tier input with a model reply and a card.
    j = post("/entries", {"journal_type": "chat"}); uc = f"/entries/{j['id']}"
    b["calls"].clear()
    j = post(uc + "/messages", {"content": "I want to kill myself"})
    ai = [m for m in j["messages"] if m["role"] == "ai"][-1]
    check(ai["content"] == "Chat model reply." and ai["card"] == "prominent" and "ChatReply" in b["calls"],
          "AI Chat unchanged: danger input still gets the model's reply, card prominent")
    check(j["conversation_status"] == "active", "AI Chat unchanged: the chat stays open")
settings.OPENAI_API_KEY = None

# Crisis content is byte-identical every time, over 50 events.
as_user(new_account("b3@x.com"))
seen_texts = {}
for i in range(50):
    j = post("/entries", {"journal_type": "free_write"})
    j = post(f"/entries/{j['id']}/captures", {"value_id": "account", "value": "I want to kill myself"})
    seen_texts.setdefault(j["crisis_event"]["variant"], set()).add(j["crisis_event"]["text"].encode("utf-8"))
check(seen_texts.get("full") == {crisis_mod.CONTENT["danger"]["full"]["english"].encode("utf-8")}
      and seen_texts.get("abbreviated") == {crisis_mod.CONTENT["danger"]["abbreviated"]["english"].encode("utf-8")},
      "crisis content byte-identical across 50 events (per variant, from the content file)")

# Empty answers are null; values a condition passed over are "not asked"; no bubble either way.
as_user(plain)
u, j = drive("check_in")
r = rows(u)
check(r["trigger_note"].status == "skipped" and r["trigger_note"].value is None, "empty answer stored as skipped (null)")
check(r["feeling_intensity"].status == "not_asked" and not r["feeling_intensity"].skipped and r["feeling_intensity"].value is None,
      "a value the schedule's condition passed over is stored as not asked, not skipped")
check([m["value_id"] for m in j["messages"] if m["role"] == "user"] == ["mood"], "no message for an empty answer")
check(not any("skip" in m["content"].lower() for m in j["messages"]), "no 'skip' anywhere in the thread")
spec_c = {"id": "x", "min_mood": 1, "max_mood": 3}
check(capture.next_value([spec_c], {"mood": 5}) is None and capture.not_asked([spec_c], {"mood": 5}) == ["x"]
      and capture.next_value([spec_c], {"mood": 2}) == spec_c, "min_mood / max_mood condition decides whether a value is asked")

# A retry saves once.
j = post("/entries", {"journal_type": "free_write"}); uf = f"/entries/{j['id']}"
cid = str(uuid.uuid4())
first = post(uf + "/captures", {"value_id": "account", "value": "a long day", "more": True, "client_id": cid})
again = post(uf + "/captures", {"value_id": "account", "value": "a long day", "more": True, "client_id": cid})
check(sum(m["role"] == "user" for m in again["messages"]) == 1 and rows(uf)["account"].value == json.dumps("a long day"),
      "same client_id twice -> recorded once")
u, j = drive("thought", until="supporting_evidence")
post(u + "/captures", {"value_id": "supporting_evidence", "value": ""})
r2 = c.post(u + "/captures", json={"value_id": "supporting_evidence", "value": ""})
check(r2.status_code == 200 and r2.json()["next_capture"]["value_id"] == "contradicting_evidence",
      "retrying an answer already recorded returns the state, not an error")

# Positions are 1..n in every thread.
with S() as w:
    by_entry = {}
    for m in w.query(Message).all():
        by_entry.setdefault(m.entry_id, []).append(m.sequence)
check(all(sorted(s) == list(range(1, len(s) + 1)) for s in by_entry.values()), f"message positions sequential in all {len(by_entry)} threads")

# Journal tense rule (rules only, classifier down), 12 phrases.
down_cls = lc.Classification(domains=None, risk_tier=None)
for text, want in [
    ("I want to kill myself", "danger"),
    ("I don't want to live anymore", "danger"),
    ("I was suicidal years ago", "mild"),
    ("My friend attempted suicide", "mild"),
    ("I used to self harm when I was a teenager", "mild"),
    ("I was suicidal years ago and lately it's back", "danger"),
    ("mujhe marne ka dil karta hai", "danger"),
    ("kuch saal pehle main khudkushi ka sochti thi", "mild"),
    ("meri dost ne khudkushi ki koshish ki", "mild"),
    ("ab bhi jeena nahi chahti", "danger"),
    ("i want to kil myslef", "danger"),
    ("i was sucidal years ago", "mild"),
]:
    check(crisis_mod.journal_tier(text, down_cls)[0] == want, f"journal tier {text!r} -> {want}")
check(crisis_mod.journal_tier("I'm going to kill myself tonight", down_cls)[0] == "emergency", "a present plan stays emergency")
check(assess_lexical("i want to kil myslef") == "clear", "AI Chat's rules unchanged (typo handling is journal-only)")

# The approval marker gates releases but never reaches a prompt or the app.
M = "DRAFT_NOT_CLINICALLY_APPROVED"
texts = [md_loader.load_base(), md_loader.load_capture_instructions(), md_loader.load("skills", "low_mood"),
         md_loader.load("clinical_context", "anxiety"), md_loader.load("journal_types", "thought"),
         json.dumps(c.get("/onboarding/distress-scale").json()), c.get("/library/why-writing-helps").text]
check(not any(M in t for t in texts), "the clinical-approval marker never reaches a prompt or a response")
app.dependency_overrides[get_current_user] = lambda: S().get(Account, acct.id)

print("--- T: Your Week in Reflection (weekly report, badges, carry-forward)")
settings.OPENAI_API_KEY = None; settings.LLM_MOCK = False  # nothing generated in this section
from app.core import weekly as wk, badges as bdg, narrative as nar
from app.models.earned_badge import EarnedBadge

TZ = 300  # Pakistan: +5h, so a local Monday starts at 19:00 UTC on Sunday

# Her own account, so the counts below are this section's and nobody else's.
wacct = Account(email="week@x.com")
with S() as w:
    w.add(wacct); w.commit(); w.refresh(wacct)
app.dependency_overrides[get_current_user] = lambda: S().get(Account, wacct.id)

MON = wk.current_week_start(TZ) - timedelta(days=21)  # a finished week, inside the history window
LOCAL_MIDNIGHT = datetime.combine(MON, datetime.min.time()) - timedelta(minutes=TZ)


def at(day, hour=12, minute=0):
    """UTC instant for a local time on day N of the week under test."""
    return LOCAL_MIDNIGHT + timedelta(days=day, hours=hour, minutes=minute)


def done(journal, when, answers):
    """
    Walk a journal the way the app does: ask the server what is next rather
    than assuming an order, so reordering a schedule changes one place, not
    every fixture. "_resume" in the answers is the pause an exposure waits on.
    """
    wanted = dict(answers)
    j = post("/entries", {"journal_type": journal}); u = f"/entries/{j['id']}"
    while j["next_capture"]:
        vid = j["next_capture"]["value_id"]
        if vid not in wanted:
            raise AssertionError(f"{journal} asked for {vid}, which this fixture has no answer for")
        value = wanted[vid]
        j = post(u + "/captures", {"value_id": vid, "value": value, "skipped": value is None})
        if "_resume" in wanted and j["pending_resume"]:
            j = post(u + "/resume", {})
    with S() as w:
        row = w.get(Entry, uuid.UUID(j["id"])); row.completed_at = when; w.commit()
    return j["id"]


def check_in_at(when, mood=3, traps=(), feelings=("tired",), triggers=("workload",), intensity=6):
    j = post("/entries", {"journal_type": "check_in"}); u = f"/entries/{j['id']}"
    answer_check_in(u, mood=mood, triggers=list(triggers), trigger_note="likha hua note",
                    thinking_traps=list(traps), feelings=list(feelings), feeling_intensity=intensity)
    with S() as w:
        row = w.get(Entry, uuid.UUID(j["id"])); row.completed_at = when; w.commit()
    return j["id"]


def savouring_at(when, event="chai with ammi", significance="felt looked after"):
    return done("savouring", when, [
        ("event", event), ("significance", significance), ("feelings", ["hopeful"])])


def thought_at(when, traps=("mind_reading",), revised="maybe she was just busy"):
    return done("thought", when, [
        ("name", None), ("situation", "message seen, no reply"),
        ("automatic_thought", "she is annoyed with me"), ("thinking_traps", list(traps)),
        ("feelings", ["hurt"]), ("emotions_before", {"hurt": 70}), ("emotions_after", {"hurt": 40}),
        ("supporting_evidence", None), ("contradicting_evidence", None), ("revised_thought", revised)])


def exposure_at(when, attempted=True, learning="utna bura nahi tha"):
    steps = [("feared_outcome", "lift mein phans jaungi"), ("distress_before", 8),
             ("planned_activity", "lift lena hai"), ("_resume", None)]
    if attempted:
        steps += [("post_account", "kar liya"), ("distress_during", 7),
                  ("distress_after", 4), ("learning", learning)]
    else:
        steps += [("post_account", None), ("distress_during", None),
                  ("distress_after", None), ("learning", None)]
    return done("exposure", when, steps)


# Two entries whose UTC day is the same Sunday, but whose local days are not:
# one is 00:30 on Monday (this week), one is 23:30 on Sunday (the week before).
inside = check_in_at(at(0, 0, 30), mood=2)
outside = check_in_at(at(-1, 23, 30), mood=5)
check_in_at(at(2), mood=4, traps=["mind_reading"])
savouring_at(at(3))
thought_at(at(4))
thought_at(at(5), traps=["catastrophising"], revised="")   # a different pattern, no balanced thought
exposure_at(at(5), attempted=True)
exposure_at(at(6), attempted=False)                # planned, never tried
draft = post("/entries", {"journal_type": "check_in"})  # started, never finished

w = c.get("/insights/weekly", params={"week_start": MON.isoformat(), "tz_offset": TZ,
                                      "narrative": False}).json()
ov = w["overview"]
check(w["week_start"] == MON.isoformat() and w["week_end"] == (MON + timedelta(days=6)).isoformat(),
      f"the week is Monday to Sunday: {w['week_start']}..{w['week_end']}")
check(ov["entries"] == 7, f"only this week's completed entries count: {ov['entries']}")
check([p["entry_id"] for p in w["mood"]["points"]].count(inside) == 1
      and outside not in [p["entry_id"] for p in w["mood"]["points"]],
      "00:30 Monday is this week and 23:30 Sunday is not, though both are the same UTC day")
check(draft["id"] not in [p["entry_id"] for p in w["mood"]["points"]], "a draft is not in the report")
check((ov["check_in"], ov["thought"], ov["savouring"]) == (2, 2, 1),
      f"each journal counted separately: {ov}")
check((ov["exposure_planned"], ov["exposure_attempted"]) == (2, 1),
      f"a step planned is not a step tried: {ov['exposure_planned']} planned, {ov['exposure_attempted']} tried")
check(len(w["mood"]["points"]) == 2 and all(p["value"] is not None for p in w["mood"]["points"]),
      "savouring and thought records have no mood, and no mood is invented for them")
check((w["mood"]["min"], w["mood"]["max"]) == (1, 5), "the scale ends come from the scale")

# What she wrote for herself, quoted back with a way to the entry.
kinds = {card["kind"] for card in w["helped"]}
check(kinds == {"significance", "revised_thought", "learning"},
      f"helpful moments come from all three journals that hold one: {kinds}")
check(all(card["entry_id"] and card["text"] for card in w["helped"]),
      "each one carries its entry so she can open the original")
check(not any("helped" in card["blurb"].lower() or "worked" in card["blurb"].lower() for card in w["helped"]),
      "and none of them claims it helped — only she can say that")

wins = {win["id"]: win for win in w["wins"]}
check(wins["thought"]["count"] == 2 and wins["exposure"]["count"] == 1,
      f"small wins count actions, and the untried step is not one: {[(k, v['count']) for k, v in wins.items()]}")
check(all(win["count"] > 0 for win in w["wins"]), "nothing she did not do is listed as a win")
check("2 check-ins" in wins["check_in"]["detail"] and wins["exposure"]["detail"].startswith("1 step "),
      f"and they are worded for one or many: {wins['check_in']['detail']!r}, {wins['exposure']['detail']!r}")

# Journal-specific sections read their own fields.
jr = w["journals"]
check(jr["thought"]["balanced_thoughts"] == 1, "one of the two thought records holds a balanced thought")
check(jr["savouring"]["moments"][0]["significance"] == "felt looked after", "savouring keeps why it mattered")
check({t["id"] for t in jr["thought"]["patterns"]} == {"mind_reading", "catastrophising"},
      "patterns come from thought records, each counted")
check(jr["exposure"]["planned"] == 2 and jr["exposure"]["attempted"] == 1, "planned and attempted stay apart")

print("--- T2: badges earn from actions, once")
ids = {b["id"] for b in w["badges"]["new"]}
check({"first_steps", "finding_my_rhythm", "four_ways_to_reflect", "thought_explorer",
       "another_perspective", "moment_keeper", "brave_first_step"} <= ids,
      f"the badges her entries qualify her for: {sorted(ids)}")
check("pattern_noticer" not in ids, "and not one she has not earned: the pattern appears in a single record")
again = c.get("/insights/weekly", params={"week_start": MON.isoformat(), "tz_offset": TZ,
                                          "narrative": False}).json()
check(again["badges"]["new"] == [], "regenerating the report awards nothing a second time")
with S() as s:
    rows = s.query(EarnedBadge).filter(EarnedBadge.account_id == wacct.id).all()
check(len(rows) == len({r.badge_id for r in rows}), "and no badge is stored twice")
check(all(r.entry_ids for r in rows), "each one records which entries earned it")

thought_at(at(6), traps=["mind_reading"])  # the same pattern, a second time
later = c.get("/insights/weekly", params={"week_start": MON.isoformat(), "tz_offset": TZ,
                                          "narrative": False}).json()
check("pattern_noticer" in {b["id"] for b in later["badges"]["new"]},
      "noticing one pattern in two separate records earns Pattern Noticer")
every = c.get("/insights/badges").json()["badges"]
check(len(every) == len(bdg.BY_ID) and every[0]["earned"], "the badge screen shows earned and locked, earned first")
check(all(b["how"] for b in every), "and every badge says what earns it")
check(not any(word in json.dumps(every).lower()
              for word in ("recovered", "cured", "streak", "happy", "improved")),
      "no badge is about mood, streaks, or getting better")

print("--- T3: carry into next week")
NEXT = MON + timedelta(days=7)
post("/insights/weekly/reflection", {"week_start": MON.isoformat(), "text": "ek cheez roz likhni hai"})
saved = c.get("/insights/weekly", params={"week_start": MON.isoformat(), "tz_offset": TZ,
                                          "narrative": False}).json()
check(saved["reflection"] == "ek cheez roz likhni hai", "what she chose to carry is saved on her week")
nxt = c.get("/insights/weekly", params={"week_start": NEXT.isoformat(), "tz_offset": TZ,
                                        "narrative": False}).json()
check(nxt["carried_in"] == "ek cheez roz likhni hai", "and shown again at the top of the next week")
post("/insights/weekly/reflection", {"week_start": MON.isoformat(), "text": "badal di"})
check(c.get("/insights/weekly", params={"week_start": MON.isoformat(), "tz_offset": TZ,
                                        "narrative": False}).json()["reflection"] == "badal di",
      "she can change it")
post("/insights/weekly/reflection", {"week_start": MON.isoformat(), "text": "   "})
check(c.get("/insights/weekly", params={"week_start": MON.isoformat(), "tz_offset": TZ,
                                        "narrative": False}).json()["reflection"] is None,
      "and take it back")
check(c.post("/insights/weekly/reflection",
             json={"week_start": MON.isoformat(), "text": "x" * 400}).status_code == 422,
      "a reflection is one thought, not an essay")

print("--- T4: empty weeks, history, and whose week it is")
QUIET = MON - timedelta(days=21)
empty = c.get("/insights/weekly", params={"week_start": QUIET.isoformat(), "tz_offset": TZ,
                                          "narrative": False}).json()
check(empty["overview"]["entries"] == 0 and empty["mood"]["points"] == [] and empty["wins"] == [],
      "a week with nothing in it says nothing, rather than showing zeros as achievements")
check(empty["helped"] == [] and empty["journals"]["savouring"]["moments"] == [],
      "and invents no cards to fill the space")
weeks = c.get("/insights/weekly/history", params={"tz_offset": TZ}).json()["weeks"]
check(any(x["week_start"] == MON.isoformat() and x["entries"] == 8 for x in weeks),
      f"history lists the weeks she wrote in, with their counts: {weeks[:3]}")
check(QUIET.isoformat() not in [x["week_start"] for x in weeks], "and not the ones she did not")
check(sum(1 for x in weeks if x["is_current"]) == 1 and weeks[0]["is_current"],
      "this week is always there to land on, newest first")
check(c.get("/insights/weekly", params={"tz_offset": TZ, "narrative": False}).json()["is_current"],
      "no week_start opens this week")
mid = c.get("/insights/weekly", params={"week_start": (MON + timedelta(days=3)).isoformat(),
                                        "tz_offset": TZ, "narrative": False}).json()
check(mid["week_start"] == MON.isoformat(), "any day in a week opens that week")

app.dependency_overrides[get_current_user] = lambda: S().get(Account, acct.id)
theirs = c.get("/insights/weekly", params={"week_start": MON.isoformat(), "tz_offset": TZ,
                                           "narrative": False}).json()
check(inside not in json.dumps(theirs),
      "one account's week never carries another's entries")
check(inside not in json.dumps(c.get("/insights/badges").json()),
      "nor name them in another's badges")
app.dependency_overrides[get_current_user] = lambda: S().get(Account, wacct.id)

print("--- T5: the narrative may not carry a figure, and may fail")
real = c.get("/insights/weekly", params={"week_start": MON.isoformat(), "tz_offset": TZ,
                                         "narrative": False}).json()
with patch.object(nar.llm_client, "generate_closing", return_value="You wrote 3 entries this week."):
    check(nar.write(real) is None, "a narrative with a number in it is thrown away, not shown")
with patch.object(nar.llm_client, "generate_closing", return_value=""):
    check(nar.write(real) is None, "a model that will not answer leaves the report standing")
with patch.object(nar.llm_client, "generate_closing",
                  return_value="Feeling tired came up more than once. What would you like to notice next week?"):
    check(nar.write(real), "a grounded one is kept")
check(nar.write(empty) is None, "and a week too thin to describe gets no narrative at all")
with patch.object(nar.llm_client, "generate_closing") as spy:
    nar.write(empty)
check(not spy.called, "which costs nothing: the model is not called for it")
evidence = nar._evidence(real)
check("mind_reading" not in evidence and "entry_id" not in evidence,
      "the model is shown her words and labels, never ids or raw field names")

print("--- U: the check-in follows her, not her mood number")
settings.OPENAI_API_KEY = None; settings.LLM_MOCK = False
app.dependency_overrides[get_current_user] = lambda: S().get(Account, acct.id)


def asked_at(mood, **answers):
    """Which values a check-in actually asks for, given these answers."""
    j = post("/entries", {"journal_type": "check_in"}); u = f"/entries/{j['id']}"
    _, asked, _ = answer_check_in(u, mood=mood, **answers)
    return asked


WROTE = "kuch chubh raha hai"
low, mixed, good = asked_at(1, trigger_note=WROTE), asked_at(3), asked_at(5)
check(low[0] == "mood" and mixed[0] == "mood" and good[0] == "mood",
      "mood is asked first, because the one condition that reads it needs it")
check(all("feelings" in path and "feeling_intensity" in path for path in (low, mixed, good)),
      f"feelings and their strength are asked at every mood: {low}, {mixed}, {good}")
check(all("trigger_note" in path for path in (low, mixed, good)),
      "and so is the open note — whatever she rated, she can say what she came to say")

check("triggers" in low and "triggers" in mixed, f"a low or mixed mood is asked what is around it: {mixed}")
check("triggers" not in good,
      f"a good mood is not asked what is behind it, because nobody asked that question: {good}")
check(asked_at(4) == good and asked_at(2, trigger_note=WROTE) == low,
      "4 and 5 behave alike, and so do 1 and 2")

# Spec 6: a low mood is not evidence of distorted thinking. Something she wrote can be.
silent = asked_at(1, trigger_note="")
check("thinking_traps" not in silent,
      f"a difficult mood on its own never raises the thinking patterns: {silent}")
check("thinking_traps" in low,
      f"a note does, because there is something of hers to look at: {low}")
wrote_at_five = asked_at(5, trigger_note="I keep thinking I ruined it for everyone")
check("thinking_traps" in wrote_at_five,
      f"and that holds at a good mood too — the note is what counts, not the number: {wrote_at_five}")

# She can feel good overall and still be carrying something. Nothing closes that off.
j = post("/entries", {"journal_type": "check_in"}); u = f"/entries/{j['id']}"
state, asked, _ = answer_check_in(u, mood=5, feelings=["hurt"], feeling_intensity=8,
                                  trigger_note="achha din tha lekin aik baat chubh gayi")
check("trigger_note" in asked and state["status"] == "completed",
      f"a good mood with a hard feeling in it still has somewhere to put it: {asked}")
recorded = {v.key: v.status for v in S().query(CapturedValue).filter(
    CapturedValue.entry_id == uuid.UUID(j["id"])).all()}
check(recorded["feelings"] == "answered" and recorded["feeling_intensity"] == "answered",
      f"and both are kept as she gave them: {recorded}")
check(recorded.get("triggers") == "not_asked",
      "what she was never asked is stored as not_asked, not as a skip")

# A high rating must never suppress a disclosure.
j = post("/entries", {"journal_type": "check_in"}); u = f"/entries/{j['id']}"
post(u + "/captures", {"value_id": "mood", "value": 5})
post(u + "/captures", {"value_id": "feelings", "value": []})
danger = post(u + "/captures", {"value_id": "trigger_note", "value": "honestly I want to end my life"})
check(danger["crisis_event"]["tier"] in ("danger", "emergency"),
      f"a disclosure at mood 5 still reaches the crisis path: {danger['crisis_event']}")
check(any(m["kind"] == "crisis" for m in danger["messages"]), "with fixed crisis content in the thread")
finished = post(u + "/captures", {"value_id": "thinking_traps", "value": [], "skipped": True})
check(finished["status"] == "completed" and finished["conversation_status"] == "suppressed",
      "the entry still finishes, and no AI is offered after it")

# The feelings list opens where she is, not where the file happens to start.
def valence_at(mood):
    j = post("/entries", {"journal_type": "check_in"}); u = f"/entries/{j['id']}"
    state = post(u + "/captures", {"value_id": "mood", "value": mood})
    return state["next_capture"]["value_id"], state["next_capture"]["prefer_valence"]

check(valence_at(5) == ("feelings", "positive") and valence_at(4) == ("feelings", "positive"),
      "a good mood opens the feelings on the good ones")
check(valence_at(1) == ("feelings", "negative") and valence_at(2) == ("feelings", "negative"),
      "a difficult mood opens them on the difficult ones")
check(valence_at(3) == ("feelings", None),
      "a mixed mood is not sorted into either half")
# The app passes that straight on to the library (?prefer=), and her own words
# stay at the top whatever it is.
def feelings_order(prefer=None):
    rows = c.get("/libraries/feelings", params={"prefer": prefer} if prefer else {}).json()["categories"]
    return [cat["id"] for cat in rows if cat["id"] != "my_words"], rows

good, all_good = feelings_order("positive")
hard, all_hard = feelings_order("negative")
check(all(cat["valence"] == "positive"
          for cat in all_good if cat["id"] != "my_words" and good.index(cat["id"]) < 2),
      f"asking for positive puts the good feelings first: {good}")
check(hard[0] in ("sad", "worried", "angry", "hurt"),
      f"and asking for negative puts the difficult ones first: {hard}")
check(sorted(good) == sorted(hard) == sorted(feelings_order()[0]),
      "with nothing dropped either way — only the order changes")

# Every point of a 0-10 scale means something, not just the two ends.
for scale in ("intensity_11", "distress_11"):
    labels = [o["label"] for o in capture.SCALES[scale]]
    check(len(labels) == len(set(labels)) == 11 and all(l.strip() for l in labels),
          f"{scale}: all eleven points carry their own words")

# The intensity scale is asked about whatever feelings she named — joy as often
# as hurt — so none of its words may treat the feeling as a problem. "Hard to
# set aside" and "Almost too much" read as nonsense against "joyful", which is
# how they were found. distress_11 is exempt: it is about distress.
UNWELCOME = ("too much", "hard to", "set aside", "overwhelm", "severe",
             "distress", "unbearable", "can't", "cannot")
offending = [o["label"] for o in capture.SCALES["intensity_11"]
             if any(word in o["label"].lower() for word in UNWELCOME)]
check(not offending,
      f"intensity words fit a good feeling as well as a hard one: {offending}")

check(capture.fallback_wording("check_in", "trigger_note"), "the note has stored wording to fall back on")
check("good_note" not in json.dumps(capture.SCHEDULES["check_in"]),
      "the 'say something positive' step is gone — it had no place at a hard moment")
# The model words every capture prompt, so a value it has never been told about
# gets whatever the id happens to suggest. That is how a good mood came to be
# asked for "something positive" while she was saying she felt hurt.
described = md_loader.load_capture_instructions()
undescribed = sorted({v["id"] for values in capture.SCHEDULES.values() for v in values}
                     - {value_id for value_id in capture.all_value_ids() if f"`{value_id}`" in described})
print("--- V2: the thought journal, per-feeling ratings and going back")
settings.OPENAI_API_KEY = None; settings.LLM_MOCK = False
app.dependency_overrides[get_current_user] = lambda: S().get(Account, acct.id)


def walk_thought(answers):
    """Drive a thought journal from next_capture, collecting each spec it sends."""
    j = post("/entries", {"journal_type": "thought"}); u = f"/entries/{j['id']}"
    specs = []
    while j["next_capture"]:
        spec = j["next_capture"]; specs.append(spec)
        value = answers.get(spec["value_id"])
        j = post(u + "/captures", {"value_id": spec["value_id"], "value": value,
                                   "skipped": value is None})
    return u, j, specs


TJ = {"name": "message seen", "situation": "msg dekha, jawab nahi aaya",
      "automatic_thought": "woh mujhse naraz hai", "feelings": ["hurt", "worried"],
      "emotions_before": {"hurt": 80, "worried": 60}, "thinking_traps": ["mind_reading"],
      "supporting_evidence": "kal bhi khafa thi", "contradicting_evidence": "woh busy bhi hoti hai",
      "revised_thought": "pata nahi, shayad busy hai", "emotions_after": {"hurt": 40, "worried": 30}}
u, j, specs = walk_thought(TJ)
order = [sp["value_id"] for sp in specs]
check(order.index("feelings") < order.index("thinking_traps"),
      f"the feelings are named before the patterns: {order}")
check(order.index("emotions_before") < order.index("revised_thought") < order.index("emotions_after"),
      "the second rating comes after the work, not before it")

# The rating card is built from what she chose, not from the whole library.
rating = next(sp for sp in specs if sp["value_id"] == "emotions_before")
check([i["id"] for i in rating["rates"]["items"]] == ["hurt", "worried"],
      f"a rating only lists the feelings she chose: {rating['rates']['items']}")
check([i["name"] for i in rating["rates"]["items"]] == ["Hurt", "Worried"], "with their names")
check((rating["rates"]["min"], rating["rates"]["max"]) == (0, 100), "rated 0-100, as the brief asks")
after = next(sp for sp in specs if sp["value_id"] == "emotions_after")
check(after["rates"]["carried"] == {"hurt": 80, "worried": 60},
      "the second rating starts from the first, so she adjusts rather than starts over")

# Step counts are counted, never hardcoded: every step within the total, in order.
steps = [(sp["step"], sp["total"]) for sp in specs]
check(all(1 <= a <= b for a, b in steps), f"every step is inside its total: {steps}")
check([a for a, _ in steps] == sorted({a for a, _ in steps}), f"steps only go forward: {steps}")
check(j["status"] == "completed", "the entry completes")

# Going back. /captures stays forward-only; the revision is its own request.
again = c.post(u + "/captures", json={"value_id": "revised_thought", "value": "phir se"})
check(again.status_code == 200 and "phir se" not in [m["content"] for m in again.json()["messages"]],
      "/captures treats a repeat as the lost-response retry it is, and changes nothing")
r = c.patch(u + "/captures/revised_thought", json={"value": "woh busy thi, baat mujh par nahi thi"})
check(r.status_code == 200, f"but it can be revised ({r.status_code})")
with S() as w:
    rows = {v.key: v for v in w.query(CapturedValue).filter(
        CapturedValue.entry_id == uuid.UUID(u.split("/")[-1])).all()}
    check(json.loads(rows["revised_thought"].value) == "woh busy thi, baat mujh par nahi thi",
          "the stored answer is the revised one")
said = [m["content"] for m in r.json()["messages"] if m["value_id"] == "revised_thought"]
check("pata nahi, shayad busy hai" in said and "woh busy thi, baat mujh par nahi thi" in said,
      f"both versions stay in the thread — nothing she wrote is destroyed: {said}")

# Dropping a feeling drops its ratings with it.
r = c.patch(u + "/captures/feelings", json={"value": ["hurt"]})
check(r.status_code == 200, "the feelings can be revised too")
with S() as w:
    rows = {v.key: v for v in w.query(CapturedValue).filter(
        CapturedValue.entry_id == uuid.UUID(u.split("/")[-1])).all()}
    check(json.loads(rows["emotions_before"].value) == {"hurt": 80},
          f"a dropped feeling takes its rating with it: {rows['emotions_before'].value}")
    check(json.loads(rows["emotions_after"].value) == {"hurt": 40}, "in both ratings")

# What a revision may not do.
check(c.patch(u + "/captures/emotions_before", json={"value": {"worried": 50}}).status_code == 422,
      "a rating of a feeling she no longer lists is refused")
check(c.patch(u + "/captures/emotions_before", json={"value": {"hurt": 140}}).status_code == 422,
      "a rating outside 0-100 is refused")
check(c.patch(u + "/captures/situation", json={"value": None, "skipped": True}).status_code == 422,
      "a required answer cannot be revised into nothing")
check(c.patch(u + "/captures/mood", json={"value": 3}).status_code == 404,
      "a value this journal never asked for is not an answer to revise")

# Another account cannot reach it, revision or not (ownership, not a client id).
other = Account(email="revise@x.com")
with S() as w:
    w.add(other); w.commit(); w.refresh(other); other_id = other.id
app.dependency_overrides[get_current_user] = lambda: S().get(Account, other_id)
check(c.patch(u + "/captures/revised_thought", json={"value": "mera nahi hai"}).status_code == 404,
      "someone else" + chr(39) + "s entry is not there to be revised")
app.dependency_overrides[get_current_user] = lambda: S().get(Account, acct.id)

# Once Echo has read the answers and replied to them, they are no longer open:
# the transcript would otherwise describe something she never said.
u2, j2, _ = walk_thought(TJ)
j2 = post(u2 + "/conversation", {"choice": "stop"})
check(j2["conversation_status"] == "declined", "turning the conversation down is not having one")
check(c.patch(u2 + "/captures/situation", json={"value": "kuch aur"}).status_code == 200,
      "and leaves her free to change what she wrote — nothing has read it")
with S() as w:
    row = w.get(Entry, uuid.UUID(u2.split("/")[-1])); row.conversation_status = "active"; w.commit()
check(c.patch(u2 + "/captures/situation", json={"value": "kuch aur phir"}).status_code == 409,
      "but once Echo has replied to her answers, they are not rewritten underneath it")

# What the two ratings add up to in Insights. Its own account, so the averages
# are of these three entries and nothing else in the suite.
rater = Account(email="ratings@x.com")
with S() as w:
    w.add(rater); w.commit(); w.refresh(rater); rater_id = rater.id
app.dependency_overrides[get_current_user] = lambda: S().get(Account, rater_id)
for before, after in (({"hurt": 80, "worried": 50}, {"hurt": 40, "worried": 60}),
                      ({"hurt": 70}, {"hurt": 50}),
                      ({"hurt": 60}, {"hurt": 30})):
    walk_thought({**TJ, "feelings": list(before),
                  "emotions_before": before, "emotions_after": after})
shifts = {r["id"]: r for r in c.get("/insights", params={"period": "30d"}).json()["rating_shifts"]}
check(shifts["hurt"]["entries"] == 3 and shifts["worried"]["entries"] == 1,
      f"a feeling is compared only in the entries that rated it twice: {shifts}")
check((shifts["hurt"]["before"], shifts["hurt"]["after"]) == (70, 40),
      f"before and after are her own numbers, averaged: {shifts['hurt']}")
check(shifts["hurt"]["change"] == -30, "and the change is the direction between them")
check(shifts["worried"]["change"] == 10,
      "a feeling that went up is reported going up — nothing assumes she felt better")
check(shifts["hurt"]["label"] == "Hurt", "named as she saw it named")
check(list(shifts) == ["hurt", "worried"], "the most-rated feeling comes first, not the alphabet")
app.dependency_overrides[get_current_user] = lambda: S().get(Account, acct.id)

print("--- V3: another perspective, offered only when she asks")
settings.OPENAI_API_KEY = "test"
perspective_calls = []


def parse_perspective(**kw):
    fmt = kw["text_format"]
    if fmt is lc.CapturePrompt:
        want = kw["instructions"].split("# Value to request\n")[1].strip()
        return SimpleNamespace(output_parsed=lc.CapturePrompt(value_id=want, message_text=f"AI asks {want}"))
    if fmt is lc.DomainResult:
        return SimpleNamespace(output_parsed=lc.DomainResult(safety=NO_HARM, domains=["general"], risk_tier="clear"))
    if fmt is lc.Perspective:
        perspective_calls.append(kw["instructions"])
        return SimpleNamespace(output_parsed=lc.Perspective(
            message_text="Uska jawab na dena kaafi mehsoos hota hai. Ho sakta hai woh busy ho, "
                         "aur yeh dono baatein saath sach ho sakti hain."))
    raise AssertionError(fmt)


fake3 = MagicMock(); fake3.responses.parse.side_effect = parse_perspective
with patch.object(lc, "_get_client", return_value=fake3):
    j = post("/entries", {"journal_type": "thought"}); up = f"/entries/{j['id']}"
    check(c.post(up + "/perspective").status_code == 409,
          "nothing to offer a perspective on before the thought is written")
    post(up + "/captures", {"value_id": "name", "value": None, "skipped": True})
    post(up + "/captures", {"value_id": "situation", "value": "msg dekha, jawab nahi aaya"})
    post(up + "/captures", {"value_id": "automatic_thought", "value": "woh mujhse naraz hai"})
    r = c.post(up + "/perspective")
    check(r.status_code == 200, f"once it is, she can ask for another angle ({r.status_code})")
    j = r.json()
    offered = [m for m in j["messages"] if m["kind"] == "perspective"]
    check(len(offered) == 1 and offered[0]["role"] == "ai",
          "it arrives as Echo's, under its own kind, so the app can label it")
    check(all(m["kind"] != "capture_answer" or "busy ho" not in m["content"] for m in j["messages"]),
          "and is never written into an answer of hers")
    with S() as w:
        saved = w.query(CapturedValue).filter(
            CapturedValue.entry_id == uuid.UUID(up.split("/")[-1]),
            CapturedValue.key == "revised_thought").first()
    check(saved is None, "the balanced thought is still hers to write, and still unwritten")
    check(j["next_capture"]["value_id"] == "feelings",
          f"asking for it does not move the entry on: {j['next_capture']['value_id']}")
    check(c.post(up + "/perspective").status_code == 409, "and it is offered once, not on tap")

    sent = perspective_calls[0]
    check("woh mujhse naraz hai" in sent, "the suggestion is built from what she recorded")
    check("Roman Urdu" in sent, "and answers in the script she has been writing in")
    check("suggestion she may take, change, or throw away" in sent,
          "the prompt says plainly that it is hers to reject")
    for value_id in ("mood", "distress_before"):
        check(value_id not in sent, f"with nothing from another journal in it ({value_id})")

    jc = post("/entries", {"journal_type": "check_in"})
    check(c.post(f"/entries/{jc['id']}/perspective").status_code == 409,
          "it belongs to the thought journal, not to every entry")

# When it cannot be written, nothing stands in for it: no stored line pretends
# to be a suggestion, and her own answer was never waiting on it.
with patch.object(lc, "generate_perspective", return_value=None):
    j = post("/entries", {"journal_type": "thought"}); uq = f"/entries/{j['id']}"
    post(uq + "/captures", {"value_id": "name", "value": None, "skipped": True})
    post(uq + "/captures", {"value_id": "situation", "value": "x"})
    post(uq + "/captures", {"value_id": "automatic_thought", "value": "y"})
    r = c.post(uq + "/perspective")
    check(r.status_code == 503, f"a suggestion nobody wrote is not offered ({r.status_code})")
    check(c.get(uq).json()["next_capture"] is not None, "and the entry carries on regardless")
settings.OPENAI_API_KEY = None

check(not undescribed, f"every value any journal asks for is described to the model: missing {undescribed}")

# An example written in the same shape as the live prompt gets read as the live
# prompt. "feelings = Hurt" sat in an illustration of what NOT to say, and the
# model lifted it into a real question on an entry where feelings was skipped.
# Every example's data must stay inside a fence that says it is invented.
LIVE_SHAPE = ("recorded:", "asking for:", "previous answer", "previous answers",
              "value to request", "mood =", "feelings =", "feeling_intensity =")
FENCE = "`" * 3
fenced, loose, unlabelled = False, [], []
for line in described.splitlines():
    stripped = line.strip()
    if stripped.startswith(FENCE):
        if not fenced and "invented" not in stripped:
            unlabelled.append(stripped[:60])
        fenced = not fenced
        continue
    if not fenced and any(stripped.lower().startswith(shape) for shape in LIVE_SHAPE):
        loose.append(stripped[:60])
check(not loose, f"example data is fenced, so it cannot be mistaken for this entry: {loose}")
check(not fenced, "every example fence in the capture instructions is closed")
check(not unlabelled, f"each example says its contents are invented: {unlabelled}")

# A bad example written as a quotable sentence is a template: with one spelled
# out, 5 of 6 generated notes opened with its first sentence verbatim. Name the
# mistake instead, so there is nothing to copy.
quoted_bad = [line.strip()[:60] for line in described.splitlines()
              if line.strip().lower().startswith("bad:") and chr(34) in line]
check(not quoted_bad, f"no example quotes a bad message word for word: {quoted_bad}")

print("--- preferred name (greeting)")
r = c.put("/onboarding/name", json={"name": "  Hajira \n  Azfar\x07 "}).json()
check(r["preferred_name"] == "Hajira Azfar", "a name is trimmed, its spaces collapsed, control characters dropped")
check(c.get("/onboarding/status").json()["preferred_name"] == "Hajira Azfar", "the onboarding status carries the name")
check(c.get("/auth/me").json()["preferred_name"] == "Hajira Azfar", "so does /auth/me")
check(c.put("/onboarding/name", json={"name": "x" * 41}).status_code == 422, "a name over 40 characters is refused")
check(c.put("/onboarding/name", json={"name": "   "}).json()["preferred_name"] is None, "a blank name clears it")

print("--- account deletion (SRS 4.12)")
from app.core.account_deletion import ACCOUNT_TABLES
from app.core.security import hash_password
from app.models.library_favourite import LibraryFavourite
keyed = {t.name for t in Base.metadata.tables.values() if "account_id" in t.c}
check(keyed == {m.__tablename__ for m in ACCOUNT_TABLES} | {"entries"},
      f"deletion covers every table with an account_id: {sorted(keyed)}")

s = S()
gone = Account(email="gone@x.com", password_hash=hash_password("correct horse"))
s.add(gone); s.commit(); gone_id = gone.id
app.dependency_overrides[get_current_user] = lambda: S().get(Account, gone_id)
gone_entry = uuid.UUID(post("/entries", {"journal_type": "check_in"})["id"])
post("/library/understanding-thinking-traps/favourite", {})
s = S()
check(s.query(Message).filter(Message.entry_id == gone_entry).count() > 0
      and s.query(LibraryFavourite).filter(LibraryFavourite.account_id == gone_id).count() == 1,
      "the account to delete has an entry, its messages and a saved article")

check(c.post("/auth/delete-account", json={"password": "wrong"}).status_code == 403, "a wrong password is refused")
check(S().get(Account, gone_id) is not None, "and deletes nothing")
check(c.post("/auth/delete-account", json={"password": "correct horse"}).status_code == 204,
      "the right password deletes the account")
s = S()
check(s.get(Account, gone_id) is None, "the account is gone")
check(s.query(Entry).filter(Entry.account_id == gone_id).count() == 0, "its entries are gone")
check(s.query(Message).filter(Message.entry_id == gone_entry).count() == 0, "their messages are gone")
check(s.query(LibraryFavourite).filter(LibraryFavourite.account_id == gone_id).count() == 0, "its saved articles are gone")
check(s.get(Account, acct.id) is not None and s.query(Entry).filter(Entry.account_id == acct.id).count() > 0,
      "nobody else's account or entries are touched")
app.dependency_overrides[get_current_user] = lambda: S().get(Account, acct.id)

print("--- PDF report (Phase B)")
r = c.get("/insights/report.pdf?period=all&tz_offset=300")
check(r.status_code == 200 and r.headers["content-type"] == "application/pdf", "the report comes back as a PDF")
check(r.content.startswith(b"%PDF") and len(r.content) > 2000, "and it is a real PDF document")
check('filename="mind-doc-report-' in r.headers["content-disposition"] and r.headers["cache-control"] == "no-store",
      "it downloads with a dated name and is never cached")
check(c.get("/insights/report.pdf?period=forever").status_code == 422, "an unknown period is refused")

from app.core import insights as insights_core, pdf_report
from datetime import date as _date
data = insights_core.build(S(), acct.id, "all", {}, tz_offset=300)
check(pdf_report.render(data, name="A <b>tag</b> & name", made_on=_date(2026, 10, 22)).startswith(b"%PDF"),
      "a name with markup characters is escaped, not interpreted")
s = S()
empty = Account(email="empty@x.com"); s.add(empty); s.commit(); empty_id = empty.id
app.dependency_overrides[get_current_user] = lambda: S().get(Account, empty_id)
r = c.get("/insights/report.pdf?period=7d")
check(r.status_code == 200 and r.content.startswith(b"%PDF"), "an account with no entries still gets a report, with its empty states")
app.dependency_overrides[get_current_user] = lambda: S().get(Account, acct.id)

print(f"\nALL {ok} CHECKS PASSED")
