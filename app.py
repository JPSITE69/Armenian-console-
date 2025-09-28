# app.py — Console Arménienne (auto-import + auto-publish + image obligatoire + titres par contenu global + >=120 mots + clé OpenAI persistée)
from flask import Flask, request, redirect, url_for, Response, render_template_string, session, flash
import sqlite3, os, hashlib, io, traceback, re, threading, time, json as _json
from datetime import datetime, timezone
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
import feedparser
from PIL import Image, UnidentifiedImageError

# ================== CONFIG ==================
APP_NAME   = "Console Arménienne"
ADMIN_PASS = os.environ.get("ADMIN_PASS", "armenie")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")
DB_PATH    = "site.db"

# Auto
AUTO_PUBLISH = True          # Publie immédiatement tout nouvel article
REQUIRE_IMAGE = True         # Photo obligatoire
IMPORT_INTERVAL_MIN = int(os.environ.get("IMPORT_INTERVAL_MIN", "10"))  # boucle auto (minutes)

# Cibles de longueur
TARGET_MIN_WORDS = int(os.environ.get("TARGET_MIN_WORDS", "120"))  # minimum strict demandé
TARGET_MAX_WORDS = int(os.environ.get("TARGET_MAX_WORDS", "420"))  # borne haute de secours (souple)

# ---- RSS par défaut
DEFAULT_FEEDS = [
    "https://www.civilnet.am/news/feed/",
    "https://armenpress.am/rss/",
    "https://news.am/eng/rss/",
    "https://factor.am/feed",
    "https://hetq.am/hy/rss",
]

# ---- Scrapers d'index par défaut (éditables dans /admin)
DEFAULT_SCRAPERS = [
    {
        "name": "CivilNet",
        "index_url": "https://www.civilnet.am/news/",
        "link_selector": "article h3 a, article a[href^='/news/']",
        "title_selector": "h1",
        "content_selector": "article .entry-content, article .post-content, article .content, article",
        "image_selectors": [
            "meta[property='og:image']::content",
            "meta[name='twitter:image']::content",
            "article img::src",
            "img::src"
        ],
        "max_items": 7
    },
    {
        "name": "Armenpress",
        "index_url": "https://armenpress.am/",
        "link_selector": "a[href*='/article/'], .news-item a, .list-item a",
        "title_selector": "h1, .article-title h1",
        "content_selector": ".article-content, .content-article, article",
        "image_selectors": [
            "meta[property='og:image']::content",
            "meta[name='twitter:image']::content",
            ".article-content img::src",
            "img::src"
        ],
        "max_items": 7
    },
    {
        "name": "News.am (eng)",
        "index_url": "https://news.am/eng/",
        "link_selector": "a[href*='/eng/news/'], .news-list a, article a",
        "title_selector": "h1",
        "content_selector": ".article, .post-content, article",
        "image_selectors": [
            "meta[property='og:image']::content",
            "meta[name='twitter:image']::content",
            ".article img::src",
            "img::src"
        ],
        "max_items": 7
    },
    {
        "name": "Factor.am",
        "index_url": "https://factor.am/",
        "link_selector": "article h2 a, .td_module_16 .entry-title a, .td-module-thumb a",
        "title_selector": "h1.entry-title, h1",
        "content_selector": ".td-post-content, article .entry-content, article",
        "image_selectors": [
            "meta[property='og:image']::content",
            "meta[name='twitter:image']::content",
            ".td-post-content img::src",
            "img::src"
        ],
        "max_items": 7
    },
    {
        "name": "Hetq (HY)",
        "index_url": "https://hetq.am/hy/",
        "link_selector": "article h3 a, .article-list a, a[href*='/hy/article/']",
        "title_selector": "h1",
        "content_selector": ".article-content, .content-article, article",
        "image_selectors": [
            "meta[property='og:image']::content",
            "meta[name='twitter:image']::content",
            ".article-content img::src",
            "img::src"
        ],
        "max_items": 7
    },
    {
        "name": "Azatutyun (RFE/RL)",
        "index_url": "https://www.azatutyun.am/news",
        "link_selector": "a[href*='/a/'], article a, .teaser a",
        "title_selector": "h1",
        "content_selector": ".wsw, article, .article-body, .text",
        "image_selectors": [
            "meta[property='og:image']::content",
            "meta[name='twitter:image']::content",
            ".wsw img::src",
            "img::src"
        ],
        "max_items": 7
    }
]

# OpenAI via ENV (écrasé par les paramètres admin si saisis)
ENV_OPENAI_KEY   = os.environ.get("OPENAI_API_KEY", "").strip()
ENV_OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ================== DB ==================
def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def column_exists(con, table, name):
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == name for r in rows)

def init_db():
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS posts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        body  TEXT,
        status TEXT DEFAULT 'draft',         -- draft | scheduled | published
        created_at TEXT,
        updated_at TEXT,
        publish_at TEXT,                     -- ISO UTC quand planifié
        image_url TEXT,
        image_sha1 TEXT,
        orig_link TEXT UNIQUE,
        source TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    if not column_exists(con, "posts", "publish_at"):
        con.execute("ALTER TABLE posts ADD COLUMN publish_at TEXT")
    con.commit(); con.close()

def get_setting(key, default=""):
    con = db()
    try:
        r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default
    finally:
        con.close()

def set_setting(key, value):
    con = db()
    try:
        con.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        con.commit()
    finally:
        con.close()

