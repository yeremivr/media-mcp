# -*- coding: utf-8 -*-
"""
test_facebook_bugs.py — pruebas de los DOS bugs de deteccion de medios que solo
afectaban a Facebook, con fixtures que replican la ESTRUCTURA REAL (no la red):

  BUG A — el "video fantasma" era la MUSICA DE FONDO. Facebook le pega una pista
          de audio a un album (`story_media_metadata.audio_url`) servida desde
          video.fbcdn.net con path de video; se colaba como el "video" del post
          y marcaba el album entero como media_type="video".

  BUG B — solo se detectaban 4 de ~12 fotos. A un crawler anonimo Facebook solo
          le incrusta el preview; declara el total en `all_subattachments.count`.
          Nivel 1: avisar "N de M" con la M correcta. Nivel 2: con cookies de
          sesion + UA de navegador, ver el album completo.

Se corre OFFLINE con resolve_html()/resolve() y `fetch` inyectado. No toca red.
Estilo identico a test_resolver.py / test_carousel.py (runner propio, sin pytest).
"""

import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import os
import tempfile
import resolver as R


def check(name, cond):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}")
    return bool(cond)


# --------------------------------------------------------------------------
# Constructor de un album de Facebook con la forma REAL del GraphQL embebido:
#   story_media_metadata.audio.audio_url  -> la MUSICA que FB le pega al post
#   ...attachment.all_subattachments      -> {count: TOTAL, nodes: [PREVIEW]}
# `count` dice el total AUNQUE `nodes` solo traiga el preview (crawler anonimo).
# --------------------------------------------------------------------------
def _fb_album_html(n_embedded: int, total: int, *, with_music: bool = True) -> str:
    nodes = ",".join(
        '{"media":{"image":{"uri":'
        f'"https:\\/\\/scontent.fbcdn.net\\/v\\/t39.0\\/foto{i}.jpg?_nc=1&oh=H{i}&oe=1",'
        '"width":960,"height":720}}}'
        for i in range(1, n_embedded + 1))
    music = (
        '"story_media_metadata":{"audio":{'
        '"audio_url":"https:\\/\\/video.fbcdn.net\\/v\\/t42.1790-2\\/'
        'levels_avicii.mp4?efg=eyJ2ZW5jIjoiaGQifQ&_nc_cat=1&oh=MUS&oe=6600",'
        '"title":"Levels","artist":"Avicii"}},' if with_music else "")
    return (
        '<!DOCTYPE html><html><head>'
        '<meta property="og:title" content="Tomorrowland 2011">'
        '<meta property="og:image" content="'
        'https://scontent.fbcdn.net/v/t39.0/foto1.jpg?_nc=1&oh=H1&oe=1">'
        '</head><body>'
        '<script>window.__d={"data":{"node":{"story":{'
        + music +
        '"attachments":[{"styles":{"attachment":{"all_subattachments":{'
        f'"count":{total},"nodes":[{nodes}]'
        '}}}}]}}}};</script>'
        '</body></html>')


# --------------------------------------------------------------------------
# Reel REAL de Facebook (browser_native_hd_url + su audio): un VIDEO de verdad,
# con su propio stream. Debe seguir resolviendo como "video".
# --------------------------------------------------------------------------
FACEBOOK_REEL = r"""
<!DOCTYPE html><html><head>
<meta property="og:title" content="Reel de verdad">
<meta property="og:image" content="https://scontent.fbcdn.net/v/t15.0/thumb.jpg?oe=abc">
</head><body>
<script>
window.__additionalData={"result":{"data":{"video":{
  "__typename":"Video","width":720,"height":1280,
  "browser_native_hd_url":"https:\/\/video.fbcdn.net\/v\/t42.1790-2\/hd_reel.mp4?efg=HD&_nc_cat=1&oh=HD&oe=6600",
  "browser_native_sd_url":"https:\/\/video.fbcdn.net\/v\/t42.1790-2\/sd_reel.mp4?efg=SD&_nc_cat=1&oh=SD&oe=6600"
}}}};
</script>
</body></html>
"""


# --------------------------------------------------------------------------
# Carrusel REAL de Instagram (sidecar de 3 fotos). NO debe cambiar en nada.
# --------------------------------------------------------------------------
INSTAGRAM_SIDECAR = r"""
<!DOCTYPE html><html><head>
<meta property="og:image" content="https://scontent.cdninstagram.com/v/t51.2885-15/A_n.jpg?_nc=a">
</head><body>
<script type="application/json">
{"edge_sidecar_to_children":{"edges":[
 {"node":{"display_url":"https://scontent.cdninstagram.com/v/t51.2885-15/A_n.jpg?_nc=a","width":1080,"height":1080}},
 {"node":{"display_url":"https://scontent.cdninstagram.com/v/t51.2885-15/B_n.jpg?_nc=b","width":1080,"height":1080}},
 {"node":{"display_url":"https://scontent.cdninstagram.com/v/t51.2885-15/C_n.jpg?_nc=c","width":1080,"height":1080}}
]}}</script>
</body></html>
"""


