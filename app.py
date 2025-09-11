
import os, sqlite3, json, re
from datetime import datetime, timezone
from flask import Flask, request, redirect, render_template_string, session, url_for, flash, Response
import feedparser
from bs4 import BeautifulSoup

APP_NAME = "Arménie Console"
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "armenie")

try:
    FEEDS = json.loads(os.environ.get("FEEDS","[]"))
except Exception:
    FEEDS = []
if not FEEDS:
    FEEDS = [
        "https://www.armenpress.am/rss/",
        "https://www.civilnet.am/feed/",
        "https://armtimes.com/rss",
        "https://factor.am/feed",
    ]

DB = "site.db"
app = Flask(__name__)
app.secret_key = SECRET_KEY

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    c = db()
    c.execute("""
    CREATE TABLE IF NOT EXISTS posts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guid TEXT UNIQUE,
        link TEXT,
        title TEXT,
        summary TEXT,
        source TEXT,
        status TEXT DEFAULT 'draft',
        created_at TEXT
    )
    """)
    c.commit(); c.close()

def clean_html(t):
    soup = BeautifulSoup(t or "", "html.parser")
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+\n", "\n", text).strip()

LAYOUT = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
<style>
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
  textarea{min-height:8rem}
  .badge{background:#eef;padding:.2rem .5rem;border-radius:.3rem;margin-left:.5rem}
  header.hero{margin:1rem 0 2rem 0}
</style>
</head>
<body class="container">
  <header class="hero">
    <nav>
      <ul><li><strong>{{ appname }}</strong></li></ul>
      <ul>
        <li><a href="{{ url_for('home') }}">Accueil</a></li>
        <li><a href="{{ url_for('feed_xml') }}" target="_blank">RSS</a></li>
        {% if session.get('ok') %}
          <li><a href="{{ url_for('admin') }}">Admin</a></li>
          <li><a href="{{ url_for('logout') }}">Déconnexion</a></li>
        {% else %}
          <li><a href="{{ url_for('admin') }}">Connexion</a></li>
        {% endif %}
      </ul>
    </nav>
  </header>
  <main>
    {% with msgs = get_flashed_messages() %}
      {% if msgs %}<article>{% for m in msgs %}<p>{{ m }}</p>{% endfor %}</article>{% endif %}
    {% endwith %}
    {{ body|safe }}
  </main>
  <footer><small>&copy; {{ year }} — {{ appname }}</small></footer>
</body>
</html>
"""

def render_page(body_html, title="Arménie Info", **ctx):
    return render_template_string(LAYOUT, body=body_html, title=title, appname=APP_NAME, year=datetime.now().year, **ctx)

@app.get("/")
def home():
    rows = db().execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC LIMIT 50").fetchall()
    items = ""
    if not rows:
        items += "<p>Aucun article publié pour le moment.</p>"
    for r in rows:
        items += f"""
        <article>
          <header><h3><a href="{r['link']}" target="_blank">{r['title'] or '(Sans titre)'}</a></h3>
          <small><span class=\"badge\">{r['source']}</span></small></header>
          <p>{(r['summary'] or '')[:600]}</p>
          <footer><a href="{r['link']}" target="_blank">Lire la source</a></footer>
        </article>
        """
    body = f"<h2>Dernières publications</h2>{items}"
    return render_page(body, title="Publications")

@app.get("/feed.xml")
def feed_xml():
    rows = db().execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC LIMIT 100").fetchall()
    rss_items = []
    for r in rows:
        title = (r["title"] or "").replace("&","&amp;")
        link = (r["link"] or "").replace("&","&amp;")
        guid = (r["guid"] or "").replace("&","&amp;")
        desc = (r["summary"] or "").replace("&","&amp;")
        pub = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S %z')
        rss_items.append(f"""
<item>
  <title>{title}</title>
  <link>{link}</link>
  <guid isPermaLink="false">{guid}</guid>
  <description><![CDATA[{desc}]]></description>
  <pubDate>{pub}</pubDate>
</item>""")
    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>{APP_NAME} — Flux publié</title>
  <link>{request.url_root}</link>
  <description>Articles approuvés</description>
  {''.join(rss_items)}
</channel></rss>"""
    return Response(rss, mimetype="application/rss+xml")

@app.route("/admin", methods=["GET","POST")
def admin():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASS:
            session["ok"] = True
            return redirect(url_for("admin"))
        else:
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
        return render_page(body, title="Connexion")

    c = db()
    drafts = c.execute("SELECT * FROM posts WHERE status='draft' ORDER BY id DESC").fetchall()
    pubs = c.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC LIMIT 10").fetchall()
    c.close()

    items = ""
    if drafts:
        for r in drafts:
            items += f"""
            <details>
              <summary><b>{r['title'] or '(Sans titre)'}</b> — <small><span class=\"badge\">{r['source']}</span></small></summary>
              <form method="post" action="{url_for('save', id=r['id'])}">
                <label>Titre<input name="title" value="{(r['title'] or '').replace('"','&quot;')}"></label>
                <label>Résumé<textarea name="summary">{r['summary'] or ''}</textarea></label>
                <label>Lien<input name="link" value="{r['link'] or ''}"></label>
                <div class="grid2">
                  <button name="action" value="save">💾 Enregistrer</button>
                  <button name="action" value="publish" class="secondary">✅ Approuver & Publier</button>
                  <a href="{r['link']}" target="_blank">Ouvrir la source</a>
                </div>
              </form>
            </details>
            """
    else:
        items = "<p>Aucun brouillon. Cliquez sur « Récupérer » pour importer.</p>"

    published = ""
    for r in pubs:
        published += f"<li><a href='{r['link']}' target='_blank'>{r['title']}</a> — <small>{r['source']}</small></li>"

    feeds_ul = "".join([f"<li>{f}</li>" for f in FEEDS])

    body = f"""
    <h3>Console de modération</h3>
    <details><summary><b>Sources RSS</b></summary>
      <ul>{feeds_ul}</ul>
      <a href="{url_for('fetch')}" role="button">🔁 Récupérer les nouveautés</a>
    </details>
    <h4>Brouillons</h4>
    {items}
    <h4>Dernières publications</h4>
    <ul>{published or '<li>—</li>'}</ul>
    <p>Flux public pour dlvr.it : <code>{request.url_root}feed.xml</code></p>
    """
    return render_page(body, title="Admin")

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

@app.get("/fetch")
def fetch():
    if not session.get("ok"): 
        return redirect(url_for("admin"))
    c = db()
    imported = 0
    for f in FEEDS:
        fp = feedparser.parse(f)
        source = (getattr(fp, "feed", {}) or {}).get("title") or (getattr(fp, "feed", {}) or {}).get("link") or "Source"
        for e in getattr(fp, "entries", [])[:20]:
            guid = e.get("id") or e.get("guid") or e.get("link")
            if not guid: 
                continue
            link = (e.get("link") or "").strip()
            title = (e.get("title") or "").strip()
            raw = e.get("summary","") or ""
            if not raw and e.get("content"):
                try:
                    raw = e["content"][0].get("value","");
                except Exception:
                    pass
            summary = clean_html(raw)[:900]
            try:
                c.execute("INSERT INTO posts(guid,link,title,summary,source,created_at) VALUES(?,?,?,?,?,?)",
                          (guid, link, title, summary, source, datetime.now(timezone.utc).isoformat()))
                imported += 1
            except sqlite3.IntegrityError:
                pass
    c.commit(); c.close()
    from flask import flash
    flash(f"Import terminé. Nouveaux éléments : {imported}")
    return redirect(url_for("admin"))

@app.post("/save/<int:id>")
def save(id):
    if not session.get("ok"):
        return redirect(url_for("admin"))
    title = request.form.get("title","\n").strip()
    summary = request.form.get("summary","\n").strip()
    link = request.form.get("link","\n").strip()
    action = request.form.get("action","save")
    c = db()
    c.execute("UPDATE posts SET title=?, summary=?, link=? WHERE id=?", (title, summary, link, id))
    if action == "publish":
        c.execute("UPDATE posts SET status='published' WHERE id=?", (id,))
        from flask import flash
        flash("Publié (apparaît sur la page publique et dans /feed.xml).");
    else:
        from flask import flash
        flash("Enregistré.");
    c.commit(); c.close()
    return redirect(url_for("admin"))

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8000, debug=False)
