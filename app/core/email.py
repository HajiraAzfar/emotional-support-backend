import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from app.core.config import settings

log = logging.getLogger(__name__)


def send_email(to: str, subject: str, html: str) -> bool:
    # Gmail SMTP: sends to any address without a verified domain (~500 a day).
    if not (settings.SMTP_USER and settings.SMTP_PASSWORD):
        return False
    msg = EmailMessage()
    msg["From"] = formataddr(("Reframe", settings.SMTP_USER))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(html, subtype="html")
    try:
        # The timeout stops a request hanging if the host blocks outbound SMTP.
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            # Google shows App Passwords in groups of four; the spaces are not part of it.
            smtp.user, smtp.password = settings.SMTP_USER, settings.SMTP_PASSWORD.replace(" ", "")
            # PLAIN only: login() retries with LOGIN, which Gmail answers by hanging up, so a
            # wrong password was logged as SMTPServerDisconnected, not SMTPAuthenticationError.
            smtp.ehlo()
            smtp.auth("PLAIN", smtp.auth_plain)
            smtp.send_message(msg)
        return True
    except Exception as e:
        # Only the error type: Gmail's messages can carry the recipient's address.
        log.error("Email not sent: %s", type(e).__name__)
        return False
