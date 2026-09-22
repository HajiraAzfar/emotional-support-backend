import logging
from typing import Literal

import openai
from pydantic import BaseModel

from app.core.capture import all_value_ids
from app.core.config import settings
from app.core.md_loader import DOMAINS

logger = logging.getLogger(__name__)

MAX_DOMAINS = 2
CLASSIFIER_HISTORY = 4  # previous messages given to the classifier for context
RESPONSE_HISTORY = 40
MAX_CAPTURE_CHARS = 400

Domain = Literal[DOMAINS]
ValueId = Literal[all_value_ids()]
ClosureReason = Literal[
    "none",
    "revised_appraisal",
    "minimal_replies",
    "repeated_concern",
    "user_asked_to_stop",
    "containment",
]


RiskTier = Literal["clear", "mild", "danger", "emergency"]


class DomainResult(BaseModel):
    domains: list[Domain]
    risk_tier: RiskTier


class Classification(BaseModel):
    domains: list[str] | None  # None when nothing usable came back
    risk_tier: str | None  # None when the model stage was unavailable


class CapturePrompt(BaseModel):
    # No crisis field: every free-text answer is already screened by the rules
    # and the classifier before a prompt is generated (crisis.assess).
    value_id: ValueId
    message_text: str


class Closing(BaseModel):
    message_text: str


class Reflection(BaseModel):
    # Field order matters: the model decides whether to close before it writes the reply.
    same_concern_count: int
    closure_reason: ClosureReason
    session_end: bool
    response_text: str
    referral_flag: bool
    crisis_indicators_noticed: bool


class LLMError(Exception):
    pass


_client: openai.OpenAI | None = None


def _get_client() -> openai.OpenAI:
    global _client
    if _client is None:
        if not settings.OPENAI_API_KEY:
            raise LLMError("OPENAI_API_KEY is not set")
        _client = openai.OpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
            timeout=settings.OPENAI_TIMEOUT_SECONDS,
            max_retries=1,
        )
    return _client


def _as_input(history: list[tuple[str, str]], text: str | None) -> list[dict]:
    """history is [(role, content)] with role "user" or "ai", oldest first."""
    items = [
        {"role": "assistant" if role == "ai" else "user", "content": content}
        for role, content in history
    ]
    if text is not None:
        items.append({"role": "user", "content": text})
    return items


_CLASSIFIER_INSTRUCTIONS = """\
You label journal messages for a wellbeing app. Return the 1 or 2 topics that best
describe the user's LATEST message (earlier messages are context only). Messages may
be English, Roman Urdu or mixed.

Topics:
- low_mood: sadness, no energy, no motivation, emptiness
- low_self_esteem: self-criticism, feeling not good enough, worthless
- relationship_issues: family, partner, friends, conflict, loneliness with people
- distraction: procrastination, can't focus, wasting time
- lack_of_self_control: urges, bingeing, impulsive habits, breaking promises to self
- overwhelmed: too much at once, pressure, stress from many demands
- overthinking: replaying, what-ifs, worry loops, rumination
- grief: death or loss of someone (or something) important
- positive: good day, achievement, gratitude, happiness
- general: neutral, everyday, or nothing clearly above

Put the most important topic first. Use general only on its own.

Also rate risk in the LATEST message as risk_tier:
- clear: no sign of risk
- mild: hopelessness or feeling like a burden, without mention of self-harm
- danger: thoughts of suicide, self-harm, or wanting to die
- emergency: intent with a plan, means, timing, or an act already taken
When unsure between two tiers, choose the higher one.
{hint}"""


def classify(
    history: list[tuple[str, str]],
    text: str,
    work_issues: list[str] | None,
) -> Classification:
    """
    Cheap first call: which skill files to load, plus the model stage of crisis
    detection (FR-CRIS-002). Never raises — fields are None when unavailable, so
    the caller falls back to previous domains and the lexical tier alone.
    """
    if settings.LLM_MOCK:
        return Classification(domains=["general"], risk_tier=None)

    hint = ""
    if work_issues:
        hint = (
            "The user said at onboarding they want to work on: "
            f"{', '.join(work_issues)}. Use this only as a tie-breaker for topics."
        )
    try:
        response = _get_client().responses.parse(
            model=settings.OPENAI_CLASSIFIER_MODEL,
            instructions=_CLASSIFIER_INSTRUCTIONS.format(hint=hint),
            input=_as_input(history[-CLASSIFIER_HISTORY:], text),
            text_format=DomainResult,
            store=False,
        )
        result = response.output_parsed
    except (openai.OpenAIError, LLMError) as exc:
        logger.warning("Classification failed: %s", type(exc).__name__)
        return Classification(domains=None, risk_tier=None)
    if result is None:
        return Classification(domains=None, risk_tier=None)

    domains = list(dict.fromkeys(result.domains))  # de-duplicate, keep order
    if len(domains) > 1:
        domains = [d for d in domains if d != "general"]
    return Classification(domains=domains[:MAX_DOMAINS] or None, risk_tier=result.risk_tier)


