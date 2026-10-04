"""
Speech to text for voice messages, through Azure Speech's fast transcription
API on the same Foundry resource as the language models. It needs no model
deployment: the Speech service comes with the resource, which matters because
no transcription model can be deployed in this resource's region.

https://learn.microsoft.com/azure/ai-services/speech-service/fast-transcription-create
"""
import json
import logging
import time
from urllib.parse import urlparse

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

API_VERSION = "2025-10-15"
# The service picks between these for what she said: English, or Urdu.
# Fast transcription does not support ur-PK ("not yet supported"); ur-IN is
# the same language and script, and recognises Pakistani speech fine.
LOCALES = ["en-US", "ur-IN"]
MAX_TRANSCRIPT_CHARS = 5000  # the most a message can hold (MessageCreate)


class SpeechError(Exception):
    pass


def _endpoint() -> str:
    if settings.AZURE_SPEECH_ENDPOINT:
        return settings.AZURE_SPEECH_ENDPOINT.rstrip("/")
    # Unset: the resource behind OPENAI_BASE_URL, whose Speech service lives on
    # the cognitiveservices host (<resource>.openai.azure.com → <resource>.cognitiveservices.azure.com).
    host = urlparse(settings.OPENAI_BASE_URL or "").hostname or ""
    if not host.endswith(".openai.azure.com"):
        raise SpeechError("AZURE_SPEECH_ENDPOINT is not set")
    return f"https://{host.split('.')[0]}.cognitiveservices.azure.com"


def transcribe(audio: bytes, filename: str) -> str:
    """
    Returns the text of the recording, or "" when no words were heard.
    Raises SpeechError when the service is unavailable.
    """
    if settings.LLM_MOCK:
        return "(Mock) This is what your voice message would say."
    key = settings.AZURE_SPEECH_KEY or settings.OPENAI_API_KEY
    if not key:
        raise SpeechError("No Speech key is set")

    url = f"{_endpoint()}/speechtotext/transcriptions:transcribe"
    files = {
        "audio": (filename, audio),
        "definition": (None, json.dumps({"locales": LOCALES}), "application/json"),
    }
    # The resource turns away calls that come too close together; one short
    # wait is enough for a single message.
    for attempt in range(2):
        try:
            response = httpx.post(
                url,
                params={"api-version": API_VERSION},
                headers={"Ocp-Apim-Subscription-Key": key},
                files=files,
                timeout=60,
            )
        except httpx.HTTPError as exc:
            logger.warning("Transcription failed: %s", type(exc).__name__)
            raise SpeechError(str(exc)) from exc
        if response.status_code == 429 and attempt == 0:
            time.sleep(2)
            continue
        break

    # Silence, or sound with no speech in it: the service could not tell which
    # language it was, which here simply means no words were heard.
    if response.status_code == 422 and "NoLanguageIdentified" in response.text:
        return ""
    if response.status_code != 200:
        # The error code only (e.g. InvalidLocale): the body never holds what she said.
        try:
            code = response.json().get("innerError", {}).get("code", "")
        except ValueError:
            code = ""
        logger.warning("Transcription failed: HTTP %s %s", response.status_code, code)
        raise SpeechError(f"HTTP {response.status_code}")
    phrases = response.json().get("combinedPhrases") or []
    text = " ".join(p.get("text", "").strip() for p in phrases).strip()
    return text[:MAX_TRANSCRIPT_CHARS]
