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

def column_exists(con, table, name):
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == name for r in rows)

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
    if not column_exists(con, "posts", "publish_at"):
        con.execute("ALTER TABLE posts ADD COLUMN publish_at TEXT")
    con.commit(); con.close()

# ================== HEALTHCHECK ==================
@app.route("/health")
def health():
    return "OK", 200

# ================== AUTRES ROUTES ==================
@app.get("/")
def home():
    con = db()
    try:
        rows = con.execute("SELECT * FROM posts WHERE status='published' ORDER BY id DESC LIMIT 50").fetchall()
    finally:
        con.close()
    if not rows:
        return "<h2>Aucune publication</h2>"
    html = "<h2>Dernières publications</h2>"
    for r in rows:
        img = f"<img src='{r['image_url']}' width='300'>" if r["image_url"] else ""
        body_html = (r['body'] or "").replace("\n", "<br>")
        html += f"<h3>{r['title']}</h3>{img}<p>{body_html}</p><hr>"
    return html

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
        items.append(f"<item><title>{title}</title><description><![CDATA[{desc}]]></description></item>")
    rss = f"<?xml version='1.0'?><rss version='2.0'><channel><title>{APP_NAME}</title>{''.join(items)}</channel></rss>"
    return Response(rss, mimetype="application/rss+xml")

@app.get("/admin")
def admin():
    return "<h1>Admin Console</h1><p>(Interface simplifiée pour test)</p>"

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

# --------- boot ---------
init_db()

if __name__ == "__main__":
    # ⚠️ Pour debug local uniquement, Render utilise gunicorn
    app.run(host="0.0.0.0", port=5000, debug=True)
