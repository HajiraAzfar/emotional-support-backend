"""
Speech to text for voice messages, through Groq's Whisper large v3.

Azure Speech's fast transcription was used first, on the same Foundry resource
as the language models. It transcribed Pakistani speech poorly: it had to be
told which language to expect, and could only choose between en-US and ur-IN
(ur-PK is "not yet supported"), which is the wrong shape for someone switching
between English and Urdu inside one sentence. Whisper detects the language
itself and handles the mix.

The endpoint is OpenAI-compatible, so this is one multipart POST rather than
another SDK. Speech that mixes English and Roman Urdu tends to come back as
Roman Urdu already; the voice router still romanises anything that arrives in
Urdu script, as it did before.

https://console.groq.com/docs/speech-to-text
"""
import logging
import re
import time

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL = "whisper-large-v3"
MAX_TRANSCRIPT_CHARS = 5000  # the most a message can hold (MessageCreate)
# Whisper invents a word rather than returning nothing: a recording with no
# speech in it comes back as " you" or " Thank you." Each segment carries how
# likely it is that nobody was speaking, and the two cases sit far apart —
# measured silence 0.70, clear English 0.0005, English mixed with Roman Urdu
# 0.12. Above this a segment is dropped, so an empty recording transcribes to
# nothing, which the router turns into "I couldn't make out any words in that".
#
# That message is worth keeping honest: it is what told us the emulator was not
# passing the host microphone through. Without this, silence reaches the thread
# as a real message and Reframe answers "you" as though she had said it.
MAX_NO_SPEECH = 0.5
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


class SpeechError(Exception):
    pass


def _ask(audio: bytes, filename: str, language: str | None = None) -> dict:
    """One call to the service. Raises SpeechError when it cannot be reached."""
    data = {"model": MODEL, "temperature": "0", "response_format": "verbose_json"}
    if language:
        data["language"] = language

    for attempt in range(2):
        try:
            response = httpx.post(
                URL,
                headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
                files={"file": (filename, audio)},
                data=data,
                timeout=60,
            )
        except httpx.HTTPError as exc:
            logger.warning("Transcription failed: %s", type(exc).__name__)
            raise SpeechError(str(exc)) from exc
        # Rate limited: one short wait is enough for a single message.
        if response.status_code == 429 and attempt == 0:
            time.sleep(2)
            continue
        break

    if response.status_code != 200:
        # The error code only: the body can quote what she said.
        try:
            code = (response.json().get("error") or {}).get("code", "")
        except ValueError:
            code = ""
        logger.warning("Transcription failed: HTTP %s %s", response.status_code, code)
        raise SpeechError(f"HTTP {response.status_code}")

    try:
        return response.json()
    except ValueError as exc:
        raise SpeechError("Unreadable response") from exc


def _heard(payload: dict) -> str:
    """The words in a response, with the segments it marked as silence dropped."""
    segments = payload.get("segments") or []
    if not segments:
        return (payload.get("text") or "").strip()
    kept = [s.get("text") or "" for s in segments
            if (s.get("no_speech_prob") or 0) <= MAX_NO_SPEECH]
    return " ".join(part.strip() for part in kept).strip()


def transcribe(audio: bytes, filename: str) -> str:
    """
    Returns the text of the recording, or "" when no words were heard.
    Raises SpeechError when the service is unavailable.
    """
    if settings.LLM_MOCK:
        return "(Mock) This is what your voice message would say."
    if not settings.GROQ_API_KEY:
        raise SpeechError("No Groq key is set")

    # No `language` on the first pass: she moves between English and Urdu, often
    # in one sentence, and Whisper reads that better than being told which to
    # expect. temperature=0 so the same recording gives the same words twice.
    payload = _ask(audio, filename)
    text = _heard(payload)

    # Urdu and Hindi are one spoken language, and Whisper's language ID picks
    # between them freely. When it picks Hindi it writes Devanagari, which she
    # neither types nor reads here — and the romaniser, which turns Urdu script
    # into Roman Urdu every time, refuses Devanagari more often than not
    # (measured 1-4 successes in 8, whatever the wording). Asking again for Urdu
    # gets Urdu script, which romanises reliably. Only when Devanagari actually
    # came back, so English is never re-read as Urdu.
    if _DEVANAGARI.search(text):
        logger.info("Devanagari transcript (language=%s); asking again for Urdu",
                    payload.get("language"))
        again = _heard(_ask(audio, filename, language="ur"))
        if again:
            text = again

    # Content-free: how long the recording was and how sure the model was that
    # nobody was speaking. Without this there is no way to tell a silent
    # microphone from a model that heard nothing.
    if not text:
        logger.info("Transcript empty: duration=%s no_speech_prob=%s",
                    payload.get("duration"),
                    [s.get("no_speech_prob") for s in payload.get("segments") or []])
    return text[:MAX_TRANSCRIPT_CHARS]
