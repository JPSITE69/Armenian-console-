from flask import Flask, request, redirect, url_for, Response, render_template_string, session, flash
import os, sqlite3, hashlib, io, re, traceback
from datetime import datetime, timezone
from urllib.parse import urljoin
import requests, feedparser
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError, ImageDraw, ImageFont

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

DEFAULT_MODEL = "gpt-4o-mini"

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ================== DB ==================
def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.execute("""CREATE TABLE IF NOT EXISTS posts(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT,
      body  TEXT,
      status TEXT DEFAULT 'draft',    -- draft | published
      created_at TEXT,
      updated_at TEXT,
      image_url TEXT,
      image_sha1 TEXT,
      orig_link TEXT UNIQUE
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

# ================== UTILS ==================
TAG_RE = re.compile(r"<[^>]+>")

def strip_tags(s: str) -> str:
    return TAG_RE.sub("", s or "")

def sanitize_filename(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", (s or "").strip())[:40] or "img"

def title_from_text(text: str) -> str:
    t = (text or "").strip()
    if not t: return "Actualité"
    t = " ".join(t.split())[:120]
    # première phrase ou ~12 mots
    first = re.split(r"[.!?\n]", t)[0]
    if len(first.split()) < 3:
        first = " ".join(t.split()[:12])
    return first[:1].upper() + first[1:]

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
    for sel, attr in [("meta[property='og:image']", "content"),
                      ("meta[name='twitter:image']", "content")]:
        m = soup.select_one(sel)
        if m and m.get(attr):
            return urljoin(base_url or "", m[attr])
    a = soup.find("article")
    if a:
        imgtag = a.find("img")
        if imgtag and imgtag.get("src"):
            return urljoin(base_url or "", imgtag["src"])
    imgtag = soup.find("img")
    if imgtag and imgtag.get("src"):
        return urljoin(base_url or "", imgtag["src"])
    return None

def get_image_from_entry(entry, page_html=None, page_url=None):
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
    for k in ("content","summary","description"):
        v = entry.get(k)
        html = ""
        if isinstance(v, list) and v: html = v[0].get("value","")
        elif isinstance(v, dict):     html = v.get("value","")
        elif isinstance(v, str):      html = v
        if html:
            s = BeautifulSoup(html, "html.parser")
            imgtag = s.find("img")
            if imgtag and imgtag.get("src"):
                return urljoin(page_url or "", imgtag["src"])
    if page_html:
        return find_main_image_in_html(page_html, base_url=page_url)
    return None

def _save_bytes_to_image(data: bytes):
    sha1 = hashlib.sha1(data).hexdigest()
    try:
        im = Image.open(io.BytesIO(data))
        im.verify()
    except (UnidentifiedImageError, Exception) as e:
        print(f"[IMG] verify fail: {e}")
        return None, None
    os.makedirs("static/images", exist_ok=True)
    path = f"static/images/{sha1}.jpg"
    if not os.path.exists(path):
        with open(path, "wb") as f: f.write(data)
    return "/"+path, sha1

def download_image(url):
    if not url: return None, None
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        return _save_bytes_to_image(r.content)
    except Exception as e:
        print(f"[IMG] download failed for {url}: {e}")
        return None, None

def create_placeholder_image(title: str):
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), (24, 24, 24))
    draw = ImageDraw.Draw(img)
    text = (title or "Actualité").strip()
    try:
        font = ImageFont.truetype("arial.ttf", 48)
    except Exception:
        font = ImageFont.load_default()
    words = text.split()
    lines, line, max_w = [], "", int(W*0.85)
    for w in words:
        test = (line + " " + w).strip()
        if draw.textlength(test, font=font) <= max_w:
            line = test
        else:
            lines.append(line); line = w
    if line: lines.append(line)
    total_h = sum(font.size + 10 for _ in lines)
    y = (H - total_h)//2
    for ln in lines:
        tw = draw.textlength(ln, font=font)
        x = (W - tw)//2
        draw.text((x, y), ln, fill=(240,240,240), font=font)
        y += font.size + 10
    os.makedirs("static/images", exist_ok=True)
    name = sanitize_filename(text)
    sha1 = hashlib.sha1(text.encode("utf-8")).hexdigest()
    path = f"static/images/placeholder-{name}-{sha1[:8]}.jpg"
    img.save(path, "JPEG", quality=88)
    return "/"+path, sha1

# ---- image par défaut (ta photo) ----
def get_default_image():
    p = get_setting("default_image_path","").strip()
    s = get_setting("default_image_sha1","").strip()
    return (p or None, s or None)

def set_default_image_from_bytes(data: bytes):
    p, s = _save_bytes_to_image(data)
    if p and s:
        set_setting("default_image_path", p)
        set_setting("default_image_sha1", s)
    return p, s

def set_default_image_from_url(url: str):
    p, s = download_image(url)
    if p and s:
        set_setting("default_image_path", p)
        set_setting("default_image_sha1", s)
    return p, s

# ================== EXTRACTION TEXTE ==================
SEL_CANDIDATES = [
    "article", ".entry-content", ".post-content", ".article-content",
    ".content-article", ".article-body", "#article-body", ".single-content", ".content"
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

# ================== OPENAI (toujours FR) ==================
def active_openai():
    key   = get_setting("openai_key", os.environ.get("OPENAI_API_KEY","")).strip()
    model = get_setting("openai_model", os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)).strip() or DEFAULT_MODEL
    return key, model

def rewrite_article_fr(title_src: str, raw_text: str):
    """Toujours renvoyer FR. Si pas de clé/mauvaise réponse, petit texte FR neutre."""
    key, model = active_openai()
    clean_input = strip_tags(raw_text or "")
    if not clean_input:
        return (title_src or "Actualité", "(Contenu indisponible) - Arménie Info")
    if not key:
        return (title_from_text(clean_input), "Traduction désactivée (ajoutez OPENAI_API_KEY). - Arménie Info")
    try:
        payload = {
            "model": model,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content":
                 "Tu es un journaliste francophone. Réécris en FRANÇAIS : "
                 "1) une première ligne = TITRE clair, 2) un corps concis (150–220 mots) qui se termine par '- Arménie Info'."},
                {"role": "user", "content": f"Titre source: {title_src}\nTexte source: {clean_input}"}
            ]
        }
        r = requests.post("https://api.openai.com/v1/chat/completions",
                          headers={"Authorization": f"Bearer {key}", "Content-Type":"application/json"},
                          json=payload, timeout=60)
        j = r.json()
        out = (j.get("choices") or [{}])[0].get("message", {}).get("content","").strip()
        parts = out.split("\n", 1)
        title = strip_tags(parts[0]).strip() or title_from_text(clean_input)
        body  = strip_tags(parts[1] if len(parts)>1 else "").strip()
        if not body.endswith("- Arménie Info"):
            body += "\n\n- Arménie Info"
        return title, body
    except Exception as e:
        print("[AI] fail:", e)
        return (title_from_text(clean_input), "Erreur de traduction automatique. - Arménie Info")

# ================== SCRAPE ==================
def scrape_once(feeds):
    created, skipped = 0, 0
    for feed in feeds:
        try:
            fp = feedparser.parse(feed)
        except Exception as e:
            print(f"[FEED] parse error {feed}: {e}")
            continue
        for e in fp.entries[:50]:  # permissif
            try:
                link = e.get("link") or ""
                if not link:
                    skipped += 1; continue

                # éviter juste les liens déjà importés
                con = db()
                try:
                    if con.execute("SELECT 1 FROM posts WHERE orig_link=?", (link,)).fetchone():
                        skipped += 1; con.close(); continue
                finally:
                    try: con.close()
                    except: pass

                title_src = (e.get("title") or "(Sans titre)").strip()

                page_html = ""
                try:
                    page_html = http_get(link)
                except Exception as ee:
                    print(f"[PAGE] fetch fail {link}: {ee}")

                article_text = extract_article_text(page_html) if page_html else ""
                if not article_text:
                    article_text = BeautifulSoup(html_from_entry(e), "html.parser").get_text(" ", strip=True)
                if not article_text:
                    article_text = "(Texte très bref.)"

                # Image
                img_url = get_image_from_entry(e, page_html=page_html, page_url=link) or None
                local_path, sha1 = download_image(img_url) if img_url else (None, None)
                if not local_path:
                    # fallback -> ton image par défaut si définie
                    def_p = get_setting("default_image_path","").strip()
                    if def_p:
                        local_path = def_p
                    else:
                        local_path, sha1 = create_placeholder_image(title_src)

                # FR d’office
                title_fr, body_text = rewrite_article_fr(title_src, article_text)

                now = datetime.now(timezone.utc).isoformat()
                con = db()
                try:
                    con.execute("""INSERT INTO posts
                      (title, body, status, created_at, updated_at, image_url, image_sha1, orig_link)
                      VALUES(?,?,?,?,?,?,?,?)""",
                      (title_fr, body_text, "draft", now, now, local_path, sha1, link))
                    con.commit()
                    created += 1
                finally:
                    con.close()
            except Exception as e:
                skipped += 1
                print(f"[ENTRY] skipped: {e}")
                traceback.print_exc()
    return created, skipped

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
    <li><a href="{{ url_for('rss_xml') }}">RSS</a></li>
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
def health(): return "OK"

@app.get("/")
def home():
    con = db()
    try:
        rows = con.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC LIMIT 50").fetchall()
    finally:
        con.close()
    if not rows:
        return page("<h2>Dernières publications</h2><p>Aucune publication.</p>", "Publications")
    cards = []
    for r in rows:
        img = f"<img src='{r['image_url']}' alt='' style='max-width:100%;height:auto'>" if r["image_url"] else ""
        created = (r['created_at'] or '')[:16].replace('T',' ')
        body_html = (r['body'] or '').replace("\n","<br>")
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
            session["ok"] = True; return redirect(url_for("admin"))
        flash("Mot de passe incorrect."); return redirect(url_for("admin"))

    if not session.get("ok"):
        return page("""<h3>Connexion</h3><form method="post">
          <input type="password" name="password" placeholder="Mot de passe" required>
          <button>Entrer</button></form>""", "Connexion")

    feeds = get_setting("feeds", "\n".join(DEFAULT_FEEDS))
    openai_key   = get_setting("openai_key", os.environ.get("OPENAI_API_KEY",""))
    openai_model = get_setting("openai_model", os.environ.get("OPENAI_MODEL", DEFAULT_MODEL))
    def_img_path, _ = get_default_image()

    con = db()
    try:
        drafts = con.execute("SELECT * FROM posts WHERE status='draft' ORDER BY id DESC").fetchall()
        pubs   = con.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC").fetchall()
    finally:
        con.close()

    def card(r, published=False):
        img = f"<img src='{r['image_url']}' style='max-width:200px'>" if r["image_url"] else "<em>— pas d’image —</em>"
        state_btn = ("<button name='action' value='unpublish' class='secondary'>⏸️ Dépublier</button>"
                     if published else "<button name='action' value='publish' class='secondary'>✅ Publier</button>")
        img_editor = f"""
        <form method="post" action="{url_for('edit_image', post_id=r['id'])}" enctype="multipart/form-data">
          <div class="grid">
            <label>Remplacer l’image (fichier)<input type="file" name="file" accept="image/*"></label>
            <label>… ou via URL<input type="url" name="url" placeholder="https://..."></label>
          </div>
          <div class="grid">
            <button name="img_action" value="replace" class="secondary">🔄 Remplacer</button>
            <button name="img_action" value="remove" class="contrast">🗑️ Retirer</button>
          </div>
        </form>"""
        return f"""
        <details>
          <summary><b>{r['title'] or '(Sans titre)'}</b> — <small>{r['status']}</small></summary>
          {img}
          {img_editor}
          <form method="post" action="{url_for('save', post_id=r['id'])}">
            <label>Titre<input name="title" value="{(r['title'] or '').replace('"','&quot;')}"></label>
            <label>Contenu<textarea name="body" rows="6">{r['body'] or ''}</textarea></label>
            <div class="grid">
              <button name="action" value="save">💾 Enregistrer</button>
              {state_btn}
              <button name="action" value="delete" class="contrast">🗑️ Supprimer</button>
            </div>
          </form>
        </details>"""

    default_img_block = f"""
      <article>
        <h4>Image par défaut (fallback)</h4>
        <div>{'<img src="'+def_img_path+'" style="max-width:200px">' if def_img_path else '<em>— aucune image par défaut —</em>'}</div>
        <form method="post" action="{url_for('upload_default_image')}" enctype="multipart/form-data">
          <div class="grid">
            <label>Fichier<input type="file" name="default_image_file" accept="image/*"></label>
            <label>URL<input type="url" name="default_image_url" placeholder="https://..."></label>
          </div>
          <div class="grid">
            <button name="act" value="set" class="secondary">📸 Mettre à jour</button>
            <button name="act" value="clear" class="contrast">❌ Supprimer</button>
          </div>
        </form>
      </article>
    """

    body = f"""
    <h3>Paramètres</h3>
    <article>
      <form method="post" action="{url_for('save_settings')}">
        <div class="grid">
          <label>OpenAI API Key<input type="password" name="openai_key" placeholder="sk-..." value="{openai_key}"></label>
          <label>OpenAI Model<input name="openai_model" value="{openai_model}"></label>
        </div>
        <label>Sources RSS (une URL par ligne)<textarea name="feeds" rows="6">{feeds}</textarea></label>
        <button>💾 Enregistrer</button>
      </form>
    </article>

    {default_img_block}

    <article>
      <form method="post" action="{url_for('import_now')}" style="margin-top:1rem">
        <button type="submit">🔁 Importer maintenant</button>
      </form>
    </article>

    <h4>Brouillons</h4>{''.join(card(r) for r in drafts) or "<p>Aucun brouillon.</p>"}
    <h4>Publiés</h4>{''.join(card(r, True) for r in pubs) or "<p>Rien de publié.</p>"}
    <p>Flux public : <code>{request.url_root}rss.xml</code></p>
    """
    return page(body, "Admin")

# ---- actions admin ----
@app.post("/save-settings")
def save_settings():
    if not session.get("ok"): return redirect(url_for("admin"))
    set_setting("openai_key", request.form.get("openai_key","").strip())
    set_setting("openai_model", request.form.get("openai_model","").strip() or DEFAULT_MODEL)
    set_setting("feeds", request.form.get("feeds",""))
    flash("Paramètres enregistrés.")
    return redirect(url_for("admin"))

@app.post("/upload-default-image")
def upload_default_image():
    if not session.get("ok"): return redirect(url_for("admin"))
    act = request.form.get("act","set")
    if act == "clear":
        set_setting("default_image_path",""); set_setting("default_image_sha1","")
        flash("Image par défaut supprimée."); return redirect(url_for("admin"))
    fs = request.files.get("default_image_file")
    url = request.form.get("default_image_url","").strip()
    if fs and fs.filename:
        data = fs.read(); p, s = set_default_image_from_bytes(data)
    elif url:
        p, s = set_default_image_from_url(url)
    else:
        p = None
    flash("Image par défaut mise à jour." if p else "Échec de mise à jour de l’image par défaut.")
    return redirect(url_for("admin"))

@app.post("/import-now")
def import_now():
    if not session.get("ok"): return redirect(url_for("admin"))
    feeds_txt = get_setting("feeds", "\n".join(DEFAULT_FEEDS))
    feed_list = [u.strip() for u in feeds_txt.splitlines() if u.strip()]
    if not feed_list:
        flash("Aucune source RSS configurée."); return redirect(url_for("admin"))
    created, skipped = scrape_once(feed_list)
    flash(f"Import terminé : {created} nouveaux, {skipped} ignorés.")
    return redirect(url_for("admin"))

@app.post("/image/<int:post_id>")
def edit_image(post_id):
    if not session.get("ok"): return redirect(url_for("admin"))
    action = request.form.get("img_action","replace")
    if action == "remove":
        con = db(); con.execute("UPDATE posts SET image_url=NULL, image_sha1=NULL WHERE id=?", (post_id,))
        con.commit(); con.close(); flash("Image retirée."); return redirect(url_for("admin"))
    fs = request.files.get("file"); url = request.form.get("url","").strip()
    p = s = None
    if fs and fs.filename:
        try:
            data = fs.read(); p, s = _save_bytes_to_image(data)
        except Exception as e:
            print("[UPLOAD POST IMG] error:", e)
    elif url:
        p, s = download_image(url)
    if not p:
        flash("Impossible de remplacer l’image."); return redirect(url_for("admin"))
    con = db(); con.execute("UPDATE posts SET image_url=?, image_sha1=? WHERE id=?", (p, s, post_id))
    con.commit(); con.close(); flash("Image mise à jour.")
    return redirect(url_for("admin"))

@app.post("/save/<int:post_id>")
def save(post_id):
    if not session.get("ok"): return redirect(url_for("admin"))
    action = request.form.get("action","save")
    title  = strip_tags(request.form.get("title","").strip())
    body   = strip_tags(request.form.get("body","").strip())
    if body and not body.rstrip().endswith("- Arménie Info"):
        body = body.rstrip() + "\n\n- Arménie Info"
    con = db()
    con.execute("UPDATE posts SET title=?, body=?, updated_at=? WHERE id=?",
                (title, body, datetime.now(timezone.utc).isoformat(timespec="minutes"), post_id))
    if action == "publish":
        con.execute("UPDATE posts SET status='published' WHERE id=?", (post_id,))
        flash("Publié.")
    elif action == "unpublish":
        con.execute("UPDATE posts SET status='draft' WHERE id=?", (post_id,))
        flash("Dépublié.")
    elif action == "delete":
        con.execute("DELETE FROM posts WHERE id=?", (post_id,))
        flash("Supprimé.")
    else:
        flash("Enregistré.")
    con.commit(); con.close()
    return redirect(url_for("admin"))

@app.get("/logout")
def logout(): session.clear(); return redirect(url_for("home"))

# -------- boot --------
def init_dirs(): os.makedirs("static/images", exist_ok=True)
init_db(); init_dirs()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
