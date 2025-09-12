import os
import sqlite3
import requests
import feedparser
from flask import Flask, request, redirect, url_for, render_template_string, session, flash, Response
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
from datetime import datetime, timezone

# ====== CONFIG ======
APP_NAME       = "Console Arménienne"
ADMIN_PASS     = os.environ.get("ADMIN_PASS", "armenie")
SECRET_KEY     = os.environ.get("SECRET_KEY", "change-moi")
DB             = "data.db"
DEFAULT_IMAGE  = "https://upload.wikimedia.org/wikipedia/commons/1/10/Flag_of_Armenia.png"

FEEDS = [
    "https://www.civilnet.am/news/feed/",
    "https://armenpress.am/rss/",
    "https://news.am/eng/rss/",
    "https://factor.am/feed",
    "https://hetq.am/hy/rss",
    "https://armenpress.am/hy/rss/articles",
    "https://www.azatutyun.am/rssfeeds",
]

# ====== APP ======
app = Flask(__name__)
app.secret_key = SECRET_KEY

@app.get("/health")
def health():
    return "OK"

# ====== DB ======
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            content TEXT,
            image TEXT,
            published INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    con.commit()

def get_setting(key, default=""):
    con = db()
    r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default

def set_setting(key, value):
    con = db()
    con.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))
    con.commit()

# ====== IMAGES (simple & fiable) ======
def extract_image(entry):
    # 1) <media:content>
    if "media_content" in entry and entry.media_content:
        u = entry.media_content[0].get("url")
        if u: return u
    # 2) enclosures/images
    if "links" in entry:
        for link in entry.links:
            if link.get("type", "").startswith("image/"):
                return link.get("href")
    # 3) <img> dans summary/description
    html = entry.get("summary", "") or entry.get("description", "")
    if html:
        soup = BeautifulSoup(html, "html.parser")
        img = soup.find("img")
        if img and img.get("src"):
            return img["src"]
    # 4) défaut
    return DEFAULT_IMAGE

# ====== RÉÉCRITURE (FR + saut de ligne + signature unique) ======
def rewrite_text(text: str) -> str:
    def _sign(t: str) -> str:
        t = t.strip()
        if not t.endswith("– Arménie Info"):
            t = f"{t}\n\n– Arménie Info"  # saut de ligne avant signature
        return t

    key = get_setting("openai_key", "")
    if not key:
        return _sign(text)

    try:
        import openai
        openai.api_key = key
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Réécris ce texte en français clair, ton journalistique, sans HTML."},
                {"role": "user", "content": text},
            ],
            temperature=0.4,
        )
        out = resp.choices[0].message["content"].strip()
        return _sign(out)
    except Exception as e:
        return _sign(text + f"\n\n(Erreur traduction: {e})")

# ====== IMPORT RSS ======
def import_rss():
    con = db()
    cur = con.cursor()
    created = 0
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[FEED] {url} error:", e)
            continue

        for entry in feed.entries:
            title = (entry.get("title") or "").strip() or "Sans titre"
            raw   = entry.get("summary", "") or entry.get("description", "") or ""
            image = extract_image(entry)

            # dédup simple par titre
            cur.execute("SELECT 1 FROM posts WHERE title=?", (title,))
            if cur.fetchone():
                continue

            content = rewrite_text(raw)
            now = datetime.now(timezone.utc).isoformat()
            cur.execute(
                "INSERT INTO posts (title, content, image, published, created_at) VALUES (?,?,?,?,?)",
                (title, content, image, 0, now),
            )
            created += 1
    con.commit()
    print(f"[IMPORT] created={created}")

# ====== ROUTES ======
@app.get("/")
def index():
    con = db()
    posts = con.execute("SELECT * FROM posts WHERE published=1 ORDER BY id DESC").fetchall()
    # garde les sauts de ligne dans <p>
    return render_template_string("""
    <h1>{{app_name}}</h1>
    {% for p in posts %}
      <article>
        <h2>{{p['title']}}</h2>
        {% if p['image'] %}
          <img src="{{p['image']}}" alt="" style="max-width:420px">
        {% endif %}
        <p style="white-space:pre-line">{{p['content']}}</p>
      </article>
      <hr>
    {% endfor %}
    """, app_name=APP_NAME, posts=posts)