def generate_capture_prompt(system_prompt: str, expected_value_id: str) -> CapturePrompt | None:
    """
    Words one capture message. Returns None whenever the result can't be used —
    service down, wrong value requested, empty or too long — so the caller
    falls back to stored wording (FR-ENT-023, FR-AIR-014).
    """
    if settings.LLM_MOCK:
        return None
    try:
        response = _get_client().responses.parse(
            model=settings.OPENAI_RESPONSE_MODEL,
            instructions=system_prompt,
            input="Write the capture message now.",
            text_format=CapturePrompt,
            store=False,
        )
        result = response.output_parsed
    except (openai.OpenAIError, LLMError) as exc:
        logger.warning("Capture prompt generation failed: %s", type(exc).__name__)
        return None

    if result is None:
        return None
    text = result.message_text.strip()
    if result.value_id != expected_value_id or not text or len(text) > MAX_CAPTURE_CHARS:
        logger.warning("Capture prompt rejected (asked for %s, expected %s)", result.value_id, expected_value_id)
        return None
    return result.model_copy(update={"message_text": text})


MAX_CLOSING_CHARS = 600


def generate_closing(system_prompt: str) -> str | None:
    """
    Closing message for a journal with no conversation. Returns None when it
    can't be used, so the caller falls back to the stored closing.
    """
    if settings.LLM_MOCK:
        return None
    try:
        response = _get_client().responses.parse(
            model=settings.OPENAI_RESPONSE_MODEL,
            instructions=system_prompt,
            input="Write the closing message now.",
            text_format=Closing,
            store=False,
        )
        result = response.output_parsed
    except (openai.OpenAIError, LLMError) as exc:
        logger.warning("Closing generation failed: %s", type(exc).__name__)
        return None
    text = result.message_text.strip() if result else ""
    if not text or len(text) > MAX_CLOSING_CHARS:
        return None
    return text


def _mock_reflection(text: str | None) -> Reflection:
    ending = text is not None and text.strip().lower() in {"ok", "okay", "yeah", "idk", "bye", "stop"}
    if ending:
        return Reflection(
            same_concern_count=1,
            response_text="(Mock) You took time to check in today. Maybe take three slow breaths before you go.",
            session_end=True,
            closure_reason="minimal_replies",
            referral_flag=False,
            crisis_indicators_noticed=False,
        )
    return Reflection(
        same_concern_count=1,
        response_text="(Mock) Thanks for sharing that. What stood out to you most?",
        session_end=False,
        closure_reason="none",
        referral_flag=False,
        crisis_indicators_noticed=False,
    )


def generate_reflection(
    system_prompt: str,
    history: list[tuple[str, str]],
    text: str | None,
) -> Reflection:
    """
    Main conversation call. text=None means Echo speaks first (FR-AIR-002).
    Raises LLMError if no usable reply comes back.
    """
    if settings.LLM_MOCK:
        return _mock_reflection(text)

    items = _as_input(history[-RESPONSE_HISTORY:], text)
    if not items:
        items = [{"role": "user", "content": "(I've finished my entry and chose to talk it through.)"}]
    # An empty or unparsable reply is occasionally transient; one quiet retry
    # spares the user the "couldn't reply" notice.
    for attempt in range(2):
        try:
            response = _get_client().responses.parse(
                model=settings.OPENAI_RESPONSE_MODEL,
                instructions=system_prompt,
                input=items,
                text_format=Reflection,
                store=False,
            )
        except (openai.OpenAIError, LLMError) as exc:
            raise LLMError(str(exc)) from exc
        if response.output_parsed is not None:
            return response.output_parsed
        logger.warning("Reflection returned no parsable output (attempt %d)", attempt + 1)
    raise LLMError("Model returned no parsable output (possibly a refusal)")
