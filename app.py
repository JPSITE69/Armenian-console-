import os
import sqlite3
import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, redirect, url_for, session
from openai import OpenAI
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler

# --- Initialisation Flask ---
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "secret")

# --- Variables ---
ADMIN_PASS = os.getenv("ADMIN_PASS", "armenie")
DB_PATH = os.getenv("DB_PATH", "console.db")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

client = OpenAI(api_key=OPENAI_API_KEY)

# --- Base de données ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT,
                    content TEXT,
                    image TEXT,
                    status TEXT,
                    publish_at TEXT,
                    created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT)""")
    conn.commit()
    conn.close()

init_db()

# --- Images ---
def fetch_image_from_url(url):
    try:
        r = requests.get(url, timeout=5)
        soup = BeautifulSoup(r.text, "html.parser")
        img = soup.find("meta", {"property": "og:image"})
        if img and img.get("content"):
            return img["content"]
        img = soup.find("img")
        if img and img.get("src"):
            return img["src"]
    except:
        pass
    return get_default_image()

# --- GPT Rewrite ---
def rewrite_article(title, content):
    try:
        prompt = f"""
        Traduis et réécris en français l’article suivant.

        Titre : {title}
        Contenu : {content}

        Format attendu :
        - Titre traduit
        - Contenu réécrit en français clair
        - Signature : Arménie Info
        """

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"{title}\n\n{content}\n\nArménie Info (Erreur GPT : {e})"

# --- Gestion articles ---
def save_article(title, content, image, status="draft", publish_at=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO articles (title, content, image, status, publish_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
              (title, content, image, status, publish_at, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def update_article(aid, title, content, image, publish_at):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE articles SET title=?, content=?, image=?, publish_at=? WHERE id=?",
              (title, content, image, publish_at, aid))
    conn.commit()
    conn.close()

def publish_article(aid):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE articles SET status='published' WHERE id=?", (aid,))
    conn.commit()
    conn.close()

# --- Scheduler ---
scheduler = BackgroundScheduler()

def check_scheduled():
    now = datetime.now().isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM articles WHERE status='draft' AND publish_at IS NOT NULL AND publish_at<=?", (now,))
    articles = c.fetchall()
    for a in articles:
        publish_article(a[0])
    conn.close()

scheduler.add_job(check_scheduled, "interval", minutes=1)
scheduler.start()

# --- Flask Routes ---
@app.route("/")
def index():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT title, content, image FROM articles WHERE status='published' ORDER BY created_at DESC")
    articles = c.fetchall()
    conn.close()

    html = "<h1>Arménie Info</h1>"
    for a in articles:
        img_html = f"<img src='{a[2]}' width='300'><br>" if a[2] else ""
        html += f"<h2>{a[0]}</h2>{img_html}<p>{a[1]}</p><hr>"
    return html

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if "logged_in" not in session:
        if request.method == "POST" and request.form.get("password") == ADMIN_PASS:
            session["logged_in"] = True
            return redirect(url_for("admin"))
        return "<h2>Connexion admin</h2><form method='post'><input type='password' name='password'><input type='submit' value='Entrer'></form>"

    action = request.args.get("action")

    # Import articles
    if action == "import":
        feeds = get_feeds()
        for feed_url in feeds:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:5]:
                image = fetch_image_from_url(entry.link)
                rewritten = rewrite_article(entry.title, entry.get("summary", ""))
                save_article(entry.title, rewritten, image)
        return redirect(url_for("admin"))

    # Publier
    if action == "publish":
        publish_article(request.args.get("id"))
        return redirect(url_for("admin"))

    # Supprimer
    if action == "delete":
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM articles WHERE id=?", (request.args.get("id"),))
        conn.commit()
        conn.close()
        return redirect(url_for("admin"))

    # Modifier
    if action == "edit":
        aid = request.args.get("id")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, title, content, image, publish_at FROM articles WHERE id=?", (aid,))
        a = c.fetchone()
        conn.close()
        return f"""
        <h2>Modifier article</h2>
        <form method='post' action='/admin?action=save_edit&id={a[0]}'>
            Titre : <input type='text' name='title' value="{a[1]}" size='80'><br>
            Contenu : <textarea name='content' rows='10' cols='80'>{a[2]}</textarea><br>
            Image URL : <input type='text' name='image' value="{a[3]}" size='80'><br>
            Publier à (YYYY-MM-DD HH:MM:SS) : <input type='text' name='publish_at' value="{a[4] or ''}" size='25'><br>
            <input type='submit' value='Enregistrer'>
        </form>
        """

    if action == "save_edit":
        aid = request.args.get("id")
        update_article(aid, request.form["title"], request.form["content"], request.form["image"], request.form["publish_at"])
        return redirect(url_for("admin"))

    # Liste des articles
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, status, publish_at FROM articles ORDER BY created_at DESC")
    articles = c.fetchall()
    conn.close()

    html = "<h1>Admin Arménie Info</h1>"
    html += "<a href='?action=import'>📥 Importer</a> | <a href='/feeds'>⚙️ Flux RSS</a> | <a href='/logout'>🚪 Déconnexion</a><hr>"
    for a in articles:
        html += f"[{a[2]}] {a[1]} (⏰ {a[3] or 'non planifié'}) - <a href='?action=publish&id={a[0]}'>Publier</a> | <a href='?action=edit&id={a[0]}'>Modifier</a> | <a href='?action=delete&id={a[0]}'>Supprimer</a><br>"
    return html

@app.route("/feeds", methods=["GET", "POST"])
def feeds():
    if "logged_in" not in session:
        return redirect(url_for("admin"))
    if request.method == "POST":
        save_setting("feeds", request.form["feeds"])
        return redirect(url_for("feeds"))
    feeds = get_feeds()
    return f"<h2>Configurer les flux RSS</h2><form method='post'><textarea name='feeds' rows='5' cols='60'>{chr(10).join(feeds)}</textarea><br><input type='submit' value='Sauvegarder'></form><br><a href='/admin'>Retour</a>"

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin"))

@app.route("/health")
def health():
    return "OK"

# --- Paramètres ---
def save_setting(key, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def get_setting(key, default=""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default

def get_feeds():
    return get_setting("feeds", "").splitlines()

def get_default_image():
    return get_setting("default_image", "")

# --- Lancement ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
