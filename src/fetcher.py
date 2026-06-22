"""
Fetcher RSS — Raccoglie articoli dalle fonti configurate e li salva in SQLite.
"""

import sqlite3
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import yaml
from dateutil import parser as dateparser

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "radar.db"
CONFIG_PATH = Path(__file__).parent.parent / "config" / "sources.yaml"


def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Crea il database e la tabella articoli se non esistono."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY,
            source_name TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            summary TEXT,
            published_at TEXT,
            fetched_at TEXT NOT NULL,
            tier INTEGER,
            category TEXT,
            language TEXT,
            flow TEXT,
            -- Campi scoring (popolati da scorer.py)
            title_it TEXT,
            score INTEGER,
            pillar TEXT,
            channel TEXT,
            scored_flow TEXT,
            angle TEXT,
            hooks TEXT,
            format TEXT,
            hashtags TEXT,
            reasoning TEXT,
            scored_at TEXT,
            -- Campi specifici Mactronics
            tempestivita TEXT,          -- "caldo" | "sempreverde"
            portfolio_match TEXT,       -- nome vendor partner se citato
            angle_gag TEXT,             -- angolo specifico Mactronics dal LLM
            area_tech TEXT,             -- "hpc_ai" | "storage" | "virtualizzazione" | "server_workstation" | "cross"
            -- Campi email
            emailed_at TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fetched_at ON articles(fetched_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_score ON articles(score)
    """)
    conn.commit()
    return conn


def load_sources(config_path: Path = CONFIG_PATH) -> tuple[list[dict], dict]:
    """Carica la configurazione fonti e settings."""
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config["sources"], config.get("settings", {})


def make_article_id(url: str) -> str:
    """Genera un ID deterministico dall'URL."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def parse_published_date(entry) -> str | None:
    """Estrae la data di pubblicazione da un entry RSS."""
    for field in ("published", "updated", "created"):
        raw = getattr(entry, field, None) or entry.get(field)
        if raw:
            try:
                dt = dateparser.parse(raw)
                if dt:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.isoformat()
            except (ValueError, TypeError):
                continue
    return None


def is_too_old(published_at: str | None, max_age_hours: int) -> bool:
    """Verifica se l'articolo è più vecchio del limite."""
    if not published_at:
        return False  # Se non c'è data, lo includiamo
    try:
        pub_dt = dateparser.parse(published_at)
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        return pub_dt < cutoff
    except (ValueError, TypeError):
        return False


def get_current_weekday() -> int:
    """Ritorna il giorno della settimana in Europe/Rome (0=Lunedì, 6=Domenica)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Rome")).weekday()
    except Exception:
        return datetime.now().weekday()


def _build_article_record(source: dict, item: dict, now_iso: str, flow: str) -> dict:
    """Costruisce il record standardizzato per il DB."""
    return {
        "id": make_article_id(item["url"]),
        "source_name": source["name"],
        "title": item["title"],
        "url": item["url"],
        "summary": item.get("summary", "") or "",
        "published_at": item.get("published_at"),
        "fetched_at": now_iso,
        "tier": source.get("tier"),
        "category": source.get("category"),
        "language": source.get("language", "it"),
        "flow": flow,
    }


def fetch_source(source: dict, settings: dict) -> list[dict]:
    """Scarica articoli da RSS o tramite scraper custom."""
    name = source["name"]
    flow = source.get("flow", "fast")
    scraper_name = source.get("scraper")
    feed_url = source.get("feed_url")

    # Finestra dinamica: lunedì = 72h (per weekend), altri = 24h
    is_monday = get_current_weekday() == 0
    if flow == "fast" and is_monday and "max_age_hours_fast_monday" in settings:
        max_age = settings["max_age_hours_fast_monday"]
    else:
        max_age = settings.get(
            f"max_age_hours_{flow}",
            24 if flow == "fast" else 168
        )

    now_iso = datetime.now(timezone.utc).isoformat()

    # ── Percorso A: scraper custom (fonti senza RSS) ──
    if scraper_name:
        logger.info(f"Scraping: {name} [{scraper_name}] — finestra {max_age}h")
        try:
            from src.scrapers import run_scraper
        except ImportError:
            from scrapers import run_scraper
        try:
            scraped_url = source.get("url") or source.get("feed_url")
            items = run_scraper(scraper_name, scraped_url)
        except Exception as e:
            logger.error(f"Errore scraper {name}: {e}")
            return []
        # Gli scrapers non hanno published_at affidabile: li accettiamo tutti
        # (la deduplicazione via URL evita di processarli ogni giorno)
        articles = [_build_article_record(source, it, now_iso, flow) for it in items]
        logger.info(f"  {name}: {len(articles)} articoli scrapati")
        return articles

    # ── Percorso B: RSS standard ──
    if not feed_url:
        logger.error(f"Fonte {name} senza feed_url né scraper")
        return []

    logger.info(f"Fetching: {name} ({feed_url}) — finestra {max_age}h")

    try:
        feed = feedparser.parse(feed_url)
    except Exception as e:
        logger.error(f"Errore fetch {name}: {e}")
        return []

    if feed.bozo and not feed.entries:
        logger.warning(f"Feed non valido o vuoto: {name} — {feed.bozo_exception}")
        return []

    articles = []
    skipped_old = 0

    for entry in feed.entries:
        url = entry.get("link", "")
        if not url:
            continue

        title = entry.get("title", "").strip()
        if not title:
            continue

        summary = entry.get("summary", entry.get("description", ""))
        # Pulisci HTML dal summary
        if summary:
            import re
            summary = re.sub(r"<[^>]+>", "", summary).strip()
            summary = summary[:1000]  # Limita lunghezza

        published_at = parse_published_date(entry)

        if is_too_old(published_at, max_age):
            skipped_old += 1
            continue

        articles.append(_build_article_record(source, {
            "title": title,
            "url": url,
            "summary": summary,
            "published_at": published_at,
        }, now_iso, flow))

    skipped_msg = f" ({skipped_old} vecchi saltati)" if skipped_old else ""
    logger.info(f"  {name}: {len(articles)} nel range{skipped_msg}")
    return articles


def save_articles(conn: sqlite3.Connection, articles: list[dict]) -> int:
    """Salva gli articoli nel database, ignorando i duplicati."""
    saved = 0
    for art in articles:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO articles
                (id, source_name, title, url, summary, published_at, fetched_at,
                 tier, category, language, flow)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                art["id"], art["source_name"], art["title"], art["url"],
                art["summary"], art["published_at"], art["fetched_at"],
                art["tier"], art["category"], art["language"], art["flow"],
            ))
            if conn.total_changes:
                saved += 1
        except sqlite3.IntegrityError:
            pass  # Duplicato, ignora
    conn.commit()
    return saved


def cleanup_old_articles(conn: sqlite3.Connection, days: int = 14) -> int:
    """Rimuove articoli più vecchi di N giorni. Ritorna il numero cancellato."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    result = conn.execute(
        "DELETE FROM articles WHERE fetched_at < ?",
        (cutoff,)
    )
    conn.commit()
    deleted = result.rowcount
    if deleted:
        logger.info(f"Cleanup: rimossi {deleted} articoli più vecchi di {days} giorni")
        # Compatta il DB per liberare spazio su disco
        conn.execute("VACUUM")
    return deleted


def fetch_all() -> int:
    """Esegue il fetch di tutte le fonti. Ritorna il numero di nuovi articoli."""
    conn = init_db()
    # Prima pulisci articoli vecchi per non crescere all'infinito
    cleanup_old_articles(conn, days=14)
    sources, settings = load_sources()

    total_new = 0
    for source in sources:
        articles = fetch_source(source, settings)
        if articles:
            before = conn.total_changes
            save_articles(conn, articles)
            new = conn.total_changes - before
            total_new += len(articles)

    conn.close()
    logger.info(f"Fetch completato: {total_new} nuovi articoli totali")
    return total_new


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    count = fetch_all()
    print(f"Fetch completato: {count} nuovi articoli")
