import os, sqlite3, hashlib, io, re, traceback, json
from datetime import datetime
from urllib.parse import urljoin
import requests, feedparser
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError
from flask import Flask, request, redirect, url_for, render_template_string, session, flash
from apscheduler.schedulers.background import BackgroundScheduler
from openai import OpenAI

# Configuration
APP_NAME = "Console Arménienne"
ADMIN_PASS = os.environ.get("ADMIN_PASS", "armenien")
DB_PATH = os.environ.get("DB_PATH", "console.db")
SECRET_KEY = os.environ.get("SECRET_KEY", "default")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

client = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__)
app.secret_key = SECRET_KEY

# Templates (réduits ici pour clarté)
HTML_INDEX = "<h1>Bienvenue dans l'interface admin.</h1>"
HTML_EDIT = '''
<h2>Modifier l’article</h2>
<form method="post">
    <input type="text" name="title" value="{{ title }}"><br><br>
    <textarea name="content" rows="20" cols="80">{{ content }}</textarea><br>
    <button type="submit">Mettre à jour</button>
</form>
'''

# Init DB
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS articles
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  title TEXT, content TEXT, image TEXT,
                  status TEXT, created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)''')
    conn.commit()
    conn.close()

init_db()

# Traduction forcée via GPT
def rewrite_article(title, content):
    prompt = f"Traduis et reformule en français cet article :\n\nTitre : {title}\n\nContenu : {content}"
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Erreur de réécriture : {e}"

# Image scraping
def fetch_image(url):
    try:
        r = requests.get(url, timeout=6)
        soup = BeautifulSoup(r.text, 'html.parser')
        img = soup.find('img')
        if img and img.get('src'):
            return urljoin(url, img['src'])
    except:
        return None
    return None

# Routes

@app.route("/health")
def health():
    return "OK"

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if session.get("admin"):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, title, content, status FROM articles ORDER BY created_at DESC")
        rows = c.fetchall()
        conn.close()
        html = "<h1>Console admin</h1>"
        for row in rows:
            html += f"<hr><b>{row[1]}</b><br>Status: {row[3]}<br>"
            html += f"<a href='/edit/{row[0]}'>Modifier</a> | "
            html += f"<a href='/publish/{row[0]}'>✅ Publier</a> | "
            html += f"<a href='/delete/{row[0]}'>❌ Supprimer</a>"
        return html
    else:
        if request.method == "POST" and request.form.get("password") == ADMIN_PASS:
            session["admin"] = True
            return redirect(url_for("admin"))
        return '''
            <form method="post">
                Mot de passe admin : <input type="password" name="password">
                <input type="submit" value="Connexion">
            </form>
        '''

@app.route("/edit/<int:article_id>", methods=["GET", "POST"])
def edit(article_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if request.method == "POST":
        c.execute("UPDATE articles SET title = ?, content = ? WHERE id = ?",
                  (request.form["title"], request.form["content"], article_id))
        conn.commit()
        conn.close()
        return redirect(url_for("admin"))
    else:
        c.execute("SELECT title, content FROM articles WHERE id = ?", (article_id,))
        row = c.fetchone()
        conn.close()
        return render_template_string(HTML_EDIT, title=row[0], content=row[1])

@app.route("/publish/<int:article_id>")
def publish(article_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE articles SET status = 'published' WHERE id = ?", (article_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin"))

@app.route("/delete/<int:article_id>")
def delete(article_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM articles WHERE id = ?", (article_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin"))

@app.route("/feed.xml")
def feed():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT title, content, created_at FROM articles WHERE status = 'published' ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()

    feed = '<?xml version="1.0" encoding="UTF-8" ?><rss version="2.0"><channel><title>Arménie Info</title>'
    for r in rows:
        feed += f"<item><title>{r[0]}</title><description>{r[1]}</description><pubDate>{r[2]}</pubDate></item>"
    feed += "</channel></rss>"
    return feed, {"Content-Type": "application/xml"}

@app.route("/feeds", methods=["GET", "POST"])
def feeds():
    if not session.get("admin"): return redirect(url_for("admin"))
    if request.method == "POST":
        url = request.form["url"]
        feed = feedparser.parse(url)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for entry in feed.entries:
            title = entry.get("title", "")
            link = entry.get("link", "")
            content = entry.get("summary", "")
            image = fetch_image(link)
            image = image or "/static/default.jpg"
            rewritten = rewrite_article(title, content)
            c.execute("SELECT COUNT(*) FROM articles WHERE title = ?", (title,))
            if c.fetchone()[0] == 0:
                c.execute("INSERT INTO articles (title, content, image, status, created_at) VALUES (?, ?, ?, 'draft', ?)",
                          (title, rewritten + "\n\nArménie Info", image, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return redirect(url_for("admin"))
    return '''
        <h2>Ajouter un flux RSS</h2>
        <form method="post">
            URL du flux : <input name="url">
            <input type="submit" value="Importer">
        </form>
    '''

if __name__ == "__main__":
    app.run(debug=True)
