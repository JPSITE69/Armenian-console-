flask import Flask, request, redirect, url_for, Response, render_template_string, session, flash
sqlite3, os, hashlib, io, traceback, re, threading, time, json as _json
datetime import datetime, timezone
urllib.parse import urljoin
requests
bs4 import BeautifulSoup
feedparser
PIL import Image, UnidentifiedImageError
render_visual import render_visual

# ================== CONFIG ==================
_NAME   = "Console ArmÃ©nienne"
_PASS = os.environ.get("ADMIN_PASS", "armenie")
_KEY = os.environ.get("SECRET_KEY", "change-me")
_PATH    = "site.db"

_FEEDS = [
   "https://www.civilnet.am/news/feed/",
   "https://armenpress.am/rss/",
   "https://news.am/eng/rss/",
   "https://factor.am/feed",
   "https://hetq.am/hy/rss",
]

_SCRAPERS = [
   {"name":"CivilNet","index_url":"https://www.civilnet.am/news/",
    "link_selector":"article h3 a, article a[href^='/news/']",
    "title_selector":"h1",
    "content_selector":"article .entry-content, article .post-content, article .content, article",
    "image_selectors":[
       "meta[property='og:image']::content","meta[name='twitter:image']::content","article img::src","img::src"
    ],
    "max_items":7},
   {"name":"Armenpress","index_url":"https://armenpress.am/",
    "link_selector":"a[href*='/article/'], .news-item a, .list-item a",
    "title_selector":"h1, .article-title h1",
    "content_selector":".article-content, .content-article, article",
    "image_selectors":[
       "meta[property='og:image']::content","meta[name='twitter:image']::content",".article-content img::src","img::src"
    ],
    "max_items":7},
   {"name":"News.am (eng)","index_url":"https://news.am/eng/",
    "link_selector":"a[href*='/eng/news/'], .news-list a, article a",
    "title_selector":"h1",
    "content_selector":".article, .post-content, article",
    "image_selectors":[
       "meta[property='og:image']::content","meta[name='twitter:image']::content",".article img::src","img::src"
    ],
    "max_items":7},
   {"name":"Factor.am","index_url":"https://factor.am/",
    "link_selector":"article h2 a, .td_module_16 .entry-title a, .td-module-thumb a",
    "title_selector":"h1.entry-title, h1",
    "content_selector":".td-post-content, article .entry-content, article",
    "image_selectors":[
       "meta[property='og:image']::content","meta[name='twitter:image']::content",".td-post-content img::src","img::src"
    ],
    "max_items":7},
   {"name":"Hetq (HY)","index_url":"https://hetq.am/hy/",
    "link_selector":"article h3 a, .article-list a, a[href*='/hy/article/']",
    "title_selector":"h1",
    "content_selector":".article-content, .content-article, article",
    "image_selectors":[
       "meta[property='og:image']::content","meta[name='twitter:image']::content",".article-content img::src","img::src"
    ],
    "max_items":7},
]

_RSS_LIMIT = 20
_SCRAPER_LIMIT = 7

= Flask(__name__)
.secret_key = SECRET_KEY

# ================== DB ==================
db():
   con = sqlite3.connect(DB_PATH, check_same_thread=False)
   con.row_factory = sqlite3.Row
   return con

column_exists(con, table, name):
   rows = con.execute(f"PRAGMA table_info({table})").fetchall()
   return any(r["name"] == name for r in rows)

init_db():
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
   if not column_exists(con, "posts", "publish_at"):
       con.execute("ALTER TABLE posts ADD COLUMN publish_at TEXT")
   con.commit(); con.close()

get_setting(key, default=""):
   con = db()
   try:
       r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
       return r["value"] if r else default
   finally:
       con.close()

set_setting(key, value):
   con = db()
   try:
       con.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
       con.commit()
   finally:
       con.close()

