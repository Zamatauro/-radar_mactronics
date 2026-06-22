"""
Sender — Invia l'email del radar via Resend API.
"""

import os
import logging

import resend

logger = logging.getLogger(__name__)

# Configurazione da variabili d'ambiente
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("RADAR_FROM_EMAIL", "radar@oida-labs.com")
TO_EMAILS = os.environ.get("RADAR_TO_EMAILS", "").split(",")


def send_email(html: str, subject: str | None = None) -> bool:
    """Invia l'email del radar. Ritorna True se l'invio è riuscito."""
    if not RESEND_API_KEY:
        logger.error("RESEND_API_KEY non configurata")
        return False

    recipients = [e.strip() for e in TO_EMAILS if e.strip()]
    if not recipients:
        logger.error("RADAR_TO_EMAILS non configurata — nessun destinatario")
        return False

    if not subject:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%d/%m/%Y")
        subject = f"Radar Editoriale — {today}"

    resend.api_key = RESEND_API_KEY

    try:
        result = resend.Emails.send({
            "from": FROM_EMAIL,
            "to": recipients,
            "subject": subject,
            "html": html,
        })
        logger.info(f"Email inviata a {recipients} — ID: {result.get('id', 'n/a')}")
        return True
    except Exception as e:
        logger.error(f"Errore invio email: {e}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    test_html = "<h1>Test Radar</h1><p>Email di test.</p>"
    ok = send_email(test_html, subject="[TEST] Radar Editoriale")
    print(f"Invio: {'OK' if ok else 'FALLITO'}")
