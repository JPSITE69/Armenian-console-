from flask import Flask, request, redirect, url_for, Response, render_template_string, session, flash
import sqlite3, os
from datetime import datetime, timezone

APP_NAME   = "Console Arménienne"
ADMIN_PASS = os.environ.get("ADMIN_PASS", "armenie")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")
DB_PATH    = "site.db"

app = Flask(__name__)
app.secret_key = SECRET_KEY

# --- DB helpers ---
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.execute("""
      CREATE TABLE IF NOT EXISTS posts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        body  TEXT,
        status TEXT DEFAULT 'draft',
        created_at TEXT,
        updated_at TEXT
      )
    """)
    con.commit(); con.close()

# créer la base au démarrage
init_db()

# --- Layout commun ---
LAYOUT = """
<!doctype html><meta charset="utf-8">
<title>{{title}}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
<body class="container">
<nav>
  <ul><li><strong>{{appname}}</strong></li></ul>
  <ul>
    <li><a href="{{ url_for('home') }}">Accueil</a></li>
    <li><a href="{{ url_for('rss_xml') }}" target="_blank">RSS</a></li>
    {% if session.get('ok') %}
      <li><a href="{{ url_for('admin') }}">Admin</a></li>
      <li><a href="{{ url_for('logout') }}">Déconnexion</a></li>
    {% else %}
      <li><a href="{{ url_for('admin') }}">Connexion</a></li>
    {% endif %}
  </ul>
</nav>
<main>
  {% with m=get_flashed_messages() %}{% if m %}<article>{% for x in m %}<p>{{x}}</p>{% endfor %}</article>{% endif %}{% endwith %}
  {{ body|safe }}
</main>
<footer><small>&copy; {{year}} — {{appname}}</small></footer>
</body>"""
def page(body, title=""):
    return render_template_string(LAYOUT, body=body, title=title or APP_NAME,
                                 appname=APP_NAME, year=datetime.now().year)

# --- Public ---
@app.get("/")
def home():
    rows = db().execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC LIMIT 50").fetchall()
    if not rows:
        return page("<h2>Dernières publications</h2><p>Aucune publication pour l’instant.</p>")
    cards = [f"<article><h3>{r['title']}</h3><p>{(r['body'] or '').replace(chr(10), '<br>')}</p></article>" for r in rows]
    return page("<h2>Dernières publications</h2>" + "".join(cards))

@app.get("/rss.xml")
def rss_xml():
    rows = db().execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC LIMIT 100").fetchall()
    items = []
    for r in rows:
        title = (r["title"] or "").replace("&","&amp;")
        desc  = (r["body"] or "").replace("&","&amp;")
        pub   = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S %z')
        items.append(f"<item><title>{title}</title><description><![CDATA[{desc}]]></description><pubDate>{pub}</pubDate></item>")
    rss = f"<?xml version='1.0'?><rss version='2.0'><channel><title>{APP_NAME}</title>{''.join(items)}</channel></rss>"
    return Response(rss, mimetype="application/rss+xml")

# --- Admin ---
@app.route("/admin", methods=["GET","POST"])
def admin():
    if request.method == "POST" and not session.get("ok"):
        if request.form.get("password") == ADMIN_PASS:
            session["ok"] = True
            return redirect(url_for("admin"))
        flash("Mot de passe incorrect."); return redirect(url_for("admin"))

    if not session.get("ok"):
        return page("""<h3>Connexion</h3><form method="post">
          <input type="password" name="password" placeholder="Mot de passe" required>
          <button>Entrer</button></form>""", "Connexion")

    con = db()
    drafts = con.execute("SELECT * FROM posts WHERE status='draft'").fetchall()
    pubs   = con.execute("SELECT * FROM posts WHERE status='published'").fetchall()
    con.close()

    body = "<h3>Console</h3><form method='post' action='/create'><input name='title' placeholder='Nouveau titre'><button>Créer</button></form>"
    body += "<h4>Brouillons</h4>" + "".join([f"<p>{r['title']} <a href='/publish/{r['id']}'>Publier</a></p>" for r in drafts]) or "<p>Aucun brouillon.</p>"
    body += "<h4>Publiés</h4>" + "".join([f"<p>{r['title']} <a href='/unpublish/{r['id']}'>Dépublier</a></p>" for r in pubs]) or "<p>Rien de publié.</p>"
    body += f"<p>Flux RSS : <code>{request.url_root}rss.xml</code></p>"
    return page(body)

@app.post("/create")
def create():
    if not session.get("ok"): return redirect(url_for("admin"))
    now = datetime.now().isoformat(timespec="minutes")
    con = db()
    con.execute("INSERT INTO posts(title, body, status, created_at, updated_at) VALUES(?,?,?,?,?)",
                (request.form.get("title",""), "", "draft", now, now))
    con.commit(); con.close()
    return redirect(url_for("admin"))

@app.get("/publish/<int:pid>")
def publish(pid):
    if not session.get("ok"): return redirect(url_for("admin"))
    con = db(); con.execute("UPDATE posts SET status='published' WHERE id=?", (pid,)); con.commit(); con.close()
    return redirect(url_for("admin"))

@app.get("/unpublish/<int:pid>")
def unpublish(pid):
    if not session.get("ok"): return redirect(url_for("admin"))
    con = db(); con.execute("UPDATE posts SET status='draft' WHERE id=?", (pid,)); con.commit(); con.close()
    return redirect(url_for("admin"))

@app.get("/logout")
def logout():
    session.clear(); return redirect(url_for("home"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
