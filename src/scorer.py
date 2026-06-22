"""
Scorer — Classifica gli articoli usando Claude API.
"""

import json
import re
import sqlite3
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import os
import anthropic

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "radar.db"
PROMPT_PATH = Path(__file__).parent.parent / "config" / "scoring_prompt.md"

# Modello: haiku per costi minimi, sonnet se serve più qualità
MODEL = "claude-haiku-4-5-20251001"

# Parallelismo scoring — 2 worker bilanciano velocità e rate limit
# (il rate limit è 50 req/min e 50k token/min di input)
MAX_WORKERS = 2

# Rate limiter: minimo 1.3 secondi tra chiamate API per stare
# sotto 50 req/min con margine di sicurezza (~46 req/min)
MIN_SECONDS_BETWEEN_CALLS = 1.3


# ─────────────────────────────────────────────────────────────
# PRE-FILTRO KEYWORD
# Scarta articoli ovviamente fuori perimetro SENZA chiamare l'API.
# Risparmio stimato: ~40-60% delle chiamate API
# ─────────────────────────────────────────────────────────────
RELEVANT_KEYWORDS = {
    # HPC / AI infrastructure (core Mactronics)
    "hpc", "high performance computing", "supercomput", "cluster",
    "gpu", "nvidia", "cuda", "tensor", "ai computing", "ai on-premise",
    "ai on premise", "intelligenza artificiale", "machine learning",
    "deep learning", "training ai", "inference", "model training",
    "neural network", "ai infrastructure", "infrastructure ai",
    "ai workload", "ai cluster", "compute cluster",
    "infiniband", "rdma", "slurm", "kubernetes ai",
    "raffreddament", "liquid cooling", "data center cooling",
    "high-performance", "high performance",
    # Storage enterprise (la nicchia "The Storage Expert")
    "storage", "san ", " san,", "san.", "nas ", " nas,", "nas.",
    "object storage", "block storage", "file storage", "file system",
    "filesystem parallel", "lustre", "beegfs", "exascaler", "ceph",
    "petabyte", "iops", "throughput", "latenza", "latency",
    "raid", "nvme", "ssd enterprise", "hdd enterprise",
    "backup", "disaster recovery", "ransomware",
    "ddn", "infortrend", "pure storage", "netapp", "huawei storage",
    # Server / hardware
    "server", "rack server", "blade", "tower", "workstation",
    "supermicro", "gigabyte", "amd epyc", "intel xeon", "epyc", "xeon",
    "data center", "datacenter", "ipmi", "redundancy", "ecc memory",
    "edge computing",
    # Virtualizzazione / HCI
    "virtualizzazion", "virtualization", "vmware", "vsphere", "esxi",
    "hyper-v", "hyperv", "red hat virtualization", "rhev",
    "hci", "iperconvergent", "hyperconverged", "vdi",
    "broadcom", "proxmox", "openstack", "container", "kubernetes",
    # Networking enterprise
    "networking", "switch", "router", "firewall", "sd-wan",
    "fibre channel", "iscsi",
    # Mercato IT / business
    "system integrator", "channel", "reseller", "var ",
    "datacenter market", "cloud privato", "private cloud", "hybrid cloud",
    "multi-cloud", "multicloud", "tco", "roi infrastruttura",
    # Regolatorio IT
    "ai act", "gdpr", "nis2", "dora", "acn", "agid",
    "pnrr digitalizzazione", "pnrr ricerca", "pnrr università",
    "digitale", "digital transformation", "trasformazione digitale",
    # Italia / target Mactronics
    "università", "universita", "centro ricerca", "ricerca scientifica",
    "pubblica amministrazione", " pa ", " pa,", "pa.",
    "ente pubblico", "ente di ricerca", "ricerca italiana",
    "policlinico", "ospedale digitale",
    # Vendor citati nel Brand Manual
    "kioxia", "western digital", "toshiba", "mellanox",
    "zutacore", "open-e", "tiger technology", "amd ",
    # Mercato AI / trend
    "agentic ai", "generativ", "gpt", "claude", "gemini",
    "llm training", "foundation model",
}

# Articoli più corti di X caratteri di titolo non valgono la pena
MIN_TITLE_LENGTH = 15


# ─────────────────────────────────────────────────────────────
# PORTFOLIO MATCH (bonus +3 nello scoring)
# Carica config/portfolio.yaml e fa keyword-match nel titolo+summary
# ─────────────────────────────────────────────────────────────
import yaml as _yaml
_PORTFOLIO_CACHE = None


