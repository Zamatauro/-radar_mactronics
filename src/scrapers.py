"""
Scrapers per fonti senza RSS.

Ogni scraper ritorna una lista di dict con le stesse chiavi
dell'output di feedparser, così il fetcher li tratta identici:
    {title, url, summary, published_at}

Aggiungere una nuova fonte:
1. Scrivere una funzione scrape_XXX(homepage_url) -> list[dict]
2. Registrarla nel dict SCRAPERS
3. In config/sources.yaml usare il campo `scraper: XXX` invece di `feed_url`
"""

import re
import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# User-Agent realistico per siti con protezione anti-bot
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}


def _fetch_html(url: str, timeout: int = 15) -> BeautifulSoup | None:
    """Scarica una pagina e ritorna BeautifulSoup. None se errore."""
    try:
        r = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except requests.RequestException as e:
        logger.error(f"Fetch fallito {url}: {e}")
        return None


def _clean_title(text: str) -> str:
    """Rimuove spazi multipli e caratteri strani dal titolo."""
    return re.sub(r"\s+", " ", text or "").strip()


def _dedupe_by_url(items: list[dict]) -> list[dict]:
    """Rimuove duplicati basandosi sull'URL assoluto."""
    seen = set()
    out = []
    for it in items:
        u = it["url"]
        if u not in seen:
            seen.add(u)
            out.append(it)
    return out


# ─────────────────────────────────────────────────────────────
# QUOTIDIANO SANITÀ
# https://www.quotidianosanita.it/
# Strategia: link con pattern quotidianosanita.it/{sezione}/{slug}
# e testo > 30 caratteri (titoli completi). Escludi pagine indice.
# ─────────────────────────────────────────────────────────────
def scrape_quotidiano_sanita(url: str = "https://www.quotidianosanita.it/") -> list[dict]:
    soup = _fetch_html(url)
    if not soup:
        return []

    # Sezioni note di articoli del quotidiano
    valid_sections = {
        "governo-e-parlamento", "regioni-e-asl", "lavoro-e-professioni",
        "studi-e-analisi", "scienza-e-farmaci", "cronache",
        "lettere-al-direttore", "europa-e-mondo",
    }

    articles = []
    pattern = re.compile(r"^https?://(?:www\.)?quotidianosanita\.it/([^/]+)/([^/?#]+)")

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = pattern.match(href)
        if not m:
            continue
        section = m.group(1)
        if section not in valid_sections:
            continue
        title = _clean_title(a.get_text())
        # Filtra titoli troppo corti (probabilmente sono "Leggi tutto" o simili)
        if len(title) < 30:
            continue
        articles.append({
            "title": title,
            "url": href.split("#")[0],
            "summary": "",
            "published_at": None,
        })

    return _dedupe_by_url(articles)[:25]


# ─────────────────────────────────────────────────────────────
# ABOUTPHARMA
# https://www.aboutpharma.com/
# Strategia: h2/h3 a[href] con URL aboutpharma.com/{categoria}/{slug}
# ─────────────────────────────────────────────────────────────
def scrape_aboutpharma(url: str = "https://www.aboutpharma.com/") -> list[dict]:
    soup = _fetch_html(url)
    if not soup:
        return []

    articles = []
    for a in soup.select("h2 a[href], h3 a[href]"):
        href = a.get("href", "")
        if "aboutpharma.com/" not in href:
            continue
        # Deve avere almeno una categoria nel path (es. /sanita-e-politica/slug)
        if href.rstrip("/").count("/") < 4:
            continue
        title = _clean_title(a.get_text())
        if len(title) < 20:
            continue
        articles.append({
            "title": title,
            "url": href.split("#")[0],
            "summary": "",
            "published_at": None,
        })

    return _dedupe_by_url(articles)[:25]


# ─────────────────────────────────────────────────────────────
# PANORAMA SANITÀ
# https://www.panoramasanita.it/  (redirect a panoramadellasanita.it)
# Strategia: h2/h3 a[href] con URL panoramadellasanita.it/site/{slug}
# Escludere link navigazione (testo troncato tipo "▌E ANCORA▌")
# ─────────────────────────────────────────────────────────────
def scrape_panorama_sanita(url: str = "https://www.panoramasanita.it/") -> list[dict]:
    soup = _fetch_html(url)
    if not soup:
        return []

    articles = []
    pattern = re.compile(r"panoramadellasanita\.it/site/[^/?#]+/?$")

    for a in soup.select("h2 a[href], h3 a[href]"):
        href = a.get("href", "")
        if not pattern.search(href):
            continue
        title = _clean_title(a.get_text())
        # Escludi link di navigazione (contengono caratteri speciali o sono troppo corti)
        if len(title) < 25 or "▌" in title or title.isupper():
            continue
        articles.append({
            "title": title,
            "url": href.split("#")[0],
            "summary": "",
            "published_at": None,
        })

    return _dedupe_by_url(articles)[:25]


