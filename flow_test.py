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


def complete_check_in(note="", traps=None):
    j = post("/entries", {"journal_type": "check_in"}); u = f"/entries/{j['id']}"
    post(u + "/captures", {"value_id": "mood", "value": 2})
    post(u + "/captures", {"value_id": "triggers", "value": ["workload"]})
    last = post(u + "/captures", {"value_id": "trigger_note", "value": note})
    post(u + "/captures", {"value_id": "thinking_traps", "value": traps or []})
    post(u + "/captures", {"value_id": "feelings", "value": ["tired"]})
    j = post(u + "/captures", {"value_id": "feeling_intensity", "value": 6})
    return u, j, last


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
for vid, val in [("mood", 3), ("triggers", []), ("trigger_note", ""), ("thinking_traps", [])]:
    j = post(uf + "/captures", {"value_id": vid, "value": val})
j = post(uf + "/captures", {"value_id": "feelings", "value": []})
check(j["status"] == "completed" and j["next_capture"] is None,
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
check(j["crisis_event"]["tier"] == "emergency" and "1122" in j["crisis_event"]["text"], "emergency event with fixed emergency content")
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
model = {"risk": "clear", "reflection_crisis": False, "shift": False}


def parse(**kw):
    calls.append(kw["text_format"].__name__)
    fmt = kw["text_format"]
    if fmt is lc.CapturePrompt:
        want = kw["instructions"].split("# Value to request\n")[1].strip()
        return SimpleNamespace(output_parsed=lc.CapturePrompt(value_id=want, message_text=f"AI asks {want}"))
    if fmt is lc.DomainResult:
        return SimpleNamespace(output_parsed=lc.DomainResult(domains=["overthinking"], risk_tier=model["risk"]))
    return SimpleNamespace(output_parsed=lc.Reflection(same_concern_count=1, shift_noticed=model.get("shift", False), response_text="Model reply here.", session_end=False, closure_reason="none",
                                                       referral_flag=False, crisis_indicators_noticed=model["reflection_crisis"]))


fake = MagicMock(); fake.responses.parse.side_effect = parse
with patch.object(lc, "_get_client", return_value=fake):
    model["risk"] = "danger"
    j = post("/entries", {"journal_type": "check_in"}); u = f"/entries/{j['id']}"
    post(u + "/captures", {"value_id": "mood", "value": 3})
    post(u + "/captures", {"value_id": "triggers", "value": []})
    calls.clear()
    j = post(u + "/captures", {"value_id": "trigger_note", "value": "sab theek hai bas"})  # lexical clear, model danger
    check(j["crisis_event"]["tier"] == "danger", "model tier wins when higher than rules (FR-CRIS-002)")
    check(calls == ["DomainResult"], f"after danger, no capture-prompt generation (calls: {calls})")
    check(j["messages"][-1]["content"] == "Did any of these thinking patterns show up? Pick any that fit, or skip.", "next prompt uses stored wording")
    calls.clear()
    post(u + "/captures", {"value_id": "thinking_traps", "value": []})
    post(u + "/captures", {"value_id": "feelings", "value": []})
    check(calls == [], "zero generative calls for the rest of a suppressed entry")

    model["risk"] = "clear"
    u, j, _ = complete_check_in(note="work")
    post(u + "/conversation", {"choice": "continue"})
    model["reflection_crisis"] = True
    j = post(u + "/messages", {"content": "whatever"})
    check(j["messages"][-1]["kind"] == "crisis" and "Model reply" not in j["messages"][-1]["content"], "model-noticed risk: reply replaced by fixed content")
    check(j["conversation_status"] == "suppressed", "and conversation suppressed")
    model["reflection_crisis"] = False

print("--- G: resume, list, delete")
settings.OPENAI_API_KEY = None
j = post("/entries", {"journal_type": "check_in"}); ud = f"/entries/{j['id']}"
post(ud + "/captures", {"value_id": "mood", "value": 4})
drafts = c.get("/entries", params={"status": "in_progress", "since_days": 7}).json()
check(drafts[0]["id"] == j["id"] and drafts[0]["mood"] == 4, "draft listed for resumption with mood")
g = c.get(ud).json()
check(g["next_capture"]["value_id"] == "triggers" and len(g["messages"]) == 3, "reopened draft resumes at the first unrecorded value")
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
j = post(u + "/captures", {"value_id": "triggers", "value": [t["term"]["id"], "money"]})
check(j["messages"][-2]["content"] == "Rishtedaar, Money", "own term selectable and shown by name")

j = post("/entries", {"journal_type": "check_in"}); u = f"/entries/{j['id']}"
post(u + "/captures", {"value_id": "mood", "value": 1})
t3 = post("/libraries/triggers/terms", {"name": "end my life", "entry_id": j["id"]})
g = c.get(u).json()
check(t3["crisis_event"]["tier"] == "danger" and g["crisis_tier"] == "danger", "danger term added inside an entry raises that entry's tier")
check(g["messages"][-1]["kind"] == "crisis", "and puts fixed crisis content in the thread")
for vid, val in [("triggers", []), ("trigger_note", ""), ("thinking_traps", []), ("feelings", [])]:
    g = post(u + "/captures", {"value_id": vid, "value": val})
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
        return SimpleNamespace(output_parsed=lc.DomainResult(domains=["positive"], risk_tier="clear"))
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
           "thinking_traps": ["mind_reading", "labelling"], "feelings": ["ashamed"], "feeling_intensity": 8, "supporting_evidence": "kisi ne meri baat nahi maani",
           "contradicting_evidence": "lead ne baad mein mera idea use kiya", "revised_thought": "shayad sab aisa nahi sochte"}
