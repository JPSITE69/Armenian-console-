import os
import sqlite3
import feedparser
import requests
import json
from flask import Flask, request, redirect, url_for, render_template_string, Response, abort, session
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import openai

# --- Config ---
DB_PATH = os.environ.get("DB_PATH", "console.db")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "armenie")
DEFAULT_IMAGE = os.environ.get("DEFAULT_IMAGE", "https://placehold.co/600x400?text=Armenie+Info")
FEEDS = eval(os.environ.get("FEEDS", "[]"))  # fallback si feeds.txt absent

SETTINGS_FILE = "settings.json"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "supersecret")
scheduler = BackgroundScheduler()
scheduler.start()

# --- Settings ---
def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_settings(data):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f)

def get_openai_key():
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ.get("OPENAI_API_KEY")
    settings = load_settings()
    return settings.get("OPENAI_API_KEY")

# --- DB init ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            content TEXT,
            image TEXT,
            link TEXT UNIQUE,
            status TEXT,
            pub_date TEXT,
            publish_at TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# --- Utils ---
def clean_html(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    return soup.get_text(separator="\n", strip=True)

def article_exists(link: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM articles WHERE link = ?", (link,))
    exists = cur.fetchone()[0] > 0
    conn.close()
    return exists

def get_image_from_page(url: str, default_image: str = DEFAULT_IMAGE) -> str:
    try:
        html = requests.get(url, timeout=5).text
        soup = BeautifulSoup(html, "html.parser")
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            return og["content"]
        tw = soup.find("meta", attrs={"name": "twitter:image"})
        if tw and tw.get("content"):
            return tw["content"]
        img = soup.find("img")
        if img and img.get("src"):
            return img["src"]
    except Exception:
        pass
    return default_image

def rewrite_article(title: str, content: str) -> (str, str):
    key = get_openai_key()
    if not key:
        return title, content
    openai.api_key = key
    prompt = f"""
    Traduis et réécris en français.
    Mets uniquement le texte révisé, sans explication.

    Titre: {title}
    Contenu: {content}
    """
    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )
        text = response.choices[0].message.content.strip()
        lines = text.split("\n", 1)
        if len(lines) == 2:
            return lines[0].strip(), lines[1].strip()
        else:
            return title, text
    except Exception:
        return title, content

def format_article(title_fr: str, content_fr: str) -> str:
    return f"{title_fr}\n\n{content_fr}\n\n— Arménie Info"

# --- Feeds management ---
def get_feeds():
    if os.path.exists("feeds.txt"):
        with open("feeds.txt") as f:
            return [line.strip() for line in f.readlines() if line.strip()]
    return FEEDS

def import_articles():
    for feed_url in get_feeds():
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:10]:
            link = entry.link
            if article_exists(link):
                continue
            title = entry.title
            summary = clean_html(getattr(entry, "summary", ""))
            image_url = get_image_from_page(link)
            try:
                html = requests.get(link, timeout=5).text
                soup = BeautifulSoup(html, "html.parser")
                paragraphs = [p.get_text() for p in soup.find_all("p")]
                full_text = "\n".join(paragraphs) if paragraphs else summary
            except Exception:
                full_text = summary
            title_fr, content_fr = rewrite_article(title, full_text)
            final_text = format_article(title_fr, content_fr)
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("""
                INSERT OR IGNORE INTO articles (title, content, image, link, status, pub_date)
                VALUES (?, ?, ?, ?, 'draft', ?)
            """, (title_fr, final_text, image_url, link, datetime.utcnow().isoformat()))
            conn.commit()
            conn.close()

# --- Scheduler ---
def check_scheduled():
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE articles SET status='published' WHERE publish_at <= ? AND status='draft'", (now,))
    conn.commit()
    conn.close()

scheduler.add_job(check_scheduled, "interval", minutes=1)

# --- Routes publiques ---
@app.route("/")
def home():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT title, content, image FROM articles WHERE status='published' ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    html = """
    <h1>Arménie Info</h1>
    {% for title, content, image in rows %}
      <h2>{{ title }}</h2>
      <img src="{{ image }}" width="400"><br>
      <pre style="white-space: pre-wrap;">{{ content }}</pre>
      <hr>
    {% endfor %}
    """
    return render_template_string(html, rows=rows)

@app.route("/feed.xml")
def feed():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT title, content, link, pub_date FROM articles WHERE status='published' ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    rss_items = ""
    for title, content, link, pub_date in rows:
        rss_items += f"""
        <item>
            <title>{title}</title>
            <link>{link}</link>
            <pubDate>{pub_date}</pubDate>
            <description><![CDATA[{content}]]></description>
        </item>
        """
    rss = f"""<?xml version="1.0"?>
    <rss version="2.0">
      <channel>
        <title>Arménie Info</title>
        <link>/</link>
        <description>Flux RSS des articles publiés</description>
        {rss_items}
      </channel>
    </rss>"""
    return Response(rss, mimetype="application/xml")

