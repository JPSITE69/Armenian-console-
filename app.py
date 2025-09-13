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
DEFAULT_IMAGE = "/static/default.jpg"

DEFAULT_FEEDS = [
    "https://www.civilnet.am/news/feed/",
    "https://armenpress.am/rss/",
    "https://news.am/eng/rss/",
]

# OpenAI via ENV
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

def rewrite_article_fr(title_src: str, raw_text: str):
    """Traduction ou fallback"""
    if not raw_text:
        return (title_src or "Actualité", "", False)

    key, model = active_openai()
    clean_input = strip_tags(raw_text)

    if not key:
        return (title_src, clean_input[:400] + "\n\n- Arménie Info", False)

    try:
        prompt = (
            "Tu es journaliste francophone. Réécris en FRANÇAIS le Titre et le Corps de l'article.\n"
            "Corps 150–220 mots. JSON avec clés 'title' et 'body'. "
            "Body doit finir par : - Arménie Info\n\n"
            f"Titre source: {title_src}\n"
            f"Texte: {clean_input}"
        )
        r = requests.post("https://api.openai.com/v1/chat/completions",
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json={"model": model, "messages":[{"role":"user","content":prompt}]}, timeout=60)
        j = r.json()
        out = (j.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        data = _json.loads(out)
        return data.get("title",""), data.get("body",""), True
    except Exception as e:
        print("[AI] erreur:", e)
        return title_src, clean_input[:400] + "\n\n- Arménie Info", False

def http_get(url, timeout=15):
    r = requests.get(url, timeout=timeout, headers={"User-Agent":"Mozilla/5.0"})
    r.raise_for_status()
    return r.text

def find_image(entry, page_html=None, page_url=None):
    soup = BeautifulSoup(page_html or "", "html.parser")
    for sel in ["meta[property='og:image']","meta[name='twitter:image']"]:
        m = soup.select_one(sel)
        if m and m.get("content"): return urljoin(page_url or "", m["content"])
    imgtag = soup.find("img")
    if imgtag and imgtag.get("src"): return urljoin(page_url or "", imgtag["src"])
    return None

def download_image(url):
    if not url: return None, None
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.content
        sha1 = hashlib.sha1(data).hexdigest()
        os.makedirs("static/images", exist_ok=True)
        path = f"static/images/{sha1}.jpg"
        if not os.path.exists(path):
            with open(path, "wb") as f: f.write(data)
        return "/" + path, sha1
    except Exception as e:
        print("[IMG] fail:", e)
        return None, None

# ================== SCRAPER ==================
def scrape_once(feeds):
    created, skipped = 0, 0
    for feed in feeds:
        try:
            fp = feedparser.parse(feed)
            for e in fp.entries[:10]:
                link = e.get("link","")
                if not link: continue
                con = db()
                if con.execute("SELECT 1 FROM posts WHERE orig_link=?", (link,)).fetchone():
                    con.close(); skipped+=1; continue
                con.close()

                title_src = e.get("title","Sans titre")
                page_html = ""
                try: page_html = http_get(link)
                except: pass
                body = BeautifulSoup(e.get("summary",""), "html.parser").get_text(" ",strip=True)
                if page_html:
                    ps = BeautifulSoup(page_html,"html.parser").find_all("p")
                    if ps: body = " ".join(p.get_text() for p in ps)[:2000]

                if not body: continue
                img_url = find_image(e, page_html, link)
                local_path, sha1 = download_image(img_url) if img_url else (DEFAULT_IMAGE, None)

                t_fr, b_fr, ok = rewrite_article_fr(title_src, body)
                now = datetime.now(timezone.utc).isoformat()

                con = db()
                con.execute("""INSERT INTO posts
                (title,body,status,created_at,updated_at,image_url,image_sha1,orig_link,source)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (t_fr, b_fr, "draft", now, now, local_path, sha1, link, fp.feed.get("title","")))
                con.commit(); con.close()
                created+=1
        except Exception as e:
            print("[SCRAPER] error:", e)
    return created, skipped

# ================== SCHEDULER ==================
def publish_due_loop():
    while True:
        now = datetime.now(timezone.utc).isoformat()
        con = db()
        rows = con.execute("SELECT id FROM posts WHERE status='scheduled' AND publish_at<=?",(now,)).fetchall()
        if rows:
            ids=[r["id"] for r in rows]
            con.execute(f"UPDATE posts SET status='published' WHERE id IN ({','.join('?'*len(ids))})",( *ids,))
            con.commit()
        con.close()
        time.sleep(30)

# ================== ROUTES ==================
@app.get("/health")
def health(): return "OK"

@app.get("/")
def home():
    con=db(); rows=con.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC").fetchall(); con.close()
    body="<h2>Dernières publications</h2>"
    for r in rows:
        img=f"<img src='{r['image_url']}' style='max-width:100%'>" if r["image_url"] else ""
        body+=f"<article><h3>{r['title']}</h3>{img}<p>{r['body']}</p></article>"
    return body

@app.route("/admin", methods=["GET","POST"])
def admin():
    if request.method=="POST" and not session.get("ok"):
        if request.form.get("password")==ADMIN_PASS: session["ok"]=True; return redirect(url_for("admin"))
        flash("Mot de passe incorrect.")
    if not session.get("ok"):
        return "<form method=post><input type=password name=password><button>Entrer</button></form>"

    return "<h2>Admin</h2><p><a href='/import-now'>Importer</a></p>"

@app.get("/import-now")
def import_now():
    if not session.get("ok"): return redirect(url_for("admin"))
    feeds_txt = get_setting("feeds","\n".join(DEFAULT_FEEDS))
    feed_list=[u.strip() for u in feeds_txt.splitlines() if u.strip()]
    c,s=scrape_once(feed_list)
    return f"Import terminé : {c} créés, {s} ignorés"

# --------- boot ---------
init_db()
threading.Thread(target=publish_due_loop, daemon=True).start()

if __name__=="__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
