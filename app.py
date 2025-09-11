from flask import Flask, render_template_string

app = Flask(__name__)

# Page d'accueil
@app.route("/")
def home():
    return render_template_string("""
    <html>
        <head><title>Console Arménienne</title></head>
        <body style="font-family: Arial; padding:20px;">
            <h1>Bienvenue sur la Console Arménienne</h1>
            <p>Ceci est un site de test déployé sur Render.</p>
        </body>
    </html>
    """)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