# ================== UTILS TEXTE ==================
_RE = re.compile(r"<[^>]+>")
strip_tags(s: str) -> str:
   return TAG_RE.sub("", s or "")

_MAX_WORDS = 220

trim_to_words(text: str, n: int = BODY_MAX_WORDS) -> str:
   words = (text or "").split()
   if len(words) <= n:
       return (text or "").strip()
   return " ".join(words[:n]).rstrip(".,;:!?)").strip()

ensure_signature(body: str) -> str:
   b = (body or "").rstrip()
   b = re.sub(r'\s*[-â€“]\s*ArmÃ©nie Info\s*$', '', b).strip()
   if not b.endswith("- ArmÃ©nie Info"):
       b += "\n\n- ArmÃ©nie Info"
   return b

# ================== HTTP & IMAGES ==================
http_get(url, timeout=20):
   r = requests.get(url, timeout=timeout, allow_redirects=True, headers={
       "User-Agent": "Mozilla/5.0 (+RenderBot)",
       "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
       "Accept-Language": "fr,en;q=0.8",
   })
   r.raise_for_status()
   r.encoding = r.encoding or "utf-8"
   return r.text

soup_select_attr(soup, selector):
   attr = None
   sel = selector
   if "::content" in selector:
       sel, attr = selector.split("::content", 1)[0], "content"
   elif "::src" in selector:
       sel, attr = selector.split("::src", 1)[0], "src"
   tag = soup.select_one(sel.strip())
   if not tag:
       return None
   if attr:
       val = tag.get(attr)
       return val if val else None
   return tag.get_text(" ", strip=True)

find_main_image_in_html(html, base_url=None):
   soup = BeautifulSoup(html, "html.parser")
   for sel in ["meta[property='og:image']","meta[name='twitter:image']"]:
       m = soup.select_one(sel)
       if m and m.get("content"):
           return urljoin(base_url or "", m["content"])
   a = soup.find("article")
   if a:
       imgtag = a.find("img")
       if imgtag and imgtag.get("src"):
           return urljoin(base_url or "", imgtag.get("src"))
   imgtag = soup.find("img")
   if imgtag and imgtag.get("src"):
       return urljoin(base_url or "", imgtag.get("src"))
   return None

get_image_from_entry(entry, page_html=None, page_url=None):
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
               if href:
                   ext = href.lower().split("?")[0]
                   if any(ext.endswith(x) for x in (".jpg",".jpeg",".png",".webp",".gif")):
                       return urljoin(page_url or "", href)
   except Exception:
       pass
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
               return urljoin(page_url or "", imgtag.get("src"))
   if page_html:
       return find_main_image_in_html(page_html, base_url=page_url)
   return None

download_image(url):
   if not url: return None, None
   try:
       r = requests.get(url, timeout=20)
       r.raise_for_status()
       data = r.content
       sha1 = hashlib.sha1(data).hexdigest()
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

# ================== SCRAPE HELPERS ==================
_CANDIDATES = [
   "article",
   ".entry-content", ".post-content", ".td-post-content",
   ".article-content", ".content-article", ".article-body",
]

extract_article_text(html):
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

html_from_entry(entry):
   if "content" in entry and getattr(entry, "content", None):
       if isinstance(entry.content, list): return entry.content[0].get("value","")
       if isinstance(entry.content, dict): return entry.content.get("value","")
   return entry.get("summary","") or entry.get("description","")

already_have_link(link: str) -> bool:
   con = db()
   try:
       return con.execute("SELECT 1 FROM posts WHERE orig_link=?", (link,)).fetchone() is not None
   finally:
       con.close()

