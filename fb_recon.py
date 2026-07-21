# -*- coding: utf-8 -*-
"""
fb_recon.py — RECONOCIMIENTO PROFUNDO de albumes de Facebook (ingenieria inversa).

Se corre EN EL TELEFONO (IP residencial: la unica que FB responde). Trabaja en
tres fases, sin cookies:

  FASE 1  Le pega al link por todas las puertas publicas y, aunque den MURO,
          COSECHA lo que Facebook filtra en el JSON: el ID del album (set=a.NNN),
          los fbids de las fotos y el conteo real.
  FASE 2  Con el ID del album, ataca la URL DIRECTA del album (/media/set/?set=)
          por www/mbasic/m x googlebot/navegador. Los albumes son superficie
          publica indexable: suelen abrir donde el /share/ no.
  FASE 3  Prueba los permalinks de foto (/photo/?fbid=X&set=a.Y) y mide si la
          pagina expone el fbid SIGUIENTE (para caminar la cadena del set).

No modifica el motor ni descarga fotos: solo mide y reporta.

    python fb_recon.py "https://www.facebook.com/share/p/XXXX/"
"""

import sys
import re
import time
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import resolver as R

_DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_UAS = [("googlebot", R._GOOGLEBOT_UA), ("movil", R._BROWSER_UA),
        ("escritorio", _DESKTOP_UA), ("facebookbot", R._FACEBOOKBOT_UA)]

_ALBUM_RE = re.compile(r"set=a\.(\d{6,})")
_PCB_RE = re.compile(r"set=pcb\.(\d{6,})")
_FBID_RE = re.compile(r"fbid=(\d{6,})|/photos?/(?:pcb\.)?(\d{6,})|"
                      r'"__isMedia":"Photo"[^}]{0,200}?"id":"(\d{6,})"')
_WALL_RE = re.compile(r"(?i)(you must log in|inicia sesi[oó]n|log into facebook|"
                      r"iniciar sesi[oó]n|/login/\?|checkpoint|login_required)")


def _swap(url: str, host: str) -> str:
    return urlparse(url)._replace(netloc=host).geturl()


def _get(url: str, ua: str):
    """(html, final_url, http_code) o (None, None, code_str)."""
    try:
        raw, final = R._http_get(url, ua, max_bytes=R._MAX_HTML_BYTES, referer=url)
        return raw.decode("utf-8", errors="replace"), final, "200"
    except Exception as e:
        return None, None, str(getattr(e, "code", None) or "ERR")


def _harvest(html: str):
    """IDs de album, fbids distintos y conteo que el HTML deja escapar."""
    albums = set(_ALBUM_RE.findall(html)) | {("pcb:" + x) for x in _PCB_RE.findall(html)}
    fbids = set()
    for a, b, c in _FBID_RE.findall(html):
        fbids.add(a or b or c)
    fbids.discard("")
    return albums, fbids


def _measure(html: str):
    """fotos que el motor puntua, total declarado, fbids, muro."""
    try:
        res = R.resolve_html(html, "https://www.facebook.com/")
        kept, avail = len(res.images), res.images_available
    except Exception:
        kept, avail = 0, None
    _, fbids = _harvest(html)
    return kept, avail, len(fbids), bool(_WALL_RE.search(html))


def _row(label, url, ua):
    t0 = time.time()
    html, final, code = _get(url, ua)
    if html is None:
        print(f"{label:24} {code:5}  (sin respuesta)")
        return None
    kb = len(html) // 1024
    kept, avail, nfb, wall = _measure(html)
    print(f"{label:24} {code:5} {kb:5d} {kept:6d} {str(avail or '-'):>6} "
          f"{nfb:6d} {('SI' if wall else 'no'):>5}  ({time.time()-t0:.1f}s)")
    return {"html": html, "final": final, "kept": kept, "avail": avail,
            "fbids": nfb, "wall": wall, "label": label}


