# app.py — Console Arménienne
# Auto-import (RSS + scrapers) • Auto-publication • Photo obligatoire (fallback + conversion JPEG)
# Réécriture FR (120–800 mots) • Titres régénérés à partir de TOUT le contenu (anti-chiffres/parasites + secours IA)
# Nettoyage source & corps • Signature obligatoire : - LesArmeniens.com
# Clé OpenAI saisie une fois (ENV → DB) • Cron HTTP de secours

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
AUTO_PUBLISH = True                    # Publie immédiatement tout nouvel article
REQUIRE_IMAGE = True                   # Photo obligatoire
IMPORT_INTERVAL_MIN = int(os.environ.get("IMPORT_INTERVAL_MIN", "10"))  # boucle auto (minutes)
MIN_SOURCE_CHARS = 40                  # longueur min (caractères) avant réécriture

# Longueurs cibles (mots)
TARGET_MIN_WORDS = int(os.environ.get("TARGET_MIN_WORDS", "120"))
TARGET_MAX_WORDS = int(os.environ.get("TARGET_MAX_WORDS", "800"))

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

# --- Bootstrap / cache OpenAI (clé une fois) ---
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

# ------- anti-titres numériques/parasites -------
ALPHA_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]")
BANNED_TITLE_WORDS = {
    "lesarmeniens", "armenien", "armeniens", "com", "published", "publie", "publication",
    "http", "https", "www", "tweet", "instagram", "facebook", "snapback"
}

def is_alpha_word(w: str) -> bool:
    return bool(ALPHA_RE.search(w or ""))

def digit_ratio(s: str) -> float:
    if not s: return 0.0
    d = sum(ch.isdigit() for ch in s)
    return d / max(1, len(s))

def sanitize_tokens(tokens: list[str]) -> list[str]:
    out = []
    for w in (tokens or []):
        if not w or len(w) < 2:
            continue
        if w in STOP_FR:
            continue
        wl = w.lower()
        if wl in BANNED_TITLE_WORDS:
            continue
        if not is_alpha_word(wl):
            continue
        if wl.isdigit():
            continue
        if digit_ratio(wl) > 0.4:
            continue
        out.append(wl)
    return out

def is_bad_title(t: str, body: str) -> bool:
    if not t:
        return True
    t_clean = re.sub(r"[^\w\sÀ-ÖØ-öø-ÿ’'-]", " ", t)
    toks = [w for w in _tokenize_words(t_clean) if w not in STOP_FR]
    alpha = [w for w in toks if is_alpha_word(w)]
    if len(alpha) < 4:
        return True
    if digit_ratio(t_clean) > 0.25:
        return True
    if len(set(alpha)) <= 2:
        return True
    if re.search(r"(?i)\blesarmeniens\s*\.?\s*com\b", t_clean):
        return True
    if re.fullmatch(r"(?i)(?:published|publié|publication)", t_clean.strip()):
        return True
    return False

