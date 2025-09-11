from flask import Flask, request, redirect, url_for, Response, render_template_string, session, flash
import sqlite3, os, hashlib, io, traceback
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
import feedparser
from PIL import Image, UnidentifiedImageError

APP_NAME   = "Console Arménienne"
ADMIN_PASS = os.environ.get("ADMIN_PASS", "armenie")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")
DB_PATH    = "site.db"

DEFAULT_FEEDS = [
    "https://www.civilnet.am/news/feed/",
    "https://armenpress.am/rss/",
    "https://news.am/eng/rss/",
]

OPENAI_KEY   = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ------------ DB helpers ------------
def db():
    # connexion par appel, thread-safe pour gunicorn gthread
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS posts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        body  TEXT,
        status TEXT DEFAULT 'draft',
        created_at TEXT,
        updated_at TEXT,
        image_url TEXT,
        image_sha1 TEXT,
        orig_link TEXT UNIQUE,
        source TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    con.commit(); con.close()

def get_setting(key, default=""):
    con = db()
    try:
        r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default
    finally:
        con.close()

def set_setting(key, value):
    con = db()
    try:
        con.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        con.commit()
    finally:
        con.close()

# ------------ HTTP & images ------------
def http_get(url, timeout=20):
    r = requests.get(url, timeout=timeout, allow_redirects=True, headers={
        "User-Agent": "Mozilla/5.0 (+RenderBot)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr,en;q=0.8",
    })
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r.text

def find_main_image(html):
    soup = BeautifulSoup(html, "html.parser")
    for sel, attr in [("meta[property='og:image']", "content"),
                      ("meta[name='twitter:image']", "content")]:
        m = soup.select_one(sel)
        if m and m.get(attr): return m[attr]
    img = soup.find("img")
    return img.get("src") if img and img.get("src") else None

def download_image(url):
    if not url: return None, None
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.content
        sha1 = hashlib.sha1(data).hexdigest()
        try:
            im = Image.open(io.BytesIO(data))
            im.verify()  # valide le fichier
        except UnidentifiedImageError:
            print(f"[IMG] Unidentified image, skipping: {url}")
            return None, None
        except Exception as e:
            print(f"[IMG] Pillow verification error: {e}")
            return None, None
        ext = "jpg"
        os.makedirs("static/images", exist_ok=True)
        path = f"static/images/{sha1}.{ext}"
        if not os.path.exists(path):
            with open(path, "wb") as f: f.write(data)
        return "/"+path, sha1
    except Exception as e:
        print(f"[IMG] download failed for {url}: {e}")
        return None, None

