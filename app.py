from flask import Flask, request, redirect, url_for, Response, render_template_string, session, flash
import sqlite3, os, hashlib, io, traceback, re, threading, time, json as _json
from datetime import datetime, timezone
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
import feedparser
from PIL import Image, UnidentifiedImageError

# ================== CONFIG ==================
APP_NAME   = "Console Arménienne"
ADMIN_PASS = os.environ.get("ADMIN_PASS", "armenie")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")
DB_PATH    = "site.db"

DEFAULT_FEEDS = [
    "https://www.civilnet.am/news/feed/",
    "https://armenpress.am/rss/",
    "https://news.am/eng/rss/",
]

# OpenAI via ENV (écrasé par les paramètres admin si saisis)
ENV_OPENAI_KEY   = os.environ.get("OPENAI_API_KEY", "").strip()
ENV_OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ================== DB ==================
def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def column_exists(con, table, name):
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == name for r in rows)

def init_db():
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS posts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        body  TEXT,
        status TEXT DEFAULT 'draft',         -- draft | scheduled | published
        created_at TEXT,
        updated_at TEXT,
        publish_at TEXT,                     -- ISO UTC quand planifié
        image_url TEXT,
        image_sha1 TEXT,
        orig_link TEXT UNIQUE,
        source TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    if not column_exists(con, "posts", "publish_at"):
        con.execute("ALTER TABLE posts ADD COLUMN publish_at TEXT")
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

# ================== UTILS TEXTE ==================
TAG_RE = re.compile(r"<[^>]+>")
FR_TOKENS = set(" le la les un une des du de au aux et en sur pour par avec dans que qui ne pas est été sont était selon afin aussi plus leur lui ses ces cette ce cela donc ainsi tandis alors contre entre vers depuis sans sous après avant comme lorsque tandis que où dont même".split())

def strip_tags(s: str) -> str:
    return TAG_RE.sub("", s or "")

def looks_french(text: str) -> bool:
    """Détection légère : présence de mots-outils FR."""
    if not text: return False
    t = text.lower()
    words = re.findall(r"[a-zàâäéèêëïîôöùûüç'-]+", t)
    if not words: return False
    hits = sum(1 for w in words[:80] if w in FR_TOKENS)
    return hits >= 5  # seuil empirique

def active_openai():
    key = get_setting("openai_key", ENV_OPENAI_KEY)
    model = get_setting("openai_model", ENV_OPENAI_MODEL)
    return (key.strip(), model.strip())

def _title_from_text_fallback(fr_text: str) -> str:
    t = (fr_text or "").strip()
    if not t:
        return "Actualité"
    words = t.split()
    base = " ".join(words[:10]).strip().rstrip(".,;:!?")
    base = base[:80]
    return base[:1].upper() + base[1:]

