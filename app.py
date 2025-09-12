import os
import sqlite3
import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, redirect, url_for, session
from openai import OpenAI
from datetime import datetime

# --- Initialisation Flask ---
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "secret")

# --- Variables ---
ADMIN_PASS = os.getenv("ADMIN_PASS", "armenie")
DB_PATH = os.getenv("DB_PATH", "console.db")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# --- Client OpenAI ---
client = OpenAI(api_key=OPENAI_API_KEY)

# --- Création DB ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS articles
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  title TEXT,
                  content TEXT,
                  image TEXT,
                  status TEXT,
                  publish_at TEXT,
                  created_at TEXT)''')
    conn.commit()
    conn.close()

init_db()

# --- Utilitaires ---
def fetch_image_from_url(url):
    """Récupère la première image trouvée sur un article"""
    try:
        r = requests.get(url, timeout=5)
        soup = BeautifulSoup(r.text, "html.parser")
        img = soup.find("img")
        if img and img.get("src"):
            return img["src"]
    except:
        pass
    return None

def rewrite_article(title, content):
    """Traduction + réécriture via OpenAI"""
    if not OPENAI_API_KEY:
        return f"{title}\n\n{content}\n\n(⚠️ Pas de clé API définie)"

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
        return f"{title}\n\n{content}\n\n(Erreur GPT : {e})"

def save_article(title, content, image, status="draft", publish_at=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO articles (title, content, image, status, publish_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
              (title, content, image, status, publish_at, datetime.now().isoformat()))
    conn.commit()
    conn.close()

# --- Routes publiques ---
@app.route("/")
def index():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, content, image, publish_at FROM articles WHERE status='published' ORDER BY created_at DESC")
    articles = c.fetchall()
    conn.close()

    html = "<h1>Arménie Info</h1>"
    for a in articles:
        img_html = f"<img src='{a[3]}' width='300'><br>" if a[3] else ""
        date_html = f"<em>📅 {a[4]}</em><br>" if a[4] else ""
        html += f"<h2>{a[1]}</h2>{date_html}{img_html}<p>{a[2]}</p><hr>"
    return html

# --- Admin ---
@app.route("/admin", methods=["GET", "POST"])
def admin():
    if "logged_in" not in session:
        if request.method == "POST" and request.form.get("password") == ADMIN_PASS:
            session["logged_in"] = True
            return redirect(url_for("admin"))
        return "<h2>Connexion admin</h2><form method='post'><input type='password' name='password'><input type='submit' value='Entrer'></form>"

    action = request.args.get("action")

    # Importation des articles
    if action == "import":
        feeds = [
            "https://www.civilnet.am/feed/",
            "https://armtimes.com/hy/rss",
            "https://civic.am/feed/"
        ]
        for feed_url in feeds:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:5]:
                image = fetch_image_from_url(entry.link)
                rewritten = rewrite_article(entry.title, entry.get("summary", ""))
                save_article(entry.title, rewritten, image)
        return redirect(url_for("admin"))

    # Publication
    if action == "publish":
        aid = request.args.get("id")
        publish_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE articles SET status='published', publish_at=? WHERE id=?", (publish_date, aid))
        conn.commit()
        conn.close()
        return redirect(url_for("admin"))

    # Suppression
    if action == "delete":
        aid = request.args.get("id")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM articles WHERE id=?", (aid,))
        conn.commit()
        conn.close()
        return redirect(url_for("admin"))

    # Edition
    if action == "edit":
        aid = request.args.get("id")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, title, content, image, publish_at FROM articles WHERE id=?", (aid,))
        art = c.fetchone()
        conn.close()

        if request.method == "POST":
            new_title = request.form["title"]
            new_content = request.form["content"]
            new_image = request.form["image"]
            new_publish_at = request.form["publish_at"]
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("UPDATE articles SET title=?, content=?, image=?, publish_at=? WHERE id=?",
                      (new_title, new_content, new_image, new_publish_at, aid))
            conn.commit()
            conn.close()
            return redirect(url_for("admin"))

        return f"""
        <h2>Modifier article</h2>
        <form method="post">
          Titre : <input type="text" name="title" value="{art[1]}"><br>
          Contenu : <textarea name="content" rows="10" cols="60">{art[2]}</textarea><br>
          Image URL : <input type="text" name="image" value="{art[3]}"><br>
          Publier à (YYYY-MM-DD HH:MM:SS) : <input type="text" name="publish_at" value="{art[4] or ''}"><br>
          <input type="submit" value="Enregistrer">
        </form>
        """

    # Liste des articles
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, status, publish_at FROM articles ORDER BY created_at DESC")
    articles = c.fetchall()
    conn.close()

    html = "<h1>Admin Arménie Info</h1>"
    html += "<a href='?action=import'>📥 Importer articles</a> | "
    html += "<a href='/logout'>🚪 Déconnexion</a><hr>"

    for a in articles:
        html += f"[{a[2]}] {a[1]} ({a[3] or '—'}) - "
        html += f"<a href='?action=publish&id={a[0]}'>Publier</a> | "
        html += f"<a href='?action=edit&id={a[0]}'>Modifier</a> | "
        html += f"<a href='?action=delete&id={a[0]}'>Supprimer</a><br>"
    return html

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin"))

# --- Healthcheck ---
@app.route("/health")
def health():
    return "OK"

# --- Lancement ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
