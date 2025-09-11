from flask import Flask, render_template, request, redirect, url_for, make_response
import sqlite3
import datetime

app = Flask(__name__)

def init_db():
    conn = sqlite3.connect("articles.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            content TEXT,
            published INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

@app.route("/")
def index():
    return "<h1>Bienvenue sur la Console Arménienne</h1><p><a href='/console'>Accéder à la console</a></p>"

@app.route("/console", methods=["GET", "POST"])
def console():
    if request.method == "POST":
        title = request.form["title"]
        content = request.form["content"]
        action = request.form["action"]
        conn = sqlite3.connect("articles.db")
        c = conn.cursor()
        published = 1 if action == "Publier" else 0
        c.execute("INSERT INTO articles (title, content, published, created_at) VALUES (?, ?, ?, ?)",
                  (title, content, published, datetime.datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()
        return redirect(url_for("console"))

    conn = sqlite3.connect("articles.db")
    c = conn.cursor()
    c.execute("SELECT id, title, published FROM articles ORDER BY id DESC")
    articles = c.fetchall()
    conn.close()

    html = "<h2>Console</h2><form method='post'>"
    html += "Titre: <input name='title'><br>Contenu:<br><textarea name='content'></textarea><br>"
    html += "<button type='submit' name='action' value='Brouillon'>Enregistrer brouillon</button>"
    html += "<button type='submit' name='action' value='Publier'>Publier</button></form><hr>"
    html += "<h3>Articles</h3><ul>"
    for a in articles:
        status = "Publié" if a[2] == 1 else "Brouillon"
        html += f"<li>{a[1]} - {status}</li>"
    html += "</ul>"
    return html

@app.route("/rss.xml")
def rss():
    conn = sqlite3.connect("articles.db")
    c = conn.cursor()
    c.execute("SELECT title, content, created_at FROM articles WHERE published=1 ORDER BY id DESC")
    articles = c.fetchall()
    conn.close()

    rss_items = ""
    for a in articles:
        rss_items += f"""
        <item>
            <title>{a[0]}</title>
            <description><![CDATA[{a[1]}]]></description>
            <pubDate>{a[2]}</pubDate>
        </item>
        """

    rss_feed = f"""<?xml version="1.0" encoding="UTF-8" ?>
    <rss version="2.0">
    <channel>
        <title>Console Arménienne - Flux RSS</title>
        <link>https://armenien-console.onrender.com</link>
        <description>Articles publiés depuis la Console Arménienne</description>
        {rss_items}
    </channel>
    </rss>"""

    response = make_response(rss_feed)
    response.headers.set("Content-Type", "application/rss+xml")
    return response

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)
