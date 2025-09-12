import os
import sqlite3
from flask import Flask, request, redirect, url_for, render_template_string, session
from datetime import datetime, timezone

# -------------------------
# CONFIGURATION
# -------------------------
APP_NAME = "Console Arménienne"
ADMIN_PASS = os.environ.get("ADMIN_PASS", "armenie")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-moi")

# Chemin base SQLite (dans disque persistant Render)
DB_PATH = os.environ.get("DB_PATH", "/var/data/data.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)  # assure que le dossier existe

# -------------------------
# APP
# -------------------------
app = Flask(__name__)
app.secret_key = SECRET_KEY

# -------------------------
# BASE DE DONNÉES
# -------------------------
def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    c = con.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            body TEXT,
            image_url TEXT,
            published_at TEXT,
            status TEXT DEFAULT 'draft'
        );
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    con.commit()
    con.close()

# Init DB au démarrage
try:
    init_db()
except Exception as e:
    app.logger.exception("Erreur init_db")

# -------------------------
# ROUTES
# -------------------------

@app.route("/")
def index():
    return "<h1>Bienvenue sur la Console Arménienne</h1><p>Serveur Flask opérationnel ✅</p>"

@app.route("/health")
def health():
    try:
        con = db()
        con.execute("SELECT 1")
        return "OK", 200
    except Exception as e:
        app.logger.exception("healthcheck db error")
        return "DB ERROR", 500

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if "logged_in" not in session:
        if request.method == "POST":
            if request.form.get("password") == ADMIN_PASS:
                session["logged_in"] = True
                return redirect(url_for("admin"))
            return "Mot de passe incorrect", 403
        return """
        <form method="post">
            <input type="password" name="password" placeholder="Mot de passe">
            <button type="submit">Connexion</button>
        </form>
        """

    return """
    <h2>Connexion réussie ✅</h2>
    <p>Bienvenue dans l’interface admin.</p>
    """

@app.route("/publish/<int:post_id>")
def publish(post_id):
    con = db()
    c = con.cursor()
    c.execute("SELECT * FROM posts WHERE id=?", (post_id,))
    post = c.fetchone()
    if not post:
        return "Post introuvable", 404

    # Ajout d’un saut de ligne entre titre et contenu
    full_text = f"{post['title']}\n\n{post['body']}\n\n- Arménie Info"

    return f"""
    <h1>{post['title']}</h1>
    <p>{post['body'].replace(chr(10), "<br>")}</p>
    <hr>
    <i>Publié par Arménie Info</i>
    """

# -------------------------
# MAIN
# -------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
