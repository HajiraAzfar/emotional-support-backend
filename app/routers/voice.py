"""
Voice input for the composer (AI chat and free write).

A recording is turned into text, and the text goes back to the app for her to
read, correct and send like anything she typed. Nothing here touches an entry:
the transcript only enters the thread when she sends it, through the same
endpoints as typed text, so crisis screening and every other safeguard that
reads text applies unchanged.

The audio is never stored. It is held in memory for this one request, passed to
Azure Speech, and dropped.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.core import speech
from app.core.dependencies import get_current_user
from app.models.account import Account

router = APIRouter(prefix="/voice", tags=["voice"])

# The app stops recording at 3 minutes; AAC mono at 48 kbps is about 1 MB for that.
MAX_AUDIO_BYTES = 10 * 1024 * 1024
# Formats Azure fast transcription accepts; the app sends m4a (AAC).
AUDIO_SUFFIXES = {".m4a", ".mp4", ".aac", ".mp3", ".wav", ".webm", ".ogg", ".flac"}


class TranscriptOut(BaseModel):
    text: str


@router.post("/transcribe", response_model=TranscriptOut)
def transcribe(
    audio: UploadFile = File(...),
    account: Account = Depends(get_current_user),
):
    suffix = Path(audio.filename or "").suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise HTTPException(status_code=415, detail="That recording format isn't supported.")

    data = audio.file.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="That recording is too long. Try a shorter one.")
    if not data:
        raise HTTPException(status_code=422, detail="The recording was empty. Please try again.")

    try:
        text = speech.transcribe(data, f"voice{suffix}")
    except speech.SpeechError:
        raise HTTPException(
            status_code=503,
            detail="Voice messages aren't available right now. You can type instead.",
        )
    if not text:
        raise HTTPException(
            status_code=422,
            detail="I couldn't make out any words in that. Try again a little closer to the phone.",
        )
    return TranscriptOut(text=text)
