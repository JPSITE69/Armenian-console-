import os
from flask import Flask, request, redirect, url_for, render_template_string

app = Flask(__name__)

# Configuration basique
APP_NAME = "Console Arménienne"
ADMIN_PASS = os.environ.get("ADMIN_PASS", "armenie")
SECRET_KEY = os.environ.get("SECRET_KEY", "changeme")
app.secret_key = SECRET_KEY

# Accueil
@app.route("/")
def home():
    return "<h1>Bienvenue sur la Console Arménienne</h1><p>Serveur Flask opérationnel ✅</p>"

# Health check (utilisé par Render pour vérifier que le service tourne)
@app.route("/health")
def health():
    return "OK", 200

# Page admin minimale
@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        pwd = request.form.get("password")
        if pwd == ADMIN_PASS:
            return "<h2>Connexion réussie ✅</h2><p>Bienvenue dans l’interface admin.</p>"
        else:
            return "<h2>Mot de passe incorrect ❌</h2>", 403

    # Formulaire HTML simple
    html = """
    <h1>Console Arménienne - Admin</h1>
    <form method="post">
        <input type="password" name="password" placeholder="Mot de passe admin"/>
        <button type="submit">Se connecter</button>
    </form>
    """
    return render_template_string(html)

# Point de départ
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