# --- Admin ---
@app.route("/admin", methods=["GET", "POST"])
def admin():
    if "logged_in" in session and session["logged_in"]:
        action = request.args.get("action")
        art_id = request.args.get("id")

        if action == "import":
            import_articles()
        elif action == "publish" and art_id:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("UPDATE articles SET status='published' WHERE id=?", (art_id,))
            conn.commit()
            conn.close()
        elif action == "delete" and art_id:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("DELETE FROM articles WHERE id=?", (art_id,))
            conn.commit()
            conn.close()

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT id, title, status, publish_at FROM articles ORDER BY id DESC")
        rows = cur.fetchall()
        conn.close()

        html = """
        <h1>Admin Arménie Info</h1>
        <a href="{{ url_for('admin', action='import') }}">Importer articles</a> |
        <a href="{{ url_for('manage_feeds') }}">Configurer les flux RSS</a> |
        <a href="{{ url_for('settings_page') }}">Paramètres</a> |
        <a href="{{ url_for('logout') }}">Se déconnecter</a>
        <ul>
        {% for id, title, status, publish_at in rows %}
          <li>
            <b>[{{ status }}]</b> {{ title }}
            {% if publish_at %}(Planifié: {{ publish_at }}){% endif %}
            <a href="{{ url_for('admin', action='publish', id=id) }}">Publier</a>
            <a href="{{ url_for('edit_article', id=id) }}">Modifier</a>
            <a href="{{ url_for('delete_article', id=id) }}">Supprimer</a>
          </li>
        {% endfor %}
        </ul>
        """
        return render_template_string(html, rows=rows)

    if request.method == "POST":
        password = request.form.get("password")
        if password == ADMIN_PASS:
            session["logged_in"] = True
            return redirect(url_for("admin"))
        else:
            return "Mot de passe incorrect", 403

    return """
    <h1>Connexion Admin</h1>
    <form method="post">
        <input type="password" name="password" placeholder="Mot de passe"/>
        <button type="submit">Se connecter</button>
    </form>
    """

@app.route("/edit/<int:id>", methods=["GET", "POST"])
def edit_article(id):
    if "logged_in" not in session or not session["logged_in"]:
        return redirect(url_for("admin"))

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    if request.method == "POST":
        title = request.form.get("title")
        content = request.form.get("content")
        image = request.form.get("image")
        publish_at = request.form.get("publish_at")
        cur.execute("UPDATE articles SET title=?, content=?, image=?, publish_at=? WHERE id=?",
                    (title, content, image, publish_at if publish_at else None, id))
        conn.commit()
        conn.close()
        return redirect(url_for("admin"))

    cur.execute("SELECT title, content, image, publish_at FROM articles WHERE id=?", (id,))
    row = cur.fetchone()
    conn.close()

    html = """
    <h1>Modifier article</h1>
    <form method="post">
        <input type="text" name="title" value="{{ row[0] }}" size="80"/><br><br>
        <textarea name="content" rows="15" cols="80">{{ row[1] }}</textarea><br><br>
        <input type="text" name="image" value="{{ row[2] }}" size="80"/><br><br>
        <label>Planifier (UTC, ex: 2025-09-13T12:00:00):</label><br>
        <input type="text" name="publish_at" value="{{ row[3] or '' }}" size="40"/><br><br>
        <button type="submit">Sauvegarder</button>
    </form>
    <a href="{{ url_for('admin') }}">Retour admin</a>
    """
    return render_template_string(html, row=row)

@app.route("/delete/<int:id>")
def delete_article(id):
    if "logged_in" not in session or not session["logged_in"]:
        return redirect(url_for("admin"))
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM articles WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin"))

@app.route("/feeds", methods=["GET", "POST"])
def manage_feeds():
    if "logged_in" not in session or not session["logged_in"]:
        return redirect(url_for("admin"))

    if request.method == "POST":
        feeds_input = request.form.get("feeds")
        feeds_list = [f.strip() for f in feeds_input.splitlines() if f.strip()]
        with open("feeds.txt", "w") as f:
            for feed in feeds_list:
                f.write(feed + "\n")
        return redirect(url_for("manage_feeds"))

    feeds_list = []
    if os.path.exists("feeds.txt"):
        with open("feeds.txt") as f:
            feeds_list = [line.strip() for line in f.readlines() if line.strip()]

    html = """
    <h1>Configurer les flux RSS</h1>
    <form method="post">
        <textarea name="feeds" rows="10" cols="60">{{ feeds_text }}</textarea><br>
        <button type="submit">Sauvegarder</button>
    </form>
    <a href="{{ url_for('admin') }}">Retour admin</a>
    """
    return render_template_string(html, feeds_text="\n".join(feeds_list))

@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if "logged_in" not in session or not session["logged_in"]:
        return redirect(url_for("admin"))

    settings = load_settings()

    if request.method == "POST":
        api_key = request.form.get("OPENAI_API_KEY")
        settings["OPENAI_API_KEY"] = api_key
        save_settings(settings)
        return redirect(url_for("settings_page"))

    html = """
    <h1>Paramètres</h1>
    <form method="post">
        <label>OpenAI API Key :</label><br>
        <input type="text" name="OPENAI_API_KEY" value="{{ current_key }}" size="60"/><br><br>
        <button type="submit">Sauvegarder</button>
    </form>
    <a href="{{ url_for('admin') }}">Retour admin</a>
    """
    return render_template_string(html, current_key=settings.get("OPENAI_API_KEY", ""))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin"))

@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