insert_post(title_fr, body_text, link, source, img_url):
   local_path, sha1 = download_image(img_url) if img_url else (None, None)
   if sha1:
       con = db()
       try:
           if con.execute("SELECT 1 FROM posts WHERE image_sha1=?", (sha1,)).fetchone():
               return False
       finally:
           con.close()
   now = datetime.now(timezone.utc).isoformat()
   con = db()
   try:
       con.execute("""INSERT INTO posts
         (title, body, status, created_at, updated_at, publish_at, image_url, image_sha1, orig_link, source)
         VALUES(?,?,?,?,?,?,?,?,?,?)""",
         (title_fr, body_text, "draft", now, now, None, local_path, sha1, link, source))
       con.commit()
       return True
   except Exception as e:
       print("[DB] insert_post error:", e)
       return False
   finally:
       con.close()

rewrite_article_fr(title_src, article_text):
   body = ensure_signature(trim_to_words(article_text))
   title = (title_src or "ActualitÃ©").strip()
   return title, body, True

scrape_rss_once(feeds, default_image_url=None):
   created, skipped = 0, 0
   rss_limit = int(get_setting("rss_limit", str(DEFAULT_RSS_LIMIT)) or DEFAULT_RSS_LIMIT)
   for feed in feeds:
       try:
           fp = feedparser.parse(feed)
       except Exception as e:
           print(f"[FEED] parse error {feed}: {e}")
           continue
       feed_title = fp.feed.get("title","") if getattr(fp, "feed", None) else ""
       for e in getattr(fp, "entries", [])[:rss_limit]:
           try:
               link = e.get("link") or ""
               if not link or already_have_link(link):
                   skipped += 1; continue
               title_src = (e.get("title") or "(Sans titre)").strip()
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
               img_url = get_image_from_entry(e, page_html=page_html, page_url=link) or default_image_url
               title_fr, body_text, _sure_fr = rewrite_article_fr(title_src, article_text)
               if not body_text:
                   skipped += 1; continue
               if insert_post(title_fr, body_text, link, feed_title, img_url):
                   created += 1
               else:
                   skipped += 1
           except Exception as ex:
               skipped += 1
               print(f"[RSS ENTRY] error: {ex}")
               traceback.print_exc()
   return created, skipped

normalize_url(base, href):
   if not href: return None
   href = href.strip()
   if href.startswith("#"): return None
   return urljoin(base, href)

scrape_index_once(scrapers_json, default_image_url=None):
   created, skipped = 0, 0
   default_limit = int(get_setting("scraper_limit", str(DEFAULT_SCRAPER_LIMIT)) or DEFAULT_SCRAPER_LIMIT)
   for cfg in scrapers_json:
       try:
           name = cfg.get("name","")
           index_url = cfg["index_url"]
           link_sel  = cfg["link_selector"]
           max_items = int(cfg.get("max_items", default_limit) or default_limit)
           html = http_get(index_url)
           soup = BeautifulSoup(html, "html.parser")
           links = []
           for a in soup.select(link_sel)[: max_items * 3]:
               href = a.get("href")
               full = normalize_url(index_url, href)
               if full and full not in links:
                   links.append(full)
               if len(links) >= max_items:
                   break
           for link in links:
               try:
                   if already_have_link(link):
                       skipped += 1; continue
                   page = http_get(link)
                   psoup = BeautifulSoup(page, "html.parser")
                   title_sel = cfg.get("title_selector","h1")
                   title_src = soup_select_attr(psoup, title_sel) or "(Sans titre)"
                   content_sel = cfg.get("content_selector") or ""
                   node_text = ""
                   if content_sel:
                       node = psoup.select_one(content_sel)
                       if node:
                           node_text = " ".join(p.get_text(" ", strip=True) for p in (node.find_all(["p","h2","li"]) or [node]))
                           node_text = re.sub(r"\s+", " ", node_text).strip()
                   if not node_text:
                       node_text = extract_article_text(page)
                   if not node_text or len(node_text) < 120:
                       skipped += 1; continue
                   img = None
                   for isel in cfg.get("image_selectors", []):
                       val = soup_select_attr(psoup, isel)
                       if val:
                           img = urljoin(link, val); break
                   if not img:
                       img = find_main_image_in_html(page, base_url=link) or default_image_url
                   title_fr, body_text, _sure = rewrite_article_fr(title_src, node_text)
                   if not body_text:
                       skipped += 1; continue
                   if insert_post(title_fr, body_text, link, name, img):
                       created += 1
                   else:
                       skipped += 1
               except Exception as inner:
                   skipped += 1
                   print(f"[SCRAPER:{name}] article error:", inner)
       except Exception as e:
           print("[SCRAPER] config error:", e)
   return created, skipped

