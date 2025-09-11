from flask import Flask, request, redirect, url_for, session, flash, Response, render_template_string
import sqlite3, os
from datetime import datetime, timezone

# ====== CONFIG ======
APP_NAME    = "Console Arménienne"
ADMIN_PASS  = os.environ.get("ADMIN_PASS", "armenie")  # change-le dans Render > Environment
SECRET_KEY  = os.environ.get("SECRET_KEY", "change-me") # ou laisse Render en générer un

DB = "site.db"

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ====== DB helpers ======
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    c = db()
    c.execute("""
    CREATE TABLE IF NOT EXISTS posts(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT, body TEXT, status TEXT DEFAULT 'draft',
      created_at TEXT, updated_at TEXT
    )
    """)
    c.commit(); c.close()

# ====== Layout minimal ======
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
    <li><a href="{{ url_for('rss') }}" target="_blank">RSS</a></li>
    {% if session.get('ok') %}
      <li><a href="{{ url_for('admin') }}">Admin</a></li>
      <li><a href="{{ url_for('logout') }}">Déconnexion</a></li>
    {% else %}
      <li><a href="{{ url_for('admin') }}">Connexion</a></li>
    {% endif %}
  </ul>
</nav>
<main>
  {% with msgs = get_flashed_messages() %}
    {% if msgs %}<article>{% for m in msgs %}<p>{{m}}</p>{% endfor %}</article>{% endif %}
  {% endwith %}
  {{ body|safe }}
</main>
<footer><small>&copy; {{year}} — {{appname}}</small></footer>
</body>
"""

def page(body, title=""):
    return render_template_string(LAYOUT, body=body, title=title or APP_NAME,
                                 appname=APP_NAME, year=datetime.now().year)

# ====== Public pages ======
@app.get("/")
def home():
    rows = db().execute(
        "SELECT id,title,body,created_at FROM posts WHERE status='published' ORDER BY id DESC LIMIT 50"
    ).fetchall()
    cards = []
    for r in rows:
        cards.append(f"""
        <article>
          <header><h3>{r['title']}</h3><small>Publié le {r['created_at'][:16].replace('T',' ')}</small></header>
          <p>{(r['body'] or '').replace(chr(10), '<br>')}</p>
        </article>
        """)
    body = "<h2>Dernières publications</h2>" + ("".join(cards) or "<p>Aucun article publié pour le moment.</p>")
    return page(body, "Publications")

@app.get("/rss.xml")
def rss():
    rows = db().execute(
        "SELECT id,title,body,created_at FROM posts WHERE status='published' ORDER BY id DESC LIMIT 100"
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

# ====== Admin ======
@app.route("/admin", methods=["GET","POST"])
def admin():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASS:
            session["ok"] = True
            return redirect(url_for("admin"))
        flash("Mot de passe incorrect.")
        return redirect(url_for("admin"))

    if not session.get("ok"):
        body = """
        <h3>Connexion</h3>
        <form method="post">
          <input type="password" name="password" placeholder="Mot de passe" required>
          <button>Entrer</button>
        </form>
        """
        return page(body, "Connexion")

    c = db()
    drafts = c.execute("SELECT * FROM posts WHERE status='draft' ORDER BY id DESC").fetchall()
    pubs   = c.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC").fetchall()
    c.close()

    def row_html(r, published=False):
        actions = f"""
        <form method="post" action="{url_for('save', post_id=r['id'])}">
          <label>Titre<input name="title" value="{(r['title'] or '').replace('"','&quot;')}"></label>
          <label>Contenu<textarea name="body">{r['body'] or ''}</textarea></label>
          <div class="grid">
            <button name="action" value="save">💾 Enregistrer</button>
            {'<button name="action" value="unpublish" class="secondary">⏸️ Dépublier</button>' if published else '<button name="action" value="publish" class="secondary">✅ Publier</button>'}
            <button name="action" value="delete" class="contrast">🗑️ Supprimer</button>
          </div>
        </form>
        """
        return f"<details><summary><b>{r['title'] or '(Sans titre)'}</b> — <small>{r['status']}</small></summary>{actions}</details>"

    draft_html = "".join([row_html(r) for r in drafts]) or "<p>Aucun brouillon.</p>"
    pub_html   = "".join([row_html(r, True) for r in pubs]) or "<p>Rien de publié.</p>"

    body = f"""
    <h3>Console d’édition</h3>
    <form method="post" action="{url_for('create')}">
      <div class="grid">
        <input name="title" placeholder="Nouveau titre" required>
        <button>+ Nouveau brouillon</button>
      </div>
    </form>

    <h4>Brouillons</h4>{draft_html}
    <h4>Publiés</h4>{pub_html}

    <p>Flux public pour dlvr.it : <code>{request.url_root}rss.xml</code></p>
    """
    return page(body, "Admin")

@app.post("/create")
def create():
    if not session.get("ok"): return redirect(url_for("admin"))
    title = request.form.get("title","").strip()
    now = datetime.now().isoformat(timespec="minutes")
    c = db()
    c.execute("INSERT INTO posts(title, body, status, created_at, updated_at) VALUES(?,?,?,?,?)",
              (title, "", "draft", now, now))
    c.commit(); c.close()
    flash("Brouillon créé.")
    return redirect(url_for("admin"))

@app.post("/save/<int:post_id>")
def save(post_id):
    if not session.get("ok"): return redirect(url_for("admin"))
    title = request.form.get("title","").strip()
    body  = request.form.get("body","").strip()
    action= request.form.get("action","save")
    c = db()
    if action == "delete":
        c.execute("DELETE FROM posts WHERE id=?", (post_id,))
        flash("Supprimé.")
    else:
        c.execute("UPDATE posts SET title=?, body=?, updated_at=? WHERE id=?",
                  (title, body, datetime.now().isoformat(timespec="minutes"), post_id))
        if action == "publish":
            c.execute("UPDATE posts SET status='published' WHERE id=?", (post_id,))
            flash("Publié.")
        elif action == "unpublish":
            c.execute("UPDATE posts SET status='draft' WHERE id=?", (post_id,))
            flash("Dépublié.")
        else:
            flash("Enregistré.")
    c.commit(); c.close()
    return redirect(url_for("admin"))

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)
