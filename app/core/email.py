import resend

from app.core.config import settings

resend.api_key = settings.RESEND_API_KEY


def send_email(to: str, subject: str, html: str) -> bool:
    try:
        resend.Emails.send({
            "from": settings.EMAIL_FROM,
            "to": [to],
            "subject": subject,
            "html": html,
        })
        return True
    except Exception:
        return False