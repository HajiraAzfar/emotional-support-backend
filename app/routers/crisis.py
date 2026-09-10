"""
Crisis resources endpoint.

FR-CRIS-009 — persistent crisis resource control (client renders the control;
this endpoint supplies the data it displays).
FR-CRIS-010 — must be reachable without authentication or app unlock, so this
router intentionally declares no dependency on get_current_user.
FR-CRIS-008 — content is served byte-identical from the versioned JSON content
set; nothing here generates or modifies resource text at runtime.
"""

import json
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/crisis", tags=["crisis"])

_CONTENT_PATH = Path(__file__).resolve().parent.parent / "content" / "crisis_resources.json"


@router.get("/resources")
def get_crisis_resources():
    """Return the fixed crisis resource list. No auth required (FR-CRIS-010)."""
    with open(_CONTENT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)