def normalize_title(src: str) -> str:
    if not src:
        return "Actualité"
    t = src.strip()
    t = SITE_SUFFIX_PAT.sub("", t)
    t = re.sub(r"(?i)\blesarmeniens\s*\.?\s*com\b", "", t)
    t = re.sub(r"(?i)\b(published|publié|publication)\b", "", t)
    t = re.sub(r"\bhttps?://\S+\b", "", t)
    t = _clean_punct(t)
    t = re.sub(r"[^\w\sÀ-ÖØ-öø-ÿ’'\"!?.,:;()-–—]", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    t = _sentence_case(t)
    if len(t) > TITLE_MAX:
        t = _smart_truncate(t, TITLE_MAX)
    t = t.rstrip()
    if t.endswith(("—", "–", "-", ":", ";", ",")):
        t = t[:-1].rstrip()
    if is_bad_title(t, ""):
        return "Actualité"
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

# ------- Nettoyage (source + corps) -------
def clean_source_html(html: str) -> str:
    """Supprime blocs parasites avant extraction: partages, related, tags, nav, footer, scripts…"""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup.find_all(["aside","nav","footer","form","script","style"]):
            tag.decompose()
        junk_selectors = [
            "[class*='share']", "[class*='sharing']", "[class*='social']",
            "[class*='tags']", "[class*='related']", "[class*='recommend']",
            "[class*='newsletter']", "[class*='subscribe']", "[class*='cookie']",
            "[class*='promo']", "[class*='advert']", "[class*='banner']",
            "[id*='share']", "[id*='social']", "[id*='related']",
            ".td-post-author-name", ".td-post-source-tags",
        ]
        for sel in junk_selectors:
            for n in soup.select(sel):
                n.decompose()
        for figcap in soup.find_all("figcaption"):
            figcap.decompose()
        return str(soup)
    except Exception:
        return html or ""

CLEAN_LINES_PAT = re.compile(
    r"""(?imx)
    ^\s*(?:Lisez\s+aussi|Lire\s+aussi|Read\s+also|Related\s+articles?|More\s+on|Voir\s+aussi)\b.*$|
    ^\s*(?:Photo|Crédit|Credit|Copyright)\s*:.*$|
    ^\s*(?:Avec\s+AFP|Sources?\s*:).*$|
    ^\s*(?:Suivez-nous|Follow\s+us|Abonnez-vous).*$|
    ^\s*(?:©|Tous\s+droits\s+réservés).*$|
    ^\s*Publié\s+le\s+.*$|
    ^\s*Auteur\s*:.*$
    """,
    re.UNICODE,
)

def clean_body_text(body: str) -> str:
    """Nettoie le texte final: supprime lignes inutiles, espaces multiples, doublons, assure signature."""
    if not body:
        return body
    sig = "- LesArmeniens.com"
    b = body.strip()
    had_sig = b.endswith(sig)
    if had_sig:
        b = b[: -len(sig)].rstrip()
    lines = [ln for ln in b.splitlines() if not CLEAN_LINES_PAT.match(ln or "")]
    b = "\n".join(lines)
    b = re.sub(r"[ \t]+", " ", b)
    b = re.sub(r"\n{3,}", "\n\n", b).strip()
    uniq = []
    for para in b.split("\n\n"):
        p = para.strip()
        if not uniq or uniq[-1] != p:
            uniq.append(p)
    b = "\n\n".join(uniq).strip()
    b = ensure_signature(b)
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
    """Si body_text < min_words, complète à partir de la source (nettoyée), borne à TARGET_MAX_WORDS, garde signature."""
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
        take = min(len(extra_words), int(need * 1.3))
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
    """Titre à partir de tout le contenu (tokens filtrés + n-grammes), en bannissant chiffres/parasites."""
    txt = strip_tags(body_text or "").strip()
    if not txt:
        return "Actualité"
    raw_tokens = _tokenize_words(txt)
    tokens = sanitize_tokens(raw_tokens)
    if not tokens:
        return normalize_title(_title_from_text_fallback(txt))

    freq = {}
    for w in tokens:
        freq[w] = freq.get(w, 0) + 1
    w_weight = {w: freq[w] * (1.0 + min(len(w), 14) / 6.0) for w in freq}

    def ngrams(words, n):
        return [" ".join(words[i:i+n]) for i in range(len(words)-n+1)]

    def valid_ng(ng: str) -> bool:
        parts = ng.split()
        if sum(1 for p in parts if is_alpha_word(p)) < max(1, len(parts)//2):
            return False
        if digit_ratio(ng) > 0.25:
            return False
        return True

    bigrams  = [g for g in ngrams(tokens, 2) if valid_ng(g)]
    trigrams = [g for g in ngrams(tokens, 3) if valid_ng(g)]

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

    chosen, used = [], set()
    def push_phrase(phrase):
        for w in phrase.split():
            if w not in used:
                chosen.append(w); used.add(w)

    if top_tri:
        push_phrase(top_tri[0][0])
    elif top_bi:
        push_phrase(top_bi[0][0])
    else:
        for w, _ in sorted(w_weight.items(), key=lambda kv: kv[1], reverse=True)[:max_words]:
            push_phrase(w)

    if len(chosen) < min_words and top_bi:
        for phrase, _ in top_bi:
            if len(chosen) >= max_words: break
            if digit_ratio(phrase) > 0.25: continue
            p_words = phrase.split()
            overlap = sum(1 for w in p_words if w in used)
            if overlap <= len(p_words) // 2:
                push_phrase(phrase)

    if len(chosen) < min_words:
        for w, _ in sorted(w_weight.items(), key=lambda kv: kv[1], reverse=True):
            if len(chosen) >= min(min_words, max_words): break
            if w not in used: push_phrase(w)

    headline = " ".join(chosen[:max_words])
    headline = normalize_title(headline)
    if is_bad_title(headline, body_text):
        for phrase, _ in top_tri + top_bi:
            cand = normalize_title(phrase)
            if not is_bad_title(cand, body_text):
                return cand
        top_words = [w for w, _ in sorted(w_weight.items(), key=lambda kv: kv[1], reverse=True)[:12]]
        cand = normalize_title(" ".join(top_words[:max_words]))
        return cand if not is_bad_title(cand, body_text) else "Actualité"
    return headline

# ===== Secours IA pour le titre (optionnel mais utile) =====
def ai_derive_title_from_body(body_text: str) -> str | None:
    """Demande à l'IA un titre 6–12 mots, sans média/URL/parasites."""
    key, model = active_openai()
    if not key:
        return None
    prompt = (
        "Donne UNIQUEMENT un titre journalistique en FRANÇAIS, 6–12 mots, "
        "basé sur le texte ci-dessous. Interdictions absolues: nom de média, URL, "
        "mots 'published/publié', signature, émojis. Pas de point final.\n\n"
        f"Texte:\n{strip_tags(body_text)[:4000]}"
    )
    try:
        r = requests.post("https://api.openai.com/v1/chat/completions",
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json={"model": model or "gpt-4o-mini","temperature":0.2,
                                "messages":[{"role":"user","content":prompt}]},
                          timeout=30)
        j = r.json()
        out = (j.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        return normalize_title(out)
    except Exception as e:
        print("[AI] ai_derive_title_from_body failed:", e)
        return None

# ================== RÉÉCRITURE ==================
def rewrite_article_fr(title_src: str, raw_text: str):
    """Retourne (title_fr, body_fr, sure_fr).
       FR + signature LesArmeniens.com + normalisation titre + 120–800 mots + recalcul titre (contenu global) + nettoyage."""
    if not raw_text:
        return (normalize_title(title_src or "Actualité"), "", False)

    key, model = active_openai()
    clean_input = strip_tags(raw_text)

    def call_openai():
        prompt = (
            "Tu es un journaliste francophone.\n"
            "Réécris en FRANÇAIS le TITRE et le CORPS de l'article ci-dessous, "
            "sans inventer de faits, en conservant les informations factuelles (noms, lieux, chiffres, citations importantes).\n"
            f"Longueur du corps : entre {TARGET_MIN_WORDS} et {TARGET_MAX_WORDS} mots (pas moins, pas plus que ±10%).\n"
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

        # Garanties longueur + signature + nettoyage
        body_fr = ensure_min_words(body_fr, clean_input, TARGET_MIN_WORDS)
        body_fr = clean_body_text(body_fr)

        # Titre : si manquant, court, parasite ou peu pertinent -> généré depuis le contenu global
        needs_derive = (
            not title_fr
            or len(_tokenize_words(title_fr)) < 4
            or title_relevance_score(title_fr, body_fr) < 0.30
            or is_bad_title(title_fr, body_fr)
        )
        if needs_derive:
            title_fr = derive_title_from_body(body_fr)

        title_fr = normalize_title(title_fr)

        # Sécurité finale: si encore mauvais → secours IA → sinon derive()
        if is_bad_title(title_fr, body_fr):
            t_ai = ai_derive_title_from_body(body_fr)
            if t_ai and not is_bad_title(t_ai, body_fr):
                title_fr = t_ai
            else:
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
            b2  = clean_body_text(b2)
            if title_relevance_score(tfb, b2) < 0.30 or len(_tokenize_words(tfb)) < 4 or is_bad_title(tfb, b2):
                alt = derive_title_from_body(b2)
                tfb = alt if not is_bad_title(alt, b2) else (ai_derive_title_from_body(b2) or "Actualité")
            return (normalize_title(tfb), b2, False)
        except Exception as e:
            print(f"[AI] rewrite_article_fr failed: {e}")

    # Fallback local (sans OpenAI)
    fr_body = " ".join(strip_tags(raw_text).split())
    if _word_count(fr_body) > TARGET_MAX_WORDS:
        fr_body = " ".join(_tokenize_words(fr_body)[:TARGET_MAX_WORDS])
    fr_body = ensure_min_words(fr_body, raw_text, TARGET_MIN_WORDS)
    fr_body = clean_body_text(fr_body)
    fr_title = derive_title_from_body(fr_body)
    if is_bad_title(fr_title, fr_body):
        fr_title = _title_from_text_fallback(fr_body)
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
        # 2bis) <link rel="image_src"> / srcset
        s2 = BeautifulSoup(page_html, "html.parser")
        link_img = s2.find("link", rel=lambda v: v and "image_src" in v)
        if link_img and link_img.get("href"):
            return urljoin(page_url or "", link_img["href"])
        imgtag = s2.find("img", attrs={"srcset": True})
        if imgtag:
            srcset = imgtag.get("srcset","").split(",")[0].strip().split(" ")[0]
            if srcset:
                return urljoin(page_url or "", srcset)
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
    # 1) si l'article n'a pas d'image → tente l'image par défaut
    if not img_url:
        default_img = get_setting("default_image_url", "").strip()
        if default_img:
            img_url = default_img
    # 2) download/conversion ; si ça échoue → retente avec l'image par défaut
    local_path, sha1 = download_image(img_url) if img_url else (None, None)
    if (not local_path or not sha1):
        default_img = get_setting("default_image_url", "").strip()
        if default_img and (not img_url or img_url != default_img):
            local_path, sha1 = download_image(default_img)
    # 3) exigence finale
    if REQUIRE_IMAGE and (not local_path or not sha1):
        print("[POST] rejet: aucune image utilisable (article + défaut)")
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
                        print("[RSS] skip: link vide/doublon", link)
                        skipped += 1; continue
                    title_src = normalize_title((e.get("title") or "(Sans titre)").
