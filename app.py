from flask import Flask, request, redirect, url_for, Response, render_template_string, session, flash
import os, sqlite3, hashlib, io, re, traceback, json
from datetime import datetime, timezone
from urllib.parse import urljoin
import requests, feedparser
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError

# Planification
from apscheduler.schedulers.background import BackgroundScheduler

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
IMG_MIN_W, IMG_MIN_H = 200, 120  # rejeter les miniatures

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
      status TEXT DEFAULT 'draft',
      created_at TEXT,
      updated_at TEXT,
      publish_at TEXT,
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

def clean_title(t: str) -> str:
    t = strip_tags(t or "").strip()
    t = re.sub(r'^(titre|title|headline)\s*[:\-–—]\s*', '', t, flags=re.I)
    t = t.strip('«»"“”\'` ').strip()
    if not t: t = "Actualité"
    return t[:1].upper() + t[1:]

SIGN_REGEX = re.compile(r'(\s*[-–—]\s*Arménie\s+Info\s*)+$', re.I)

def ensure_signature(text: str) -> str:
    t = strip_tags(text or "").rstrip()
    t = SIGN_REGEX.sub('', t).rstrip()
    return (t + "\n\n- Arménie Info").strip()

def http_get(url, timeout=25):
    r = requests.get(url, timeout=timeout, allow_redirects=True, headers={
        "User-Agent": "Mozilla/5.0 (+RenderBot)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr,en;q=0.8",
    })
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    return r.text

# --- Sauvegarde image locale + validation taille ---
def _save_bytes_to_image(data: bytes):
    sha1 = hashlib.sha1(data).hexdigest()
    try:
        im = Image.open(io.BytesIO(data))
        im.verify()
        im = Image.open(io.BytesIO(data))  # reopen to read size
        w, h = im.size
        if w < IMG_MIN_W or h < IMG_MIN_H:
            return None, None
    except (UnidentifiedImageError, Exception):
        return None, None
    os.makedirs("static/images", exist_ok=True)
    path = f"static/images/{sha1}.jpg"
    if not os.path.exists(path):
        with open(path, "wb") as f: f.write(data)
    return "/"+path, sha1

def download_image(url):
    if not url: return None, None
    try:
        r = requests.get(url, timeout=25)
        r.raise_for_status()
        return _save_bytes_to_image(r.content)
    except Exception:
        return None, None

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

# ================== IMAGES — recherche agressive ==================
def _extract_from_jsonld(soup, base):
    for sc in soup.find_all("script", type=lambda v: v and "ld+json" in v):
        try:
            data = json.loads(sc.string or "{}")
        except Exception:
            continue
        def pick(obj):
            if isinstance(obj, str):
                return urljoin(base, obj)
            if isinstance(obj, list) and obj:
                return pick(obj[0])
            if isinstance(obj, dict):
                return pick(obj.get("url") or obj.get("@id") or obj.get("contentUrl") or obj.get("thumbnailUrl"))
            return None
        cand = None
        if isinstance(data, list):
            for item in data:
                cand = pick(item.get("image")) or pick(item.get("thumbnailUrl"))
                if cand: return cand
        if isinstance(data, dict):
            cand = pick(data.get("image")) or pick(data.get("thumbnailUrl"))
            if cand: return cand
    return None

def _extract_srcset(tag, base):
    srcset = tag.get("srcset") or tag.get("data-srcset")
    if not srcset: return None
    best_url, best_w = None, -1
    for part in srcset.split(","):
        part = part.strip()
        if not part: continue
        bits = part.split()
        u = urljoin(base, bits[0])
        w = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try: w = int(bits[1][:-1])
            except: w = 0
        if w > best_w:
            best_w, best_url = w, u
    return best_url

def image_candidates_from_html(html, base_url=None):
    soup = BeautifulSoup(html, "html.parser")
    cands = []

    # OpenGraph / Twitter
    for sel, attr in [
        ("meta[property='og:image:secure_url']", "content"),
        ("meta[property='og:image']", "content"),
        ("meta[name='twitter:image']", "content"),
        ("meta[itemprop='image']", "content"),
        ("link[rel='image_src']", "href"),
    ]:
        for m in soup.select(sel):
            if m.get(attr): cands.append(urljoin(base_url or "", m[attr]))

    # JSON-LD (NewsArticle)
    jld = _extract_from_jsonld(soup, base_url or "")
    if jld: cands.append(jld)

    # Article / figure
    roots = soup.select("article, .entry-content, .post-content, .article-content, .content-article, .article-body, .single-content, .content")
    if not roots: roots = [soup]
    for root in roots:
        for imgtag in root.find_all(["img","amp-img"]):
            u = imgtag.get("src") or imgtag.get("data-src") or imgtag.get("data-original")
            if not u:
                u = _extract_srcset(imgtag, base_url or "")
            if u:
                u = urljoin(base_url or "", u)
                # filtrer logos/icones sprites
                low = u.lower()
                if any(x in low for x in ["sprite", "icon", "logo", "placeholder", "blank"]):
                    continue
                cands.append(u)

    # dédupliquer en gardant l'ordre
    seen, uniq = set(), []
    for u in cands:
        if u not in seen:
            seen.add(u); uniq.append(u)
    return uniq

