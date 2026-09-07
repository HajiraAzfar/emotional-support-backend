from datetime import datetime, timedelta, timezone

MAX_ATTEMPTS = 5
WINDOW_MINUTES = 15

_failed_attempts: dict[str, list[datetime]] = {}


def _recent_attempts(email: str) -> list[datetime]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)
    attempts = _failed_attempts.get(email, [])
    return [a for a in attempts if a > cutoff]


def is_rate_limited(email: str) -> bool:
    return len(_recent_attempts(email)) >= MAX_ATTEMPTS


def record_failure(email: str) -> None:
    attempts = _recent_attempts(email)
    attempts.append(datetime.now(timezone.utc))
    _failed_attempts[email] = attempts


def clear_failures(email: str) -> None:
    _failed_attempts.pop(email, None)