# ------------ Rewriting ------------
def rewrite_html_fr(title, body_html):
    # OpenAI optionnel
    if OPENAI_KEY:
        try:
            payload = {
                "model": OPENAI_MODEL,
                "temperature": 0.3,
                "messages": [
                    {"role":"system","content":
                        "Tu es rédacteur francophone. Réécris clairement le contenu en 120–200 mots, "
                        "structure en <p>, garde les faits, ton neutre."},
                    {"role":"user","content": f"Titre: {title}\n\nHTML source:\n{body_html}"}
                ]
            }
            r = requests.post("https://api.openai.com/v1/chat/completions",
                              headers={"Authorization": f"Bearer {OPENAI_KEY}",
                                       "Content-Type":"application/json"},
                              json=payload, timeout=60)
            j = r.json()
            if j.get("choices"):
                return j["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"[AI] rewrite failed: {e}")
    # fallback local
    txt = BeautifulSoup(body_html or "", "html.parser").get_text(" ")
    words = txt.split()
    return f"<p>{' '.join(words[:180])}</p>"

def html_from_entry(entry):
    if "content" in entry and entry.content:
        if isinstance(entry.content, list): return entry.content[0].get("value","")
        if isinstance(entry.content, dict): return entry.content.get("value","")
    return entry.get("summary","") or entry.get("description","")

# ------------ Scraper ------------
def scrape_once(feeds):
    created = 0
    skipped = 0
    for feed in feeds:
        try:
            fp = feedparser.parse(feed)
        except Exception as e:
            print(f"[FEED] parse error {feed}: {e}")
            continue

        for e in fp.entries[:10]:
            try:
                link = e.get("link") or ""
                if not link:
                    skipped += 1; continue

                # doublon par lien
                con = db()
                try:
                    if con.execute("SELECT 1 FROM posts WHERE orig_link=?", (link,)).fetchone():
                        skipped += 1; con.close(); continue
                finally:
                    con.close()

                title = (e.get("title") or "(Sans titre)").strip()
                html_src = html_from_entry(e)

                img_url = None
                try:
                    page = http_get(link)
                    img_url = find_main_image(page)
                except Exception as ee:
                    print(f"[PAGE] fetch fail {link}: {ee}")

                local_path, sha1 = download_image(img_url) if img_url else (None, None)

                # doublon image
                if sha1:
                    con = db()
                    try:
                        if con.execute("SELECT 1 FROM posts WHERE image_sha1=?", (sha1,)).fetchone():
                            skipped += 1; con.close(); continue
                    finally:
                        con.close()

                html_final = rewrite_html_fr(title, html_src)
                now = datetime.now(timezone.utc).isoformat()

                con = db()
                try:
                    con.execute("""INSERT INTO posts
                      (title, body, status, created_at, updated_at, image_url, image_sha1, orig_link, source)
                      VALUES(?,?,?,?,?,?,?,?,?)""",
                      (title, html_final, "draft", now, now, local_path, sha1, link, fp.feed.get("title","")))
                    con.commit()
                    created += 1
                finally:
                    con.close()
            except Exception as e:
                skipped += 1
                print(f"[ENTRY] skipped due to error: {e}")
                traceback.print_exc()
                continue
    return created, skipped

# ------------ UI ------------
LAYOUT = """
<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
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

@app.get("/health")
def health():
    return "OK"

@app.get("/")
def home():
    con = db()
    try:
        rows = con.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC LIMIT 50").fetchall()
    finally:
        con.close()
    if not rows:
        return page("<h2>Dernières publications</h2><p>Aucune publication pour l’instant.</p>", "Publications")
    cards = []
    for r in rows:
        img = f"<img src='{r['image_url']}' alt='' style='max-width:100%;height:auto'>" if r["image_url"] else ""
        created = (r['created_at'] or '')[:16].replace('T',' ')
        cards.append(f"<article><header><h3>{r['title']}</h3><small>{created}</small></header>{img}<p>{(r['body'] or '').replace(chr(10), '<br>')}</p></article>")
    return page("<h2>Dernières publications</h2>" + "".join(cards), "Publications")

@app.get("/rss.xml")
def rss_xml():
    con = db()
    try:
        rows = con.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC LIMIT 100").fetchall()
    finally:
        con.close()
    items = []
    for r in rows:
        title = (r["title"] or "").replace("&","&amp;")
        desc  = (BeautifulSoup(r["body"] or "", "html.parser").get_text(" ") or "").replace("&","&amp;")
        enclosure = f"<enclosure url='{request.url_root.rstrip('/') + r['image_url']}' type='image/jpeg'/>" if r["image_url"] else ""
        pub   = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S %z')
        items.append(f"<item><title>{title}</title><link>{request.url_root}</link><guid isPermaLink='false'>{r['id']}</guid><description><![CDATA[{desc}]]></description>{enclosure}<pubDate>{pub}</pubDate></item>")
    rss = f"<?xml version='1.0' encoding='UTF-8'?><rss version='2.0'><channel><title>{APP_NAME} — Flux</title><link>{request.url_root}</link><description>Articles publiés</description>{''.join(items)}</channel></rss>"
    return Response(rss, mimetype="application/rss+xml")

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

    feeds = get_setting("feeds", "\n".join(DEFAULT_FEEDS))
    con = db()
    try:
        drafts = con.execute("SELECT * FROM posts WHERE status='draft' ORDER BY id DESC").fetchall()
        pubs   = con.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC").fetchall()
    finally:
        con.close()

    def card(r, published=False):
        img = f"<img src='{r['image_url']}' style='max-width:200px'>" if r["image_url"] else ""
        return f"""
        <details>
          <summary><b>{r['title'] or '(Sans titre)'}</b> — <small>{r['status']}</small></summary>
          {img}
          <form method="post" action="{url_for('save', post_id=r['id'])}">
            <label>Titre<input name="title" value="{(r['title'] or '').replace('"','&quot;')}"></label>
            <label>Contenu<textarea name="body" rows="6">{r['body'] or ''}</textarea></label>
            <div class="grid">
              <button name="action" value="save">💾 Enregistrer</button>
              {"<button name='action' value='unpublish' class='secondary'>⏸️ Dépublier</button>" if published else "<button name='action' value='publish' class='secondary'>✅ Publier</button>"}
              <button name="action" value="delete" class="contrast">🗑️ Supprimer</button>
            </div>
          </form>
        </details>"""

    body = f"""
    <h3>Console</h3>
    <article>
      <form method="post" action="{url_for('update_feeds')}">
        <h4>Sources (une URL RSS par ligne)</h4>
        <textarea name="feeds" rows="6">{feeds}</textarea>
        <button>💾 Enregistrer les sources</button>
      </form>
      <form method="post" action="{url_for('import_now')}" style="margin-top:1rem">
        <button>🔁 Importer maintenant (scraping + réécriture)</button>
      </form>
    </article>
    <h4>Brouillons</h4>{''.join(card(r) for r in drafts) or "<p>Aucun brouillon.</p>"}
    <h4>Publiés</h4>{''.join(card(r, True) for r in pubs) or "<p>Rien de publié.</p>"}
    <p>Flux public : <code>{request.url_root}rss.xml</code></p>
    """
    return page(body, "Admin")

@app.post("/update-feeds")
def update_feeds():
    if not session.get("ok"): return redirect(url_for("admin"))
    set_setting("feeds", request.form.get("feeds",""))
    flash("Sources mises à jour."); return redirect(url_for("admin"))

@app.post("/import-now")
def import_now():
    if not session.get("ok"): return redirect(url_for("admin"))
    feeds_txt = get_setting("feeds", "\n".join(DEFAULT_FEEDS))
    feed_list = [u.strip() for u in feeds_txt.splitlines() if u.strip()]
    try:
        created, skipped = scrape_once(feed_list)
        flash(f"Import terminé : {created} nouveaux, {skipped} ignorés.")
    except Exception as e:
        print("[IMPORT] fatal:", e)
        traceback.print_exc()
        flash(f"Erreur d’import : {e}")
    return redirect(url_for("admin"))

@app.post("/save/<int:post_id>")
def save(post_id):
    if not session.get("ok"): return redirect(url_for("admin"))
    action = request.form.get("action","save")
    title  = request.form.get("title","").strip()
    body   = request.form.get("body","").strip()
    con = db()
    try:
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
        con.commit()
    finally:
        con.close()
    return redirect(url_for("admin"))

@app.get("/logout")
def logout():
    session.clear(); return redirect(url_for("home"))

@app.get("/console")
def alias_console():
    return redirect(url_for("admin"))

# --------- boot ---------
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