# --- Bootstrap / cache OpenAI (clé une seule fois) ---
_OPENAI_CACHE = {"key": None, "model": None}

def bootstrap_openai_key():
    db_key = get_setting("openai_key", "").strip()
    if not db_key and ENV_OPENAI_KEY:
        set_setting("openai_key", ENV_OPENAI_KEY.strip())
    db_model = get_setting("openai_model", "").strip()
    if not db_model and ENV_OPENAI_MODEL:
        set_setting("openai_model", ENV_OPENAI_MODEL.strip())

def active_openai():
    if _OPENAI_CACHE["key"] and _OPENAI_CACHE["model"]:
        return _OPENAI_CACHE["key"], _OPENAI_CACHE["model"]
    key = get_setting("openai_key", ENV_OPENAI_KEY).strip()
    model = get_setting("openai_model", ENV_OPENAI_MODEL).strip()
    _OPENAI_CACHE["key"] = key
    _OPENAI_CACHE["model"] = model
    return key, model

# ================== UTILS TEXTE ==================
TAG_RE = re.compile(r"<[^>]+>")
FR_TOKENS = set(" le la les un une des du de au aux et en sur pour par avec dans que qui ne pas est été sont était selon afin aussi plus leur lui ses ces cette ce cela donc ainsi tandis alors contre entre vers depuis sans sous après avant comme lorsque tandis que où dont même".split())

TITLE_MIN = 20
TITLE_MAX = 110
SITE_SUFFIX_PAT = re.compile(
    r"\s*(?:[-–|»]\s*)?(?:CivilNet|Armenpress|News\.am|Factor\.am|Hetq|Azatutyun|RFE/RL)\s*$",
    re.I
)

def strip_tags(s: str) -> str:
    return TAG_RE.sub("", s or "")

def looks_french(text: str) -> bool:
    if not text: return False
    t = text.lower()
    words = re.findall(r"[a-zàâäéèêëïîôöùûüç'-]+", t)
    if not words: return False
    hits = sum(1 for w in words[:80] if w in FR_TOKENS)
    return hits >= 5

def _smart_truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0].strip(",.;:—-– ")
    return cut if len(cut) >= TITLE_MIN else s[:limit].rstrip()