# ================== SCHEDULER ==================
publish_due_loop():
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
= """
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
     <li><a href="{{ url_for('logout') }}">DÃ©connexion</a></li>
   {% else %}
     <li><a href="{{ url_for('admin') }}">Connexion</a></li>
   {% endif %}
 </ul>
</nav>
<main>
 {% with m=get_flashed_messages() %}{% if m %}<article>{% for x in m %}<p>{{x}}</p>{% endfor %}</article>{% endif %}{% endwith %}
 {{ body|safe }}
</main>
<footer><small>&copy; {{year}} â€” {{appname}}</small></footer>
</body>"""
page(body, title=""):
   return render_template_string(LAYOUT, body=body, title=title or APP_NAME,
                                appname=APP_NAME, year=datetime.now().year)

@app.get("/health")
health():
   return "OK"

@app.get("/")
home():
   con = db()
   try:
       rows = con.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC LIMIT 50").fetchall()
   finally:
       con.close()
   if not rows:
       return page("<h2>DerniÃ¨res publications</h2><p>Aucune publication pour lâ€™instant.</p>", "Publications")
   cards = []
   for r in rows:
       img = f"<img src='{r['image_url']}' alt='' style='max-width:100%;height:auto'>" if r["image_url"] else ""
       created = (r['created_at'] or '')[:16].replace('T',' ')
       body_html = (r['body'] or '').replace("\n", "<br>")
       cards.append(f"<article><header><h3>{r['title']}</h3><small>{created}</small></header>{img}<p>{body_html}</p></article>")
   return page("<h2>DerniÃ¨res publications</h2>" + "".join(cards), "Publications")

@app.get("/rss.xml")
rss_xml():
   con = db()
   try:
       rows = con.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC LIMIT 100").fetchall()
   finally:
       con.close()
   items = []
   base_url = request.url_root.rstrip("/")
   for r in rows:
       title = (r["title"] or "").replace("&","&amp;")
       desc  = (r["body"] or "").replace("&","&amp;")
       img_url = r['image_url'] or ""
       enclosure = f"<enclosure url='{base_url + img_url}' type='image/jpeg'/>" if img_url else ""
       pub   = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S %z')
       items.append(
           f"<item><title>{title}</title>"
           f"<link>{base_url}</link>"
           f"<guid isPermaLink='false'>{r['id']}</guid>"
           f"<description><![CDATA[{desc}]]></description>"
           f"{enclosure}<pubDate>{pub}</pubDate></item>"
       )
   rss = (
       "<?xml version='1.0' encoding='UTF-8'?>"
       "<rss version='2.0'><channel>"
       f"<title>{APP_NAME} â€” Flux</title>"
       f"<link>{base_url}</link>"
       f"<description>Articles publiÃ©s</description>"
       f"{''.join(items)}</channel></rss>"
   )
   return Response(rss, mimetype="application/rss+xml")