# ─────────────────────────────────────────────────────────────
# DOCTOR33
# https://www.doctor33.it/
# Strategia: link con URL doctor33.it/articolo/{id}/{slug}
# ─────────────────────────────────────────────────────────────
def scrape_doctor33(url: str = "https://www.doctor33.it/") -> list[dict]:
    soup = _fetch_html(url)
    if not soup:
        return []

    articles = []
    pattern = re.compile(r"doctor33\.it/articolo/\d+/")

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not pattern.search(href):
            continue
        title = _clean_title(a.get_text())
        if len(title) < 30:
            continue
        articles.append({
            "title": title,
            "url": href.split("#")[0],
            "summary": "",
            "published_at": None,
        })

    return _dedupe_by_url(articles)[:25]


# ─────────────────────────────────────────────────────────────
# NURSE24
# https://www.nurse24.it/
# Strategia: <article> con h1/h2/h3 + link relativo
# ─────────────────────────────────────────────────────────────
def scrape_nurse24(url: str = "https://www.nurse24.it/") -> list[dict]:
    soup = _fetch_html(url)
    if not soup:
        return []

    articles = []
    for art in soup.find_all("article"):
        title_el = art.find(["h1", "h2", "h3"])
        link_el = art.find("a", href=True)
        if not title_el or not link_el:
            continue
        title = _clean_title(title_el.get_text())
        if len(title) < 20:
            continue
        href = link_el["href"]
        # Link relativi → assoluti
        full_url = urljoin(url, href)
        if "nurse24.it" not in full_url:
            continue
        articles.append({
            "title": title,
            "url": full_url.split("#")[0],
            "summary": "",
            "published_at": None,
        })

    return _dedupe_by_url(articles)[:25]


# ─────────────────────────────────────────────────────────────
# PHARMASTAR
# https://www.pharmastar.it/news/
# Strategia: pagina /news/ con h2/h3/h4 a[href], URL relativo /news/...
# ─────────────────────────────────────────────────────────────
def scrape_pharmastar(url: str = "https://www.pharmastar.it/news/") -> list[dict]:
    soup = _fetch_html(url)
    if not soup:
        return []

    articles = []
    base = "https://www.pharmastar.it"

    for a in soup.select("h2 a[href], h3 a[href], h4 a[href]"):
        href = a.get("href", "")
        if not href.startswith("/news/"):
            continue
        title = _clean_title(a.get_text())
        if len(title) < 20:
            continue
        # Pulisci doppi slash tipici di PharmaStar
        href_clean = re.sub(r"/+", "/", href)
        full_url = base + href_clean
        articles.append({
            "title": title,
            "url": full_url.split("#")[0],
            "summary": "",
            "published_at": None,
        })

    return _dedupe_by_url(articles)[:25]


# ─────────────────────────────────────────────────────────────
# REGISTRY — da usare in config/sources.yaml come `scraper: nome`
# ─────────────────────────────────────────────────────────────
SCRAPERS = {
    "quotidiano_sanita": scrape_quotidiano_sanita,
    "aboutpharma": scrape_aboutpharma,
    "panorama_sanita": scrape_panorama_sanita,
    "doctor33": scrape_doctor33,
    "nurse24": scrape_nurse24,
    "pharmastar": scrape_pharmastar,
}


def run_scraper(scraper_name: str, url: str | None = None) -> list[dict]:
    """Esegue uno scraper specifico. Ritorna lista di articoli."""
    fn = SCRAPERS.get(scraper_name)
    if not fn:
        logger.error(f"Scraper sconosciuto: {scraper_name}")
        return []
    if url:
        return fn(url)
    return fn()


if __name__ == "__main__":
    # Test rapido di tutti gli scrapers
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    for name in SCRAPERS:
        print(f"\n=== {name} ===")
        arts = run_scraper(name)
        print(f"Trovati: {len(arts)} articoli")
        for a in arts[:3]:
            print(f"  • {a['title'][:70]}")
            print(f"    {a['url'][:90]}")