def _header():
    print(f"{'PUERTA':24} {'HTTP':5} {'KB':>5} {'fotos':>6} {'total':>6} "
          f"{'fbids':>6} {'muro':>5}")
    print("-" * 70)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url = sys.argv[1].strip()
    p = urlparse(url)
    fbhost = (p.hostname or "").endswith("facebook.com")

    print(f"\nRECON de: {url}\n" + "=" * 70)
    print("\n### FASE 1 — puertas sobre el link original (y que filtra el JSON)")
    _header()
    hosts = [("www", url)]
    if fbhost:
        hosts += [("m", _swap(url, "m.facebook.com")),
                  ("mbasic", _swap(url, "mbasic.facebook.com"))]
    all_albums, all_fbids, best_final = set(), set(), url
    for hlbl, hurl in hosts:
        for ualbl, ua in _UAS:
            r = _row(f"{hlbl:7} + {ualbl:11}", hurl, ua)
            if r:
                al, fb = _harvest(r["html"])
                all_albums |= al
                all_fbids |= fb
                if r["final"] and "/login" not in r["final"]:
                    best_final = r["final"]

    print("-" * 70)
    print(f"\nCOSECHA:  albumes={sorted(all_albums) or '-'}  "
          f"fbids_distintos={len(all_fbids)}  url_real={best_final[:60]}")

    numeric_albums = [a for a in all_albums if not a.startswith("pcb:")]
    if not fbhost or not numeric_albums:
        print("\nSin ID de album numerico que atacar. Comparte esta salida y "
              "vemos el siguiente angulo (permalink/canonica).")
        return 0

    print("\n### FASE 2 — ATAQUE DIRECTO AL ALBUM (/media/set/?set=a.ID)")
    _header()
    win2 = None
    for alb in numeric_albums[:2]:
        for form in (f"https://www.facebook.com/media/set/?set=a.{alb}",
                     f"https://www.facebook.com/media/set/?set=a.{alb}&type=3"):
            for hlbl, host in (("www", "www.facebook.com"),
                               ("mbasic", "mbasic.facebook.com"),
                               ("m", "m.facebook.com")):
                for ualbl, ua in (("googlebot", R._GOOGLEBOT_UA),
                                  ("movil", R._BROWSER_UA)):
                    r = _row(f"{hlbl:6}/{ualbl:9}", _swap(form, host), ua)
                    if r and (win2 is None or r["kept"] > win2["kept"]
                              or r["fbids"] > win2["fbids"]):
                        win2 = r
            break  # el form con &type=3 es de respaldo; probamos el simple 1o
    print("-" * 70)

    print("\n### FASE 3 — PERMALINK DE FOTO + ¿expone el siguiente fbid?")
    _header()
    sample = list(all_fbids)[:2]
    alb = numeric_albums[0]
    for fb in sample:
        for hlbl, host, ua in (("www/gbot", "www.facebook.com", R._GOOGLEBOT_UA),
                               ("mbasic/mov", "mbasic.facebook.com", R._BROWSER_UA)):
            u = _swap(f"https://www.facebook.com/photo/?fbid={fb}&set=a.{alb}", host)
            r = _row(f"{hlbl:12} {fb[:8]}", u, ua)
            if r:
                _, fbs_here = _harvest(r["html"])
                otros = fbs_here - {fb}
                if otros:
                    print(f"      ↳ esta foto expone OTROS fbids "
                          f"({len(otros)}): se puede CAMINAR la cadena.")
    print("-" * 70)

    print("\n### VEREDICTO")
    total = max((r for r in ([win2] if win2 else []) if r["avail"]),
                key=lambda r: r["avail"], default=None)
    if win2 and win2["kept"] >= 2:
        print(f"✅ El album directo SI abre: [{win2['label'].strip()}] vio "
              f"{win2['kept']} fotos. Cableamos /media/set/ al motor -> album "
              f"completo SIN cookies.")
    elif all_fbids and len(all_fbids) >= 3:
        print(f"🟡 El album no se renderiza entero, pero tenemos "
              f"{len(all_fbids)} fbids + el ID del album. Camino: enumerar los "
              f"permalinks de foto (con o sin caminar la cadena). Sin cookies.")
    else:
        print("🔴 Ni el album directo ni los permalinks sueltan las fotos a un "
              "anonimo. Aqui FB exige sesion de verdad: cookies como respaldo.")
    print("\nPega esta salida completa y decidimos el cableado con datos.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
