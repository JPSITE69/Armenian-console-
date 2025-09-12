# app.py
from flask import Flask, request, redirect, url_for, Response, render_template_string, session, flash
import os, sqlite3, hashlib, io, re, json, traceback
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
import requests, feedparser
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError
from apscheduler.schedulers.background import BackgroundScheduler

# -------------------- CONFIG --------------------
APP_NAME   = "Console Arménienne"
ADMIN_PASS = os.environ.get("ADMIN_PASS", "armenie")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")
DB_PATH    = "site.db"

DEFAULT_FEEDS = [
    "https://www.civilnet.am/news/feed/",
    "https://armenpress.am/rss/",
    "https://news.am/eng/rss/",
    "https://factor.am/feed",
    "https://hetq.am/hy/rss",
    "https://armenpress.am/hy/rss/articles",
    "https://www.azatutyun.am/rssfeeds"
]
DEFAULT_MODEL = "gpt-4o-mini"

# seuil images (permissif)
IMG_MIN_W, IMG_MIN_H = 80, 80

app = Flask(__name__)
app.secret_key = SECRET_KEY

# -------------------- DB --------------------
def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS posts(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT,
      body  TEXT,
      status TEXT DEFAULT 'draft',  -- draft | published
      created_at TEXT,
      updated_at TEXT,
      publish_at TEXT,              -- "YYYY-MM-DDTHH:MM"
      image_url TEXT,               -- chemin local /static/... ou URL absolue http(s)
      image_sha1 TEXT,
      orig_link TEXT UNIQUE
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS settings(
      key TEXT PRIMARY KEY,
      value TEXT
    )""")
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
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        con.commit()
    finally:
        con.close()

# -------------------- UTILS --------------------
TAG_RE = re.compile(r"<[^>]+>")

def strip_tags(s: str) -> str:
    return TAG_RE.sub("", s or "")

def clean_title(t: str) -> str:
    t = strip_tags(t or "").strip()
    t = re.sub(r'^(titre|title|headline)\s*[:\-–—]\s*', "", t, flags=re.I)
    t = t.strip('«»"“”\'` ').strip()
    if not t:
        t = "Actualité"
    return t[:1].upper() + t[1:]

SIGN_REGEX = re.compile(r'(\s*[-–—]\s*Arménie\s+Info\s*)+$', re.I)

def ensure_signature(text: str) -> str:
    t = strip_tags(text or "").rstrip()
    t = SIGN_REGEX.sub("", t).rstrip()
    return (t + "\n\n- Arménie Info").strip()

def http_get(url, timeout=25, headers=None):
    h = {
        "User-Agent": "Mozilla/5.0 (compatible; ArmInfo/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr,en;q=0.8",
    }
    if headers: h.update(headers)
    r = requests.get(url, timeout=timeout, allow_redirects=True, headers=h)
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r.text

# --- sauver bytes -> image locale validée (taille mini) ---
def _save_bytes_to_image(data: bytes):
    sha1 = hashlib.sha1(data).hexdigest()
    try:
        im = Image.open(io.BytesIO(data))
        im.verify()
        im = Image.open(io.BytesIO(data))
        w, h = im.size
        if w < IMG_MIN_W or h < IMG_MIN_H:
            return None, None
    except (UnidentifiedImageError, Exception):
        return None, None
    os.makedirs("static/images", exist_ok=True)
    path = f"static/images/{sha1}.jpg"
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return "/" + path, sha1

def download_image(url):
    if not url:
        return None, None
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent":"Mozilla/5.0"})
        r.raise_for_status()
        return _save_bytes_to_image(r.content)
    except Exception:
        return None, None

# ---- image par défaut (ta photo) ----
def get_default_image():
    p = get_setting("default_image_path", "").strip()
    s = get_setting("default_image_sha1", "").strip()
    return (p or None, s or None)

def set_default_image_from_bytes(data: bytes):
    p, s = _save_bytes_to_image(data)
    if p and s:
        set_setting("default_image_path", p)
        set_setting("default_image_sha1", s)
    return p, s

def set_default_image_from_url(url: str):
    p, s = download_image(url)
    if p and s:
        set_setting("default_image_path", p)
        set_setting("default_image_sha1", s)
    return p, s

# -------------------- IMAGES : collecte permissive --------------------
def _jsonld_image(soup, base):
    for sc in soup.find_all("script", type=lambda v: v and "ld+json" in v):
        try:
            data = json.loads(sc.string or "{}")
        except Exception:
            continue
        def pick(obj):
            if isinstance(obj, str): return urljoin(base, obj)
            if isinstance(obj, list) and obj: return pick(obj[0])
            if isinstance(obj, dict):
                return pick(obj.get("image") or obj.get("thumbnailUrl") or obj.get("url") or obj.get("@id") or obj.get("contentUrl"))
            return None
        if isinstance(data, list):
            for item in data:
                u = pick(item)
                if u: return u
        elif isinstance(data, dict):
            u = pick(data)
            if u: return u
    return None

def _extract_srcset(tag, base):
    srcset = tag.get("srcset") or tag.get("data-srcset")
    if not srcset:
        return None
    best_url, best_w = None, -1
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        u = urljoin(base, bits[0])
        w = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                w = int(bits[1][:-1])
            except:
                w = 0
        if w > best_w:
            best_w, best_url = w, u
    return best_url

def image_candidates_from_html(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    cands = []

    # 1) métadonnées
    for sel, attr in [
        ("meta[property='og:image:secure_url']", "content"),
        ("meta[property='og:image']", "content"),
        ("meta[name='twitter:image']", "content"),
        ("meta[itemprop='image']", "content"),
        ("link[rel='image_src']", "href"),
    ]:
        for m in soup.select(sel):
            if m.get(attr): cands.append(urljoin(base_url, m[attr]))

    # 2) JSON-LD
    j = _jsonld_image(soup, base_url or "")
    if j: cands.append(j)

    # 3) images dans le contenu
    roots = soup.select(
        "article, .entry-content, .post-content, .article-content, "
        ".content-article, .article-body, #article-body, .single-content, .content"
    )
    if not roots:
        roots = [soup]
    for root in roots:
        for imgtag in root.find_all(["img","amp-img","picture","figure"]):
            if imgtag.name in ("img","amp-img"):
                u = imgtag.get("src") or imgtag.get("data-src") or imgtag.get("data-original")
                if not u:
                    u = _extract_srcset(imgtag, base_url)
                if u:
                    cands.append(urljoin(base_url, u))
            else:
                im = imgtag.find("img")
                if im:
                    u = im.get("src") or im.get("data-src") or im.get("data-original") or _extract_srcset(im, base_url)
                    if u: cands.append(urljoin(base_url, u))

    # dédup
    seen, uniq = set(), []
    for u in cands:
        if u not in seen:
            seen.add
