import os
import json
import sqlite3
import requests
from flask import Flask, request, redirect, url_for, render_template_string, session
from bs4 import BeautifulSoup
import feedparser
import openai

# -------------------------------
# Config
# -------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "changeme")

DB_PATH = os.environ.get("DB_PATH", "console.db")
DEFAULT_IMAGE = "https://placehold.co/600x400?text=Armenie+Info"

SETTINGS_FILE = "settings.json"


# -------------------------------
# Helpers
# -------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            content TEXT,
            image TEXT,
            status TEXT,
            publish_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_settings(data):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f)


def get_openai_key():
    # 1) Render ENV
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ.get("OPENAI_API_KEY")
    # 2) settings.json
    settings = load_settings()
    return settings.get("OPENAI_API_KEY")


def rewrite_article(title, content):
    api_key = get_openai_key()
    if not api_key:
        return title, content  # fallback : pas de GPT
    openai.api_key = api_key

    prompt = f"""
    Traduis et réécris l'article suivant en français clair.
    Mets le titre sur une ligne, saute une ligne, puis le contenu, puis une signature.

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
        if "\n" in text:
            parts = text.split("\n", 1)
            new_title = parts[0].strip()
            new_content = parts[1].strip() + "\n\n— Arménie Info"
            return new_title, new_content
        else:
            return title, content + "\n\n— Arménie Info"
    except Exception as e:
        print("Erreur GPT:", e)
        return title, content


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

        imgs = soup.find_all("img")
        for img in imgs:
            src = img.get("src")
            if src and src.startswith("http"):
                return src
    except Exception as e:
        print("Erreur image:", e)

    return default_image


def get_feeds():
    if os.path.exists("feeds.txt"):
        with open("feeds.txt") as f:
            return [line.strip() for line in f.readlines() if line.strip()]
    return []


# -------------------------------
# Routes
# -------------------------------
@app.route("/")
def index():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, content, image FROM articles WHERE status='published' ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()

    html = "<h1>Arménie Info</h1>"
    for row in rows:
        html += f"<h2>{row[1]}</h2>"
        if row[3]:
            html += f'<img src="{row[3]}" style="max-width:600px"><br>'
        html += f"<p>{row[2]}</p><hr>"
    return html


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if not session.get("logged_in"):
        if request.method == "POST":
            if request.form.get("password") == os.environ.get("ADMIN_PASS", "armenie"):
                session["logged_in"] = True
                return redirect(url_for("admin"))
        return """
        <h1>Connexion Admin</h1>
        <form method="post">
            <input type="password" name="password" placeholder="Mot de passe"/>
            <button type="submit">Se connecter</button>
        </form>
        """

    action = request.args.get("action")

    if action == "import":
        feeds = get_feeds()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for feed_url in feeds:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                title = entry.title
                link = entry.link
                content = entry.get("summary", "")
                image = get_image_from_page(link)

                # traduction
                title, content = rewrite_article(title, content)

                c.execute("INSERT INTO articles (title, content, image, status) VALUES (?, ?, ?, ?)",
                          (title, content, image, "draft"))
        conn.commit()
        conn.close()
        return redirect(url_for("admin"))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, status FROM articles ORDER BY id DESC")
    articles = c.fetchall()
    conn.close()

    html = "<h1>Admin Arménie Info</h1>"
    html += '<a href="?action=import">Importer articles</a> | '
    html += '<a href="/feeds">Configurer les flux RSS</a> | '
    html += '<a href="/settings">Paramètres</a> | '
    html += '<a href="/logout">Se déconnecter</a><hr>'

    for art in articles:
        html += f"[{art[2]}] {art[1]} "
        html += f'<a href="/publish/{art[0]}">Publier</a> '
        html += f'<a href="/edit/{art[0]}">Modifier</a> '
        html += f'<a href="/delete/{art[0]}">Supprimer</a><br>'
    return html


@app.route("/publish/<int:art_id>")
def publish(art_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE articles SET status='published' WHERE id=?", (art_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin"))


@app.route("/delete/<int:art_id>")
def delete(art_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM articles WHERE id=?", (art_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin"))


@app.route("/edit/<int:art_id>", methods=["GET", "POST"])
def edit(art_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if request.method == "POST":
        title = request.form.get("title")
        content = request.form.get("content")
        image = request.form.get("image")
        c.execute("UPDATE articles SET title=?, content=?, image=? WHERE id=?", (title, content, image, art_id))
        conn.commit()
        conn.close()
        return redirect(url_for("admin"))

    c.execute("SELECT title, content, image FROM articles WHERE id=?", (art_id,))
    art = c.fetchone()
    conn.close()

    html = f"""
    <h1>Modifier article</h1>
    <form method="post">
        Titre:<br><input type="text" name="title" value="{art[0]}" size="80"><br>
        Contenu:<br><textarea name="content" rows="15" cols="80">{art[1]}</textarea><br>
        Image URL:<br><input type="text" name="image" value="{art[2]}" size="80"><br>
        <button type="submit">Sauvegarder</button>
    </form>
    """
    return html


@app.route("/feeds", methods=["GET", "POST"])
def manage_feeds():
    if not session.get("logged_in"):
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
    <a href="/admin">Retour admin</a>
    """
    return render_template_string(html, feeds_text="\n".join(feeds_list))


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if not session.get("logged_in"):
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
    <a href="/admin">Retour admin</a>
    """
    return render_template_string(html, current_key=settings.get("OPENAI_API_KEY", ""))


@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    return redirect(url_for("admin"))


# -------------------------------
# Lancement
# -------------------------------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)