def find_best_image(html, page_url):
    for u in image_candidates_from_html(html, base_url=page_url):
        p, s = download_image(u)
        if p:  # validée par taille + Pillow
            return p, s
    return None, None

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

# ================== OPENAI (FR d’office) ==================
def active_openai():
    key   = get_setting("openai_key", os.environ.get("OPENAI_API_KEY","")).strip()
    model = get_setting("openai_model", os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)).strip() or DEFAULT_MODEL
    return key, model

def rewrite_article_fr(title_src: str, raw_text: str):
    key, model = active_openai()
    clean_input = strip_tags(raw_text or "")
    if not clean_input:
        return clean_title(title_src or "Actualité"), ensure_signature("(Contenu indisponible)")
    if not key:
        return clean_title(title_src or "Actualité"), ensure_signature("Traduction désactivée (ajoutez OPENAI_API_KEY).")

    try:
        payload = {
            "model": model,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content":
                 "Tu es un journaliste francophone. Réécris en FRANÇAIS : "
                 "1) une première ligne = un TITRE clair (sans le mot 'Titre'), "
                 "2) un corps concis (150–220 mots), sans HTML. N’ajoute rien d’autre."},
                {"role": "user", "content": f"Titre source: {title_src}\nTexte source: {clean_input}"}
            ]
        }
        r = requests.post("https://api.openai.com/v1/chat/completions",
                          headers={"Authorization": f"Bearer {key}", "Content-Type":"application/json"},
                          json=payload, timeout=60)
        j = r.json()
        out = (j.get("choices") or [{}])[0].get("message", {}).get("content","").strip()
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        if not lines: raise RuntimeError("empty AI result")
        ai_title = clean_title(lines[0])
        ai_body  = ensure_signature("\n".join(lines[1:]) if len(lines)>1 else clean_input)
        return ai_title, ai_body
    except Exception as e:
        print("[AI] fail:", e)
        return clean_title(title_src or "Actualité"), ensure_signature("Erreur de traduction automatique.")