@app.route("/admin", methods=["GET","POST"])
admin():
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
   default_image = get_setting("default_image_url", "").strip()

   rss_limit = int(get_setting("rss_limit", str(DEFAULT_RSS_LIMIT)) or DEFAULT_RSS_LIMIT)
   scraper_limit = int(get_setting("scraper_limit", str(DEFAULT_SCRAPER_LIMIT)) or DEFAULT_SCRAPER_LIMIT)

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

                   if published else
                   "<button name='action' value='publish' class='secondary'>âœ… Publier maintenant</button>")
       return f"""
       <details>
         <summary><b>{r['title'] or '(Sans titre)'}</b> â€” <small>{r['status']}</small></summary>
         {img}
         <form method="post" action="{url_for('save', post_id=r['id'])}">
           <label>Titre<input name="title" value="{(r['title'] or '').replace('"','&quot;')}"></label>
           <label>Image (URL)
             <input name="image_url" value="{(r['image_url'] or '').replace('"','&quot;')}" placeholder="https://...">
           </label>
           <label>Contenu<textarea name="body" rows="8">{r['body'] or ''}</textarea></label>
           <div class="grid">
             <button name="action" value="save">ðŸ’¾ Enregistrer</button>
             {state_btns}

           </div>
           <label>Publier Ã  (UTC)
             <input type="datetime-local" name="publish_at" value="{pub_at}">
           </label>
           <div class="grid">
             <button name="action" value="schedule" class="secondary">ðŸ•’ Planifier</button>
           </div>
         </form>
       </details>"""

   body = f"""
   <h3>ParamÃ¨tres</h3>
   <article>
     <form method="post" action="{url_for('save_settings')}">
       <div class="grid">
         <label>Limite par flux RSS
           <input type="number" name="rss_limit" min="1" max="100" value="{rss_limit}">
         </label>
         <label>Limite par site (scraping)
           <input type="number" name="scraper_limit" min="1" max="50" value="{scraper_limit}">
         </label>
       </div>
       <label>Image par dÃ©faut (URL)
         <input name="default_image_url" placeholder="https://..." value="{default_image}">
       </label>
       <label>Sources RSS (une URL par ligne)
         <textarea name="feeds" rows="5">{feeds}</textarea>
       </label>
       <button>ðŸ’¾ Enregistrer les paramÃ¨tres</button>
     </form>

     <form method="post" action="{url_for('import_now')}" style="margin-top:1rem">

     </form>
   </article>

   <h4>Brouillons</h4>{''.join(card(r) for r in drafts) or "<p>Aucun brouillon.</p>"}
   <h4>PlanifiÃ©s</h4>{''.join(card(r) for r in scheduled) or "<p>Aucun article planifiÃ©.</p>"}
   <h4>PubliÃ©s</h4>{''.join(card(r, True) for r in pubs) or "<p>Rien de publiÃ©.</p>"}
   <p>Flux public : <code>{request.url_root}rss.xml</code></p>
   """
   return page(body, "Admin")

@app.post("/save-settings")
save_settings():
   if not session.get("ok"): return redirect(url_for("admin"))
   set_setting("feeds", request.form.get("feeds",""))
   set_setting("default_image_url", request.form.get("default_image_url","").strip())
   set_setting("rss_limit", request.form.get("rss_limit","").strip() or str(DEFAULT_RSS_LIMIT))
   set_setting("scraper_limit", request.form.get("scraper_limit","").strip() or str(DEFAULT_SCRAPER_LIMIT))
   flash("ParamÃ¨tres enregistrÃ©s.")
   return redirect(url_for("admin"))

@app.post("/import-now")
import_now():
   if not session.get("ok"): return redirect(url_for("admin"))
   default_image = get_setting("default_image_url","").strip() or None
   feeds_txt = get_setting("feeds", "\n".join(DEFAULT_FEEDS))
   feed_list = [u.strip() for u in feeds_txt.splitlines() if u.strip()]
   try:
       scrapers_cfg = _json.loads(get_setting("scrapers_json", "[]"))
       if not isinstance(scrapers_cfg, list):
           scrapers_cfg = []
   except Exception:
       scrapers_cfg = []
   c1, s1 = scrape_rss_once(feed_list, default_image_url=default_image)
   c2, s2 = scrape_index_once(scrapers_cfg, default_image_url=default_image)
   flash(f"Import terminÃ© : {c1+c2} nouveaux, {s1+s2} ignorÃ©s.")
   return redirect(url_for("admin"))

