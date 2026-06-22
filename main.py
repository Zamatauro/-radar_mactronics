"""
Radar Mactronics — Orchestratore principale.

Esegue in sequenza:
1. Fetch RSS da tutte le fonti
2. Scoring con Claude API
3. Composizione email
4. Invio via Resend

Uso: python main.py [--dry-run] [--preview] [--skip-if-sent] [--min-hour=N]
  --dry-run        Esegue tutto tranne l'invio email
  --preview        Salva l'HTML dell'email in data/preview_email.html
  --skip-if-sent   Esce se è già stata inviata un'email oggi (per cron doppio)
  --min-hour=N     Esce se l'ora Europe/Rome è minore di N (default: nessun controllo)
"""

import sys
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def email_already_sent_today() -> bool:
    """Controlla nel DB se è già stata inviata un'email oggi (Europe/Rome)."""
    db_path = Path(__file__).parent / "data" / "radar.db"
    if not db_path.exists():
        return False
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Europe/Rome")).date().isoformat()
    except Exception:
        today = datetime.now(timezone.utc).date().isoformat()
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE emailed_at LIKE ?",
            (f"{today}%",)
        ).fetchone()
        conn.close()
        return row[0] > 0
    except sqlite3.Error:
        return False


def get_rome_hour() -> int:
    """Ritorna l'ora attuale in Europe/Rome (0-23)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Rome")).hour
    except Exception:
        return datetime.now().hour


def parse_min_hour(argv: list[str]) -> int | None:
    """Estrae il valore di --min-hour=N dagli argomenti. None se assente."""
    for arg in argv:
        if arg.startswith("--min-hour="):
            try:
                return int(arg.split("=", 1)[1])
            except (ValueError, IndexError):
                return None
    return None


def main():
    dry_run = "--dry-run" in sys.argv
    preview = "--preview" in sys.argv
    skip_if_sent = "--skip-if-sent" in sys.argv
    min_hour = parse_min_hour(sys.argv)

    # Guard min-hour: esci se è troppo presto in Europe/Rome
    # (per gestire il fatto che GitHub cron è solo UTC ma noi vogliamo 8 Rome)
    if min_hour is not None:
        rome_hour = get_rome_hour()
        if rome_hour < min_hour:
            logger.info(
                f"Ora Europe/Rome: {rome_hour:02d}:00 — prima delle {min_hour:02d}:00, esco."
            )
            return

    # Guard anti-doppio-invio: se oggi ho già spedito, esco subito
    # (evita sprechi API se GitHub Actions triggera il cron due volte o in ritardo)
    if skip_if_sent and email_already_sent_today():
        logger.info("Email già inviata oggi — esco per evitare doppio invio.")
        return

    logger.info("=" * 50)
    logger.info("RADAR MACTRONICS — v1.0")
    logger.info("Pre-filtro keyword + portfolio match + 2 worker + rate limiter")
    logger.info("=" * 50)

    # 1. Fetch
    logger.info("--- FASE 1: Fetch RSS ---")
    from src.fetcher import fetch_all
    new_articles = fetch_all()
    logger.info(f"Nuovi articoli raccolti: {new_articles}")

    if new_articles == 0:
        logger.info("Nessun nuovo articolo — verifico se ci sono articoli non scorati")

    # 2. Score
    logger.info("--- FASE 2: Scoring ---")
    from src.scorer import score_all
    scored = score_all()
    logger.info(f"Articoli classificati: {scored}")

    # 3. Compose
    logger.info("--- FASE 3: Composizione email ---")
    from src.composer import compose_email
    html = compose_email()

    if not html:
        logger.info("Nessun articolo sopra la soglia — niente email oggi")
        return

    # Preview opzionale
    if preview or dry_run:
        preview_path = Path(__file__).parent / "data" / "preview_email.html"
        preview_path.write_text(html, encoding="utf-8")
        logger.info(f"Preview salvata: {preview_path}")

    # 4. Send
    if dry_run:
        logger.info("--- DRY RUN: email NON inviata ---")
    else:
        logger.info("--- FASE 4: Invio email ---")
        from src.sender import send_email
        ok = send_email(html)
        if ok:
            logger.info("Email inviata con successo")
        else:
            logger.error("Invio email fallito")
            sys.exit(1)

    logger.info("=" * 50)
    logger.info("Radar completato")


if __name__ == "__main__":
    main()
