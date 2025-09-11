from flask import Flask, render_template_string, request, redirect, url_for

app = Flask(__name__)

# Page d'accueil
@app.route("/")
def home():
    return "<h1>Bienvenue sur la Console Arménienne</h1><p>Ceci est un site de test déployé sur Render.</p>"

# Page d'administration
@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        password = request.form.get("password")
        if password == "armenie":  # mot de passe simple
            return "<h2>Bienvenue dans la console d’administration</h2><p>Ici tu pourras gérer tes articles et ton flux RSS.</p>"
        else:
            return "<p>Mot de passe incorrect</p>"
    
    # Formulaire de login
    return '''
        <h2>Connexion à la Console</h2>
        <form method="post">
            <input type="password" name="password" placeholder="Mot de passe"/>
            <button type="submit">Entrer</button>
        </form>
    '''

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
