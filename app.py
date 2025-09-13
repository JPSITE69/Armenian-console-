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

DEFAULT_FEEDS = [
    "https://www.civilnet.am/news/feed/",
    "https://armenpress.am/rss/",
    "https://news.am/eng/rss/",
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
        status TEXT DEFAULT 'draft',
        created_at TEXT,
        updated_at TEXT,
        publish_at TEXT,
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

# ================== UTILS TEXTE ==================
TAG_RE = re.compile(r"<[^>]+>")
FR_TOKENS = set(" le la les un une des du de au aux et en sur pour par avec dans que qui ne pas est été sont était selon afin aussi plus leur lui ses ces cette ce cela donc ainsi tandis alors contre entre vers depuis sans sous après avant comme lorsque tandis que où dont même".split())

def strip_tags(s: str) -> str:
    return TAG_RE.sub("", s or "")

def looks_french(text: str) -> bool:
    if not text: return False
    t = text.lower()
    words = re.findall(r"[a-zàâäéèêëïîôöùûüç'-]+", t)
    if not words: return False
    hits = sum(1 for w in words[:80] if w in FR_TOKENS)
    return hits >= 5

def active_openai():
    key = get_setting("openai_key", "").strip() or ENV_OPENAI_KEY
    model = get_setting("openai_model", "").strip() or ENV_OPENAI_MODEL
    return (key, model)

def _title_from_text_fallback(fr_text: str) -> str:
    t = (fr_text or "").strip()
    if not t:
        return "Actualité"
    words = t.split()
    base = " ".join(words[:10]).strip().rstrip(".,;:!?")
    base = base[:80]
    return base[:1].upper() + base[1:]

# ================== AI ==================
def rewrite_article_fr(title_src: str, raw_text: str):
    if not raw_text:
        return (title_src or "Actualité", "", False)

    key, model = active_openai()
    clean_input = strip_tags(raw_text)

    def call_openai():
        prompt = (
            "Tu es un journaliste francophone. "
            "Traduis/réécris en FRANÇAIS le Titre et le Corps de l'article ci-dessous. "
            "Ton neutre et factuel, 150–220 mots pour le corps. "
            "RENVOIE STRICTEMENT du JSON avec les clés 'title' et 'body'. "
            "Le 'body' doit être du TEXTE BRUT (PAS de balises HTML) et DOIT se terminer par: - Arménie Info.\n\n"
            f"Titre (source): {title_src}\n"
            f"Texte (source): {clean_input}"
        )
        r = requests.post("https://api.openai.com/v1/chat/completions",
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json={
                              "model": model,
                              "temperature": 0.2,
                              "messages": [
                                  {"role": "system", "content": "Tu écris en français clair et concis."},
                                  {"role": "user", "content": prompt}
                              ]
                          }, timeout=60)
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
            body_fr = strip_tags(clean_input)
            body_fr = " ".join(body_fr.split()[:200]).strip()
        if not body_fr.endswith("- Arménie Info"):
            body_fr += "\n\n- Arménie Info"
        if not title_fr:
            title_fr = _title_from_text_fallback(body_fr)
        return title_fr, body_fr

    if key:
        try:
            t1, b1 = call_openai()
            if looks_french(b1) and looks_french(t1):
                return (t1, b1, True)
        except Exception as e:
            print(f"[AI] rewrite_article_fr failed: {e}")

    fr_body = strip_tags(raw_text)
    fr_body = " ".join(fr_body.split()[:200]).strip()
    if not fr_body.endswith("- Arménie Info"):
        fr_body += "\n\n- Arménie Info"
    fr_body += "\n(à traduire)"
    fr_title = _title_from_text_fallback(fr_body)
    return (fr_title, fr_body, False)

# ================== IMAGES ==================
def download_image(url):
    default_path = "/static/images/default.jpg"
    if not url:
        return default_path, None
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.content
        sha1 = hashlib.sha1(data).hexdigest()
        os.makedirs("static/images", exist_ok=True)
        path = f"static/images/{sha1}.jpg"
        if not os.path.exists(path):
            with open(path, "wb") as f: f.write(data)
        return "/" + path, sha1
    except Exception as e:
        print(f"[IMG] download failed for {url}: {e}")
        return default_path, None

# (⚠️ le reste du code = identique à ton app.py d’origine :
# scraping, admin, routes, scheduler, etc.)
