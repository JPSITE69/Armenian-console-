from flask import Flask

# Création de l'application Flask
app = Flask(__name__)

# Route d'accueil
@app.route("/")
def home():
    return "Bienvenue sur Console Arménie 🚀 — Déploiement Render OK"

# Route healthcheck (Render en a besoin)
@app.route("/health")
def health():
    return "OK", 200

# Exemple d'admin (mot de passe géré par variable d'environnement ADMIN_PASS)
@app.route("/admin")
def admin():
    return "Page admin (protégée plus tard)"

# Point d'entrée local (utile pour tests en local)
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
