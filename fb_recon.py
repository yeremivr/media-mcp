# -*- coding: utf-8 -*-
"""
fb_recon.py — INSTRUMENTO DE RECONOCIMIENTO para albumes de Facebook.

Se corre EN EL TELEFONO (IP residencial: la unica que Facebook responde). Le
pega al MISMO post por TODAS las puertas publicas conocidas —sin cookies— y te
dice, medido y no supuesto, CUANTAS fotos saca cada una. Con esa tabla decidimos
que puerta cablear al motor para bajar el album completo sin iniciar sesion.

    python fb_recon.py "https://www.facebook.com/<pagina>/posts/<id>/"

No modifica nada ni descarga fotos: solo baja HTML y cuenta. Si tienes cookies
de Facebook configuradas, tambien mide "con sesion" para comparar el techo real.
"""

import sys
import re
import time
from urllib.parse import urlparse, parse_qs

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import resolver as R

# UA de navegador de ESCRITORIO (mbasic a veces rinde distinto que el movil).
_DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _swap_host(url: str, host: str) -> str:
    return urlparse(url)._replace(netloc=host).geturl()


def _surfaces(url: str):
    """(etiqueta, url, ua) de cada puerta publica a medir para este post."""
    p = urlparse(url)
    base = (p.hostname or "").lower()
    variants = [("www", url)]
    if base.endswith("facebook.com"):
        variants += [
            ("m", _swap_host(url, "m.facebook.com")),
            ("mbasic", _swap_host(url, "mbasic.facebook.com")),
            ("web", _swap_host(url, "web.facebook.com")),
        ]
    uas = [("googlebot", R._GOOGLEBOT_UA),
           ("movil", R._BROWSER_UA),
           ("escritorio", _DESKTOP_UA),
           ("facebookbot", R._FACEBOOKBOT_UA)]
    for host_lbl, u in variants:
        for ua_lbl, ua in uas:
            # mbasic no tiene sentido con bots de tarjeta; se prueba con navegador.
            if host_lbl == "mbasic" and ua_lbl == "facebookbot":
                continue
            yield (f"{host_lbl:7} + {ua_lbl:11}", u, ua)


_FBID_RE = re.compile(r"(?:fbid=|/photos?/(?:pcb\.)?)(\d{6,})")
_SET_RE = re.compile(r"set=([a-z]+\.\d{6,}|pcb\.\d{6,}|\d{6,})", re.I)
_WALL_RE = re.compile(r"(?i)(you must log in|inicia sesi[oó]n|log into facebook|"
                      r"iniciar sesi[oó]n para continuar|/login/\?|checkpoint)")


def _analyze(html: str):
    """Senales crudas: fotos que el motor puntua, fbids distintos en el HTML,
    el set/album id, si hay muro de login."""
    try:
        res = R.resolve_html(html, "https://www.facebook.com/")
        kept = len(res.images)
        avail = res.images_available
    except Exception:
        kept, avail = 0, None
    fbids = set(_FBID_RE.findall(html))
    sets = set(_SET_RE.findall(html))
    wall = bool(_WALL_RE.search(html))
    return kept, avail, len(fbids), sets, wall


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url = sys.argv[1].strip()
    print(f"\nRECON de: {url}\n" + "=" * 78)
    print(f"{'PUERTA':22} {'HTTP':5} {'KB':>5} {'fotos':>6} {'total':>6} "
          f"{'fbids':>6} {'muro':>5}  set")
    print("-" * 78)

    rows = []
    for label, target, ua in _surfaces(url):
        t0 = time.time()
        try:
            raw, final = R._http_get(target, ua, max_bytes=R._MAX_HTML_BYTES,
                                     referer=url)
            html = raw.decode("utf-8", errors="replace")
            code = "200"
        except Exception as e:
            code = getattr(e, "code", None) or "ERR"
            print(f"{label:22} {str(code):5}  (sin respuesta: {str(e)[:38]})")
            continue
        kb = len(html) // 1024
        kept, avail, nfbids, sets, wall = _analyze(html)
        set_s = (next(iter(sets)) if sets else "-")[:26]
        dt = time.time() - t0
        print(f"{label:22} {code:5} {kb:5d} {kept:6d} {str(avail or '-'):>6} "
              f"{nfbids:6d} {('SI' if wall else 'no'):>5}  {set_s}  ({dt:.1f}s)")
        rows.append((label, kept, avail, nfbids, wall))

    print("-" * 78)
    if rows:
        best = max(rows, key=lambda r: (r[1], r[3]))   # mas fotos, luego mas fbids
        print(f"\nGANADORA por fotos vistas: [{best[0].strip()}] "
              f"-> {best[1]} fotos, {best[3]} fbids en el HTML.")
        techo = max((r[2] or 0) for r in rows)
        if techo:
            print(f"Total real declarado por el album (count): {techo}")
            if best[1] >= techo:
                print("✅ Una puerta SIN cookies ya trae el album completo. "
                      "No hacen falta cookies: cableamos esa puerta al motor.")
            elif best[3] >= techo:
                print("🟡 Ninguna puerta MUESTRA todas, pero una trae todos los "
                      "fbids en el HTML: se pueden enumerar las fotos una a una "
                      "(caminar el set). Sin cookies. Vale la pena implementarlo.")
            else:
                print("🔴 Ninguna puerta anonima llega al total ni expone los "
                      "fbids. Aqui Facebook SI exige sesion: cookies como "
                      "respaldo honesto (o aceptar 'N de M').")
    print("\nComparte esta tabla y decidimos el siguiente paso con datos.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
