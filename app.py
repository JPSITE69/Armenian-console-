import os
import sqlite3
import feedparser
import requests
from flask import Flask, request, redirect, url_for, render_template_string, Response, abort
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import openai  # ✅ nouvelle manière d'utiliser OpenAI

# --- Config ---
DB_PATH = os.environ.get("DB_PATH", "console.db")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "armenie")
DEFAULT_IMAGE = os.environ.get("DEFAULT_IMAGE", "https://placehold.co/600x400?text=Armenie+Info")
FEEDS = eval(os.environ.get("FEEDS", "[]"))  # liste JSON de flux
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

openai.api_key = OPENAI_API_KEY  # ✅ correcte pour openai>=1.0

app = Flask(__name__)
scheduler = BackgroundScheduler()
scheduler.start()

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
            pub_date TEXT
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
    """
    Utilise GPT pour traduire + réécrire un article.
    Retourne (titre_fr, contenu_fr).
    """
    if not OPENAI_API_KEY:
        return title, content

    prompt = f"""
    Traduis et réécris en français.
    Mets uniquement le texte révisé, sans explication.

    Titre: {title}
    Contenu: {content}
    """

    response = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7
    )
    text = response.choices[0].message.content.strip()

    # Séparer le titre et le contenu si GPT renvoie les deux
    lines = text.split("\n", 1)
    if len(lines) == 2:
        return lines[0].strip(), lines[1].strip()
    else:
        return title, text

def format_article(title_fr: str, content_fr: str) -> str:
    return f"{title_fr}\n\n{content_fr}\n\n— Arménie Info"

def import_articles():
    for feed_url in FEEDS:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:10]:
            link = entry.link
            if article_exists(link):
                continue
            title = entry.title
            summary = clean_html(getattr(entry, "summary", ""))
            # Scraper page pour contenu complet et image
            image_url = get_image_from_page(link)
            try:
                html = requests.get(link, timeout=5).text
                soup = BeautifulSoup(html, "html.parser")
                paragraphs = [p.get_text() for p in soup.find_all("p")]
                full_text = "\n".join(paragraphs) if paragraphs else summary
            except Exception:
                full_text = summary
            # Réécrire avec GPT
            title_fr, content_fr = rewrite_article(title, full_text)
            final_text = format_article(title_fr, content_fr)
            # Sauvegarder en brouillon
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("""
                INSERT OR IGNORE INTO articles (title, content, image, link, status, pub_date)
                VALUES (?, ?, ?, ?, 'draft', ?)
            """, (title_fr, final_text, image_url, link, datetime.utcnow().isoformat()))
            conn.commit()
            conn.close()

# --- Routes ---
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

@app.route("/admin", methods=["GET", "POST"])
def admin():
    password = request.args.get("password")
    if password != ADMIN_PASS:
        return abort(403)

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
    cur.execute("SELECT id, title, status FROM articles ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()

    html = """
    <h1>Admin Arménie Info</h1>
    <a href="{{ url_for('admin', password=password, action='import') }}">Importer articles</a>
    <ul>
    {% for id, title, status in rows %}
      <li>
        <b>[{{ status }}]</b> {{ title }}
        <a href="{{ url_for('admin', password=password, action='publish', id=id) }}">Publier</a>
        <a href="{{ url_for('admin', password=password, action='delete', id=id) }}">Supprimer</a>
      </li>
    {% endfor %}
    </ul>
    """
    return render_template_string(html, rows=rows, password=password)

@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
