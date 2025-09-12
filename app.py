import os
import sqlite3
import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, redirect, url_for, session
from openai import OpenAI
from datetime import datetime

# --- Flask ---
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "secret")

# --- Variables ---
ADMIN_PASS = os.getenv("ADMIN_PASS", "armenie")
DB_PATH = os.getenv("DB_PATH", "console.db")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# --- OpenAI Client (corrigé sans proxies) ---
client = OpenAI(api_key=OPENAI_API_KEY)

# --- DB Init ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS articles
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  title TEXT,
                  content TEXT,
                  image TEXT,
                  status TEXT,
                  created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)''')
    conn.commit()
    conn.close()

init_db()

# --- Fonctions utiles ---
def fetch_image_from_url(url):
    """Récupère une image d’un article"""
    try:
        r = requests.get(url, timeout=5)
        soup = BeautifulSoup(r.text, "html.parser")
        img = soup.find("img")
        if img and img.get("src"):
            return img["src"]
    except:
        pass
    return get_default_image()

def rewrite_article(title, content):
    """Réécriture et traduction via GPT"""
    try:
        prompt = f"""
        Traduis et réécris en français l’article suivant.

        Titre : {title}
        Contenu : {content}

        Format attendu :
        - Titre traduit
        - Saut de ligne
        - Contenu réécrit en français clair
        - Saut de ligne
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

def save_article(title, content, image, status="draft"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO articles (title, content, image, status, created_at) VALUES (?, ?, ?, ?, ?)",
              (title, content, image, status, datetime.now().isoformat()))
    conn.commit()
    conn.close()

# --- Routes principales ---
@app.route("/")
def index():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, content, image FROM articles WHERE status='published' ORDER BY created_at DESC")
    articles = c.fetchall()
    conn.close()

    html = "<h1>Arménie Info</h1>"
    for a in articles:
        img_html = f"<img src='{a[3]}' width='300'><br>" if a[3] else ""
        html += f"<h2>{a[1]}</h2>{img_html}<p>{a[2]}</p><hr>"
    return html

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if "logged_in" not in session:
        if request.method == "POST" and request.form.get("password") == ADMIN_PASS:
            session["logged_in"] = True
            return redirect(url_for("admin"))
        return "<h2>Connexion admin</h2><form method='post'><input type='password' name='password'><input type='submit' value='Entrer'></form>"

    action = request.args.get("action")

    if action == "import":
        feeds = get_feeds()
        for feed_url in feeds:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:5]:
                image = fetch_image_from_url(entry.link)
                rewritten = rewrite_article(entry.title, entry.get("summary", ""))
                save_article(entry.title, rewritten, image)
        return redirect(url_for("admin"))

    if action == "publish":
        aid = request.args.get("id")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE articles SET status='published' WHERE id=?", (aid,))
        conn.commit()
        conn.close()
        return redirect(url_for("admin"))

    if action == "delete":
        aid = request.args.get("id")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM articles WHERE id=?", (aid,))
        conn.commit()
        conn.close()
        return redirect(url_for("admin"))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, status FROM articles ORDER BY created_at DESC")
    articles = c.fetchall()
    conn.close()

    html = "<h1>Admin Arménie Info</h1>"
    html += "<a href='?action=import'>📥 Importer articles</a> | "
    html += "<a href='/feeds'>⚙️ Configurer flux RSS</a> | "
    html += "<a href='/settings'>🖼 Paramètres</a> | "
    html += "<a href='/logout'>🚪 Déconnexion</a><hr>"

    for a in articles:
        html += f"[{a[2]}] {a[1]} - <a href='?action=publish&id={a[0]}'>Publier</a> | <a href='?action=delete&id={a[0]}'>Supprimer</a><br>"
    return html

@app.route("/feeds", methods=["GET", "POST"])
def feeds():
    if "logged_in" not in session:
        return redirect(url_for("admin"))
    if request.method == "POST":
        save_setting("feeds", request.form["feeds"])
        return redirect(url_for("feeds"))
    feeds = get_feeds()
    return f"<h2>Configurer les flux RSS</h2><form method='post'><textarea name='feeds' rows='5' cols='60'>{chr(10).join(feeds)}</textarea><br><input type='submit' value='Sauvegarder'></form><br><a href='/admin'>Retour admin</a>"

@app.route("/settings", methods=["GET", "POST"])
def settings():
    if "logged_in" not in session:
        return redirect(url_for("admin"))
    if request.method == "POST":
        save_setting("default_image", request.form["default_image"])
        return redirect(url_for("settings"))
    return f"<h2>Paramètres</h2><form method='post'>Image par défaut : <input type='text' name='default_image' value='{get_default_image()}' size='50'><input type='submit' value='Sauvegarder'></form><br><a href='/admin'>Retour admin</a>"

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin"))

@app.route("/feed.xml")
def rss():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT title, content, created_at FROM articles WHERE status='published' ORDER BY created_at DESC")
    articles = c.fetchall()
    conn.close()

    rss = '<?xml version="1.0"?><rss version="2.0"><channel><title>Arménie Info</title>'
    for a in articles:
        rss += f"<item><title>{a[0]}</title><description><![CDATA[{a[1]}]]></description><pubDate>{a[2]}</pubDate></item>"
    rss += "</channel></rss>"
    return rss, {"Content-Type": "application/rss+xml"}

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

# --- Health check ---
@app.route("/health")
def health():
    return "OK"

# --- Run ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
