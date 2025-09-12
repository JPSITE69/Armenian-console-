import os
import sqlite3
import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, redirect, url_for, render_template_string, session
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from openai import OpenAI

# -------------------------
# Config
# -------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "secret")

DB_PATH = os.environ.get("DB_PATH", "console.db")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "armenie")
DEFAULT_IMAGE = os.environ.get("DEFAULT_IMAGE", "https://placehold.co/600x400?text=Armenie+Info")

# OpenAI client
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Scheduler
scheduler = BackgroundScheduler()
scheduler.start()

# -------------------------
# DB init
# -------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        link TEXT,
        content TEXT,
        image TEXT,
        status TEXT DEFAULT 'draft',
        publish_at TEXT
    )
    """)
    conn.commit()
    conn.close()

init_db()

# -------------------------
# Utils
# -------------------------
def rewrite_article(title, content):
    if not client:
        return f"{title}\n\n{content}\n\nArménie Info"

    try:
        prompt = f"""Traduis et réécris en français l'article suivant.
Titre: {title}
Contenu: {content}

Format attendu :
[Titre en français]

[Contenu réécrit en français]

Arménie Info
"""
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print("⚠️ Erreur OpenAI:", e)
        return f"{title}\n\n{content}\n\nArménie Info"

def get_image_from_page(url):
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
    except Exception as e:
        print("⚠️ Erreur scraping image:", e)

    return DEFAULT_IMAGE

def auto_publish():
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE articles SET status='published' WHERE status='draft' AND publish_at IS NOT NULL AND publish_at <= ?", (now,))
    conn.commit()
    conn.close()

scheduler.add_job(auto_publish, "interval", minutes=1)

# -------------------------
# Routes publiques
# -------------------------
@app.route("/")
def home():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT title, content, image FROM articles WHERE status='published' ORDER BY id DESC")
    articles = c.fetchall()
    conn.close()

    html = "<h1>Arménie Info</h1>"
    for art in articles:
        html += f"<h2>{art[0]}</h2>"
        if art[2]:
            html += f'<img src="{art[2]}" width="400"><br>'
        html += f"<p>{art[1]}</p><hr>"
    return html

@app.route("/health")
def health():
    return "OK", 200

# -------------------------
# Admin
# -------------------------
@app.route("/admin", methods=["GET", "POST"])
def admin():
    if "logged_in" not in session:
        if request.method == "POST":
            if request.form.get("password") == ADMIN_PASS:
                session["logged_in"] = True
                return redirect(url_for("admin"))
            return "Mot de passe incorrect"
        return """
        <h1>Connexion Admin</h1>
        <form method="post">
            <input type="password" name="password" placeholder="Mot de passe">
            <input type="submit" value="Connexion">
        </form>
        """

    # Liste articles
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, status, publish_at FROM articles ORDER BY id DESC")
    articles = c.fetchall()
    conn.close()

    html = "<h1>Admin Arménie Info</h1>"
    html += '<a href="/import">Importer articles</a> | '
    html += '<a href="/feeds">Configurer flux RSS</a> | '
    html += '<a href="/logout">Se déconnecter</a><br><br>'
    for art in articles:
        html += f"[{art[2]}] {art[1]} "
        if art[3]:
            html += f"(Planifié: {art[3]}) "
        html += f'<a href="/publish/{art[0]}">Publier</a> '
        html += f'<a href="/edit/{art[0]}">Modifier</a> '
        html += f'<a href="/delete/{art[0]}">Supprimer</a><br>'
    return html

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin"))

# -------------------------
# Gestion RSS
# -------------------------
@app.route("/feeds", methods=["GET", "POST"])
def feeds():
    if "logged_in" not in session:
        return redirect(url_for("admin"))

    feeds_file = "feeds.txt"
    if request.method == "POST":
        with open(feeds_file, "w") as f:
            f.write(request.form.get("feeds", ""))
        return redirect(url_for("feeds"))

    data = ""
    if os.path.exists(feeds_file):
        with open(feeds_file, "r") as f:
            data = f.read()

    return f"""
    <h1>Configurer les flux RSS</h1>
    <form method="post">
        <textarea name="feeds" rows="10" cols="60">{data}</textarea><br>
        <input type="submit" value="Sauvegarder">
    </form>
    <a href="/admin">Retour admin</a>
    """

@app.route("/import")
def import_articles():
    if "logged_in" not in session:
        return redirect(url_for("admin"))

    feeds_file = "feeds.txt"
    if not os.path.exists(feeds_file):
        return "Aucun flux RSS configuré."

    with open(feeds_file, "r") as f:
        feeds = f.read().splitlines()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    for feed in feeds:
        parsed = feedparser.parse(feed)
        for entry in parsed.entries:
            title = entry.get("title", "")
            link = entry.get("link", "")
            content = entry.get("summary", "")

            # Vérif doublon
            c.execute("SELECT id FROM articles WHERE link=?", (link,))
            if c.fetchone():
                continue

            # Réécriture + traduction
            rewritten = rewrite_article(title, content)

            # Image
            image = get_image_from_page(link)

            c.execute("INSERT INTO articles (title, link, content, image, status) VALUES (?, ?, ?, ?, ?)",
                      (title, link, rewritten, image, "draft"))

    conn.commit()
    conn.close()
    return redirect(url_for("admin"))

# -------------------------
# Articles
# -------------------------
@app.route("/publish/<int:article_id>")
def publish(article_id):
    if "logged_in" not in session:
        return redirect(url_for("admin"))
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE articles SET status='published' WHERE id=?", (article_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin"))

@app.route("/delete/<int:article_id>")
def delete(article_id):
    if "logged_in" not in session:
        return redirect(url_for("admin"))
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM articles WHERE id=?", (article_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin"))

@app.route("/edit/<int:article_id>", methods=["GET", "POST"])
def edit(article_id):
    if "logged_in" not in session:
        return redirect(url_for("admin"))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if request.method == "POST":
        title = request.form.get("title")
        content = request.form.get("content")
        image = request.form.get("image")
        publish_at = request.form.get("publish_at") or None
        c.execute("UPDATE articles SET title=?, content=?, image=?, publish_at=? WHERE id=?",
                  (title, content, image, publish_at, article_id))
        conn.commit()
        conn.close()
        return redirect(url_for("admin"))

    c.execute("SELECT title, content, image, publish_at FROM articles WHERE id=?", (article_id,))
    art = c.fetchone()
    conn.close()

    html = f"""
    <h1>Modifier article</h1>
    <form method="post">
        Titre:<br><input type="text" name="title" value="{art[0]}" size="80"><br>
        Contenu:<br><textarea name="content" rows="15" cols="80">{art[1]}</textarea><br>
        Image:<br><input type="text" name="image" value="{art[2]}" size="80"><br>
        Date publication (UTC ex: 2025-09-13T12:00:00):<br>
        <input type="text" name="publish_at" value="{art[3] or ''}" size="40"><br>
        <button type="submit">Sauvegarder</button>
    </form>
    <a href="/admin">Retour admin</a>
    """
    return html

# -------------------------
# Lancement Render
# -------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
