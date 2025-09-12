import os
import sqlite3
import requests
import feedparser
from flask import Flask, request, redirect, url_for, render_template_string, session, flash
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup

# ===== CONFIG =====
APP_NAME = "Console Arménienne"
ADMIN_PASS = os.environ.get("ADMIN_PASS", "armenie")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-moi")

DB = "data.db"

DEFAULT_IMAGE = "https://upload.wikimedia.org/wikipedia/commons/1/10/Flag_of_Armenia.png"

FEEDS = [
    "https://www.civilnet.am/news/feed/",
    "https://armenpress.am/rss/",
    "https://news.am/eng/rss/",
    "https://factor.am/feed",
    "https://hetq.am/hy/rss",
    "https://armenpress.am/hy/rss/articles",
    "https://www.azatutyun.am/rssfeeds",
]

# ===== APP =====
app = Flask(__name__)
app.secret_key = SECRET_KEY


@app.get("/health")
def health():
    return "OK"


# ===== DB =====
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
            published INTEGER DEFAULT 0
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
    cur = con.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    con = db()
    con.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))
    con.commit()


# ===== IMAGE =====
def extract_image(entry):
    if "media_content" in entry and entry.media_content:
        url = entry.media_content[0].get("url")
        if url:
            return url
    if "links" in entry:
        for link in entry.links:
            if link.get("type", "").startswith("image/"):
                return link.get("href")
    if "summary" in entry:
        soup = BeautifulSoup(entry.summary, "html.parser")
        img = soup.find("img")
        if img and img.get("src"):
            return img["src"]
    return DEFAULT_IMAGE


# ===== RÉÉCRITURE =====
def rewrite_text(text: str) -> str:
    """Réécrit en français avec OpenAI si dispo, sinon renvoie brut.
    Ajoute la signature avec saut de ligne unique."""
    def _sign(t: str) -> str:
        t = t.strip()
        if not t.endswith("– Arménie Info"):
            t = f"{t}\n\n– Arménie Info"
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
                {"role": "system", "content": "Réécris ce texte en français, style journalistique, sans HTML."},
                {"role": "user", "content": text},
            ],
            temperature=0.4,
        )
        content = resp.choices[0].message["content"].strip()
        return _sign(content)
    except Exception as e:
        return _sign(text + f"\n\n(Erreur traduction: {e})")


# ===== IMPORT RSS =====
def import_rss():
    con = db()
    cur = con.cursor()
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[FEED] {url} error: {e}")
            continue

        for entry in feed.entries:
            title = entry.get("title", "").strip() or "Sans titre"
            raw = entry.get("summary", "") or entry.get("description", "") or ""
            image = extract_image(entry)

            cur.execute("SELECT 1 FROM posts WHERE title=?", (title,))
            if cur.fetchone():
                continue

            content = rewrite_text(raw)
            cur.execute(
                "INSERT INTO posts (title, content, image, published) VALUES (?, ?, ?, 0)",
                (title, content, image),
            )
    con.commit()


# ===== ROUTES =====
@app.get("/")
def index():
    con = db()
    posts = con.execute("SELECT * FROM posts WHERE published=1 ORDER BY id DESC").fetchall()
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
    """, posts=posts, app_name=APP_NAME)


@app.route("/admin", methods=["GET", "POST"])
def admin():
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
    pubs = con.execute("SELECT * FROM posts WHERE published=1 ORDER BY id DESC").fetchall()
    return render_template_string("""
    <h1>Admin</h1>

    <form method="post" action="{{ url_for('set_key') }}">
      <label>OpenAI API Key :
        <input type="password" name="key" value="{{key}}">
      </label>
      <button>Enregistrer</button>
    </form>

    <form method="post" action="{{ url_for('import_now') }}">
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
      <article>
        <h3>{{p['title']}}</h3>
      </article>
    {% endfor %}
    """, drafts=drafts, pubs=pubs, key=key)


@app.post("/set-key")
def set_key():
    if not session.get("admin"):
        return redirect(url_for("admin"))
    key = request.form.get("key", "").strip()
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


# ===== MAIN =====
def start_scheduler():
    sched = BackgroundScheduler()
    sched.add_job(import_rss, "interval", minutes=180)
    sched.start()


if __name__ == "__main__":
    init_db()
    start_scheduler()
    app.run(host="0.0.0.0", port=5000, debug=False)
