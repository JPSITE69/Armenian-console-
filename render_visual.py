from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
import requests, io

# Police Anton (place le fichier Anton-Regular.ttf dans static/fonts/Anton-Regular.ttf)
ANTON = "static/fonts/Anton-Regular.ttf"

def _open_image_from_url_or_path(src):
    if isinstance(src, str) and src.startswith(("http://","https://")):
        r = requests.get(src, timeout=12)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGBA")
    return Image.open(src).convert("RGBA")

def render_visual(template_path, photo_src, title, out_path,
                  photo_ratio_top=0.60,
                  band_left=0.08, band_right=0.92,
                  min_px=40, max_px=80, max_lines=4):
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

    # 2) Zone texte = tout le rectangle noir
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
