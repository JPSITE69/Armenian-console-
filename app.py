def rewrite_article_fr(title_src: str, raw_text: str):
    key, model = active_openai()
    clean_input = strip_tags(raw_text or "")
    if not clean_input:
        return clean_title(title_src or "Actualité"), ensure_signature("(Contenu indisponible)")
    if not key:
        return clean_title(title_src or "Actualité"), ensure_signature(clean_input)

    try:
        payload = {
            "model": model,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content":
                 "Tu es un journaliste francophone. Réécris en FRANÇAIS : "
                 "1) Première ligne = TITRE clair (sans le mot 'Titre'), "
                 "2) Corps 150–220 mots, style info, sans HTML."},
                {"role": "user", "content":
                 f"Titre source: {title_src}\nTexte source: {clean_input}"}
            ]
        }
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload, timeout=60
        )
        j = r.json()
        out = (j.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()

        # découper lignes
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        ai_title = clean_title(lines[0])
        ai_body  = "\n\n".join(lines[1:]) if len(lines) > 1 else clean_input

        # assurer saut de ligne entre titre et contenu
        body_text = f"{ai_title}\n\n{ai_body}"
        body_text = ensure_signature(body_text)

        return ai_title, body_text

    except Exception as e:
        print("[AI] fail:", e)
        return clean_title(title_src or "Actualité"), ensure_signature(clean_input)
