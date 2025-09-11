# Arménie Console — Web & RSS (sans Telegram)

Mini site + console de modération :
- Récupère des articles depuis des **flux RSS**.
- Tu **édites/valide** dans `/admin` (protégé par mot de passe).
- Les éléments **validés** apparaissent sur la page publique `/` et dans le **flux RSS** `/feed.xml`.
- Idéal pour brancher **dlvr.it → Facebook**.

## Déploiement sur Render (gratuit)
1. Crée un dépôt GitHub avec ces fichiers.
2. Sur dashboard.render.com : **New → Web Service** → connecte le dépôt.
3. Laisse les commandes par défaut (déjà dans `render.yaml`).

### Variables d'environnement
- `ADMIN_PASS` : mot de passe d'accès à `/admin` (défaut: `armenie`, change-le).
- `SECRET_KEY` : générée automatiquement par Render.
- `FEEDS` : liste JSON des flux à importer.

## Utilisation
- Va sur `/admin` → connecte-toi.
- Clique **Récupérer** pour importer les nouveautés.
- Édite si besoin → **Approuver** pour publier.
- Les articles publiés : page d'accueil `/` + **RSS** `/feed.xml` (à fournir à dlvr.it).