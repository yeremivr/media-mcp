"""Iconos de la PWA Cauce generados por codigo (no incrustados como binario).

server.py importa ICON_192_B64 / ICON_512_B64 y los sirve en /icon-192.png y
/icon-512.png. Se generan aqui con Pillow en vez de hardcodear un PNG porque:
  1) el commit de binarios/base64 largo via la API de GitHub es fragil
     (un solo caracter cambiado corrompe el PNG);
  2) es mas elegante y reproducible: el icono es matematica vectorial, no un
     asset opaco. Motivo grafico: tres olas teal = 'cauce' que fluye.
"""

import io
import math
import base64
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter

BG = (11, 14, 20)         # #0B0E14 fondo
BG2 = (18, 24, 34)        # leve degradado
FLOW = (61, 214, 196)     # #3DD6C4 teal claro
FLOW_DEEP = (31, 166, 151)  # #1FA697 teal profundo


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


@lru_cache(maxsize=4)
def render_icon(size: int) -> bytes:
    """Bytes PNG del icono al tamano pedido (fondo a sangre completa = maskable)."""
    SS = 4  # supersampling para bordes suaves
    S = size * SS
    img = Image.new("RGB", (S, S), BG)

    # Degradado vertical sutil del fondo.
    top = Image.new("RGB", (S, S), BG2)
    mask = Image.new("L", (S, S))
    md = ImageDraw.Draw(mask)
    for y in range(S):
        md.line([(0, y), (S, y)], fill=int(90 * (1 - y / S)))
    img = Image.composite(top, img, mask)

    # Glow teal detras de las olas.
    glow = Image.new("RGB", (S, S), BG)
    gd = ImageDraw.Draw(glow)
    gd.ellipse([S * 0.18, S * 0.34, S * 0.82, S * 0.66], fill=_lerp(BG, FLOW_DEEP, 0.35))
    glow = glow.filter(ImageFilter.GaussianBlur(S * 0.14))
    img = Image.blend(img, glow, 0.18)

    # Tres olas como bandas rellenas (bordes limpios, grosor uniforme).
    draw = ImageDraw.Draw(img)
    x0, x1 = S * 0.20, S * 0.80
    half = S * 0.028
    amp = S * 0.052
    wl = x1 - x0
    steps = 260
    for idx, cyf in enumerate((0.40, 0.50, 0.60)):
        cy = S * cyf
        color = _lerp(FLOW, FLOW_DEEP, idx / 2)
        top_pts, bot_pts = [], []
        for i in range(steps + 1):
            x = x0 + wl * i / steps
            ph = (x - x0) / wl * 2 * math.pi * 1.5 + idx * 0.9
            y = cy + amp * math.sin(ph)
            dydx = amp * math.cos(ph) * (2 * math.pi * 1.5 / wl)
            nlen = math.hypot(1.0, dydx)
            nx, ny = -dydx / nlen, 1.0 / nlen
            top_pts.append((x + nx * half, y + ny * half))
            bot_pts.append((x - nx * half, y - ny * half))
        draw.polygon(top_pts + bot_pts[::-1], fill=color)
        # Puntas redondeadas en los extremos.
        yb = cy + amp * math.sin(idx * 0.9)
        ye = cy + amp * math.sin(2 * math.pi * 1.5 + idx * 0.9)
        draw.ellipse([x0 - half, yb - half, x0 + half, yb + half], fill=color)
        draw.ellipse([x1 - half, ye - half, x1 + half, ye + half], fill=color)

    img = img.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# server.py consume estas constantes (base64 de los PNG ya renderizados).
ICON_192_B64 = base64.b64encode(render_icon(192)).decode()
ICON_512_B64 = base64.b64encode(render_icon(512)).decode()