def _load_portfolio() -> dict:
    """Carica config/portfolio.yaml. Cached."""
    global _PORTFOLIO_CACHE
    if _PORTFOLIO_CACHE is None:
        path = Path(__file__).parent.parent / "config" / "portfolio.yaml"
        try:
            with open(path, "r", encoding="utf-8") as f:
                _PORTFOLIO_CACHE = _yaml.safe_load(f) or {}
        except FileNotFoundError:
            _PORTFOLIO_CACHE = {"maison": [], "gruppi": []}
    return _PORTFOLIO_CACHE


def detect_portfolio_match(title: str, summary: str = "") -> str | None:
    """Cerca nel testo un brand del portfolio Mactronics. Ritorna il primo trovato o None."""
    if not title:
        return None
    text = f" {title.lower()} {summary.lower()} "
    portfolio = _load_portfolio()
    # Prima cerca i gruppi (più rari e specifici)
    for group in portfolio.get("gruppi", []):
        if f" {group.lower()} " in text or f" {group.lower()}." in text or f" {group.lower()}," in text:
            return group
    # Poi le maison/brand singole
    for maison in portfolio.get("maison", []):
        m_lower = maison.lower()
        if m_lower in text:
            return maison
    return None


# Pattern di affermazione di distribuzione/possesso da parte di Mactronics.
# Se compaiono in angle/hooks SENZA un portfolio_match valido, è hallucination.
_MACTRONICS_DISTRIBUTION_PATTERNS = re.compile(
    r"\b(mactronics\s+(?:distribuisce|installa|ha|porta|propone|seleziona|"
    r"rappresenta|offre|cura|gestisce|tratta|integra)|"
    r"(?:nel(?:la)?|nei|nelle)\s+(?:nostr[oa]|sua|loro|mactronics)\s+"
    r"(?:selezione|portafoglio|catalogo|offerta|distribuzione|partnership)|"
    r"(?:nostr[oi]|nostre|nostra)\s+(?:server|storage|cluster|hardware|"
    r"vendor|partner|catalogo|stack))",
    re.IGNORECASE,
)


def has_unverified_distribution_claim(text: str | None, has_portfolio_match: bool) -> bool:
    """Verifica se il testo afferma che Mactronics distribuisce qualcosa SENZA
    un portfolio_match validato. Indica hallucination del LLM.
    """
    if not text or has_portfolio_match:
        return False
    return bool(_MACTRONICS_DISTRIBUTION_PATTERNS.search(text))


def validate_portfolio_match(llm_value: str | None) -> str | None:
    """Valida che il portfolio_match dichiarato dal LLM corrisponda a un brand reale.

    Accetta SOLO match esatto del nome del brand, niente descrizioni o parentesi.
    """
    if not llm_value or not isinstance(llm_value, str):
        return None
    llm_lower = llm_value.lower().strip()
    # Hallucination con elaborazione del LLM
    if any(marker in llm_lower for marker in ["(", ")", "group", "holding", "gruppo"]):
        return None
    portfolio = _load_portfolio()
    for group in portfolio.get("gruppi", []):
        if group.lower() == llm_lower:
            return group
    for maison in portfolio.get("maison", []):
        if maison.lower() == llm_lower:
            return maison
    return None


# Regex word-boundary per match di parole brevi del settore IT enterprise
# (matchano "AI" come parola, non come substring di altro)
SHORT_WORDS_RE = re.compile(
    r"\b(ai|hpc|gpu|cpu|ram|ssd|hdd|nvme|sas|san|nas|hci|vdi|vm|"
    r"ipmi|tco|roi|sla|pa|pmi|cto|cio|it|ml|dl|dr|ha|fc|ib|rdma)\b",
    re.IGNORECASE
)


def is_relevant_by_keyword(title: str, summary: str = "") -> bool:
    """Veloce check se il titolo/summary contiene almeno una keyword rilevante.

    Bypass automatico se contiene un brand del portfolio Mactronics.
    """
    if not title or len(title) < MIN_TITLE_LENGTH:
        return False
    # Bypass: se cita un vendor partner, è SEMPRE rilevante
    if detect_portfolio_match(title, summary):
        return True
    text = f" {title.lower()} {summary.lower()} "
    for kw in RELEVANT_KEYWORDS:
        if kw in text:
            return True
    if SHORT_WORDS_RE.search(text):
        return True
    return False


# ─────────────────────────────────────────────────────────────
# RATE LIMITER
# ─────────────────────────────────────────────────────────────
class RateLimiter:
    """Assicura un minimo intervallo tra chiamate. Thread-safe."""
    def __init__(self, min_seconds: float):
        self.min_seconds = min_seconds
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self.min_seconds:
                time.sleep(self.min_seconds - elapsed)
            self._last_call = time.monotonic()


