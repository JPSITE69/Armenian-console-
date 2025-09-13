from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
import requests, io

# Police Anton (place le fichier Anton-Regular.ttf dans static/fonts/Anton-Regular.ttf)
ANTON = "static/fonts/Anton-Regular.ttf"

def _open_image_from_url_or_path(src):
    if isinstance(src, str) and src.startswith(("http://", "https://")):
        r = requests.get(src, timeout=12)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGBA")
    return Image.open(src).convert("RGBA")

def render_visual(template_path, photo_src, title, out_path,
                  photo_ratio_top=0.60,
                  band_left=0.08, band_right=0.92,
                  min_px=40, max_px=80, max_lines=4):
    """
    Colle la photo en haut (cover) puis centre le TITRE (Anton, MAJUSCULES)
    dans tout le bandeau noir en bas.
    - photo_ratio_top : % de hauteur réservée à la photo (0.60 = 60%)
    - band_left/band_right : marge latérale du texte (en % de largeur)
    - min_px/max_px : taille de police autorisée
    - max_lines : nb max de lignes pour le titre
    """
    tpl = Image.open(template_path).convert("RGBA")
    W, H = tpl.size

    # 1) Zone photo
    img_zone = (0, 0, W, int(H*photo_ratio_top))
    zone_w, zone_h = img_zone[2]-img_zone[0], img_zone[3]-img_zone[1]

    photo = _open_image_from_url_or_path(photo_src)
    ph_w, ph_h = photo.size
    scale = max(zone_w/ph_w, zone_h/ph_h)
    photo = photo.resize((int(ph_w*scale), int(ph_h*scale)), Image.Resampling.LANCZOS)

    left = (photo.width - zone_w)//2
    top  = (photo.height- zone_h)//2
    ph = photo.crop((left, top, left+zone_w, top+zone_h))

    canvas = tpl.copy()
    canvas.paste(ph, img_zone, img_zone)

    # 2) Zone texte = tout le rectangle noir
    band_x0, band_x1 = int(W*band_left), int(W*band_right)
    max_w = band_x1 - band_x0
    max_h = H - img_zone[3]

    draw = ImageDraw.Draw(canvas)
    title = (title or "").upper()

    # Fonction wrap texte
    def wrap_lines(font, text, max_w, max_lines):
        words = text.split()
        lines, line = [], []
        for w in words:
            test = " ".join(line+[w])
            if draw.textlength(test, font=font) <= max_w:
                line.append(w)
            else:
                if line: lines.append(" ".join(line))
                line = [w]
            if len(lines) >= max_lines:
                break
        if line and len(lines) < max_lines:
            lines.append(" ".join(line))
        return lines

    # Cherche la plus grande taille de police qui rentre
    best_font, best_lines = None, []
    for size in range(max_px, min_px-1, -2):
        font = ImageFont.truetype(ANTON, size)
        lines = wrap_lines(font, title, max_w, max_lines)
        if not lines: continue
        h = sum(font.getbbox(l)[3] for l in lines)
        if h <= max_h:
            best_font, best_lines = font, lines
            break

    # Dessine le texte centré
    if best_font and best_lines:
        total_h = sum(best_font.getbbox(l)[3] for l in best_lines)
        y = img_zone[3] + (max_h-total_h)//2
        for l in best_lines:
            w = draw.textlength(l, font=best_font)
            x = band_x0 + (max_w-w)//2
            draw.text((x,y), l, font=best_font, fill="white")
            y += best_font.getbbox(l)[3]

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "JPEG", quality=90)
    return out_path
