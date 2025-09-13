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

ENV_OPENAI_KEY   = os.environ.get("OPENAI_API_KEY", "").strip()
ENV_OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

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

# ================== UTILS ==================
TAG_RE = re.compile(r"<[^>]+>")
def strip_tags(s: str) -> str:
    return TAG_RE.sub("", s or "")

def active_openai():
    key = get_setting("openai_key", ENV_OPENAI_KEY)
    model = get_setting("openai_model", ENV_OPENAI_MODEL)
    return (key.strip(), model.strip())

def _title_from_text_fallback(fr_text: str) -> str:
    t = (fr_text or "").strip()
    if not t: return "Actualité"
    words = t.split()
    base = " ".join(words[:10]).strip().rstrip(".,;:!?")
    return base[:1].upper() + base[1:]

def rewrite_article_fr(title_src: str, raw_text: str):
    """Retourne (title_fr, body_fr). Forcé en français + signature."""
    if not raw_text:
        return (title_src or "Actualité", "", False)

    key, model = active_openai()
    clean_input = strip_tags(raw_text)

    def call_openai():
        prompt = (
            "Tu es un journaliste francophone. "
            "Traduis/réécris en FRANÇAIS le Titre et le Corps. "
            "Ton neutre et factuel, 150–220 mots. "
            "Réponds en JSON {\"title\":\"...\",\"body\":\"...\"}. "
            "Le body doit se terminer par: - Arménie Info.\n\n"
            f"Titre: {title_src}\n"
            f"Texte: {clean_input}"
        )
        r = requests.post("https://api.openai.com/v1/chat/completions",
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json={"model": model, "messages":[{"role":"user","content":prompt}], "temperature":0.2},
                          timeout=60)
        j = r.json()
        out = (j.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        try:
            data = _json.loads(out)
            title_fr = strip_tags(data.get("title","")).strip()
            body_fr  = strip_tags(data.get("body","")).strip()
        except Exception:
            parts = out.split("\n", 1)
            title_fr = strip_tags(parts[0]).strip()
            body_fr  = strip_tags(parts[1] if len(parts)>1 else "").strip()
        if not body_fr.endswith("- Arménie Info"):
            body_fr += "\n\n- Arménie Info"
        if not title_fr:
            title_fr = _title_from_text_fallback(body_fr)
        return title_fr, body_fr

    if key:
        try:
            return (*call_openai(), True)
        except Exception as e:
            print("[AI] erreur:", e)

    fr_body = strip_tags(raw_text)
    fr_body = " ".join(fr_body.split()[:200]).strip()
    if not fr_body.endswith("- Arménie Info"):
        fr_body += "\n\n- Arménie Info"
    return (_title_from_text_fallback(fr_body), fr_body, False)

# ================== HTTP & IMAGES ==================
def http_get(url, timeout=20):
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (+Bot)"})
    r.raise_for_status(); r.encoding = r.encoding or "utf-8"
    return r.text

def find_main_image_in_html(html, base_url=None):
    soup = BeautifulSoup(html, "html.parser")
    m = soup.select_one("meta[property='og:image']")
    if m and m.get("content"): return urljoin(base_url or "", m["content"])
    imgtag = soup.find("img")
    if imgtag and imgtag.get("src"): return urljoin(base_url or "", imgtag["src"])
    return None

def download_image(url):
    if not url: return None, None
    try:
        r = requests.get(url, timeout=20); r.raise_for_status()
        data = r.content
        sha1 = hashlib.sha1(data).hexdigest()
        os.makedirs("static/images", exist_ok=True)
        path = f"static/images/{sha1}.jpg"
        if not os.path.exists(path):
            with open(path,"wb") as f: f.write(data)
        return "/"+path, sha1
    except Exception as e:
        print(f"[IMG] fail {url}: {e}")
        return None, None

# ================== SCRAPE ==================
def scrape_once(feeds):
    created, skipped = 0, 0
    for feed in feeds:
        fp = feedparser.parse(feed)
        for e in fp.entries[:10]:
            link = e.get("link"); 
            if not link: continue
            con = db()
            if con.execute("SELECT 1 FROM posts WHERE orig_link=?", (link,)).fetchone():
                con.close(); skipped+=1; continue
            con.close()

            title_src = (e.get("title") or "").strip()
            page_html = ""
            try: page_html = http_get(link)
            except: pass
            article_text = BeautifulSoup(e.get("summary",""), "html.parser").get_text(" ", strip=True)
            if not article_text and page_html:
                article_text = BeautifulSoup(page_html, "html.parser").get_text(" ", strip=True)
            if len(article_text)<120: skipped+=1; continue

            # image → si rien → image par défaut
            img_url = find_main_image_in_html(page_html, link) if page_html else None
            local_path, sha1 = download_image(img_url) if img_url else (None,None)
            if not local_path:
                default_img = get_setting("default_image", "")
                if default_img:
                    local_path, sha1 = download_image(default_img)

            title_fr, body_text, _ = rewrite_article_fr(title_src, article_text)
            if not body_text: skipped+=1; continue

            now = datetime.now(timezone.utc).isoformat()
            con = db()
            con.execute("""INSERT INTO posts
              (title, body, status, created_at, updated_at, publish_at, image_url, image_sha1, orig_link, source)
              VALUES(?,?,?,?,?,?,?,?,?,?)""",
              (title_fr, body_text, "draft", now, now, None, local_path, sha1, link, fp.feed.get("title","")))
            con.commit(); con.close()
            created+=1
    return created, skipped

# ================== ADMIN ==================
LAYOUT = """<!doctype html><meta charset="utf-8"><title>{{title}}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
<body class="container"><main>{{ body|safe }}</main></body>"""
def page(body, title=""): return render_template_string(LAYOUT, body=body, title=title)

@app.route("/admin", methods=["GET","POST"])
def admin():
    if request.method=="POST" and not session.get("ok"):
        if request.form.get("password")==ADMIN_PASS:
            session["ok"]=True; return redirect(url_for("admin"))
    if not session.get("ok"):
        return page("<h3>Connexion</h3><form method=post><input type=password name=password><button>Entrer</button></form>")

    feeds = get_setting("feeds", "\n".join(DEFAULT_FEEDS))
    openai_key=get_setting("openai_key", ENV_OPENAI_KEY)
    openai_model=get_setting("openai_model", ENV_OPENAI_MODEL)
    default_img=get_setting("default_image","")

    body=f"""
    <h3>Paramètres</h3>
    <form method=post action="{url_for('save_settings')}">
      <label>OpenAI API Key <input type=password name=openai_key value="{openai_key}"></label>
      <label>OpenAI Model <input name=openai_model value="{openai_model}"></label>
      <label>Sources RSS<textarea name=feeds rows=5>{feeds}</textarea></label>
      <label>Image par défaut <input name=default_image value="{default_img}" placeholder="https://.../fallback.jpg"></label>
      <button>Enregistrer</button>
    </form>
    <form method=post action="{url_for('import_now')}"><button>Importer maintenant</button></form>
    """
    return page(body,"Admin")

@app.post("/save-settings")
def save_settings():
    if not session.get("ok"): return redirect(url_for("admin"))
    set_setting("openai_key", request.form.get("openai_key",""))
    set_setting("openai_model", request.form.get("openai_model",""))
    set_setting("feeds", request.form.get("feeds",""))
    set_setting("default_image", request.form.get("default_image",""))
    flash("Paramètres enregistrés.")
    return redirect(url_for("admin"))

@app.post("/import-now")
def import_now():
    feeds_txt=get_setting("feeds","\n".join(DEFAULT_FEEDS))
    feed_list=[u.strip() for u in feeds_txt.splitlines() if u.strip()]
    created, skipped=scrape_once(feed_list)
    flash(f"Import: {created} créés, {skipped} ignorés.")
    return redirect(url_for("admin"))

# ================== MAIN ==================
init_db()
if __name__=="__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
