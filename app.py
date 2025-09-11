from flask import Flask, request, redirect, url_for, Response, render_template_string, session, flash
import sqlite3, os
from datetime import datetime, timezone

# ---------- CONFIG ----------
APP_NAME   = "Console Arménienne"
ADMIN_PASS = os.environ.get("ADMIN_PASS", "armenie")          # change-le plus tard dans Render > Environment
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")        # clé secrète pour sessions
DB_PATH    = "site.db"

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ---------- DB HELPERS ----------
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
        status TEXT DEFAULT 'draft',  -- 'draft' | 'published'
        created_at TEXT,
        updated_at TEXT
      )
    """)
    con.commit()
    con.close()

def ensure_db():
    try:
        con = db()
        con.execute("SELECT 1 FROM posts LIMIT 1")
        con.close()
    except Exception:
        init_db()

# crée la base dès l'import ET avant la 1re requête (utile avec gunicorn)
init_db()

@app.before_first_request
def _before_first():
    init_db()

# ---------- LAYOUT ----------
LAYOUT = """
<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
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
  {% with m=get_flashed_messages() %}
    {% if m %}<article>{% for x in m %}<p>{{x}}</p>{% endfor %}</article>{% endif %}
  {% endwith %}
  {{ body|safe }}
</main>
<footer><small>&copy; {{year}} — {{appname}}</small></footer>
</body>
"""
def page(body, title=""):
    return render_template_string(LAYOUT, body=body, title=title or APP_NAME,
                                 appname=APP_NAME, year=datetime.now().year)

# ---------- PUBLIC ----------
@app.get("/")
def home():
    ensure_db()
    rows = db().execute(
        "SELECT * FROM posts WHERE status='published' ORDER BY id DESC LIMIT 50"
    ).fetchall()
    if not rows:
        return page("<h2>Dernières publications</h2><p>Aucune publication pour l’instant.</p>", "Publications")
    cards = []
    for r in rows:
        cards.append(f"""
        <article>
          <header><h3>{r['title']}</h3><small>{r['created_at'][:16].replace('T',' ')}</small></header>
          <p>{(r['body'] or '').replace(chr(10), '<br>')}</p>
        </article>""")
    return page("<h2>Dernières publications</h2>" + "".join(cards), "Publications")

@app.get("/rss.xml")
def rss_xml():
    ensure_db()
    rows = db().execute(
        "SELECT * FROM posts WHERE status='published' ORDER BY id DESC LIMIT 100"
    ).fetchall()
    items = []
    for r in rows:
        title = (r["title"] or "").replace("&","&amp;")
        desc  = (r["body"] or "").replace("&","&amp;")
        pub   = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S %z')
        items.append(f"""
<item>
  <title>{title}</title>
  <link>{request.url_root}</link>
  <guid isPermaLink="false">{r['id']}</guid>
  <description><![CDATA[{desc}]]></description>
  <pubDate>{pub}</pubDate>
</item>""")
    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>{APP_NAME} — Flux</title>
  <link>{request.url_root}</link>
  <description>Articles publiés</description>
  {''.join(items)}
</channel></rss>"""
    return Response(rss, mimetype="application/rss+xml")

# ---------- ADMIN ----------
@app.route("/admin", methods=["GET","POST"])
def admin():
    ensure_db()
    if request.method == "POST" and not session.get("ok"):
        if request.form.get("password") == ADMIN_PASS:
            session["ok"] = True
            return redirect(url_for("admin"))
        flash("Mot de passe incorrect.")
        return redirect(url_for("admin"))

    if not session.get("ok"):
        return page("""
        <h3>Connexion</h3>
        <form method="post">
          <input type="password" name="password" placeholder="Mot de passe" required>
          <button>Entrer</button>
        </form>""", "Connexion")

    con = db()
    drafts = con.execute("SELECT * FROM posts WHERE status='draft' ORDER BY id DESC").fetchall()
    pubs   = con.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC").fetchall()
    con.close()

    def bloc(r, published=False):
        return f"""
        <details>
          <summary><b>{r['title'] or '(Sans titre)'}</b> — <small>{r['status']}</small></summary>
          <form method="post" action="{url_for('save', post_id=r['id'])}">
            <label>Titre<input name="title" value="{(r['title'] or '').replace('"','&quot;')}"></label>
            <label>Contenu<textarea name="body">{r['body'] or ''}</textarea></label>
            <div class="grid">
              <button name="action" value="save">💾 Enregistrer</button>
              {"<button name='action' value='unpublish' class='secondary'>⏸️ Dépublier</button>" if published else "<button name='action' value='publish' class='secondary'>✅ Publier</button>"}
              <button name="action" value="delete" class="contrast">🗑️ Supprimer</button>
            </div>
          </form>
        </details>"""

    body = f"""
    <h3>Console</h3>
    <form method="post" action="{url_for('create')}">
      <div class="grid">
        <input name="title" placeholder="Nouveau titre" required>
        <button>+ Nouveau brouillon</button>
      </div>
    </form>
    <h4>Brouillons</h4>{''.join(bloc(r) for r in drafts) or "<p>Aucun brouillon.</p>"}
    <h4>Publiés</h4>{''.join(bloc(r, True) for r in pubs) or "<p>Rien de publié.</p>"}
    <p>Flux public pour dlvr.it : <code>{request.url_root}rss.xml</code></p>
    """
    return page(body, "Admin")

@app.post("/create")
def create():
    ensure_db()
    if not session.get("ok"): return redirect(url_for("admin"))
    now = datetime.now().isoformat(timespec="minutes")
    title = request.form.get("title","").strip()
    con = db()
    con.execute("INSERT INTO posts(title, body, status, created_at, updated_at) VALUES(?,?,?,?,?)",
                (title, "", "draft", now, now))
    con.commit(); con.close()
    return redirect(url_for("admin"))

@app.post("/save/<int:post_id>")
def save(post_id):
    ensure_db()
    if not session.get("ok"): return redirect(url_for("admin"))
    action = request.form.get("action","save")
    title  = request.form.get("title","").strip()
    body   = request.form.get("body","").strip()
    con = db()
    if action == "delete":
        con.execute("DELETE FROM posts WHERE id=?", (post_id,))
        flash("Supprimé.")
    else:
        con.execute("UPDATE posts SET title=?, body=?, updated_at=? WHERE id=?",
                    (title, body, datetime.now().isoformat(timespec="minutes"), post_id))
        if action == "publish":
            con.execute("UPDATE posts SET status='published' WHERE id=?", (post_id,))
            flash("Publié.")
        elif action == "unpublish":
            con.execute("UPDATE posts SET status='draft' WHERE id=?", (post_id,))
            flash("Dépublié.")
        else:
            flash("Enregistré.")
    con.commit(); con.close()
    return redirect(url_for("admin"))

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

# Alias pratique
@app.get("/console")
def alias_console():
    return redirect(url_for("admin"))

# ---------- LANCEMENT (debug ON) ----------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
