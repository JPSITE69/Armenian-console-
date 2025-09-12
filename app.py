import os
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

import feedparser
import requests
from bs4 import BeautifulSoup
from flask import (
    Flask, request, redirect, url_for, render_template_string,
    session, Response, flash
)
from apscheduler.schedulers.background import BackgroundScheduler

# =========================
# CONFIG de base
# =========================
APP_NAME      = "Console Arménienne"
ADMIN_PASS    = os.environ.get("ADMIN_PASS", "armenie")
SECRET_KEY    = os.environ.get("SECRET_KEY", "change-moi")
DB_PATH       = "data.db"
DEFAULT_IMAGE = "https://upload.wikimedia.org/wikipedia/commons/1/10/Flag_of_Armenia.png"

# Valeurs par défaut (modifiables dans Admin)
DEFAULT_SOURCES = [
    "https://www.civilnet.am/news/feed/",
    "https://armenpress.am/rss/",
    "https://news.am/eng/rss/",
    "https://factor.am/feed",
    "https://hetq.am/hy/rss",
    "https://armenpress.am/hy/rss/articles",
    "https://www.azatutyun.am/rssfeeds",
]
DEFAULT_MODEL   = "gpt-4o-mini"
DEFAULT_INTERVAL_MIN = 180  # 0 = désactivé

# =========================
# App Flask
# =========================
app = Flask(__name__)
app.secret_key = SECRET_KEY

# =========================
# DB helpers
# =========================
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS posts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            content TEXT,
            image TEXT,
            source_url TEXT,
            published INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    con.commit()
    # Valeurs par défaut si absentes
    if not get_setting("openai_model"):
        set_setting("openai_model", DEFAULT_MODEL)
    if not get_setting("auto_minutes"):
        set_setting("auto_minutes", str(DEFAULT_INTERVAL_MIN))
    if not get_setting("sources"):
        set_setting("sources", "\n".join(DEFAULT_SOURCES))
    if not get_setting("fallback_image"):
        set_setting("fallback_image", "")

def get_setting(key: str, default: str = "") -> str:
    con = db()
    r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default

def set_setting(key: str, value: str):
    con = db()
    con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (key, value))
    con.commit()

