import os, sqlite3, hashlib, io, re, traceback, json
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests, feedparser
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError

# (optionnel) OpenAI v1.x — si non présent, l’app tourne sans réécriture IA
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# ===========================
# CONFIG (env + valeurs par défaut)
# ===========================
APP_NAME         = "Console Arménienne"
ADMIN_PASS       = os.environ.get("ADMIN_PASS", "armenie")
SECRET_KEY       = os.environ.get("SECRET_KEY", "change-moi")

DEFAULT_MODEL    = "gpt-4o-mini"
IMG_MIN_W, IMG_MIN_H = 200, 120   # filtre mini pour éviter les micro-vignettes

DB_PATH = os.environ.get("DB_PATH", "/var/data/armenien_console.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# ===========================
# Flask
# ===========================
from flask import (
    Flask, request, redirect, url_for, Response, render_template_string,
    session, flash
)
app = Flask(__name__)
app.secret_key = SECRET_KEY

# ===========================
# DB helpers
# ===========================
def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db(); c = con.cursor()
    c.execute("""
      CREATE TABLE IF NOT EXISTS posts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guid TEXT UNIQUE,
        url TEXT,
        title TEXT,
        body TEXT,
        image_url TEXT,
        status TEXT DEFAULT 'draft',
        created_at TEXT,
        published_at TEXT
      )""")
    c.execute("""
      CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT
      )""")
    # valeurs par défaut (si absentes)
    defaults = {
        "openai_api_key": "",
        "openai_model": DEFAULT_MODEL,
        "auto_minutes": "0",
        "rss_sources": "\n".join([
            "https://www.civilnet.am/news/feed/",
            "https://armenpress.am/rss/",
            "https://news.am/eng/rss/",
            "https://factor.am/feed",
            "https://hetq.am/hy/rss",
            "https://armenpress.am/hy/rss/articles",
            "https://www.azatutyun.am/rssfeeds",
        ]),
        "fallback_image": ""   # laisse vide si tu ne veux pas d’image par défaut
    }
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    con.commit(); con.close()

init_db()

def get_setting(key, default=""):
    con = db(); c = con.cursor()
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    con.close()
    return r["value"] if r else default

def set_setting(key, value):
    con = db(); c = con.cursor()
    c.execute("REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
    con.commit(); con.close()

# ===========================
# Utils
# ===========================
USER_AGENT = {"User-Agent": "Mozilla/5.0 (news console)"}

def clean_html_to_text(html):
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    # supprime doubles espaces
    return re.sub(r"\s{2,}", " ", text)

def unique_key(*parts):
    m = hashlib.sha256()
    for p in parts:
        if p:
            m.update(p.encode("utf-8", errors="ignore"))
    return m.hexdigest()

def fetch_url(url, timeout=12):
    try:
        r = requests.get(url, headers=USER_AGENT, timeout=timeout, allow_redirects=True)
        if r.ok:
            return r.text, r.url
    except Exception:
        pass
    return None, url

def absolute_url(base, src):
    try:
        return urljoin(base, src)
    except Exception:
        return src

def try_image_size(url, min_w=IMG_MIN_W, min_h=IMG_MIN_H):
    """Retourne True si l’image semble assez grande. On tente via PIL en streaming."""
    try:
        r = requests.get(url, headers=USER_AGENT, timeout=12, stream=True)
        r.raise_for_status()
        # charge un petit bout
        content = r.raw.read(200000)  # 200 Ko
        img = Image.open(io.BytesIO(content))
        w, h = img.size
        return (w >= min_w and h >= min_h)
    except (UnidentifiedImageError, requests.RequestException, Exception):
        return False

def extract_best_image(entry, article_html, article_url):
    # 1) RSS enclosure/media
    if "media_content" in entry:
        for m in entry.media_content:
            u = m.get("url")
            if u and try_image_size(u):
                return u
    if "media_thumbnail" in entry:
        for m in entry.media_thumbnail:
            u = m.get("url")
            if u and try_image_size(u):
                return u
    if "links" in entry:
        for L in entry.links:
            if L.get("rel") == "enclosure" and "image" in (L.get("type") or ""):
                u = L.get("href")
                if u and try_image_size(u):
                    return u

    # 2) OpenGraph/Twitter sur la page
    if article_html:
        s = BeautifulSoup(article_html, "html.parser")
        for prop in ["og:image", "twitter:image", "image", "og:image:url"]:
            tag = s.find("meta", attrs={"property": prop}) or s.find("meta", attrs={"name": prop})
            if tag:
                u = tag.get("content")
                if u:
                    u = absolute_url(article_url, u)
                    if try_image_size(u):
                        return u

        # 3) <img> dans l’article
        for img in s.find_all("img"):
            u = img.get("src") or img.get("data-src") or img.get("data-original")
            if not u:
                continue
            u = absolute_url(article_url, u)
            if try_image_size(u):
                return u

    # 4) fallback paramétrable
    fb = get_setting("fallback_image", "")
    return fb or ""

# ---------------------------
# OpenAI helpers (réécriture FR)
# ---------------------------
def build_openai():
    api_key = get_setting("openai_api_key", "").strip()
    if not api_key or OpenAI is None:
        return None, None
    model = get_setting("openai_model", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    client = OpenAI(api_key=api_key)
    return client, model

PROMPT = (
    "Réécris ce texte en français clair et concis (3 à 6 phrases), "
    "sans balises HTML, sans hashtags, sans émojis, "
    "style neutre de média d’actualité français. "
    "Ne répète pas le titre. Ne signe rien. "
    "Texte : «{body}»"
)

def rewrite_french(title, body):
    """Retourne (title_fr, body_fr). Si pas d’API, on nettoie simplement le texte."""
    clean_title = clean_html_to_text(title or "").strip()
    clean_body  = clean_html_to_text(body or "").strip()
    client, model = build_openai()
    if not client:
        # fallback : on garde le titre, on nettoie le corps
        return clean_title, clean_body

    try:
        msg = PROMPT.format(body=clean_body)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": msg}],
            temperature=0.3,
            max_tokens=500,
        )
        new_body = (resp.choices[0].message.content or "").strip()
        # On renvoie aussi le titre passé (on peut garder le titre propre)
        return clean_title, new_body
    except Exception:
        return clean_title, clean_body

# ===========================
# SCRAPE + IMPORT
# ===========================
def import_once():
    sources = [s.strip() for s in get_setting("rss_sources", "").splitlines() if s.strip()]
    if not sources:
        return (0, 0)

    added = 0
    ignored = 0
    for feed_url in sources:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception:
            continue

        for e in parsed.entries:
            link  = (e.get("link") or "").strip()
            guid  = e.get("id") or e.get("guid") or link or e.get("title","")
            title = e.get("title") or ""
            # HTML brut potentiel dans content/summary
            body_html = ""
            if "content" in e and e.content:
                body_html = e.content[0].value or ""
            elif "summary" in e:
                body_html = e.summary or ""

            # Récup page article (pour images + texte plus propre si besoin)
            page_html, real_url = fetch_url(link) if link else (None, link)

            # Extraction image
            image_url = extract_best_image(e, page_html, real_url)

            # Réécriture FR (titre nettoyé + corps)
            title_fr, body_fr = rewrite_french(title, body_html)

            # Ajout signature une seule fois
            if not body_fr.rstrip().endswith("Arménie Info"):
                body_fr = f"{body_fr}\n\n- Arménie Info"

            # dédoublon
            hash_id = unique_key(real_url or link, title_fr)
            con = db(); c = con.cursor()
            ex = c.execute("SELECT id FROM posts WHERE guid=?", (hash_id,)).fetchone()
            if ex:
                con.close()
                ignored += 1
                continue

            c.execute("""INSERT INTO posts(guid,url,title,body,image_url,status,created_at)
                         VALUES(?,?,?,?,?,'draft',?)""",
                      (hash_id, real_url or link, title_fr, body_fr, image_url,
                       datetime.now(timezone.utc).isoformat()))
            con.commit(); con.close()
            added += 1

    return (added, ignored)

# ===========================
# VUES
# ===========================
BASE = """
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<link rel="icon" href="data:,">
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto;max-width:900px;margin:24px auto;padding:0 12px;color:#eaeaea;background:#121212}
header a{color:#9bd}
a{color:#8ad}
.card{background:#1b1b1b;border:1px solid #2b2b2b;border-radius:10px;padding:14px;margin:12px 0}
label{display:block;margin:.6rem 0 .2rem;color:#bbb}
input,textarea{width:100%;background:#111;border:1px solid #333;color:#eee;border-radius:8px;padding:.6rem}
button{background:#2e7d32;color:#fff;border:0;padding:.6rem 1rem;border-radius:8px;cursor:pointer}
.btn{display:inline-block;margin-right:.5rem}
nav a{margin-right:12px}
small.muted{color:#888}
img.cover{max-width:100%;height:auto;border-radius:8px;border:1px solid #333;margin:.5rem 0}
</style>
</head>
<body>
<header class="card">
  <h2>{{ app_name }}</h2>
  <nav>
    <a href="{{ url_for('home') }}">Accueil</a>
    <a href="{{ url_for('rss') }}">RSS</a>
    {% if session.get('ok') %}
      <a href="{{ url_for('admin') }}">Admin</a>
      <a href="{{ url_for('logout') }}">Déconnexion</a>
    {% else %}
      <a href="{{ url_for('admin') }}">Se connecter</a>
    {% endif %}
  </nav>
</header>

<div>
{{ body|safe }}
</div>
</body></html>
"""

def render(page_title, body_html):
    return render_template_string(BASE, title=page_title, body=body_html, app_name=APP_NAME)

@app.route("/")
def home():
    return render("Console Arménienne", "<p>Serveur Flask opérationnel ✅</p>")

@app.route("/health")
def health():
    try:
        con = db()
        con.execute("SELECT 1")
        con.close()
        return "OK", 200
    except Exception:
        return "DB ERROR", 500

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if not session.get("ok"):
        if request.method == "POST":
            if request.form.get("password") == ADMIN_PASS:
                session["ok"] = True
                return redirect(url_for("admin"))
            else:
                return render("Admin", "<div class='card'>Mot de passe incorrect</div>"+login_form())
        return render("Admin", login_form())

    # connecté → console
    added = request.args.get("added")
    ignored = request.args.get("ignored")

    # liste brouillons
    con = db(); c = con.cursor()
    drafts = c.execute("SELECT * FROM posts WHERE status='draft' ORDER BY id DESC LIMIT 200").fetchall()
    published = c.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC LIMIT 50").fetchall()
    con.close()

    flash_box = ""
    if added is not None and ignored is not None:
        flash_box = f"<div class='card'>Import terminé : {added} nouveaux, {ignored} ignorés.</div>"

    settings_html = settings_form()

    drafts_html = ""
    if drafts:
        li = []
        for d in drafts:
            li.append(f"""
            <div class='card'>
              <b>{d['title']}</b> — <small class='muted'>draft</small><br>
              <div class="btns">
                <a class='btn' href="{url_for('edit_post', pid=d['id'])}"><button>Ouvrir</button></a>
                <a class='btn' href="{url_for('delete_post', pid=d['id'])}"><button style="background:#8b0000">Supprimer</button></a>
              </div>
            </div>
            """)
        drafts_html = "<h3>Brouillons</h3>" + "\n".join(li)
    else:
        drafts_html = "<h3>Brouillons</h3><div class='card'>Aucun pour l’instant.</div>"

    pub_html = ""
    if published:
        li = []
        for p in published:
            li.append(f"""
            <div class='card'>
              <b>{p['title']}</b> — <small class='muted'>{p['published_at']}</small>
            </div>
            """)
        pub_html = "<h3>Publiés</h3>" + "\n".join(li)

    body = f"""
    <a class='btn' href="{url_for('import_now')}" onclick="return confirm('Lancer l\\'import maintenant ?')">
      <button>Importer maintenant</button></a>
    {flash_box}
    {drafts_html}
    {pub_html}
    <h3>Paramètres</h3>
    <div class='card'>{settings_html}</div>
    """
    return render("Admin", body)

def login_form():
    return """
    <div class='card'>
      <form method="post">
        <label>Mot de passe</label>
        <input type="password" name="password" placeholder="armenie">
        <button type="submit">Connexion</button>
      </form>
    </div>
    """

def settings_form():
    return f"""
    <form method="post" action="{url_for('save_settings')}">
      <label>OpenAI API Key (priorité base)</label>
      <input name="openai_api_key" value="{get_setting('openai_api_key')}">

      <label>OpenAI Model</label>
      <input name="openai_model" value="{get_setting('openai_model', DEFAULT_MODEL)}">

      <label>Import automatique (minutes, 0 = désactivé)</label>
      <input name="auto_minutes" value="{get_setting('auto_minutes','0')}">

      <label>Sources RSS (une URL par ligne)</label>
      <textarea name="rss_sources" rows="8">{get_setting('rss_sources')}</textarea>

      <label>Image par défaut (URL, optionnel)</label>
      <input name="fallback_image" value="{get_setting('fallback_image','')}">

      <button type="submit">Enregistrer</button>
    </form>
    """

@app.route("/save-settings", methods=["POST"])
def save_settings():
    if not session.get("ok"):
        return redirect(url_for("admin"))
    for key in ["openai_api_key","openai_model","auto_minutes","rss_sources","fallback_image"]:
        set_setting(key, request.form.get(key, "").strip())
    flash("Paramètres enregistrés")
    return redirect(url_for("admin"))

@app.route("/import-now")
def import_now():
    if not session.get("ok"):
        return redirect(url_for("admin"))
    try:
        added, ignored = import_once()
    except Exception:
        app.logger.error(traceback.format_exc())
        added, ignored = 0, 0
    return redirect(url_for("admin", added=added, ignored=ignored))

@app.route("/edit/<int:pid>", methods=["GET", "POST"])
def edit_post(pid):
    if not session.get("ok"):
        return redirect(url_for("admin"))
    con = db(); c = con.cursor()
    p = c.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if not p:
        con.close()
        return render("Édition", "<div class='card'>Introuvable</div>")

    if request.method == "POST":
        title = request.form.get("title","").strip()
        body  = request.form.get("body","").strip()
        img   = request.form.get("image_url","").strip()
        act   = request.form.get("action","save")
        if act == "publish":
            c.execute("""UPDATE posts SET title=?, body=?, image_url=?, status='published',
                         published_at=? WHERE id=?""",
                      (title, body, img, datetime.now(timezone.utc).isoformat(), pid))
        else:
            c.execute("UPDATE posts SET title=?, body=?, image_url=? WHERE id=?",
                      (title, body, img, pid))
        con.commit(); con.close()
        return redirect(url_for("admin"))

    con.close()
    form = f"""
    <div class='card'>
      <form method="post">
        <label>Remplacer l’image (URL)</label>
        <input name="image_url" value="{p['image_url'] or ''}">
        {"<img class='cover' src='%s'>" % p['image_url'] if p['image_url'] else ""}

        <label>Titre</label>
        <input name="title" value="{(p['title'] or '').replace('"','&quot;')}">

        <label>Contenu</label>
        <textarea name="body" rows="12">{p['body'] or ""}</textarea>
        <div style="margin-top:.6rem">
          <button name="action" value="save" class="btn">Enregistrer</button>
          <button name="action" value="publish" class="btn" style="background:#2e7d32">Publier</button>
        </div>
      </form>
    </div>
    """
    return render("Édition", form)

@app.route("/delete/<int:pid>")
def delete_post(pid):
    if not session.get("ok"):
        return redirect(url_for("admin"))
    con = db(); c = con.cursor()
    c.execute("DELETE FROM posts WHERE id=?", (pid,))
    con.commit(); con.close()
    return redirect(url_for("admin"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

# ===========================
# RSS
# ===========================
def rss_item(p):
    title = (p['title'] or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    body  = (p['body'] or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    link  = p['url'] or ""
    pub   = p['published_at'] or p['created_at'] or datetime.now(timezone.utc).isoformat()
    enclosure = ""
    if p['image_url']:
        enclosure = f'<enclosure url="{p["image_url"]}" type="image/jpeg" />'
    return f"""
<item>
  <title>{title}</title>
  <link>{link}</link>
  <guid isPermaLink="false">{p['id']}</guid>
  <pubDate>{pub}</pubDate>
  <description><![CDATA[{body}]]></description>
  {enclosure}
</item>"""

@app.route("/rss.xml")
def rss():
    con = db(); c = con.cursor()
    rows = c.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC LIMIT 100").fetchall()
    con.close()
    items = "\n".join(rss_item(r) for r in rows)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>{APP_NAME}</title>
  <link>{request.url_root}</link>
  <description>Flux RSS — {APP_NAME}</description>
  {items}
</channel>
</rss>"""
    return Response(xml, mimetype="application/rss+xml")

# ===========================
# Lancement local
# ===========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