@app.get("/import-now")
import_now_get():
   flash("Utilise le bouton Â« Importer maintenant Â» dans lâ€™admin.")
   return redirect(url_for("admin"))

# --- GET /save pour Ã©viter 405 si on ouvre lâ€™URL Ã  la main
@app.get("/save/<int:post_id>")
save_get(post_id):
   return redirect(url_for("admin"))

@app.post("/save/<int:post_id>")
save(post_id):
   if not session.get("ok"): return redirect(url_for("admin"))
   action     = request.form.get("action","save")
   title      = strip_tags(request.form.get("title","").strip())
   body       = strip_tags(request.form.get("body","").strip())
   publish_at = request.form.get("publish_at","").strip()

   if body:
       body = trim_to_words(body, BODY_MAX_WORDS)
       body = ensure_signature(body)

   # RÃ©cupÃ¨re l'image de dÃ©part : formulaire -> valeur en DB -> image par dÃ©faut
   con = db()
   try:
       prev = con.execute("SELECT image_url FROM posts WHERE id=?", (post_id,)).fetchone()
   finally:
       con.close()
   image_in = (request.form.get("image_url","") or (prev and prev["image_url"]) or get_setting("default_image_url","")).strip()

   # GÃ©nÃ¨re le visuel brandÃ© et remplace l'URL
   try:
       slug = re.sub(r"[^a-zA-Z0-9\-]+", "-", (title or str(post_id))).strip("-").lower() or f"post-{post_id}"
       out_path = render_visual(
           template_path="static/templates/news_template.jpg",
           photo_src=image_in,
           title=title,
           out_path=f"static/renders/{slug}.jpg",
           photo_ratio_top=0.60, band_left=0.08, band_right=0.92,
           min_px=40, max_px=80, max_lines=4
       )
       final_image_url = "/" + out_path.lstrip("/")
   except Exception as e:
       print("[RENDER] failed:", e)
       final_image_url = image_in

   con = db()
   try:
       con.execute("UPDATE posts SET title=?, body=?, image_url=?, updated_at=? WHERE id=?",
                   (title, body, final_image_url, datetime.now(timezone.utc).isoformat(timespec="minutes"), post_id))

       if action == "publish":
           con.execute("UPDATE posts SET status='published', publish_at=NULL WHERE id=?", (post_id,))
           flash("PubliÃ© immÃ©diatement.")
       elif action == "unpublish":
           con.execute("UPDATE posts SET status='draft', publish_at=NULL WHERE id=?", (post_id,))
           flash("DÃ©publiÃ©.")
       elif action == "schedule":
           if not publish_at:
               flash("Choisis une date/heure (UTC) pour planifier.")
           else:
               iso_utc = publish_at if len(publish_at) == 16 else publish_at[:16]
               iso_utc += ":00+00:00" if len(iso_utc) == 16 else ""
               con.execute("UPDATE posts SET status='scheduled', publish_at=? WHERE id=?", (iso_utc, post_id))
               flash(f"PlanifiÃ© pour {iso_utc} (UTC).")
       elif action == "delete":
           con.execute("DELETE FROM posts WHERE id=?", (post_id,))
           flash("SupprimÃ©.")
       else:
           flash("EnregistrÃ©.")
       con.commit()
   finally:
       con.close()
   return redirect(url_for("admin"))

@app.get("/logout")
logout():
   session.clear(); return redirect(url_for("home"))

@app.get("/console")
alias_console():
   return redirect(url_for("admin"))

# --------- boot ---------
_db()
.Thread(target=publish_due_loop, daemon=True).start()

__name__ == "__main__":
   app.run(host="0.0.0.0", port=5000, debug=True)


2) Fichier: render_visual.py
# render_visual.py
PIL import Image, ImageDraw, ImageFont
pathlib import Path
requests, io

