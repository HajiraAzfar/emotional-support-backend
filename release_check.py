"""
Release gate: fails while any clinical text is still unapproved.

Every file of clinical wording (crisis text, fallback prompts, scope notices,
grounding, skills, clinical context, scales, libraries, articles) carries the
marker DRAFT_NOT_CLINICALLY_APPROVED until the clinical advisor signs it off.
The advisor removes the marker file by file; this exits 0 only when none is left.

    venv\\Scripts\\python release_check.py      → exit 1 and the list, while any remain
"""
import pathlib
import sys

MARKER = "DRAFT_NOT_CLINICALLY_APPROVED"
APP = pathlib.Path(__file__).resolve().parent / "app"

pending = sorted(
    p.relative_to(APP.parent).as_posix()
    for p in APP.rglob("*")
    if p.suffix in {".json", ".md"} and MARKER in p.read_text(encoding="utf-8")
)
if pending:
    print(f"RELEASE BLOCKED: {len(pending)} clinical text files are not clinically approved ({MARKER}):")
    print("\n".join(f"  {p}" for p in pending))
    sys.exit(1)
print("Release check passed: no unapproved clinical text.")