def _clean_punct(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[\[\(](?:photos?|video|vidéo|live|mise à jour|update)[\]\)]", "", s, flags=re.I)
    s = re.sub(r"[\[\(].{0,40}?[\]\)]\s*$", "", s)
    s = re.sub(r"[“”]", '"', s)
    s = re.sub(r"[‘’]", "'", s)
    s = s.strip(" -–|:;,.")
    return s

def _sentence_case(fr: str) -> str:
    if not fr: return fr
    fr = fr.strip()
    if fr.isupper() and len(fr) > 4:
        fr = fr.capitalize()
    if fr and fr[0].islower():
        fr = fr[0].upper() + fr[1:]
    return fr

def normalize_title(src: str) -> str:
    if not src: return "Actualité"
    t = src.strip()
    t = SITE_SUFFIX_PAT.sub("", t)
    t = _clean_punct(t)
    t = re.sub(r"[^\w\sÀ-ÖØ-öø-ÿ’'\"!?.,:;()-–—]", "", t)
    t = _sentence_case(t)
    if len(t) > TITLE_MAX:
        t = _smart_truncate(t, TITLE_MAX)
    t = t.rstrip()
    if t.endswith(("—", "–", "-", ":", ";", ",")):
        t = t[:-1].rstrip()
    return t or "Actualité"

def _title_from_text_fallback(fr_text: str) -> str:
    t = (fr_text or "").strip()
    if not t: return "Actualité"
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9'’\-]+", t)
    base = " ".join(words[:16]).strip()
    base = re.sub(r"\s+", " ", base)
    base = base[:TITLE_MAX]
    base = normalize_title(base)
    return base

def ensure_signature(body: str) -> str:
    b = (body or "").rstrip()
    if not b.endswith("- LesArmeniens.com"):
        b += "\n\n- LesArmeniens.com"
    return b

# ------- Helpers longueur >= 120 et titre basé contenu -------
STOP_FR = set("""
le la les un une des du de au aux en sur pour par avec dans que qui ne pas est été
sont était selon afin aussi plus leur lui ses ces cette ce cela donc ainsi tandis
alors contre entre vers depuis sans sous après avant comme lorsque où dont même
""".split())

def _tokenize_words(text: str) -> list[str]:
    return re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9'’\-]+", (text or "").lower())

def _word_count(text: str) -> int:
    return len(_tokenize_words(text))

def ensure_min_words(body_text: str, source_text: str, min_words: int = TARGET_MIN_WORDS) -> str:
    """
    Si body_text < min_words, on complète avec du texte propre issu de source_text.
    On respecte la signature - LesArmeniens.com (pas de doublon).
    """
    body = (body_text or "").strip()
    sig = "- LesArmeniens.com"
    if body.endswith(sig):
        body = body[:-len(sig)].rstrip()

    if _word_count(body) >= min_words:
        return ensure_signature(body)

    src_clean = strip_tags(source_text or "")
    extra = " ".join([w for w in src_clean.split() if w not in set(body.split())]) or src_clean

    body_words = _tokenize_words(body)
    extra_words = _tokenize_words(extra)
    need = max(0, min_words - len(body_words))
    if need > 0 and extra_words:
        take = min(len(extra_words), int(need * 1.2))
        body = (body + " " + " ".join(extra_words[:take])).strip()

    if _word_count(body) > TARGET_MAX_WORDS:
        body = " ".join(_tokenize_words(body)[:TARGET_MAX_WORDS])

    return ensure_signature(body)

def title_relevance_score(title: str, body_text: str) -> float:
    tw = [w for w in _tokenize_words(title) if w not in STOP_FR]
    if not tw:
        return 0.0
    bw = set([w for w in _tokenize_words(body_text) if w not in STOP_FR])
    hits = sum(1 for w in tw if w in bw)
    return hits / max(1, len(tw))

def derive_title_from_body(body_text: str, min_words: int = 6, max_words: int = 14) -> str:
    """
    Titre à partir de TOUT le contenu :
      - tokens pleins (sans stopwords),
      - score mots = fréquence * bonus longueur,
      - bigrammes/trigrammes scorés et assemblés,
      - normalisation finale.
    """
    txt = strip_tags(body_text or "").strip()
    if not txt:
        return "Actualité"

    tokens = [w for w in _tokenize_words(txt) if w not in STOP_FR and len(w) > 2]
    if not tokens:
        return normalize_title(_title_from_text_fallback(txt))

    freq = {}
    for w in tokens:
        freq[w] = freq.get(w, 0) + 1
    w_weight = {w: freq[w] * (1.0 + min(len(w), 14) / 6.0) for w in freq}

    def ngrams(words, n):
        return [" ".join(words[i:i+n]) for i in range(len(words)-n+1)]
    bigrams  = ngrams(tokens, 2)
    trigrams = ngrams(tokens, 3)

    def score_ngram(ng):
        parts = ng.split()
        uniq = set(parts)
        base = sum(w_weight.get(p, 0.0) for p in parts)
        rep_penalty = (len(parts) / len(uniq)) if len(uniq) else 2.0
        return base / rep_penalty

    cand_tri = {}
    for g in trigrams:
        cand_tri[g] = cand_tri.get(g, 0.0) + score_ngram(g)
    cand_bi = {}
    for g in bigrams:
        cand_bi[g] = cand_bi.get(g, 0.0) + score_ngram(g)

    top_tri = sorted(cand_tri.items(), key=lambda kv: kv[1], reverse=True)[:12]
    top_bi  = sorted(cand_bi.items(),  key=lambda kv: kv[1], reverse=True)[:12]

    chosen = []
    used_words = set()

    def push_phrase(phrase):
        nonlocal chosen, used_words
        for w in phrase.split():
            if w not in used_words:
                chosen.append(w)
                used_words.add(w)

    if top_tri:
        push_phrase(top_tri[0][0])
    elif top_bi:
        push_phrase(top_bi[0][0])
    else:
        for w, _ in sorted(w_weight.items(), key=lambda kv: kv[1], reverse=True)[:max_words]:
            push_phrase(w)

    if len(chosen) < min_words and top_bi:
        for phrase, _sc in top_bi:
            if len(chosen) >= max_words:
                break
            p_words = phrase.split()
            overlap = sum(1 for w in p_words if w in used_words)
            if overlap <= len(p_words) // 2:
                push_phrase(phrase)

    if len(chosen) < min_words:
        for w, _ in sorted(w_weight.items(), key=lambda kv: kv[1], reverse=True):
            if len(chosen) >= min(min_words, max_words):
                break
            if w not in used_words:
                push_phrase(w)

    headline = " ".join(chosen[:max_words])
    return normalize_title(headline)

# ================== RÉÉCRITURE ==================
def rewrite_article_fr(title_src: str, raw_text: str):
    """Retourne (title_fr, body_fr, sure_fr).
       FR + signature LesArmeniens.com + normalisation titre + >=120 mots + recalcul titre (contenu global)."""
    if not raw_text:
        return (normalize_title(title_src or "Actualité"), "", False)

    key, model = active_openai()
    clean_input = strip_tags(raw_text)

    def call_openai():
        prompt = (
            "Tu es un journaliste francophone.\n"
            "Réécris en FRANÇAIS le TITRE et le CORPS de l'article ci-dessous, "
            "sans inventer de faits, en conservant les informations factuelles (noms, lieux, chiffres, citations importantes).\n"
            f"Longueur du corps : au moins {TARGET_MIN_WORDS} mots (idéalement <= {TARGET_MAX_WORDS}).\n"
            "Style neutre, informatif, fluide, sans listes à puces ni intertitres HTML.\n"
            "RENVOIE UNIQUEMENT du JSON avec les clés 'title' et 'body'.\n"
            "Le 'body' doit être du TEXTE BRUT (pas de balises) et DOIT se terminer par: - LesArmeniens.com.\n\n"
            f"Titre (source): {title_src}\n"
            f"Texte (source): {clean_input}"
        )
        payload = {
            "model": model or "gpt-4o-mini",
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": "Tu écris en français clair et concis. Réponds uniquement au format demandé."},
                {"role": "user", "content": prompt}
            ]
        }
        r = requests.post("https://api.openai.com/v1/chat/completions",
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json=payload, timeout=60)
        j = r.json()
        out = (j.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        try:
            data = _json.loads(out)
            title_fr = strip_tags(data.get("title","")).strip()
            body_fr  = strip_tags(data.get("body","")).strip()
        except Exception:
            parts = out.split("\n", 1)
            title_fr = strip_tags(parts[0]).strip()
            body_fr  = strip_tags(parts[1] if len(parts) > 1 else "").strip()
        if not body_fr:
            body_fr = " ".join(clean_input.split()).strip()

        # garanties longueur + signature
        body_fr = ensure_min_words(body_fr, clean_input, TARGET_MIN_WORDS)

        # titre : si manquant, court ou peu pertinent -> re-généré depuis le contenu global
        needs_derive = (
            not title_fr
            or len(_tokenize_words(title_fr)) < 4
            or title_relevance_score(title_fr, body_fr) < 0.25
        )
        if needs_derive:
            title_fr = derive_title_from_body(body_fr)

        title_fr = normalize_title(title_fr)
        return title_fr, body_fr

    if key:
        try:
            t1, b1 = call_openai()
            if looks_french(b1) and looks_french(t1):
                return (t1, b1, True)
            t2, b2 = call_openai()
            if looks_french(b2) and looks_french(t2):
                return (t2, b2, True)
            tfb = _title_from_text_fallback(b2)
            b2  = ensure_min_words(b2, clean_input, TARGET_MIN_WORDS)
            if title_relevance_score(tfb, b2) < 0.25 or len(_tokenize_words(tfb)) < 4:
                tfb = derive_title_from_body(b2)
            return (normalize_title(tfb), b2, False)
        except Exception as e:
            print(f"[AI] rewrite_article_fr failed: {e}")

    # Fallback local
    fr_body = " ".join(strip_tags(raw_text).split())
    if _word_count(fr_body) > TARGET_MAX_WORDS:
        fr_body = " ".join(_tokenize_words(fr_body)[:TARGET_MAX_WORDS])
    fr_body = ensure_min_words(fr_body, raw_text, TARGET_MIN_WORDS)
    fr_title = _title_from_text_fallback(fr_body)
    if title_relevance_score(fr_title, fr_body) < 0.25 or len(_tokenize_words(fr_title)) < 4:
        fr_title = derive_title_from_body(fr_body)
    return (normalize_title(fr_title), fr_body, False)

# ================== HTTP & IMAGES ==================
def http_get(url, timeout=20):
    r = requests.get(url, timeout=timeout, allow_redirects=True, headers={
        "User-Agent": "Mozilla/5.0 (+RenderBot)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr,en;q=0.8",
    })
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r.text

def fetch_xml(url, timeout=25):
    r = requests.get(
        url, timeout=timeout, allow_redirects=True,
        headers={
            "User-Agent": "Console-Armenie/1.0 (+https://armenian-console.onrender.com)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
            "Accept-Language": "fr,en;q=0.8",
        },
    )
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r.text

def soup_select_attr(soup, selector):
    attr = None
    sel = selector
    if "::content" in selector:
        sel, attr = selector.split("::content", 1)[0], "content"
    elif "::src" in selector:
        sel, attr = selector.split("::src", 1)[0], "src"
    tag = soup.select_one(sel.strip())
    if not tag:
        return None
    if attr:
        val = tag.get(attr)
        return val if val else None
    return tag.get_text(" ", strip=True)

def find_main_image_in_html(html, base_url=None):
    soup = BeautifulSoup(html, "html.parser")
    for sel in ["meta[property='og:image']","meta[name='twitter:image']"]:
        m = soup.select_one(sel)
        if m and m.get("content"):
            return urljoin(base_url or "", m["content"])
    a = soup.find("article")
    if a:
        imgtag = a.find("img")
        if imgtag and imgtag.get("src"):
            return urljoin(base_url or "", imgtag["src"])
    imgtag = soup.find("img")
    if imgtag and imgtag.get("src"):
        return urljoin(base_url or "", imgtag["src"])
    return None

def get_image_from_entry(entry, page_html=None, page_url=None):
    try:
        media = entry.get("media_content") or entry.get("media_thumbnail")
        if isinstance(media, list) and media:
            u = media[0].get("url")
            if u: return urljoin(page_url or "", u)
    except Exception:
        pass
    try:
        enc = entry.get("enclosures") or entry.get("links")
        if isinstance(enc, list):
            for en in enc:
                href = en.get("href") if isinstance(en, dict) else None
                if href and any(href.lower().split("?")[0].endswith(ext) for ext in (".jpg",".jpeg",".png",".webp",".gif")):
                    return urljoin(page_url or "", href)
    except Exception:
        pass
    for k in ("content","summary","description"):
        v = entry.get(k)
        if not v: continue
        html = ""
        if isinstance(v, list) and v:
            html = v[0].get("value", "")
        elif isinstance(v, dict):
            html = v.get("value","")
        elif isinstance(v, str):
            html = v
        if html:
            s = BeautifulSoup(html, "html.parser")
            imgtag = s.find("img")
            if imgtag and imgtag.get("src"):
                return urljoin(page_url or "", imgtag["src"])
    if page_html:
        return find_main_image_in_html(page_html, base_url=page_url)
    return None

def download_image(url):
    if not url:
        return None, None
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.content

        try:
            im = Image.open(io.BytesIO(data))
            im.load()
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")

            os.makedirs("static/images", exist_ok=True)
            sha1 = hashlib.sha1(data).hexdigest()
            path = f"static/images/{sha1}.jpg"
            if not os.path.exists(path):
                im.save(path, format="JPEG", quality=88, optimize=True)

            return "/" + path, sha1
        except Exception as e:
            print(f"[IMG] convert/save fail {url}: {e}")
            return None, None

    except Exception as e:
        print(f"[IMG] download failed for {url}: {e}")
        return None, None

# ================== EXTRACTION TEXTE ==================
SEL_CANDIDATES = [
    "article",
    ".entry-content", ".post-content", ".td-post-content",
    ".article-content", ".content-article", ".article-body",
    "#article-body", "#content article", ".post__text", ".story-content",
    ".single-content", ".content"
]

def extract_article_text(html):
    soup = BeautifulSoup(html, "html.parser")
    node_text, best_len = "", 0
    for sel in SEL_CANDIDATES:
        cand = soup.select_one(sel)
        if cand:
            text = " ".join(p.get_text(" ", strip=True) for p in (cand.find_all(["p","h2","li"]) or [cand]))
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > best_len:
                best_len = len(text); node_text = text
    if not node_text:
        text = " ".join(p.get_text(" ", strip=True) for p in soup.find_all("p"))
        node_text = re.sub(r"\s+", " ", text).strip()
    return node_text[:5000] if node_text else ""

def html_from_entry(entry):
    if "content" in entry and getattr(entry, "content", None):
        if isinstance(entry.content, list): return entry.content[0].get("value","")
        if isinstance(entry.content, dict): return entry.content.get("value","")
    return entry.get("summary","") or entry.get("description","")

# ================== SCRAPE (RSS + index) ==================
def already_have_link(link: str) -> bool:
    con = db()
    try:
        return con.execute("SELECT 1 FROM posts WHERE orig_link=?", (link,)).fetchone() is not None
    finally:
        con.close()

def insert_post(title_fr, body_text, link, source, img_url):
    # Image obligatoire ⇒ si pas d'URL trouvée, on tente l'image par défaut (si paramétrée)
    if not img_url:
        default_img = get_setting("default_image_url", "").strip()
        if default_img:
            img_url = default_img

    if REQUIRE_IMAGE and not img_url:
        return False

    local_path, sha1 = download_image(img_url) if img_url else (None, None)
    if REQUIRE_IMAGE and (not local_path or not sha1):
        return False

    # anti-doublon image
    if sha1:
        con = db()
        try:
            if con.execute("SELECT 1 FROM posts WHERE image_sha1=?", (sha1,)).fetchone():
                return False
        finally:
            con.close()

    now = datetime.now(timezone.utc).isoformat()
    status = "published" if AUTO_PUBLISH else "draft"

    con = db()
    try:
        con.execute("""INSERT INTO posts
          (title, body, status, created_at, updated_at, publish_at, image_url, image_sha1, orig_link, source)
          VALUES(?,?,?,?,?,?,?,?,?,?)""",
          (title_fr, body_text, status, now, now, None, local_path, sha1, link, source))
        con.commit()
        return True
    except Exception as e:
        print("[DB] insert_post error:", e)
        return False
    finally:
        con.close()

def scrape_rss_once(feeds):
    created, skipped = 0, 0
    for feed in feeds:
        try:
            try:
                xml = fetch_xml(feed)
                fp = feedparser.parse(xml)
            except Exception as e:
                print(f"[FEED] fetch/parse error {feed}: {e}")
                skipped += 1
                continue

            feed_title = fp.feed.get("title","") if getattr(fp, "feed", None) else ""
            for e in getattr(fp, "entries", [])[:20]:
                try:
                    link = e.get("link") or ""
                    if not link or already_have_link(link):
                        skipped += 1; continue

                    title_src = normalize_title((e.get("title") or "(Sans titre)").strip())

                    # page → extraction texte
                    page_html = ""
                    try:
                        page_html = http_get(link)
                    except Exception as ee:
                        print(f"[PAGE] fetch fail {link}: {ee}")
                    article_text = extract_article_text(page_html) if page_html else ""
                    if not article_text:
                        article_text = BeautifulSoup(html_from_entry(e), "html.parser").get_text(" ", strip=True)
                    if not article_text or len(article_text) < 120:
                        skipped += 1; continue

                    # image : tente la page / RSS, sinon image par défaut
                    img_url = get_image_from_entry(e, page_html=page_html, page_url=link)
                    if not img_url:
                        default_img = get_setting("default_image_url", "").strip()
                        if default_img:
                            img_url = default_img
                    if REQUIRE_IMAGE and not img_url:
                        skipped += 1
                        continue

                    # FR
                    title_fr, body_text, _sure_fr = rewrite_article_fr(title_src, article_text)
                    if not body_text:
                        skipped += 1; continue

                    if insert_post(title_fr, body_text, link, feed_title, img_url):
                        created += 1
                    else:
                        skipped += 1
                except Exception as ex:
                    skipped += 1
                    print(f"[RSS ENTRY] error: {ex}")
                    traceback.print_exc()
        except Exception as e:
            print(f"[FEED] parse error {feed}: {e}")
            continue
    return created, skipped

def normalize_url(base, href):
    if not href: return None
    href = href.strip()
    if href.startswith("#"): return None
    return urljoin(base, href)

def scrape_index_once(scrapers_json):
    created, skipped = 0, 0
    for cfg in scrapers_json:
        try:
            name = cfg.get("name","")
            index_url = cfg["index_url"]
            link_sel  = cfg["link_selector"]
            max_items = int(cfg.get("max_items", 6))
            html = http_get(index_url)
            soup = BeautifulSoup(html, "html.parser")
            links = []
            for a in soup.select(link_sel)[: max_items * 3]:
                href = a.get("href")
                full = normalize_url(index_url, href)
                if full and full not in links:
                    links.append(full)
                if len(links) >= max_items:
                    break

            for link in links:
                try:
                    if already_have_link(link):
                        skipped += 1; continue
                    page = http_get(link)
                    psoup = BeautifulSoup(page, "html.parser")

                    # titre
                    title_sel = cfg.get("title_selector","h1")
                    title_src = soup_select_attr(psoup, title_sel) or "(Sans titre)"
                    title_src = normalize_title(title_src)

                    # contenu
                    content_sel = cfg.get("content_selector") or ""
                    node_text = ""
                    if content_sel:
                        node = psoup.select_one(content_sel)
                        if node:
                            node_text = " ".join(p.get_text(" ", strip=True) for p in (node.find_all(["p","h2","li"]) or [node]))
                            node_text = re.sub(r"\s+", " ", node_text).strip()
                    if not node_text:
                        node_text = extract_article_text(page)
                    if not node_text or len(node_text) < 120:
                        skipped += 1; continue

                    # image : page / meta / fallback par défaut
                    img = None
                    for isel in cfg.get("image_selectors", []):
                        val = soup_select_attr(psoup, isel)
                        if val:
                            img = urljoin(link, val)
                            break
                    if not img:
                        img = find_main_image_in_html(page, base_url=link)
                    if not img:
                        default_img = get_setting("default_image_url", "").strip()
                        if default_img:
                            img = default_img
                    if REQUIRE_IMAGE and not img:
                        skipped += 1
                        continue

                    # FR
                    title_fr, body_text, _sure = rewrite_article_fr(title_src, node_text)
                    if not body_text:
                        skipped += 1; continue

                    if insert_post(title_fr, body_text, link, name, img):
                        created += 1
                    else:
                        skipped += 1
                except Exception as inner:
                    skipped += 1
                    print(f"[SCRAPER:{name}] article error:", inner)
        except Exception as e:
            print("[SCRAPER] config error:", e)
    return created, skipped

# ================== SCHEDULER (publication auto) ==================
def publish_due_loop():
    while True:
        try:
            now = datetime.now(timezone.utc).isoformat()
            con = db()
            try:
                rows = con.execute(
                    "SELECT id FROM posts WHERE status='scheduled' AND publish_at IS NOT NULL AND publish_at <= ?",
                    (now,)).fetchall()
                if rows:
                    ids = [r["id"] for r in rows]
                    con.execute(
                        f"UPDATE posts SET status='published', updated_at=? WHERE id IN ({','.join('?'*len(ids))})",
                        (now, *ids)
                    )
                    con.commit()
                    print(f"[SCHED] Published IDs: {ids}")
            finally:
                con.close()
        except Exception as e:
            print("[SCHED] loop error:", e)
        time.sleep(30)

# ======== Boucle d'import automatique (RSS + scrapers) ========
def import_loop():
    while True:
        try:
            print("[IMPORT LOOP] démarrage cycle...")
            # RSS
            feeds_txt = get_setting("feeds", "\n".join(DEFAULT_FEEDS))
            feed_list = [u.strip() for u in feeds_txt.splitlines() if u.strip()]
            # Scrapers
            try:
                scrapers_cfg = _json.loads(get_setting("scrapers_json", _json.dumps(DEFAULT_SCRAPERS)))
                if not isinstance(scrapers_cfg, list):
                    raise ValueError("Le JSON de scrapers doit être une liste []")
            except Exception as e:
                print(f"[IMPORT LOOP] Config sites JSON invalide: {e}")
                scrapers_cfg = []
            c1, s1 = scrape_rss_once(feed_list)
            c2, s2 = scrape_index_once(scrapers_cfg)
            set_setting("last_import_result", f"OK (auto): {c1+c2} nouveaux, {s1+s2} ignorés.")
            print(f"[IMPORT LOOP] terminé: {c1+c2} nouveaux, {s1+s2} ignorés.")
        except Exception as e:
            msg = f"Erreur (auto import): {e}\n{traceback.format_exc()}"
            print("[IMPORT LOOP] fatal:", msg)
            set_setting("last_import_result", msg)
        time.sleep(max(60, IMPORT_INTERVAL_MIN * 60))

# ================== UI ==================
LAYOUT = """
<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{title}}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
<body class="container">
<nav>
  <ul><li><strong>{{appname}}</strong></li></ul>
  <ul>
    <li><a href="{{ url_for('home') }}">Accueil</a></li>
    <li><a href="{{ url_for('rss_xml') }}" target="_blank">RSS</a></li>
    {% if session.get('ok') %}
      <li><a href="{{ url_for('admin') }}">Admin</a></li>
      <li><a href="{{ url_for('logout') }}">Déconnexion</a></li>
    {% else %}
      <li><a href="{{ url_for('admin') }}">Connexion</a></li>
    {% endif %}
  </ul>
</nav>
<main>
  {% with m=get_flashed_messages() %}{% if m %}<article>{% for x in m %}<p>{{x}}</p>{% endfor %}</article>{% endif %}{% endwith %}
  {{ body|safe }}
</main>
<footer><small>&copy; {{year}} — {{appname}}</small></footer>
</body>"""
def page(body, title=""):
    return render_template_string(LAYOUT, body=body, title=title or APP_NAME,
                                 appname=APP_NAME, year=datetime.now().year)

@app.get("/health")
def health():
    return "OK"

@app.get("/")
def home():
    con = db()
    try:
        rows = con.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC LIMIT 50").fetchall()
    finally:
        con.close()
    if not rows:
        return page("<h2>Dernières publications</h2><p>Aucune publication pour l’instant.</p>", "Publications")
    cards = []
    for r in rows:
        img = f"<img src='{r['image_url']}' alt='' style='max-width:100%;height:auto'>" if r["image_url"] else ""
        created = (r['created_at'] or '')[:16].replace('T',' ')
        body_html = (r['body'] or '').replace("\n", "<br>")
        cards.append(f"<article><header><h3>{r['title']}</h3><small>{created}</small></header>{img}<p>{body_html}</p></article>")
    return page("<h2>Dernières publications</h2>" + "".join(cards), "Publications")

@app.get("/rss.xml")
def rss_xml():
    con = db()
    try:
        rows = con.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC LIMIT 100").fetchall()
    finally:
        con.close()
    items = []
    for r in rows:
        title = (r["title"] or "").replace("&","&amp;")
        desc  = (r["body"] or "").replace("&","&amp;")
        enclosure = f"<enclosure url='{request.url_root.rstrip('/') + r['image_url']}' type='image/jpeg'/>" if r["image_url"] else ""
        pub   = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S %z')
        items.append(f"<item><title>{title}</title><link>{request.url_root}</link><guid isPermaLink='false'>{r['id']}</guid><description><![CDATA[{desc}]]></description>{enclosure}<pubDate>{pub}</pubDate></item>")
    rss = f"<?xml version='1.0' encoding='UTF-8'?><rss version='2.0'><channel><title>{APP_NAME} — Flux</title><link>{request.url_root}</link><description>Articles publiés</description>{''.join(items)}</channel></rss>"
    return Response(rss, mimetype="application/rss+xml")

@app.route("/admin", methods=["GET","POST"])
def admin():
    if request.method == "POST" and not session.get("ok"):
        if request.form.get("password") == ADMIN_PASS:
            session["ok"] = True
            return redirect(url_for("admin"))
        flash("Mot de passe incorrect."); return redirect(url_for("admin"))

    if not session.get("ok"):
        return page("""<h3>Connexion</h3><form method="post">
          <input type="password" name="password" placeholder="Mot de passe" required>
          <button>Entrer</button></form>""", "Connexion")

    feeds = get_setting("feeds", "\n".join(DEFAULT_FEEDS))
    openai_key   = get_setting("openai_key", ENV_OPENAI_KEY)
    openai_model = get_setting("openai_model", ENV_OPENAI_MODEL)
    default_image = get_setting("default_image_url", "").strip()
    scrapers_json_txt = get_setting("scrapers_json", _json.dumps(DEFAULT_SCRAPERS, ensure_ascii=False, indent=2))
    last_result = get_setting("last_import_result", "").strip()

    con = db()
    try:
        drafts    = con.execute("SELECT * FROM posts WHERE status='draft' ORDER BY id DESC").fetchall()
        scheduled = con.execute("SELECT * FROM posts WHERE status='scheduled' ORDER BY publish_at ASC").fetchall()
        pubs      = con.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC").fetchall()
    finally:
        con.close()

    def card(r, published=False):
        img = f"<img src='{r['image_url']}' style='max-width:200px'>" if r["image_url"] else "<small style='color:#900'>⚠️ Pas d'image</small>"
        pub_at = (r['publish_at'] or '')[:16]
        state_btns = ("<button name='action' value='unpublish' class='secondary'>⏸️ Dépublier</button>"
                      if published else
                      "<button name='action' value='publish' class='secondary'>✅ Publier maintenant</button>")
        return f"""
        <details>
          <summary><b>{r['title'] or '(Sans titre)'}</b> — <small>{r['status']}</small></summary>
          {img}
          <form method="post" action="{url_for('save', post_id=r['id'])}">
            <label>Titre<input name="title" value="{(r['title'] or '').replace('"','&quot;')}"></label>
            <label>Contenu<textarea name="body" rows="6">{r['body'] or ''}</textarea></label>
            <div class="grid">
              <button name="action" value="save">💾 Enregistrer</button>
              {state_btns}
              <button name="action" value="delete" class="contrast">🗑️ Supprimer</button>
            </div>
            <label>Publier à (UTC)
              <input type="datetime-local" name="publish_at" value="{pub_at}">
            </label>
            <div class="grid">
              <button name="action" value="schedule" class="secondary">🕒 Planifier</button>
            </div>
          </form>
        </details>"""

    body = f"""
    <h3>Paramètres</h3>
    {f"<p><mark>{last_result}</mark></p>" if last_result else ""}
    <article>
      <form method="post" action="{url_for('save_settings')}">
        <div class="grid">
          <label>OpenAI API Key (secours si ENV absente)
            <input type="password" name="openai_key" placeholder="sk-..." value="{openai_key}">
          </label>
          <label>OpenAI Model
            <input name="openai_model" placeholder="gpt-4o-mini" value="{openai_model}">
          </label>
        </div>
        <label>Image par défaut (URL)
          <input name="default_image_url" placeholder="https://..." value="{default_image}">
        </label>
        <label>Sources RSS (une URL par ligne)
          <textarea name="feeds" rows="5">{feeds}</textarea>
        </label>
        <label>Scrapers de sites (JSON)</label>
        <textarea name="scrapers_json" rows="18" style="font-family:monospace">{scrapers_json_txt}</textarea>
        <button>💾 Enregistrer les paramètres</button>
      </form>

      <form method="post" action="{url_for('import_now')}" style="margin-top:1rem">
        <button type="submit">🔁 Importer maintenant (RSS + Scraping)</button>
      </form>
      <p><small>Import automatique toutes les {IMPORT_INTERVAL_MIN} min.</small></p>
    </article>

    <h4>Brouillons</h4>{''.join(card(r) for r in drafts) or "<p>Aucun brouillon.</p>"}
    <h4>Planifiés</h4>{''.join(card(r) for r in scheduled) or "<p>Aucun article planifié.</p>"}
    <h4>Publiés</h4>{''.join(card(r, True) for r in pubs) or "<p>Rien de publié.</p>"}
    <p>Flux public : <code>{request.url_root}rss.xml</code></p>
    """
    return page(body, "Admin")

@app.post("/save-settings")
def save_settings():
    if not session.get("ok"): return redirect(url_for("admin"))
    set_setting("openai_key", request.form.get("openai_key","").strip())
    set_setting("openai_model", request.form.get("openai_model","").strip())
    set_setting("feeds", request.form.get("feeds",""))
    set_setting("default_image_url", request.form.get("default_image_url","").strip())
    scrapers_txt = request.form.get("scrapers_json","").strip()
    try:
        _json.loads(scrapers_txt or "[]")
        set_setting("scrapers_json", scrapers_txt or "[]")
        flash("Paramètres enregistrés.")
    except Exception as e:
        flash(f"Config sites JSON invalide : {e}")
    return redirect(url_for("admin"))

@app.post("/import-now")
def import_now():
    if not session.get("ok"): return redirect(url_for("admin"))

    feeds_txt = get_setting("feeds", "\n".join(DEFAULT_FEEDS))
    feed_list = [u.strip() for u in feeds_txt.splitlines() if u.strip()]

    try:
        scrapers_cfg = _json.loads(get_setting("scrapers_json", _json.dumps(DEFAULT_SCRAPERS)))
        if not isinstance(scrapers_cfg, list):
            raise ValueError("Le JSON de scrapers doit être une liste []")
    except Exception as e:
        flash(f"Erreur d’import (sites) : Config sites JSON invalide: {e}")
        return redirect(url_for("admin"))

    def worker(feeds, scrapers):
        try:
            c1, s1 = scrape_rss_once(feeds)
            c2, s2 = scrape_index_once(scrapers)
            set_setting("last_import_result", f"OK : {c1+c2} nouveaux, {s1+s2} ignorés.")
        except Exception as e:
            msg = f"Erreur : {e}\n{traceback.format_exc()}"
            print("[IMPORT WORKER] fatal:", msg)
            set_setting("last_import_result", msg)

    threading.Thread(target=worker, args=(feed_list, scrapers_cfg), daemon=True).start()
    flash("Import lancé en arrière-plan. Recharge l’admin dans ~1 minute pour voir le résultat.")
    return redirect(url_for("admin"))

@app.get("/import-now")
def import_now_get():
    flash("Utilise le bouton « Importer maintenant » dans l’admin.")
    return redirect(url_for("admin"))

@app.post("/save/<int:post_id>")
def save(post_id):
    if not session.get("ok"): return redirect(url_for("admin"))
    action     = request.form.get("action","save")
    title      = strip_tags(request.form.get("title","").strip())
    body       = strip_tags(request.form.get("body","").strip())
    publish_at = request.form.get("publish_at","").strip()

    if body:
        body = ensure_min_words(body, body, TARGET_MIN_WORDS)

    con = db()
    try:
        con.execute("UPDATE posts SET title=?, body=?, updated_at=? WHERE id=?",
                    (normalize_title(title), body, datetime.now(timezone.utc).isoformat(timespec="minutes"), post_id))
        if action == "publish":
            row = con.execute("SELECT image_url FROM posts WHERE id=?", (post_id,)).fetchone()
            if REQUIRE_IMAGE and (not row or not row["image_url"]):
                flash("Publication refusée : une image est obligatoire.")
            else:
                con.execute("UPDATE posts SET status='published', publish_at=NULL WHERE id=?", (post_id,))
                flash("Publié immédiatement.")
        elif action == "unpublish":
            con.execute("UPDATE posts SET status='draft', publish_at=NULL WHERE id=?", (post_id,))
            flash("Dépublié.")
        elif action == "schedule":
            if not publish_at:
                flash("Choisis une date/heure (UTC) pour planifier.")
            else:
                iso_utc = publish_at if len(publish_at) == 16 else publish_at[:16]
                iso_utc += ":00+00:00" if len(iso_utc) == 16 else ""
                con.execute("UPDATE posts SET status='scheduled', publish_at=? WHERE id=?", (iso_utc, post_id))
                flash(f"Planifié pour {iso_utc} (UTC).")
        elif action == "delete":
            con.execute("DELETE FROM posts WHERE id=?", (post_id,))
            flash("Supprimé.")
        else:
            flash("Enregistré.")
        con.commit()
    finally:
        con.close()
    return redirect(url_for("admin"))

@app.get("/logout")
def logout():
    session.clear(); return redirect(url_for("home"))

@app.get("/console")
def alias_console():
    return redirect(url_for("admin"))

# --------- boot ---------
init_db()
bootstrap_openai_key()  # Clé OpenAI bootstrapée une fois depuis l'ENV vers la DB
threading.Thread(target=publish_due_loop, daemon=True).start()
threading.Thread(target=import_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
