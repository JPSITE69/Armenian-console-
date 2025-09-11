from flask import Flask, request, redirect, url_for, Response, render_template_string, session, flash
import sqlite3, os
from datetime import datetime, timezone

# ---------------- CONFIG ----------------
APP_NAME = "Console Arménienne"
ADMIN_PASS = os.environ.get("ADMIN_PASS", "armenie")  # mot de passe par défaut
SECRET_KEY = os.environ.get("SECRET_KEY", "change-moi")  # clé secrète pour sessions
DB = "data.db"

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ---------------- BASE DE DONNÉES ----------------
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    c = db().cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS posts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            body TEXT,
            status TEXT, 
            created_at TEXT
        )
    """)
    c.close()

@app.before_first_request
def _ensure_db():
    init_db()

# ---------------- ROUTES PUBLIQUES ----------------
@app.route("/")
def index():
    con = db()
    posts = con.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC").fetchall()
    return render_template_string("""
        <h1>{{ appname }}</h1>
        <ul>
        {% for p in posts %}
            <li><b>{{p['title']}}</b> - {{p['body']}}</li>
        {% endfor %}
        </ul>
        <p><a href="/rss.xml">Flux RSS</a></p>
    """, posts=posts, appname=APP_NAME)

@app.route("/rss.xml")
def rss():
    con = db()
    posts = con.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC").fetchall()
    rss_items = "".join([
        f"<item><title>{p['title']}</title><description>{p['body']}</description></item>"
        for p in posts
    ])
    rss_feed = f"""<?xml version="1.0"?>
    <rss version="2.0"><channel>
    <title>{APP_NAME}</title>
    {rss_items}
    </channel></rss>"""
    return Response(rss_feed, mimetype="application/rss+xml")

# ---------------- CONSOLE ADMIN ----------------
@app.route("/admin", methods=["GET","POST"])
def admin():
    if "auth" not in session:
        if request.method == "POST":
            if request.form.get("password") == ADMIN_PASS:
                session["auth"] = True
                return redirect(url_for("admin"))
            else:
                flash("Mot de passe incorrect")
        return """
            <h2>Connexion</h2>
            <form method="post">
                <input type="password" name="password" placeholder="Mot de passe">
                <button type="submit">Entrer</button>
            </form>
        """

    con = db()
    if request.method == "POST":
        title = request.form.get("title")
        body = request.form.get("body")
        status = request.form.get("status", "draft")
        con.execute("INSERT INTO posts(title, body, status, created_at) VALUES (?,?,?,?)",
                    (title, body, status, datetime.now(timezone.utc).isoformat()))
        con.commit()
        return redirect(url_for("admin"))

    posts = con.execute("SELECT * FROM posts ORDER BY id DESC").fetchall()
    return render_template_string("""
        <h1>Console {{ appname }}</h1>
        <form method="post">
            <input name="title" placeholder="Titre"><br>
            <textarea name="body" placeholder="Texte"></textarea><br>
            <select name="status">
              <option value="draft">Brouillon</option>
              <option value="published">Publié</option>
            </select>
            <button type="submit">Ajouter</button>
        </form>
        <h2>Articles</h2>
        <ul>
        {% for p in posts %}
          <li>[{{p['status']}}] <b>{{p['title']}}</b> - {{p['body']}}</li>
        {% endfor %}
        </ul>
        <p><a href="/">← Retour site</a></p>
    """, posts=posts, appname=APP_NAME)

# ---------------- LANCEMENT ----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
    
