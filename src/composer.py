"""
Composer — Assembla l'email del radar Mactronics dal database.

Differenze rispetto a OIDA/Praxalia:
- Flusso UNICO rankizzato (no fast/slow)
- Pilastri: 5 filoni narrativi F1-F5 (Autorevolezza / Caso / Trend / Partner / Heritage)
- Aree tecnologiche: hpc_ai / storage / virtualizzazione / server_workstation
- Canali: page_company / page_ceo (Samir Gomaa)
- Sezione speciale "Vendor partner nelle news" per portfolio_match
- Tag tempestività + area_tech visibili nel template
"""

import json
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "radar.db"
TEMPLATE_DIR = Path(__file__).parent.parent / "templates"


PILLAR_LABELS = {
    "f1_autorevolezza_tecnica": "F1 · Autorevolezza Tecnica",
    "f2_caso_uso": "F2 · Caso d'uso velato",
    "f3_trend_thought_leadership": "F3 · Trend & Thought Leadership",
    "f4_ecosystem_partner": "F4 · Ecosystem Partner",
    "f5_heritage": "F5 · Heritage & Identità",
    # Fallback compatti
    "f1": "F1 · Autorevolezza",
    "f2": "F2 · Caso d'uso",
    "f3": "F3 · Trend",
    "f4": "F4 · Partner",
    "f5": "F5 · Heritage",
    "generico": "Generico",
}

AREA_TECH_LABELS = {
    "hpc_ai": "HPC & AI",
    "storage": "Storage",
    "virtualizzazione": "Virtualizzazione",
    "server_workstation": "Server & Workstation",
    "cross": "Cross-area",
}

CHANNEL_LABELS = {
    "page_company": "Company Page",
    "page_ceo": "Profilo CEO",
    # Fallback per output LLM che usa "company"/"ceo" senza prefisso
    "company": "Company Page",
    "ceo": "Profilo CEO",
}

TEMPESTIVITA_LABELS = {
    "caldo": "Caldo",
    "sempreverde": "Sempreverde",
}

LENGTH_HINTS = {
    "solo_testo": "~250 parole · 1.300-1.500 caratteri",
    "visual_dato": "~150 parole · 800-1.000 caratteri + visual",
    "visual_lista": "~150 parole · 800-1.000 caratteri + visual",
    "carousel": "6-10 slide · max 30 parole per slide",
    "long_form": "400-600 parole · LinkedIn long post",
    "commento": "100-200 parole · post di commento veloce",
    "infografica": "~100 parole di caption + infografica",
}


def get_publish_time_hint(weekday: int) -> str:
    """Orario consigliato di pubblicazione (cadenza bisettimanale lun+gio)."""
    # 0=LUN, 1=MAR, 2=MER, 3=GIO, 4=VEN, 5=SAB, 6=DOM
    if weekday == 0:  # lunedì
        return "Pubblica oggi 9:00-11:00 CET — finestra LinkedIn B2B ottimale"
    if weekday == 3:  # giovedì
        return "Pubblica oggi 9:00-11:00 CET — secondo slot settimanale"
    if weekday in (1, 2, 4):
        return "Pubblica oggi 9:00-11:00 CET o 12:00-13:00 (lunch)"
    return "Rimanda a lunedì — weekend sconsigliato per B2B HORECA"


def get_length_hint(fmt: str | None) -> str:
    if not fmt:
        return "~200 parole"
    return LENGTH_HINTS.get(fmt, "~200 parole")


def get_articles(conn: sqlite3.Connection, min_score: int = 5) -> dict:
    """Recupera articoli per l'email (flusso unico rankizzato).

    Ritorna:
    - top: top 10 articoli sopra soglia, max 2 per fonte (suggerimenti principali)
    - maison: articoli con portfolio_match (sezione speciale, anche se sovrapposti)
    """
    # Prendi i top sopra soglia, ordinati per score
    rows = conn.execute("""
        SELECT * FROM articles
        WHERE score >= ? AND emailed_at IS NULL
        ORDER BY score DESC, fetched_at DESC
        LIMIT 50
    """, (min_score,)).fetchall()

    # Max 2 articoli per fonte (diversità)
    top = []
    source_count = {}
    for row in rows:
        src = row["source_name"]
        source_count[src] = source_count.get(src, 0) + 1
        if source_count[src] <= 2:
            top.append(row)
        if len(top) >= 10:
            break

    # Sezione maison: tutti gli articoli con portfolio_match, anche score 4+
    maison_rows = conn.execute("""
        SELECT * FROM articles
        WHERE portfolio_match IS NOT NULL AND portfolio_match != ''
          AND score >= 4
          AND emailed_at IS NULL
        ORDER BY score DESC, fetched_at DESC
        LIMIT 10
    """).fetchall()

    return {
        "top": [dict(r) for r in top],
        "maison": [dict(r) for r in maison_rows],
    }