# ================== SCRAPE (avec image forcée) ==================
def scrape_once(feeds):
    created, skipped = 0, 0
    for feed in feeds:
        try: fp = feedparser.parse(feed)
        except Exception as e:
            print(f"[FEED] parse error {feed}: {e}"); continue

        for e in fp.entries[:50]:
            try:
                link = e.get("link") or ""
                if not link: skipped += 1; continue

                con = db()
                try:
                    if con.execute("SELECT 1 FROM posts WHERE orig_link=?", (link,)).fetchone():
                        skipped += 1; con.close(); continue
                finally:
                    try: con.close()
                    except: pass

                title_src = (e.get("title") or "(Sans titre)").strip()

                page_html = ""
                try: page_html = http_get(link)
                except Exception as ee:
                    print(f"[PAGE] fetch fail {link}: {ee}")

                article_text = extract_article_text(page_html) if page_html else ""
                if not article_text:
                    article_text = BeautifulSoup(html_from_entry(e), "html.parser").get_text(" ", strip=True)
                if not article_text: article_text = "(Texte très bref.)"

                # IMAGES : agressif -> si rien validé, essaye image par défaut; sinon rien
                local_path = sha1 = None
                if page_html:
                    local_path, sha1 = find_best_image(page_html, link)
                if not local_path:
                    # Dernière tentative: candidats simples depuis l'entrée RSS
                    media = e.get("media_content") or e.get("media_thumbnail") or []
                    if isinstance(media, list) and media:
                        local_path, sha1 = download_image(media[0].get("url"))
                if not local_path:
                    def_p, _ = get_default_image()
                    if def_p: local_path = def_p  # fallback à ta photo

                # FR d'office
                title_fr, body_text = rewrite_article_fr(title_src, article_text)

                now = datetime.now(timezone.utc).isoformat()
                con = db()
                try:
                    con.execute("""INSERT INTO posts
                      (title, body, status, created_at, updated_at, image_url, image_sha1, orig_link)
                      VALUES(?,?,?,?,?,?,?,?)""",
                      (title_fr, body_text, "draft", now, now, local_path, sha1, link))
                    con.commit(); created += 1
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
        img = f"<img src='{r['image_url']}' alt='' style='max-width:100%;height:auto;margin:.5rem 0'>" if r["image_url"] else ""
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
    auto_minutes = get_setting("auto_minutes", "0")
    def_img_path, _ = get_default_image()

    con = db()
    try:
        drafts = con.execute("SELECT * FROM posts WHERE status='draft' ORDER BY id DESC").fetchall()
        pubs   = con.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC").fetchall()
    finally:
        con.close()

    def card(r, published=False):
        img = f"<img src='{r['image_url']}' style='max-width:220px'>" if r["image_url"] else "<em>— pas d’image —</em>"
        state_btn = ("<button name='action' value='unpublish' class='secondary'>⏸️ Dépublier</button>"
                     if published else "<button name='action' value='publish' class='secondary'>✅ Publier</button>")
        publish_at = (r["publish_at"] or "").replace("Z","")
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
              <label>Programmer (date/heure)<input type="datetime-local" name="publish_at" value="{publish_at}"></label>
            </div>
            <div class="grid">
              <button name="action" value="save">💾 Enregistrer</button>
              {state_btn}
              <button name="action" value="delete" class="contrast">🗑️ Supprimer</button>
            </div>
          </form>
        </details>"""

    body = f"""
    <h3>Paramètres</h3>
    <article>
      <form method="post" action="{url_for('save_settings')}">
        <div class="grid">
          <label>OpenAI API Key<input type="password" name="openai_key" placeholder="sk-..." value="{openai_key}"></label>
          <label>OpenAI Model<input name="openai_model" value="{openai_model}"></label>
          <label>Import automatique (minutes, 0=off)<input name="auto_minutes" value="{auto_minutes}"></label>
        </div>
        <label>Sources RSS (une URL par ligne)<textarea name="feeds" rows="6">{feeds}</textarea></label>
        <button>💾 Enregistrer</button>
      </form>
    </article>

    <article>
      <h4>Image par défaut (fallback)</h4>
      <div>{'<img src="'+def_img_path+'" style="max-width:220px">' if def_img_path else '<em>— aucune image par défaut —</em>'}</div>
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
    set_setting("auto_minutes", request.form.get("auto_minutes","0").strip())
    reschedule_jobs()
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
    action     = request.form.get("action","save")
    title      = clean_title(request.form.get("title",""))
    body       = ensure_signature(request.form.get("body",""))
    publish_at = request.form.get("publish_at","").strip() or None

    con = db()
    con.execute("UPDATE posts SET title=?, body=?, publish_at=?, updated_at=? WHERE id=?",
                (title, body, publish_at, datetime.now(timezone.utc).isoformat(timespec="minutes"), post_id))
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

# ================== JOBS (APScheduler) ==================
scheduler = BackgroundScheduler()

def job_auto_import():
    feeds_txt = get_setting("feeds", "\n".join(DEFAULT_FEEDS))
    feed_list = [u.strip() for u in feeds_txt.splitlines() if u.strip()]
    if not feed_list: return
    try:
        created, skipped = scrape_once(feed_list)
        print(f"[AUTO-IMPORT] {created} nouveaux, {skipped} ignorés.")
    except Exception as e:
        print("[AUTO-IMPORT] error:", e)

def job_auto_publish():
    now_iso = datetime.now().replace(second=0, microsecond=0).isoformat()
    con = db()
    try:
        rows = con.execute(
            "SELECT id FROM posts WHERE status='draft' AND publish_at IS NOT NULL AND publish_at <= ?",
            (now_iso,)
        ).fetchall()
        for r in rows:
            con.execute("UPDATE posts SET status='published' WHERE id=?", (r["id"],))
        con.commit()
        if rows:
            print(f"[AUTO-PUBLISH] {len(rows)} article(s) publiés.")
    except Exception as e:
        print("[AUTO-PUBLISH] error:", e)
    finally:
        con.close()

def reschedule_jobs():
    try:
        scheduler.remove_job("auto_import")
    except Exception: pass
    minutes = 0
    try: minutes = int(get_setting("auto_minutes","0") or "0")
    except: minutes = 0
    if minutes > 0:
        scheduler.add_job(job_auto_import, "interval", minutes=minutes, id="auto_import", replace_existing=True)
    # auto publish (chaque minute)
    try:
        scheduler.remove_job("auto_publish")
    except Exception: pass
    scheduler.add_job(job_auto_publish, "interval", minutes=1, id="auto_publish", replace_existing=True)

# -------- boot --------
def init_dirs(): os.makedirs("static/images", exist_ok=True)
init_db(); init_dirs()
if not scheduler.running:
    scheduler.start()
reschedule_jobs()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