# Police Anton (place le fichier Anton-Regular.ttf dans static/fonts/Anton-Regular.ttf)
= "static/fonts/Anton-Regular.ttf"

_open_image_from_url_or_path(src):
   if isinstance(src, str) and src.startswith(("http://","https://")):
       r = requests.get(src, timeout=12)
       r.raise_for_status()
       return Image.open(io.BytesIO(r.content)).convert("RGBA")
   return Image.open(src).convert("RGBA")

render_visual(template_path, photo_src, title, out_path,
                 photo_ratio_top=0.60,
                 band_left=0.08, band_right=0.92,
                 min_px=40, max_px=80, max_lines=4):
   """
   Colle la photo en haut (cover) puis centre le TITRE (Anton, MAJUSCULES) dans tout le bandeau noir.
   - photo_ratio_top : % de hauteur rÃ©servÃ©e Ã  la photo (0.60 = 60%)
   - band_left/band_right : marges latÃ©rales du titre (en % de largeur)
   - min_px/max_px : taille de police autorisÃ©e
   - max_lines : nb max de lignes pour le titre
   """
   tpl = Image.open(template_path).convert("RGBA")
   W, H = tpl.size

   # 1) Zone photo
   img_zone = (0, 0, W, int(H*photo_ratio_top))
   zone_w, zone_h = img_zone[2]-img_zone[0], img_zone[3]-img_zone[1]

   photo = _open_image_from_url_or_path(photo_src)
   sc = max(zone_w/photo.width, zone_h/photo.height)
   ph = photo.resize((int(photo.width*sc), int(photo.height*sc)), Image.Resampling.LANCZOS)
   left = (ph.width-zone_w)//2
   top  = (ph.height-zone_h)//2
   ph = ph.crop((left, top, left+zone_w, top+zone_h))

   canvas = tpl.copy()
   canvas.paste(ph, (img_zone[0], img_zone[1]))

   # 2) Zone texte = tout le rectangle sous la photo
   l, r = int(W*band_left), int(W*band_right)
   t, b = img_zone[3], H
   max_w, max_h = r-l, b-t
   draw = ImageDraw.Draw(canvas)
   title = (title or "").upper()

   def wrap_lines(txt, font):
       words, lines, cur = txt.split(), [], ""
       for w in words:
           test = (cur+" "+w).strip()
           wbox = draw.textbbox((0,0), test, font=font)
           if wbox[2]-wbox[0] <= max_w:
               cur = test
           else:
               if cur: lines.append(cur)
               cur = w
       if cur: lines.append(cur)
       return lines

   best_font = None
   for size in range(max_px, min_px-1, -1):
       fnt = ImageFont.truetype(ANTON, size)
       lines = wrap_lines(title, fnt)
       if len(lines) <= max_lines:
           asc, desc = fnt.getmetrics()
           line_h = asc+desc
           total_h = int(len(lines)*line_h*1.18 - (line_h*0.18))
           if total_h <= max_h:
               best_font, best_lines, best_line_h = fnt, lines, line_h
               break
   if best_font is None:
       best_font = ImageFont.truetype(ANTON, min_px)
       best_lines = wrap_lines(title, best_font)[:max_lines]
       asc, desc = best_font.getmetrics()
       best_line_h = asc+desc

   total_h = int(len(best_lines)*best_line_h*1.18 - (best_line_h*0.18))
   y = t + (max_h - total_h)//2
   for ln in best_lines:
       bbox = draw.textbbox((0,0), ln, font=best_font)
       w_line = bbox[2]-bbox[0]
       x = l + (max_w - w_line)//2
       draw.text((x+2, y+2), ln, font=best_font, fill=(0,0,0,180))  # ombre
       draw.text((x, y), ln, font=best_font, fill=(255,255,255,255))  # texte
       y += int(best_line_h*1.18)

   Path(out_path).parent.mkdir(parents=True, exist_ok=True)
   canvas.convert("RGB").save(out_path, quality=95)
   return out_path
