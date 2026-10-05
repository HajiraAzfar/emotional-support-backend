import logging
import re
from dataclasses import dataclass

from app.core.llm_client import ChatReply, Reflection

logger = logging.getLogger(__name__)

# FR-AIR-009: how many times the same concern may be restated before the session
# closes, and how many minimal replies in a row count as disengagement. These are
# the defaults; a journal type can set its own in content/conversation.json,
# because an extended free write is meant to circle and to pause.
REPEAT_LIMIT = 3
MINIMAL_REPLIES = 2
# Conversation stages: exploring what she is stuck in, then one consolidating
# turn after she moves, then the closing message.
EXPLORING, CONFIRMING = "exploring", "confirming"

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?؟])\s+")
# The same split, keeping what separated the sentences (a space, or the line
# break before a numbered idea) so a trimmed reply keeps its shape.
_SENTENCE_SPLIT_KEEP = re.compile(r"(?<=[.!?؟])(\s+)")


@dataclass
class Outcome:
    reply_text: str
    crisis: bool  # the model noticed risk → caller records a danger-tier event
    referral: bool
    # She moved this turn: the next reply is the closing one (see required_closure).
    shift_noticed: bool = False
    session_end: bool = False
    closure_reason: str | None = None


_MINIMAL_REPLIES = {
    "ok", "okay", "k", "kk", "yeah", "yea", "yes", "yep", "no", "nope", "idk", "i dont know",
    "i don't know", "dunno", "hmm", "hm", "mm", "fine", "sure", "maybe", "whatever", "nothing",
    "haan", "han", "ji", "nahi", "pata nahi", "theek", "theek hai", "thik", "thik hai", "acha", "achha",
}


def is_minimal(text: str) -> bool:
    normalised = re.sub(r"[^\w\s']", "", text.lower()).strip()
    return normalised in _MINIMAL_REPLIES or len(normalised) <= 3


def required_closure(
    history: list[tuple[str, str]],
    text: str | None,
    containment_turns: int,
    stage: str | None = None,
    minimal_replies: int = MINIMAL_REPLIES,
) -> str | None:
    """
    Closure conditions the code can detect with certainty, checked before the
    reply is generated so the model writes a proper closing message:
    - minimal_replies: her last `minimal_replies` replies are minimal (FR-AIR-004/009)
    - revised_appraisal: she moved last turn and Echo consolidated it, so this
      reply is the closing one — the conversation tapers instead of stopping dead
    - containment: the invisible length failsafe (FR-AIR-013)
    """
    user_turns = [content for role, content in history if role == "user"]
    if text is not None:
        user_turns.append(text)
    if len(user_turns) >= minimal_replies and all(is_minimal(t) for t in user_turns[-minimal_replies:]):
        return "minimal_replies"
    if stage == CONFIRMING:
        return "revised_appraisal"
    if sum(1 for role, _ in history if role == "ai") >= containment_turns:
        return "containment"
    return None


# Openers that hand her own message back to her before the reply begins. The
# prompt asks for the thing itself instead, and the model mostly complies — this
# removes the ones that slip through, because a reply that starts by repeating
# her is the difference between being listened to and being processed.
_RECAP_OPENER = re.compile(
    r"^(aap ?ne likha|aap ?ne bataya|aap keh rahi hain|aap ne kaha|"
    r"you wrote that|you mentioned that|you said that|you have (told|written))"
    r"\b[^.?!]*[.?!]+\s*",
    re.I,
)
# What is left has to still be a reply, not a fragment.
_MIN_WORDS_AFTER_STRIP = 6


def drop_recap_opener(text: str) -> str:
    """Remove a leading restatement, keeping the reply that follows it."""
    rest = _RECAP_OPENER.sub("", text, count=1).strip()
    if rest == text or len(rest.split()) < _MIN_WORDS_AFTER_STRIP:
        return text
    return rest[0].upper() + rest[1:] if rest[0].islower() else rest


def count_questions(text: str) -> int:
    """
    Questions Echo actually asks: sentences that end in a question mark. A quoted
    question of the user's ("that 'what if?' keeps coming back") is not one.
    """
    return sum(1 for s in _SENTENCE_SPLIT.split(text) if s.rstrip().endswith(("?", "؟")))


def limit_questions(text: str, allowed: int) -> str:
    """
    FR-AIR-003 / FR-AIR-010: keep at most `allowed` question sentences. Later
    questions are dropped; statements are kept. Falls back to the original if
    nothing would remain.
    """
    if count_questions(text) <= allowed:
        return text
    parts = _SENTENCE_SPLIT_KEEP.split(text)
    sentences, separators = parts[0::2], parts[1::2] + [""]
    kept, asked = [], 0
    for sentence, separator in zip(sentences, separators):
        is_question = sentence.rstrip().endswith(("?", "؟"))
        if is_question:
            if asked >= allowed:
                continue
            asked += 1
        kept.append(sentence + separator)
    result = "".join(kept).strip()
    return result or text


def enforce(
    reflection: Reflection,
    forced_closure: str | None = None,
    repeat_limit: int = REPEAT_LIMIT,
) -> Outcome:
    """
    Runs last and overrides the model. The model can only raise flags, never
    clear them. When it notices risk, the caller replaces its reply with fixed
    crisis content (FR-CRIS-008) and suppresses further AI for the entry.
    """
    crisis = reflection.crisis_indicators_noticed
    session_end = reflection.session_end and not crisis
    reason = reflection.closure_reason if session_end else None

    # A message where she sees it differently is a move, not another repeat: hold
    # the ending back by one turn so the conversation can wind down (unless she
    # asked to stop, or the code already requires a close).
    shift = reflection.shift_noticed and not crisis
    if shift and not forced_closure and reason != "user_asked_to_stop":
        session_end, reason = False, None

    if not crisis and forced_closure:
        session_end, reason = True, forced_closure
    elif not crisis and not session_end and not shift and reflection.same_concern_count >= repeat_limit:
        # The model counted the loop but did not close; a reply that keeps asking would feed it.
        session_end, reason = True, "repeated_concern"

    if session_end and reason in (None, "none"):
        reason = "unspecified"

    text = reflection.response_text.strip() or "I'm here. Tell me a bit more?"
    text = limit_questions(text, allowed=0 if session_end else 1)
    if not session_end:
        # A closing message is allowed to restate what she reached (FR-AIR-011);
        # an ordinary reply is not allowed to open by replaying her.
        text = drop_recap_opener(text)
    if session_end and count_questions(text):
        logger.warning("Closing message still contains a question")

    return Outcome(
        reply_text=text,
        shift_noticed=reflection.shift_noticed and not session_end,
        crisis=crisis,
        referral=reflection.referral_flag or crisis,
        session_end=session_end,
        closure_reason=reason,
    )


def enforce_chat(reply: ChatReply, closing: bool = False) -> Outcome:
    """
    AI Chat: the same last word as enforce(), without the journal's closure
    rules — she ends a chat by starting a new one. Only the containment
    failsafe closes it (`closing`), and risk is never cleared.
    """
    crisis = reply.crisis_indicators_noticed
    session_end = closing and not crisis
    text = reply.response_text.strip() or "I'm here. Tell me more?"
    text = limit_questions(text, allowed=0 if session_end else 1)
    if not session_end:
        text = drop_recap_opener(text)
    return Outcome(
        reply_text=text,
        crisis=crisis,
        referral=reply.referral_flag or crisis,
        session_end=session_end,
        closure_reason="containment" if session_end else None,
    )