while j["next_capture"]:
    vid = j["next_capture"]["value_id"]; order.append(vid)
    j = post(ut + "/captures", {"value_id": vid, "value": answers[vid]})
check(order == ["name", "situation", "automatic_thought", "thinking_traps", "feelings", "feeling_intensity",
                "supporting_evidence", "contradicting_evidence", "revised_thought"], f"nine steps in SRS order: {order}")
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
    post(u + "/captures", {"value_id": "mood", "value": mood})
    post(u + "/captures", {"value_id": "triggers", "value": list(triggers)})
    post(u + "/captures", {"value_id": "trigger_note", "value": None, "skipped": True})
    post(u + "/captures", {"value_id": "thinking_traps", "value": list(traps)})
    post(u + "/captures", {"value_id": "feelings", "value": list(feelings)})
    if feelings:
        post(u + "/captures", {"value_id": "feeling_intensity", "value": intensity})
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
check([t["id"] for t in j["triggers"]][0] == "workload" and j["triggers"][0]["count"] == 2,
      "trigger counts, most frequent first (FR-INS-006)")
check(all("count" in t and "%" not in str(t) for t in j["triggers"]), "counts of entries, no percentages (FR-INS-007)")
check(j["triggers"][0]["name"] == "Workload", "items are labelled by name, not id")
traps = {t["id"]: t for t in j["thinking_traps"]}
check(traps["mind_reading"]["count"] == 2 and traps["catastrophising"]["count"] == 1, "thinking trap counts (FR-INS-010)")
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

# enough history in the previous period, so the change appears
for day in (35, 37, 39):
    finished_check_in(3, now - timedelta(days=day), traps=["mind_reading"])
traps = {t["id"]: t for t in c.get("/insights").json()["thinking_traps"]}
check(traps["mind_reading"].get("change") == -2, f"change against the previous period (FR-INS-011): {traps['mind_reading'].get('change')}")

print("--- T: streak and local days (module 4 + FR-INS-015)")
s = S(); s.query(Message).delete(); s.query(CapturedValue).delete(); s.query(Entry).delete(); s.commit()

def entry_on(when):
    """A completed check-in whose completed_at is exactly `when` (UTC)."""
    j = post("/entries", {"journal_type": "check_in"}); u = f"/entries/{j['id']}"
    for vid, val in [("mood", 3), ("triggers", []), ("trigger_note", ""), ("thinking_traps", []), ("feelings", [])]:
        post(u + "/captures", {"value_id": vid, "value": val})
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
    post(u + "/captures", {"value_id": "mood", "value": mood})
    post(u + "/captures", {"value_id": "triggers", "value": list(triggers)})
    post(u + "/captures", {"value_id": "trigger_note", "value": "", "skipped": True})
    post(u + "/captures", {"value_id": "thinking_traps", "value": list(traps)})
    post(u + "/captures", {"value_id": "feelings", "value": []})
    with S() as w:
        row = w.get(Entry, uuid.UUID(j["id"])); row.completed_at = when; w.commit()
    return j["id"]

now = datetime.utcnow()
# two weeks: the older one worse and full of mind reading, the newer one better
for back, mood, traps in [(16, 2, ["mind_reading"]), (15, 2, ["mind_reading", "catastrophising"]), (14, 3, ["mind_reading"]),
                          (3, 4, ["mind_reading"]), (2, 4, []), (1, 5, [])]:
    check_in_on(now - timedelta(days=back), mood, traps, triggers=["workload"])

j = c.get("/insights", params={"period": "3m", "tz_offset": PKT}).json()

trend = j["mood_trend"]
check(len(trend["points"]) >= 2, f"one point per week she wrote in ({len(trend['points'])})")
check(trend["points"][0]["value"] < trend["points"][-1]["value"],
      f"the weekly average moved with her mood: {[p['value'] for p in trend['points']]}")
check(sum(p["entries"] for p in trend["points"]) == 6, "every entry counts towards exactly one week")
check((trend["min"], trend["max"]) == (1, 5), "the trend carries the ends of the mood scale")

dist = {row["value"]: row["count"] for row in j["mood_distribution"]}
check(dist == {1: 0, 2: 2, 3: 1, 4: 2, 5: 1}, f"the distribution counts every point of the scale: {dist}")
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
check(workload["change"] == 0, f"workload: 3 this week and 3 the week before, so no change ({workload})")
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

print(f"\nALL {ok} CHECKS PASSED")