def rewrite_article_fr(title_src: str, raw_text: str):
    """
    Retourne (title_fr, body_fr). Force le FR :
    - 2 tentatives OpenAI si pas FR à la 1re
    - sinon fallback local + marque '(à traduire)' et reste en brouillon
    """
    if not raw_text:
        return (title_src or "Actualité", "", False)

    key, model = active_openai()
    clean_input = strip_tags(raw_text)

    def call_openai():
        prompt = (
            "Tu es un journaliste francophone. "
            "Traduis/réécris en FRANÇAIS le Titre et le Corps de l'article ci-dessous. "
            "Ton neutre et factuel, 150–220 mots pour le corps. "
            "RENVOIE STRICTEMENT du JSON avec les clés 'title' et 'body'. "
            "Le 'body' doit être du TEXTE BRUT (PAS de balises HTML) et DOIT se terminer par: - Arménie Info.\n\n"
            f"Titre (source): {title_src}\n"
            f"Texte (source): {clean_input}"
        )
        payload = {
            "model": model or "gpt-4o-mini",
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": "Tu écris en français clair et concis. Réponds uniquement au format demandé."},
                {"role": "user", "content": prompt}
            ]
        }
        r = requests.post("https://api.openai.com/v1/chat/completions",
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json=payload, timeout=60)
        j = r.json()
        out = (j.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        # JSON strict si possible
        try:
            data = _json.loads(out)
            title_fr = strip_tags(data.get("title","")).strip()
            body_fr  = strip_tags(data.get("body","")).strip()
        except Exception:
            parts = out.split("\n", 1)
            title_fr = strip_tags(parts[0]).strip()
            body_fr  = strip_tags(parts[1] if len(parts) > 1 else "").strip()
        if not body_fr:
            body_fr = strip_tags(clean_input)
            body_fr = " ".join(body_fr.split()[:200]).strip()
        if not body_fr.endswith("- Arménie Info"):
            body_fr += "\n\n- Arménie Info"
        if not title_fr:
            title_fr = _title_from_text_fallback(body_fr)
        return title_fr, body_fr

    # Avec OpenAI → deux tentatives si pas en FR
    if key:
        try:
            t1, b1 = call_openai()
            if looks_french(b1) and looks_french(t1):
                return (t1, b1, True)
            # 2e tentative, instruction plus ferme
            print("[AI] Second attempt to enforce FR")
            t2, b2 = call_openai()
            if looks_french(b2) and looks_french(t2):
                return (t2, b2, True)
            # si toujours pas FR, on marque à traduire
            b2 = b2 + "\n\n(à traduire) - Arménie Info" if not b2.endswith("- Arménie Info") else b2 + "\n(à traduire)"
            return (_title_from_text_fallback(b2), b2, False)
        except Exception as e:
            print(f"[AI] rewrite_article_fr failed: {e}")

    # Fallback local (pas de vraie traduction)
    fr_body = strip_tags(raw_text)
    fr_body = " ".join(fr_body.split()[:200]).strip()
    if not fr_body.endswith("- Arménie Info"):
        fr_body += "\n\n- Arménie Info"
    fr_body += "\n(à traduire)"
    fr_title = _title_from_text_fallback(fr_body)
    return (fr_title, fr_body, False)

# ================== HTTP & IMAGES ==================
def http_get(url, timeout=20):
    r = requests.get(url, timeout=timeout, allow_redirects=True, headers={
        "User-Agent": "Mozilla/5.0 (+RenderBot)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr,en;q=0.8",
    })
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r.text

def find_main_image_in_html(html, base_url=None):
    soup = BeautifulSoup(html, "html.parser")
    # og:image / twitter:image
    for sel, attr in [("meta[property='og:image']", "content"),
                      ("meta[name='twitter:image']", "content")]:
        m = soup.select_one(sel)
        if m and m.get(attr):
            return urljoin(base_url or "", m[attr])
    # première image dans <article>
    a = soup.find("article")
    if a:
        imgtag = a.find("img")
        if imgtag and imgtag.get("src"):
            return urljoin(base_url or "", imgtag["src"])
    # fallback: première image de la page
    imgtag = soup.find("img")
    if imgtag and imgtag.get("src"):
        return urljoin(base_url or "", imgtag["src"])
    return None

def get_image_from_entry(entry, page_html=None, page_url=None):
    # 1) champs RSS media/enclosure
    try:
        media = entry.get("media_content") or entry.get("media_thumbnail")
        if isinstance(media, list) and media:
            u = media[0].get("url")
            if u: return urljoin(page_url or "", u)
    except Exception:
        pass
    try:
        enc = entry.get("enclosures") or entry.get("links")
        if isinstance(enc, list):
            for en in enc:
                href = en.get("href") if isinstance(en, dict) else None
                if href and any(href.lower().endswith(ext) for ext in (".jpg",".jpeg",".png",".webp",".gif")):
                    return urljoin(page_url or "", href)
    except Exception:
        pass
    # 2) img dans le contenu RSS (summary/content)
    for k in ("content","summary","description"):
        v = entry.get(k)
        if not v: continue
        html = ""
        if isinstance(v, list) and v:
            html = v[0].get("value", "")
        elif isinstance(v, dict):
            html = v.get("value","")
        elif isinstance(v, str):
            html = v
        if html:
            s = BeautifulSoup(html, "html.parser")
            imgtag = s.find("img")
            if imgtag and imgtag.get("src"):
                return urljoin(page_url or "", imgtag["src"])
    # 3) page HTML
    if page_html:
        return find_main_image_in_html(page_html, base_url=page_url)
    return None

def download_image(url):
    if not url: return None, None
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.content
        sha1 = hashlib.sha1(data).hexdigest()
        # valide l'image
        try:
            im = Image.open(io.BytesIO(data))
            im.verify()
        except (UnidentifiedImageError, Exception) as e:
            print(f"[IMG] verify fail {url}: {e}")
            return None, None
        os.makedirs("static/images", exist_ok=True)
        path = f"static/images/{sha1}.jpg"
        if not os.path.exists(path):
            with open(path, "wb") as f: f.write(data)
        return "/"+path, sha1
    except Exception as e:
        print(f"[IMG] download failed for {url}: {e}")
        return None, None

# ================== EXTRACTION TEXTE ==================
SEL_CANDIDATES = [
    "article",
    ".entry-content", ".post-content", ".td-post-content",
    ".article-content", ".content-article", ".article-body",
    "#article-body", "#content article", ".post__text", ".story-content",
    ".single-content", ".content"
]

def extract_article_text(html):
    soup = BeautifulSoup(html, "html.parser")
    node_text, best_len = "", 0
    for sel in SEL_CANDIDATES:
        cand = soup.select_one(sel)
        if cand:
            text = " ".join(p.get_text(" ", strip=True) for p in (cand.find_all(["p","h2","li"]) or [cand]))
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > best_len:
                best_len = len(text); node_text = text
    if not node_text:
        text = " ".join(p.get_text(" ", strip=True) for p in soup.find_all("p"))
        node_text = re.sub(r"\s+", " ", text).strip()
    return node_text[:5000] if node_text else ""

def html_from_entry(entry):
    if "content" in entry and entry.content:
        if isinstance(entry.content, list): return entry.content[0].get("value","")
        if isinstance(entry.content, dict): return entry.content.get("value","")
    return entry.get("summary","") or entry.get("description","")

# ================== SCRAPE ==================
def scrape_once(feeds):
    created, skipped = 0, 0
    for feed in feeds:
        try:
            fp = feedparser.parse(feed)
        except Exception as e:
            print(f"[FEED] parse error {feed}: {e}")
            continue
        feed_link = (getattr(fp, "href", None) or getattr(fp.feed, "link", None) or "").strip()
        for e in fp.entries[:20]:
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

                title_src = (e.get("title") or "(Sans titre)").strip()

                # page → extraction texte
                page_html = ""
                try:
                    page_html = http_get(link)
                except Exception as ee:
                    print(f"[PAGE] fetch fail {link}: {ee}")
                article_text = extract_article_text(page_html) if page_html else ""
                if not article_text:
                    article_text = BeautifulSoup(html_from_entry(e), "html.parser").get_text(" ", strip=True)
                if not article_text or len(article_text) < 120:
                    skipped += 1; continue

                # image : RSS d'abord, puis page
                img_url = get_image_from_entry(e, page_html=page_html, page_url=link) or None
                if not img_url and feed_link:
                    img_url = get_image_from_entry(e, page_html=page_html, page_url=feed_link)
                local_path, sha1 = download_image(img_url) if img_url else (None, None)
                if sha1:
                    con = db()
                    try:
                        if con.execute("SELECT 1 FROM posts WHERE image_sha1=?", (sha1,)).fetchone():
                            skipped += 1; con.close(); continue
                    finally:
                        con.close()

                # TITRE + TEXTE EN FR (robuste)
                title_fr, body_text, sure_fr = rewrite_article_fr(title_src, article_text)
                if not body_text:
                    skipped += 1; continue

                now = datetime.now(timezone.utc).isoformat()
                status = "draft" if not sure_fr else "draft"  # reste en brouillon pour validation

                con = db()
                try:
                    con.execute("""INSERT INTO posts
                      (title, body, status, created_at, updated_at, publish_at, image_url, image_sha1, orig_link, source)
                      VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (title_fr, body_text, status, now, now, None, local_path, sha1, link, fp.feed.get("title","")))
                    con.commit()
                    created += 1
                finally:
                    con.close()
            except Exception as e:
                skipped += 1
                print(f"[ENTRY] skipped due to error: {e}")
                traceback.print_exc()
    return created, skipped

# ================== SCHEDULER (publication auto) ==================
def publish_due_loop():
    while True:
        try:
            now = datetime.now(timezone.utc).isoformat()
            con = db()
            try:
                rows = con.execute(
                    "SELECT id FROM posts WHERE status='scheduled' AND publish_at IS NOT NULL AND publish_at <= ?",
                    (now,)).fetchall()
                if rows:
                    ids = [r["id"] for r in rows]
                    con.execute(
                        f"UPDATE posts SET status='published', updated_at=? WHERE id IN ({','.join('?'*len(ids))})",
                        (now, *ids)
                    )
                    con.commit()
                    print(f"[SCHED] Published IDs: {ids}")
            finally:
                con.close()
        except Exception as e:
            print("[SCHED] loop error:", e)
        time.sleep(30)

# ================== UI ==================
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
        body_html = (r['body'] or '').replace("\n", "<br>")
        cards.append(f"<article><header><h3>{r['title']}</h3><small>{created}</small></header>{img}<p>{body_html}</p></article>")
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
        desc  = (r["body"] or "").replace("&","&amp;")
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
    openai_key   = get_setting("openai_key", ENV_OPENAI_KEY)
    openai_model = get_setting("openai_model", ENV_OPENAI_MODEL)

    con = db()
    try:
        drafts    = con.execute("SELECT * FROM posts WHERE status='draft' ORDER BY id DESC").fetchall()
        scheduled = con.execute("SELECT * FROM posts WHERE status='scheduled' ORDER BY publish_at ASC").fetchall()
        pubs      = con.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC").fetchall()
    finally:
        con.close()

    def card(r, published=False):
        img = f"<img src='{r['image_url']}' style='max-width:200px'>" if r["image_url"] else ""
        pub_at = (r['publish_at'] or '')[:16]
        state_btns = ("<button name='action' value='unpublish' class='secondary'>⏸️ Dépublier</button>"
                      if published else
                      "<button name='action' value='publish' class='secondary'>✅ Publier maintenant</button>")
        return f"""
        <details>
          <summary><b>{r['title'] or '(Sans titre)'}</b> — <small>{r['status']}</small></summary>
          {img}
          <form method="post" action="{url_for('save', post_id=r['id'])}">
            <label>Titre<input name="title" value="{(r['title'] or '').replace('"','&quot;')}"></label>
            <label>Contenu<textarea name="body" rows="6">{r['body'] or ''}</textarea></label>
            <div class="grid">
              <button name="action" value="save">💾 Enregistrer</button>
              {state_btns}
              <button name="action" value="delete" class="contrast">🗑️ Supprimer</button>
            </div>
            <label>Publier à (UTC)
              <input type="datetime-local" name="publish_at" value="{pub_at}">
            </label>
            <div class="grid">
              <button name="action" value="schedule" class="secondary">🕒 Planifier</button>
            </div>
          </form>
        </details>"""

    body = f"""
    <h3>Paramètres</h3>
    <article>
      <form method="post" action="{url_for('save_settings')}">
        <div class="grid">
          <label>OpenAI API Key (priorité base)
            <input type="password" name="openai_key" placeholder="sk-..." value="{openai_key}">
          </label>
          <label>OpenAI Model
            <input name="openai_model" placeholder="gpt-4o-mini" value="{openai_model}">
          </label>
        </div>
        <label>Sources RSS (une URL par ligne)
          <textarea name="feeds" rows="6">{feeds}</textarea>
        </label>
        <button>💾 Enregistrer les paramètres</button>
      </form>
      <form method="post" action="{url_for('import_now')}" style="margin-top:1rem">
        <button type="submit">🔁 Importer maintenant (scraping + réécriture)</button>
      </form>
    </article>

    <h4>Brouillons</h4>{''.join(card(r) for r in drafts) or "<p>Aucun brouillon.</p>"}
    <h4>Planifiés</h4>{''.join(card(r) for r in scheduled) or "<p>Aucun article planifié.</p>"}
    <h4>Publiés</h4>{''.join(card(r, True) for r in pubs) or "<p>Rien de publié.</p>"}
    <p>Flux public : <code>{request.url_root}rss.xml</code></p>
    """
    return page(body, "Admin")

@app.post("/save-settings")
def save_settings():
    if not session.get("ok"): return redirect(url_for("admin"))
    set_setting("openai_key", request.form.get("openai_key","").strip())
    set_setting("openai_model", request.form.get("openai_model","").strip())
    set_setting("feeds", request.form.get("feeds",""))
    flash("Paramètres enregistrés.")
    return redirect(url_for("admin"))

@app.post("/import-now")
def import_now():
    if not session.get("ok"): return redirect(url_for("admin"))
    feeds_txt = get_setting("feeds", "\n".join(DEFAULT_FEEDS))
    feed_list = [u.strip() for u in feeds_txt.splitlines() if u.strip()]
    if not feed_list:
        flash("Aucune source RSS configurée.")
        return redirect(url_for("admin"))
    try:
        created, skipped = scrape_once(feed_list)
        flash(f"Import terminé : {created} nouveaux, {skipped} ignorés.")
    except Exception as e:
        print("[IMPORT] fatal:", e)
        traceback.print_exc()
        flash(f"Erreur d’import : {e}")
    return redirect(url_for("admin"))

@app.get("/import-now")
def import_now_get():
    flash("Utilise le bouton « Importer maintenant » dans l’admin.")
    return redirect(url_for("admin"))

@app.post("/save/<int:post_id>")
def save(post_id):
    if not session.get("ok"): return redirect(url_for("admin"))
    action     = request.form.get("action","save")
    title      = strip_tags(request.form.get("title","").strip())
    body       = strip_tags(request.form.get("body","").strip())
    publish_at = request.form.get("publish_at","").strip()

    if body and not body.rstrip().endswith("- Arménie Info"):
        body = body.rstrip() + "\n\n- Arménie Info"

    con = db()
    try:
        con.execute("UPDATE posts SET title=?, body=?, updated_at=? WHERE id=?",
                    (title, body, datetime.now(timezone.utc).isoformat(timespec="minutes"), post_id))
        if action == "publish":
            con.execute("UPDATE posts SET status='published', publish_at=NULL WHERE id=?", (post_id,))
            flash("Publié immédiatement.")
        elif action == "unpublish":
            con.execute("UPDATE posts SET status='draft', publish_at=NULL WHERE id=?", (post_id,))
            flash("Dépublié.")
        elif action == "schedule":
            if not publish_at:
                flash("Choisis une date/heure (UTC) pour planifier.")
            else:
                iso_utc = publish_at if len(publish_at) == 16 else publish_at[:16]
                iso_utc += ":00+00:00" if len(iso_utc) == 16 else ""
                con.execute("UPDATE posts SET status='scheduled', publish_at=? WHERE id=?", (iso_utc, post_id))
                flash(f"Planifié pour {iso_utc} (UTC).")
        elif action == "delete":
            con.execute("DELETE FROM posts WHERE id=?", (post_id,))
            flash("Supprimé.")
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
threading.Thread(target=publish_due_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