def main():
    ok = True

    print("\n=== BUG A: la musica de fondo NO es el video del post ===")
    r = R.resolve_html(_fb_album_html(4, 12),
                       "https://www.facebook.com/rudgr/posts/12345")
    ok &= check("un album de fotos + musica resuelve como 'carousel', no 'video'",
                r.media_type == "carousel")
    ok &= check("NINGUN formato de video sale del audio_url (la cancion)",
                all(f.kind != "video" for f in r.formats))
    ok &= check("si la pista de musica se expone, es kind='audio', no 'video'",
                all(("levels_avicii" not in f.url) or f.kind == "audio"
                    for f in r.formats))
    ok &= check("se detectan las 4 fotos del preview (no se pierde la 1a/ancla)",
                len(r.images) == 4)
    ok &= check("ningun 'formato' descargable es una de las fotos",
                all("foto" not in f.url for f in r.formats))

    print("\n=== BUG A (regresion): un VIDEO real de Facebook sigue siendo video ===")
    rv = R.resolve_html(FACEBOOK_REEL, "https://www.facebook.com/reel/999")
    ok &= check("el reel resuelve como 'video'", rv.media_type == "video")
    ok &= check("el ganador es el HD (browser_native_hd_url)",
                rv.formats and "hd_reel.mp4" in rv.formats[0].url
                and rv.formats[0].kind == "video")

    print("\n=== BUG A (regresion): Instagram intacto (3 fotos, sin fantasmas) ===")
    ri = R.resolve_html(INSTAGRAM_SIDECAR, "https://www.instagram.com/p/ABC/")
    ok &= check("el sidecar de IG sigue siendo 'carousel' de 3",
                ri.media_type == "carousel" and len(ri.images) == 3)
    ok &= check("IG no gana ningun formato de video fantasma",
                all(f.kind != "video" for f in ri.formats))

    print("\n=== BUG B Nivel 1: se reporta el CONTEO REAL del album ===")
    ok &= check("images_available = 12 aunque solo se vean 4",
                r.images_available == 12 and len(r.images) == 4)
    ok &= check("el diagnostico tambien lo expone",
                r.diagnostics.get("images_available") == 12)
    # Un album SIN campo count declarado: images_available cae a lo que se ve.
    r_nc = R.resolve_html(_fb_album_html(4, 4, with_music=False),
                          "https://www.facebook.com/x/posts/1")
    ok &= check("sin total declarado, images_available = las vistas (no miente)",
                r_nc.images_available == 4)
    # Instagram: como entrega la lista completa, el total == lo detectado.
    ok &= check("IG: images_available == fotos detectadas (no lo tocamos)",
                ri.images_available == 3)

    print("\n=== BUG B Nivel 2: con cookies de sesion se ve el album COMPLETO ===")
    # `fetch` inyectado que EMULA lo que hace _http_get: si hay cookie de FB
    # configurada, Facebook server-renderiza el album entero (12); si no, solo
    # el preview (4). Asi se prueba el flujo completo sesion -> album completo.
    def fake_fetch(url, ua, *, max_bytes, referer=None):
        if R._cookie_header_for(url):
            return _fb_album_html(12, 12).encode("utf-8"), url
        return _fb_album_html(4, 12).encode("utf-8"), url

    ck = (".facebook.com\tTRUE\t/\tTRUE\t0\tc_user\t100\n"
          ".facebook.com\tTRUE\t/\tTRUE\t0\txs\tSECRET\n")
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    f.write(ck); f.close()
    try:
        # SIN cookies -> Nivel 1: 4 vistas, pero avisa 12.
        os.environ.pop("FB_COOKIES_FILE", None)
        R.reset_fb_cookie_cache()
        r1 = R.resolve("https://www.facebook.com/rudgr/posts/12345",
                       fetch=fake_fetch, max_attempts=1)
        ok &= check("sin sesion: baja 4 pero reporta 12",
                    len(r1.images) == 4 and r1.images_available == 12)

        # CON cookies -> Nivel 2: 12 fotos de verdad.
        os.environ["FB_COOKIES_FILE"] = f.name
        R.reset_fb_cookie_cache()
        r2 = R.resolve("https://www.facebook.com/rudgr/posts/12345",
                       fetch=fake_fetch, max_attempts=1)
        ok &= check("con sesion: se detectan y bajan las 12 fotos del album",
                    len(r2.images) == 12 and r2.media_type == "carousel")
        ok &= check("y sigue sin colar la musica como video",
                    all(fmt.kind != "video" for fmt in r2.formats))
    finally:
        os.environ.pop("FB_COOKIES_FILE", None)
        R.reset_fb_cookie_cache()
        try:
            os.unlink(f.name)
        except Exception:
            pass

    print("\n=== BUG B Nivel 2 (mecanismo): _http_get inyecta el header Cookie ===")
    os.environ["FB_COOKIES_FILE"] = _write_tmp_cookies()
    R.reset_fb_cookie_cache()
    try:
        ok &= check("hay header Cookie para facebook.com",
                    R._cookie_header_for("https://www.facebook.com/x") is not None)
        ok &= check("hay header Cookie para el CDN (fbcdn.net)",
                    R._cookie_header_for("https://scontent.fbcdn.net/v/f.jpg") is not None)
        ok &= check("NO se manda cookie a Instagram (sesion de FB no le aplica)",
                    R._cookie_header_for("https://www.instagram.com/p/x/") is None)
    finally:
        os.environ.pop("FB_COOKIES_FILE", None)
        R.reset_fb_cookie_cache()

    print("\n" + ("=" * 62))
    print("RESULTADO:", "TODO PASA (OK)" if ok else "HAY FALLOS (FAIL)")
    return 0 if ok else 1


def _write_tmp_cookies() -> str:
    ck = (".facebook.com\tTRUE\t/\tTRUE\t0\tc_user\t100\n"
          ".facebook.com\tTRUE\t/\tTRUE\t0\txs\tSECRET\n"
          ".fbcdn.net\tTRUE\t/\tTRUE\t0\tdatr\tD\n")
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    f.write(ck); f.close()
    return f.name


if __name__ == "__main__":
    sys.exit(main())