def prepare_article(art: dict, weekday: int = 0) -> dict:
    """Prepara un articolo per il template, parsando i campi JSON."""
    art = dict(art)
    for field in ("hooks", "hashtags"):
        val = art.get(field)
        if isinstance(val, str):
            try:
                art[field] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                art[field] = []
        elif val is None:
            art[field] = []
    pillar_key = (art.get("pillar") or "").lower()
    channel_key = (art.get("channel") or "").lower()
    tempestivita_key = (art.get("tempestivita") or "").lower()
    art["pillar_label"] = PILLAR_LABELS.get(pillar_key, art.get("pillar") or "")
    art["channel_label"] = CHANNEL_LABELS.get(channel_key, art.get("channel") or "")
    art["tempestivita_label"] = TEMPESTIVITA_LABELS.get(tempestivita_key, "")
    area_key = (art.get("area_tech") or "").lower()
    art["area_tech_label"] = AREA_TECH_LABELS.get(area_key, "")
    # Usa titolo italiano se disponibile, altrimenti originale
    art["display_title"] = art.get("title_it") or art.get("title", "")
    # Hint operativi LinkedIn
    art["length_hint"] = get_length_hint(art.get("format"))
    art["publish_hint"] = get_publish_time_hint(weekday)
    art["comments_hint"] = "Prepara 2-3 auto-commenti per il boost algoritmico nei primi 15 min"
    return art


def compose_email(min_score: int = 5) -> str | None:
    """Genera l'HTML dell'email. Ritorna None se non ci sono articoli rilevanti."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    articles = get_articles(conn, min_score)

    if not articles["top"] and not articles["maison"]:
        logger.info("Nessun articolo sopra la soglia — niente email oggi")
        conn.close()
        return None

    # Calcola info giornata (timezone Europe/Rome)
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Europe/Rome"))
    except Exception:
        now = datetime.now(timezone.utc)
    GIORNI_IT = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
    weekday = now.weekday()
    today_label = f"{GIORNI_IT[weekday]} {now.strftime('%d/%m/%Y')}"

    # Conteggio suggerimenti per angolo (per debug nell'header)
    top_prepared = [prepare_article(a, weekday) for a in articles["top"]]
    n_company = sum(1 for a in top_prepared if (a.get("channel") or "").lower() in ("page_company", "company"))
    n_ceo = sum(1 for a in top_prepared if (a.get("channel") or "").lower() in ("page_ceo", "ceo"))
    angle_summary = f"{n_company} suggerimenti Company · {n_ceo} suggerimenti CEO"

    maison_prepared = [prepare_article(a, weekday) for a in articles["maison"]]

    # Render template
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,
    )
    template = env.get_template("email.html")

    html = template.render(
        today=today_label,
        angle_summary=angle_summary,
        top_items=top_prepared,
        maison_items=maison_prepared,
        total_scored=len(top_prepared),
        generated_at=now.strftime("%H:%M Rome"),
    )

    # Segna gli articoli come inviati
    now_iso = now.isoformat()
    all_sent_ids = {a["id"] for a in articles["top"]} | {a["id"] for a in articles["maison"]}
    for aid in all_sent_ids:
        conn.execute(
            "UPDATE articles SET emailed_at = ? WHERE id = ?",
            (now_iso, aid)
        )
    conn.commit()
    conn.close()

    logger.info(
        f"Email composta: {len(top_prepared)} top "
        f"({n_company}C / {n_ceo}CEO) + {len(maison_prepared)} maison"
    )
    return html


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    html = compose_email()
    if html:
        out = Path(__file__).parent.parent / "data" / "preview_email.html"
        out.write_text(html, encoding="utf-8")
        print(f"Preview salvata in {out}")
    else:
        print("Nessun articolo rilevante oggi")
