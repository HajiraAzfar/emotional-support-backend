"""Deleting an account and everything that belongs to it (SRS 4.12).

Every table that holds the user's data is cleared explicitly, children before parents,
rather than trusting the database's ON DELETE CASCADE: the outcome is then the
same on Postgres and on the SQLite the tests run on, and a table added later
without a cascade cannot quietly keep rows behind. A new model with an
account_id has to be added to ACCOUNT_TABLES, and the flow test checks it is.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.captured_value import CapturedValue
from app.models.crisis_event import CrisisEvent
from app.models.earned_badge import EarnedBadge
from app.models.entry import Entry
from app.models.exposure_ladder import ExposureLadder, ExposureLadderItem
from app.models.focus_area import FocusArea
from app.models.known_device import KnownDevice
from app.models.library_favourite import LibraryFavourite
from app.models.message import Message
from app.models.questionnaire_response import QuestionnaireResponse
from app.models.refresh_token import RefreshToken
from app.models.user_term import UserTerm
from app.models.verification_token import VerificationToken
from app.models.weekly_reflection import WeeklyReflection

# Rows keyed by account_id, deleted before the entries and the account itself.
ACCOUNT_TABLES = (
    CrisisEvent,
    EarnedBadge,
    ExposureLadder,
    FocusArea,
    KnownDevice,
    LibraryFavourite,
    QuestionnaireResponse,
    RefreshToken,
    UserTerm,
    VerificationToken,
    WeeklyReflection,
)


def delete_account(db: Session, account: Account) -> None:
    """Remove the account and all of its data in one transaction."""
    entry_ids = select(Entry.id).where(Entry.account_id == account.id)
    try:
        db.query(Message).filter(Message.entry_id.in_(entry_ids)).delete(synchronize_session=False)
        db.query(CapturedValue).filter(CapturedValue.entry_id.in_(entry_ids)).delete(
            synchronize_session=False
        )
        # A ladder's steps are owned through the ladder: they go first.
        ladder_ids = select(ExposureLadder.id).where(ExposureLadder.account_id == account.id)
        db.query(ExposureLadderItem).filter(ExposureLadderItem.ladder_id.in_(ladder_ids)).delete(
            synchronize_session=False
        )
        for model in ACCOUNT_TABLES:
            db.query(model).filter(model.account_id == account.id).delete(synchronize_session=False)
        # Exposure cycles point at their parent entry; clear that first so no
        # row is ever left pointing at one already gone.
        db.query(Entry).filter(Entry.account_id == account.id).update(
            {Entry.parent_entry_id: None, Entry.source_entry_id: None}, synchronize_session=False
        )
        db.query(Entry).filter(Entry.account_id == account.id).delete(synchronize_session=False)
        db.query(Account).filter(Account.id == account.id).delete(synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