# =========================
# Image helpers
# =========================
def first_image_from_html(html: str) -> Optional[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    # og:image
    og = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
    if og and og.get("content"):
        return og["content"]
    # twitter:image
    tw = soup.find("meta", attrs={"name": "twitter:image"}) or soup.find("meta", property="twitter:image")
    if tw and tw.get("content"):
        return tw["content"]
    # première balise <img>
    im = soup.find("img")
    if im and im.get("src"):
        return im["src"]
    return None

def extract_image(entry) -> str:
    # 1) media:content
    if "media_content" in entry and entry.media_content:
        u = entry.media_content[0].get("url")
        if u: return u
    # 2) links type image/*
    if "links" in entry:
        for l in entry.links:
            if (l.get("type") or "").startswith("image/") and l.get("href"):
                return l["href"]
    # 3) <img> dans summary
    html = entry.get("summary", "") or entry.get("description", "")
    if html:
        u = first_image_from_html(html)
        if u: return u
    # 4) tenter la page source
    link = entry.get("link")
    if link:
        try:
            r = requests.get(link, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok:
                u = first_image_from_html(r.text)
                if u: return u
        except Exception:
            pass
    # 5) fallback personnalisé ou drapeau
    fallback = get_setting("fallback_image", "").strip()
    return fallback or DEFAULT_IMAGE

# =========================
# Réécriture FR (OpenAI si clé)
# =========================
def rewrite_french(raw_text: str) -> str:
    """
    - supprime HTML
    - réécrit en FR si openai_key défini (sinon texte brut)
    - ajoute TOUJOURS un saut de ligne + signature « – Arménie Info »
    (jamais de doublon)
    """
    # Nettoyage HTML -> texte brut
    soup = BeautifulSoup(raw_text or "", "html.parser")
    text = soup.get_text(separator=" ", strip=True)

    key = get_setting("openai_key", "").strip()
    if not key:
        return sign_once(text)

    model = get_setting("openai_model", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    try:
        # OpenAI SDK >= 1.0
        from openai import OpenAI
        client = OpenAI(api_key=key)
        resp = client.chat.completions.create(
            model=model,
            temperature=0.4,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Tu es un journaliste francophone. "
                        "Réécris en français clair et factuel. "
                        "Supprime le HTML, conserves l'information essentielle. "
                        "N'ajoute pas d'opinion. Pas d'emojis."
                    ),
                },
                {"role": "user", "content": text},
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        return sign_once(out or text)
    except Exception as e:
        return sign_once(f"{text}\n\n(Traduction indisponible : {e})")

def sign_once(txt: str) -> str:
    t = (txt or "").rstrip()
    signature = "– Arménie Info"
    if t.endswith(signature):
        return t
    return f"{t}\n\n{signature}"

# =========================
# Import RSS
# =========================
def lines_to_list(s: str) -> List[str]:
    return [x.strip() for x in (s or "").splitlines() if x.strip()]

def import_rss_once() -> int:
    """Retourne le nb d'articles ajoutés (non publiés)."""
    added = 0
    sources = lines_to_list(get_setting("sources", "\n".join(DEFAULT_SOURCES)))
    con = db()
    cur = con.cursor()

    for url in sources:
        try:
            feed = feedparser.parse(url)
        except Exception:
            continue

        for entry in feed.entries:
            title = (entry.get("title") or "").strip() or "Sans titre"
            raw   = entry.get("summary", "") or entry.get("description", "") or ""
            link  = (entry.get("link") or "").strip()
            img   = extract_image(entry)

            # anti-doublon sur link ou titre
            if link:
                exists = cur.execute("SELECT 1 FROM posts WHERE source_url=?", (link,)).fetchone()
                if exists: continue
            else:
                exists = cur.execute("SELECT 1 FROM posts WHERE title=?", (title,)).fetchone()
                if exists: continue

            content = rewrite_french(raw)
            now = datetime.now(timezone.utc).isoformat()
            cur.execute(
                "INSERT INTO posts(title,content,image,source_url,published,created_at) VALUES(?,?,?,?,0,?)",
                (title, content, img, link, now),
            )
            added += 1

    con.commit()
    return added

# =========================
# Scheduler
# =========================
_scheduler: Optional[BackgroundScheduler] = None
_job_id = "auto_import"

def start_scheduler():
    global _scheduler
    minutes = int(get_setting("auto_minutes", str(DEFAULT_INTERVAL_MIN)) or "0")
    if minutes <= 0:
        # désactivé
        if _scheduler:
            try: _scheduler.remove_job(_job_id)
            except Exception: pass
        return

    if not _scheduler:
        _scheduler = BackgroundScheduler()
        _scheduler.start()

    # (re)programme
    try:
        _scheduler.remove_job(_job_id)
    except Exception:
        pass
    _scheduler.add_job(import_rss_once, "interval", minutes=minutes, id=_job_id)

# =========================
# Templates
# =========================
ADMIN_TPL = """
<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8">
<title>Admin — {{app_name}}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu;max-width:1080px;margin:24px auto;padding:0 16px}
 h1{margin:0 0 16px}
 input,textarea,select{width:100%;padding:10px;border:1px solid #ccc;border-radius:6px;font-size:16px}
 button{padding:10px 14px;border:0;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer}
 .row{display:grid;grid-template-columns:1fr 1fr;gap:16px}
 .card{background:#1118270d;padding:16px;border-radius:10px;margin:10px 0}
 textarea{min-height:140px}
 img{max-width:320px;height:auto;border-radius:6px}
 .muted{color:#666}
 .badge{display:inline-block;padding:3px 8px;border-radius:12px;background:#10b981;color:#fff;font-size:12px;margin-left:8px}
 .list article{border-top:1px solid #e5e7eb;padding:12px 0}
 .flex{display:flex;gap:10px;align-items:center}
</style>
</head><body>
<h1>Console Admin <span class="badge">connecté</span></h1>

{% with messages = get_flashed_messages() %}
  {% if messages %}
    {% for m in messages %}<p class="muted">{{ m }}</p>{% endfor %}
  {% endif %}
{% endwith %}

<div class="row">
  <div class="card">
    <h3>Paramètres</h3>
    <form method="post" action="{{ url_for('save_settings') }}">
      <label>OpenAI API Key
        <input type="password" name="openai_key" value="{{openai_key or ''}}">
      </label>
      <div class="row">
        <label>Modèle OpenAI
          <input name="openai_model" value="{{openai_model}}">
        </label>
        <label>Import automatique (minutes, 0 = désactivé)
          <input name="auto_minutes" value="{{auto_minutes}}">
        </label>
      </div>
      <label>Image par défaut (URL, optionnelle)
        <input name="fallback_image" value="{{fallback_image or ''}}">
      </label>
      <label>Sources RSS (une URL par ligne)
        <textarea name="sources" rows="6">{{sources}}</textarea>
      </label>
      <div class="flex">
        <button>Enregistrer</button>
        <a href="{{ url_for('import_now') }}" class="muted">Importer maintenant</a>
        <span class="muted">· Flux RSS : <a href="{{ url_for('rss') }}">{{ request.url_root.rstrip('/') + url_for('rss') }}</a></span>
      </div>
    </form>
  </div>

  <div class="card">
    <h3>Créer un article vide</h3>
    <form method="post" action="{{ url_for('create_post') }}">
      <input name="title" placeholder="Titre">
      <textarea name="content" placeholder="Contenu (FR)…"></textarea>
      <input name="image" placeholder="URL image (optionnel)">
      <button>Créer brouillon</button>
    </form>
  </div>
</div>

<div class="card">
  <h3>Brouillons</h3>
  <div class="list">
    {% if not drafts %}<p class="muted">Aucun brouillon.</p>{% endif %}
    {% for p in drafts %}
      <article>
        <h4>{{ p['title'] }}</h4>
        {% if p['image'] %}<img src="{{p['image']}}"><br>{% endif %}
        <form method="post" action="{{ url_for('publish', pid=p['id']) }}">
          <textarea name="content">{{ p['content'] }}</textarea>
          <div class="flex">
            <button>Publier</button>
            <a href="{{ url_for('delete_post', pid=p['id']) }}" class="muted">Supprimer</a>
          </div>
        </form>
      </article>
    {% endfor %}
  </div>
</div>

<div class="card">
  <h3>Publiés</h3>
  <div class="list">
    {% for p in pubs %}
      <article>
        <h4>{{ p['title'] }}</h4>
        <a class="muted" href="{{ url_for('unpublish', pid=p['id']) }}">Dépublier</a>
      </article>
    {% endfor %}
  </div>
</div>

</body></html>
"""

INDEX_TPL = """
<!doctype html><meta charset="utf-8">
<title>{{app_name}}</title>
<h1>{{app_name}}</h1>
{% if not posts %}<p>Aucun article publié.</p>{% endif %}
{% for p in posts %}
  <article>
    <h2>{{ p['title'] }}</h2>
    {% if p['image'] %}<img src="{{p['image']}}" style="max-width:480px"><br>{% endif %}
    <div style="white-space:pre-line">{{ p['content'] }}</div>
  </article>
  <hr>
{% endfor %}
"""

# =========================
# Routes
# =========================

@app.get("/health")
def health():
    return "OK", 200

@app.get("/")
def home():
    con = db()
    posts = con.execute("SELECT * FROM posts WHERE published=1 ORDER BY id DESC").fetchall()
    return render_template_string(INDEX_TPL, app_name=APP_NAME, posts=posts)

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if not session.get("admin"):
        if request.method == "POST" and request.form.get("password") == ADMIN_PASS:
            session["admin"] = True
            flash("Connexion réussie.")
            return redirect(url_for("admin"))
        return "<h2>Connexion</h2><form method='post'><input type='password' name='password'><button>Entrer</button></form>"

    con = db()
    drafts = con.execute("SELECT * FROM posts WHERE published=0 ORDER BY id DESC").fetchall()
    pubs   = con.execute("SELECT * FROM posts WHERE published=1 ORDER BY id DESC").fetchall()

    ctx = dict(
        app_name=APP_NAME,
        drafts=drafts, pubs=pubs,
        openai_key=get_setting("openai_key",""),
        openai_model=get_setting("openai_model", DEFAULT_MODEL),
        auto_minutes=get_setting("auto_minutes", str(DEFAULT_INTERVAL_MIN)),
        sources=get_setting("sources", "\n".join(DEFAULT_SOURCES)),
        fallback_image=get_setting("fallback_image",""),
    )
    return render_template_string(ADMIN_TPL, **ctx)

@app.post("/save-settings")
def save_settings():
    if not session.get("admin"): return redirect(url_for("admin"))
    set_setting("openai_key", (request.form.get("openai_key") or "").strip())
    set_setting("openai_model", (request.form.get("openai_model") or DEFAULT_MODEL).strip())
    set_setting("auto_minutes", (request.form.get("auto_minutes") or "0").strip())
    set_setting("sources", (request.form.get("sources") or "").strip())
    set_setting("fallback_image", (request.form.get("fallback_image") or "").strip())
    start_scheduler()
    flash("Paramètres enregistrés.")
    return redirect(url_for("admin"))

@app.route("/import-now", methods=["GET", "POST"])
def import_now():
    if not session.get("admin"): return redirect(url_for("admin"))
    n = import_rss_once()
    flash(f"Import terminé : {n} nouveaux.")
    return redirect(url_for("admin"))

@app.post("/create-post")
def create_post():
    if not session.get("admin"): return redirect(url_for("admin"))
    title   = (request.form.get("title") or "Sans titre").strip()
    content = sign_once((request.form.get("content") or "").strip())
    image   = (request.form.get("image") or "").strip()
    now = datetime.now(timezone.utc).isoformat()
    con = db()
    con.execute(
        "INSERT INTO posts(title,content,image,source_url,published,created_at) VALUES(?,?,?,?,0,?)",
        (title, content, image or get_setting("fallback_image","") or DEFAULT_IMAGE, "", now)
    )
    con.commit()
    flash("Brouillon créé.")
    return redirect(url_for("admin"))

@app.route("/delete/<int:pid>")
def delete_post(pid):
    if not session.get("admin"): return redirect(url_for("admin"))
    con = db()
    con.execute("DELETE FROM posts WHERE id=?", (pid,))
    con.commit()
    flash("Supprimé.")
    return redirect(url_for("admin"))

@app.route("/unpublish/<int:pid>")
def unpublish(pid):
    if not session.get("admin"): return redirect(url_for("admin"))
    con = db()
    con.execute("UPDATE posts SET published=0 WHERE id=?", (pid,))
    con.commit()
    flash("Dépublié.")
    return redirect(url_for("admin"))

@app.post("/publish/<int:pid>")
def publish(pid):
    if not session.get("admin"): return redirect(url_for("admin"))
    content = (request.form.get("content") or "").strip()
    # s'assurer du saut de ligne + signature unique
    content = sign_once(content)
    con = db()
    con.execute("UPDATE posts SET content=?, published=1 WHERE id=?", (content, pid))
    con.commit()
    flash("Publié.")
    return redirect(url_for("admin"))

@app.get("/rss.xml")
def rss():
    con = db()
    rows = con.execute("SELECT * FROM posts WHERE published=1 ORDER BY id DESC LIMIT 100").fetchall()

    items = []
    for r in rows:
        title = (r["title"] or "").replace("&", "&amp;")
        desc  = (r["content"] or "").replace("&", "&amp;")
        # conserver les sauts de ligne dans un CDATA
        desc = f"<![CDATA[{desc}]]>"
        enclosure = ""
        if r["image"]:
            enclosure = f'<enclosure url="{r["image"]}" type="image/jpeg" />'
        pubdate = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S %z')
        items.append(
            "<item>"
            f"<title>{title}</title>"
            f"<link>{request.url_root}</link>"
            f"<guid isPermaLink=\"false\">{r['id']}</guid>"
            f"<description>{desc}</description>"
            f"{enclosure}"
            f"<pubDate>{pubdate}</pubDate>"
            "</item>"
        )

    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<rss version='2.0'>"
        "<channel>"
        f"<title>{APP_NAME} — Flux</title>"
        f"<link>{request.url_root}</link>"
        "<description>Articles publiés</description>"
        + "".join(items) +
        "</channel></rss>"
    )
    return Response(xml, mimetype="application/rss+xml")

# =========================
# Entrée
# =========================
if __name__ == "__main__":
    init_db()
    start_scheduler()
    app.run(host="0.0.0.0", port=5000)
