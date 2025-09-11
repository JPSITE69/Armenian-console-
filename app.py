from flask import Flask

app = Flask(__name__)

@app.get("/")
def index():
    return "OK - serveur Flask opérationnel"

@app.get("/admin")
def admin():
    return "ADMIN OK - route /admin trouvée"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
