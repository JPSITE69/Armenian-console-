import os
import sqlite3
import feedparser
import requests
from flask import Flask, request, redirect, url_for, render_template_string, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "secret")

# DB
DB_PATH = os.environ.get("DB_PATH", "console.db")

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
        status TEXT DEFAULT 'draft'
    )
    """)
    conn.commit()
    conn.close()

init_db()

# Admin password
ADMIN_PASS = os.environ.get("ADMIN_PASS", "armenie")

# Home
@app.route("/")
def home():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, content, image FROM articles WHERE status='published' ORDER BY id DESC")
    articles = c.fetchall()
    conn.close()

    html = "<h1>Arménie Info</h1>"
    for art in articles:
        html += f"<h2>{art[1]}</h2>"
        if art[3]:
            html += f'<img src="{art[3]}" width="300"><br>'
        html += f"<p>{art[2]}</p><hr>"
    return html

# Health check
@app.route("/health")
def health():
    return "OK", 200

# Login admin
@app.route("/admin", methods=["GET", "POST"])
def admin():
    if "logged_in" not in session:
        if request.method == "POST":
            if request.form.get("password") == ADMIN_PASS:
                session["logged_in"] = True
                return redirect(url_for("admin"))
            return "Mot de passe incorrect"
        return """
        <form method="post">
            <input type="password" name="password" placeholder="Mot de passe">
            <input type="submit" value="Connexion">
        </form>
        """
    # Menu admin
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, status FROM articles ORDER BY id DESC")
    articles = c.fetchall()
    conn.close()

    html = "<h1>Admin Arménie Info</h1>"
    html += '<a href="/import">Importer articles</a> | '
    html += '<a href="/feeds">Configurer les flux RSS</a> | '
    html += '<a href="/logout">Se déconnecter</a><br><br>'
    for art in articles:
        html += f"[{art[2]}] {art[1]} "
        html += f'<a href="/publish/{art[0]}">Publier</a> '
        html += f'<a href="/delete/{art[0]}">Supprimer</a><br>'
    return html

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

# Config RSS feeds
@app.route("/feeds", methods=["GET", "POST"])
def feeds():
    if "logged_in" not in session:
        return redirect(url_for("admin"))

    feeds_file = "feeds.txt"

    if request.method == "POST":
        data = request.form.get("feeds", "")
        with open(feeds_file, "w") as f:
            f.write(data)
        return redirect(url_for("feeds"))

    feeds_data = ""
    if os.path.exists(feeds_file):
        with open(feeds_file, "r") as f:
            feeds_data = f.read()

    return f"""
    <h1>Configurer les flux RSS</h1>
    <form method="post">
        <textarea name="feeds" rows="10" cols="60">{feeds_data}</textarea><br>
        <input type="submit" value="Sauvegarder">
    </form>
    <a href="/admin">Retour admin</a>
    """

# Import RSS
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
            image = ""

            # chercher image si dispo
            if "media_content" in entry:
                image = entry.media_content[0].get("url", "")
            elif "links" in entry:
                for l in entry.links:
                    if l.get("type", "").startswith("image"):
                        image = l.get("href", "")

            # insérer si non existant
            c.execute("SELECT id FROM articles WHERE link=?", (link,))
            if not c.fetchone():
                c.execute("INSERT INTO articles (title, link, content, image, status) VALUES (?, ?, ?, ?, ?)",
                          (title, link, content, image, "draft"))

    conn.commit()
    conn.close()

    return redirect(url_for("admin"))

# Publier un article
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

# Supprimer un article
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

# Lancer app pour Render
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