def get_api_key() -> str:
    """Recupera la API key, pulendo eventuali spazi."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY non configurata o vuota")
    return key


def load_scoring_prompt() -> str:
    """Carica il prompt di scoring dal file di configurazione."""
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def get_unscored_articles(conn: sqlite3.Connection) -> list[dict]:
    """Recupera gli articoli non ancora classificati."""
    rows = conn.execute("""
        SELECT id, source_name, title, url, summary, tier, category, language, flow
        FROM articles
        WHERE score IS NULL
        ORDER BY fetched_at DESC
    """).fetchall()
    return [dict(row) for row in rows]


def score_article(client: anthropic.Anthropic, system_prompt: str, article: dict) -> dict:
    """Chiama Claude API per classificare un singolo articolo.

    Pre-rileva i brand del portfolio Mactronics e li passa al prompt come hint.
    """
    portfolio_hint = detect_portfolio_match(
        article.get("title", ""), article.get("summary", "") or ""
    )
    hint_line = f"\n**Portfolio match rilevato:** {portfolio_hint} (applica bonus +3)" if portfolio_hint else ""

    user_message = f"""Analizza questo articolo e rispondi con il JSON di scoring.

**Fonte:** {article['source_name']}
**Tier:** {article['tier']}
**Categoria:** {article['category']}
**Lingua:** {article['language']}
**Titolo:** {article['title']}
**Sommario:** {article.get('summary', 'Non disponibile')[:800]}
**URL:** {article['url']}{hint_line}"""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            timeout=60.0,  # max 60s per chiamata, poi raise e skip
            # Prompt caching: il system prompt (sempre uguale) va in cache
            # e costa 1/10 dopo la prima chiamata del batch
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )

        # Log usage cache (solo primo articolo per non intasare)
        usage = getattr(response, "usage", None)
        if usage and getattr(usage, "cache_read_input_tokens", 0) == 0:
            cache_created = getattr(usage, "cache_creation_input_tokens", 0)
            if cache_created:
                logger.info(f"  Cache creata: {cache_created} token (prossime chiamate pagano 1/10)")

        raw = response.content[0].text.strip()

        # Pulisci eventuale markdown wrapping
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        result = json.loads(raw)

        # Validazione anti-hallucination portfolio_match: il code-detector
        # ha priorità sul LLM. Se il LLM ha inventato un brand, lo scartiamo.
        code_match = detect_portfolio_match(
            article.get("title", ""), article.get("summary", "") or ""
        )
        if code_match:
            result["portfolio_match"] = code_match
        else:
            llm_pm = result.get("portfolio_match")
            validated = validate_portfolio_match(llm_pm)
            if llm_pm and not validated:
                logger.warning(
                    f"  Portfolio match LLM scartato (hallucination): "
                    f"'{llm_pm}' → null  [{article.get('title', '')[:50]}]"
                )
            result["portfolio_match"] = validated

        # Check anti-hallucination su angle/hooks: false attribuzioni di
        # distribuzione/partnership Mactronics
        has_pm = bool(result.get("portfolio_match"))
        suspicious_texts = []
        if has_unverified_distribution_claim(result.get("angle_gag"), has_pm):
            suspicious_texts.append("angle_gag")
        if has_unverified_distribution_claim(result.get("angle"), has_pm):
            suspicious_texts.append("angle")
        for h in result.get("hooks", []) or []:
            if has_unverified_distribution_claim(h, has_pm):
                suspicious_texts.append("hooks")
                break
        if suspicious_texts:
            logger.warning(
                f"  Affermazione distribuzione Mactronics NON verificata in "
                f"{', '.join(suspicious_texts)} [{article.get('title', '')[:50]}]"
            )
            warn = "[WARNING: il LLM afferma distribuzione Mactronics senza portfolio_match. Verifica manualmente.]"
            result["reasoning"] = warn + " " + (result.get("reasoning") or "")

        return result

    except json.JSONDecodeError as e:
        logger.error(f"JSON non valido per '{article['title']}': {e}\nRaw: {raw[:500]}")
        return {"score": 0, "reasoning": f"Errore parsing JSON: {e}"}
    except anthropic.APIError as e:
        logger.error(f"Errore API per '{article['title']}': {e}")
        return {"score": 0, "reasoning": f"Errore API: {e}"}


def save_score(conn: sqlite3.Connection, article_id: str, result: dict):
    """Salva il risultato dello scoring nel database.

    Schema Mactronics: include tempestivita, portfolio_match, angle_gag, area_tech.
    Il prompt usa "angolo" per il canale (page_company/page_ceo)
    e "pilastro" per il filone (f1_..., f2_..., ecc.).
    """
    now = datetime.now(timezone.utc).isoformat()
    channel = result.get("channel") or result.get("angolo")
    pillar = result.get("pillar") or result.get("pilastro")
    fmt = result.get("format") or result.get("formato")
    angle = result.get("angle") or result.get("angle_gag")

    conn.execute("""
        UPDATE articles SET
            title_it = ?,
            score = ?,
            pillar = ?,
            channel = ?,
            scored_flow = ?,
            angle = ?,
            hooks = ?,
            format = ?,
            hashtags = ?,
            reasoning = ?,
            scored_at = ?,
            tempestivita = ?,
            portfolio_match = ?,
            angle_gag = ?,
            area_tech = ?
        WHERE id = ?
    """, (
        result.get("title_it"),
        result.get("score", 0),
        pillar,
        channel,
        result.get("flow"),
        angle,
        json.dumps(result.get("hooks", []), ensure_ascii=False),
        fmt,
        json.dumps(result.get("hashtags", []), ensure_ascii=False),
        result.get("reasoning"),
        now,
        result.get("tempestivita"),
        result.get("portfolio_match"),
        result.get("angle_gag") or angle,
        result.get("area_tech"),
        article_id,
    ))
    conn.commit()


def score_all() -> int:
    """Classifica articoli non scorati.

    Strategia:
    1. Pre-filtro keyword: articoli ovviamente off-topic → score=0 senza API
    2. Resto: chiamata Claude API con parallelismo limitato e rate limiting
    3. Ogni thread ha la propria connessione SQLite (thread-safety)
    """
    # Connessione del thread principale solo per lettura iniziale
    main_conn = sqlite3.connect(str(DB_PATH))
    main_conn.row_factory = sqlite3.Row
    articles = get_unscored_articles(main_conn)
    main_conn.close()

    if not articles:
        logger.info("Nessun articolo da classificare")
        return 0

    total = len(articles)

    # ── FASE 1: pre-filtro keyword ──
    # Articoli fuori perimetro → score 0 senza chiamare API
    to_score = []
    off_topic = 0
    off_topic_conn = sqlite3.connect(str(DB_PATH))
    for art in articles:
        if is_relevant_by_keyword(art["title"], art.get("summary", "") or ""):
            to_score.append(art)
        else:
            # Salva score 0 con nota "off-topic"
            save_score(off_topic_conn, art["id"], {
                "score": 0,
                "reasoning": "Pre-filtro keyword: titolo fuori perimetro Mactronics (IT enterprise/HPC/storage)",
            })
            off_topic += 1
    off_topic_conn.close()

    logger.info(
        f"{total} articoli totali: {off_topic} scartati via pre-filtro keyword, "
        f"{len(to_score)} da classificare con LLM"
    )

    if not to_score:
        logger.info("Nessun articolo rilevante dopo pre-filtro")
        return 0

    # ── FASE 2: scoring con Claude API ──
    client = anthropic.Anthropic(api_key=get_api_key())
    system_prompt = load_scoring_prompt()
    rate_limiter = RateLimiter(MIN_SECONDS_BETWEEN_CALLS)

    scored = 0
    scored_lock = threading.Lock()

    def process_one(art: dict) -> tuple[int, str, int]:
        """Processa un articolo: rate limit, API call, save su conn del thread."""
        rate_limiter.wait()
        result = score_article(client, system_prompt, art)
        # Ogni thread ha la propria connessione SQLite (thread-safety)
        thread_conn = sqlite3.connect(str(DB_PATH))
        try:
            save_score(thread_conn, art["id"], result)
        finally:
            thread_conn.close()
        nonlocal scored
        with scored_lock:
            scored += 1
            current = scored
        return (result.get("score", 0), art["title"], current)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_one, art): art for art in to_score}
        for future in as_completed(futures):
            try:
                score, title, current = future.result()
                # Log progresso ogni 10 articoli o sui primi 3
                if current <= 3 or current % 10 == 0 or current == len(to_score):
                    logger.info(
                        f"  [{current}/{len(to_score)}] score {score}/10 — {title[:55]}..."
                    )
            except Exception as e:
                logger.error(f"Errore processing articolo: {e}")

    logger.info(f"Scoring completato: {scored} articoli classificati via LLM + {off_topic} scartati via keyword")
    return scored


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    count = score_all()
    print(f"Scoring completato: {count} articoli")
