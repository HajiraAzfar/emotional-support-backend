from datetime import datetime

from sqlalchemy.orm import Session

from app.core.email import send_email
from app.models.known_device import KnownDevice


def register_device(db: Session, account_id, email: str, fingerprint: str | None) -> None:
    if not fingerprint:
        return

    device = (
        db.query(KnownDevice)
        .filter(
            KnownDevice.account_id == account_id,
            KnownDevice.fingerprint == fingerprint,
        )
        .first()
    )

    if device is not None:
        device.last_seen_at = datetime.utcnow()
        db.commit()
        return

    db.add(KnownDevice(account_id=account_id, fingerprint=fingerprint))
    db.commit()

    send_email(
        to=email,
        subject="New sign-in to your account",
        html=(
            "<p>Your account was signed in to from a device that has not been used before.</p>"
            "<p>If this was you, no action is needed.</p>"
            "<p>If it was not, please change your password.</p>"
        ),
    )