@app.route("/admin", methods=["GET", "POST"])
def admin():
    # login minimal
    if not session.get("admin"):
        if request.method == "POST" and request.form.get("password") == ADMIN_PASS:
            session["admin"] = True
            return redirect(url_for("admin"))
        return """
        <h1>Connexion</h1>
        <form method="post">
          <input type="password" name="password" placeholder="Mot de passe" />
          <button>Entrer</button>
        </form>
        """

    key = get_setting("openai_key", "")

    con = db()
    drafts = con.execute("SELECT * FROM posts WHERE published=0 ORDER BY id DESC").fetchall()
    pubs   = con.execute("SELECT * FROM posts WHERE published=1 ORDER BY id DESC").fetchall()
    return render_template_string("""
    <h1>Admin</h1>

    <form method="post" action="{{ url_for('set_key') }}">
      <label>OpenAI API Key :
        <input type="password" name="key" value="{{key}}">
      </label>
      <button>Enregistrer</button>
    </form>

    <form method="post" action="{{ url_for('import_now') }}" style="margin-top:1rem">
      <button>🔁 Importer maintenant</button>
    </form>
    <br>

    <h2>Brouillons</h2>
    {% for p in drafts %}
      <form method="post" action="{{ url_for('publish', pid=p['id']) }}">
        <h3>{{p['title']}}</h3>
        {% if p['image'] %}<img src="{{p['image']}}" style="max-width:300px"><br>{% endif %}
        <textarea name="content" rows="8" cols="100">{{p['content']}}</textarea><br>
        <button type="submit">Publier</button>
      </form>
      <hr>
    {% endfor %}

    <h2>Publiés</h2>
    {% for p in pubs %}
      <article><h3>{{p['title']}}</h3></article>
    {% endfor %}

    <p>Flux RSS : <a href="{{ url_for('rss') }}">{{ request.url_root.rstrip('/') + url_for('rss') }}</a></p>
    """, drafts=drafts, pubs=pubs, key=key)

@app.post("/set-key")
def set_key():
    if not session.get("admin"):
        return redirect(url_for("admin"))
    key = (request.form.get("key") or "").strip()
    set_setting("openai_key", key)
    flash("Clé OpenAI enregistrée.")
    return redirect(url_for("admin"))

@app.route("/import-now", methods=["GET", "POST"])
def import_now():
    import_rss()
    return redirect(url_for("admin"))

@app.post("/publish/<int:pid>")
def publish(pid):
    content = (request.form.get("content") or "").strip()
    if not content.endswith("– Arménie Info"):
        content = f"{content}\n\n– Arménie Info"
    con = db()
    con.execute("UPDATE posts SET content=?, published=1 WHERE id=?", (content, pid))
    con.commit()
    return redirect(url_for("index"))

# ====== RSS FEED ======
@app.get("/rss.xml")
def rss():
    con = db()
    rows = con.execute("SELECT * FROM posts WHERE published=1 ORDER BY id DESC LIMIT 100").fetchall()
    items = []
    for r in rows:
        title = (r["title"] or "").replace("&", "&amp;")
        desc  = (r["content"] or "")
        # On met le contenu complet (avec sauts de ligne) en CDATA
        desc_cdata = f"<![CDATA[{desc}]]>"
        enclosure = ""
        if r["image"]:
            enclosure = f"<enclosure url=\"{r['image']}\" type=\"image/jpeg\"/>"
        pubdate = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S %z')
        items.append(
            f"<item>"
            f"<title>{title}</title>"
            f"<link>{request.url_root}</link>"
            f"<guid isPermaLink=\"false\">{r['id']}</guid>"
            f"<description>{desc_cdata}</description>"
            f"{enclosure}"
            f"<pubDate>{pubdate}</pubDate>"
            f"</item>"
        )

    rss_xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<rss version='2.0'>"
        "<channel>"
        f"<title>{APP_NAME} — Flux</title>"
        f"<link>{request.url_root}</link>"
        "<description>Articles publiés</description>"
        + "".join(items) +
        "</channel></rss>"
    )
    return Response(rss_xml, mimetype="application/rss+xml")

# ====== MAIN ======
def start_scheduler():
    sched = BackgroundScheduler()
    # import auto toutes les 3h (stable)
    sched.add_job(import_rss, "interval", minutes=180)
    sched.start()

if __name__ == "__main__":
    init_db()
    start_scheduler()
    app.run(host="0.0.0.0", port=5000, debug=False)
