# -*- coding: utf-8 -*-
"""
test_resolver.py — pruebas del motor con fixtures que replican la ESTRUCTURA
REAL de cada plataforma (no la red). Verifica que el grafo+scorer:
  * encuentra la URL del medio aunque este anidada / con clave renombrada,
  * elige HD sobre SD, video sobre miniatura,
  * descarta avatares, posters y pixeles de tracking,
  * fusiona duplicados del mismo archivo (Union-Find),
  * extrae titulo/miniatura/autor del JSON-LD.

Se corre offline con resolve_html(). Tambien prueba resolve() con un `fetch`
falso (inyeccion de dependencia) para cubrir la cascada de UA/rewrites sin red.
"""

import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import resolver as R


# --------------------------------------------------------------------------
# FIXTURE 1 — Post de LinkedIn (estructura del extractor real de yt-dlp:
# <video data-sources='[{src,type,data-bitrate}]'> + og + JSON-LD + ruido)
# --------------------------------------------------------------------------
LINKEDIN_POST = """
<!DOCTYPE html><html><head>
<meta property="og:title" content="Una charla increible sobre grafos">
<meta property="og:image" content="https://media.licdn.com/dms/image/D4E05AQabc/thumb-1280.jpg?e=123&t=xyz">
<meta property="og:description" content="mira esto">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"SocialMediaPosting",
 "author":{"@type":"Person","name":"Ada Lovelace"},
 "headline":"Una charla increible sobre grafos",
 "thumbnailUrl":"https://media.licdn.com/dms/image/D4E05AQabc/thumb-1280.jpg?e=123&t=xyz"}
</script>
</head><body>
<img class="avatar" src="https://media.licdn.com/dms/image/profile-100x100.jpg?e=1&t=a">
<img src="https://static.licdn.com/tracking/pixel.gif?impression=1">
<video data-sources='[{"src":"https://dms.licdn.com/playlist/vid/v2/D4E05AQ720/mp4-720p-30fp-crf28/0/1699999?e=2145916800&v=beta&t=SIGNED720","type":"video/mp4","data-bitrate":"2400000"},{"src":"https://dms.licdn.com/playlist/vid/v2/D4E05AQ360/mp4-360p-30fp-crf28/0/1699999?e=2145916800&v=beta&t=SIGNED360","type":"video/mp4","data-bitrate":"600000"}]'
   data-captions-url="https://media.licdn.com/captions/en.vtt">
</video>
<script>window.__tracking={"beaconUrl":"https://www.linkedin.com/li/track?ver=1"};</script>
</body></html>
"""


# --------------------------------------------------------------------------
# FIXTURE 2 — Reel de Facebook (estado GraphQL embebido en un <script> que NO
# es JSON puro; claves reales: browser_native_hd_url / _sd_url / playable_url;
# __typename Video; width/height; + fotos de perfil y thumbs en fbcdn de ruido)
# --------------------------------------------------------------------------
FACEBOOK_REEL = r"""
<!DOCTYPE html><html><head>
<meta property="og:title" content="Reel de musica clasica subtitulada">
<meta property="og:image" content="https://scontent.fbcdn.net/v/t15.0/thumb_reel.jpg?_nc_cat=1&oh=THUMB&oe=abc">
</head><body>
<img src="https://scontent.fbcdn.net/v/t1.0/avatar_profile_50x50.jpg?_nc=AV&oe=1">
<script>
requireLazy(["ServerJS"],function(){
  window.__additionalData = {"require":[["ScheduledServerJS","handle",null,[{
    "__bbox":{"result":{"data":{"video":{
      "__typename":"Video",
      "id":"1122334455",
      "playable_duration_in_ms":34000,
      "width":720,"height":1280,
      "browser_native_hd_url":"https:\/\/video-gru2-1.xx.fbcdn.net\/v\/t42.1790-2\/hd_reel.mp4?efg=eyJ2ZW5jIjoiaGQifQ&_nc_cat=1&oh=HD&oe=6600AAAA&bytestart=0",
      "browser_native_sd_url":"https:\/\/video-gru2-1.xx.fbcdn.net\/v\/t42.1790-2\/sd_reel.mp4?efg=eyJ2ZW5jIjoic2QifQ&_nc_cat=1&oh=SD&oe=6600BBBB",
      "playable_url":"https:\/\/video-gru2-1.xx.fbcdn.net\/v\/t42.1790-2\/sd_reel.mp4?efg=eyJ2ZW5jIjoic2QifQ&_nc_cat=1&oh=SD&oe=6600BBBB",
      "preferred_thumbnail":{"image":{"uri":"https:\/\/scontent.fbcdn.net\/v\/t15.0\/thumb_big.jpg?oe=cc"}}
    }}}}
  }]]]};
});
</script>
</body></html>
"""


# --------------------------------------------------------------------------
# FIXTURE 3 — JSON-LD VideoObject generico (sitio sin extractor; clave
# `contentUrl` anidada) en un CDN cloudfront. Debe encontrarlo igual.
# --------------------------------------------------------------------------
GENERIC_JSONLD = """
<html><head>
<script type="application/ld+json">
{"@type":"VideoObject","name":"Demo","duration":"PT1M30S",
 "thumbnailUrl":["https://img.example.com/poster.jpg"],
 "author":"Grace Hopper",
 "clip":{"deep":{"contentUrl":"https://d123.cloudfront.net/videos/demo-1080.mp4?Expires=999&Signature=SIG&Key-Pair-Id=K","width":1920,"height":1080,"encodingFormat":"video/mp4"}}}
</script></head><body></body></html>
"""


# --------------------------------------------------------------------------
# FIXTURE 4 — Nada de video (solo login-wall). Debe devolver ok=False limpio.
# --------------------------------------------------------------------------
LOGIN_WALL = """
<html><head><meta property="og:title" content="Log in"></head>
<body><img src="https://scontent.fbcdn.net/logo.png"><a href="/login">Entrar</a></body></html>
"""


def _fmt(res):
    lines = [f"  ok={res.ok} conf={res.confidence} strat={res.strategy}",
             f"  title={res.title!r} uploader={res.uploader!r}",
             f"  thumb={ (res.thumbnail or '')[:70]!r}",
             f"  diagnostics={res.diagnostics}"]
    for f in res.formats:
        lines.append(f"    [{f.score:6.1f}] {f.kind:5} {str(f.height)+'p':>6} {f.ext} "
                     f":: {f.url[:78]}")
    return "\n".join(lines)


def check(name, cond):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}")
    return cond


def main():
    allok = True

    print("\n=== FIXTURE 1: LinkedIn post ===")
    r1 = R.resolve_html(LINKEDIN_POST, "https://www.linkedin.com/posts/ada_grafos-activity-7151241570371948544-4Gu7")
    print(_fmt(r1))
    top = r1.formats[0] if r1.formats else None
    allok &= check("resuelve algo", r1.ok)
    allok &= check("el ganador es el 720p de dms.licdn.com",
                   top and "dms.licdn.com" in top.url and top.height == 720)
    allok &= check("SIGNED720 (HD) gana a SIGNED360 (SD)",
                   top and "SIGNED720" in top.url)
    allok &= check("encuentra AMBAS calidades (720 y 360)",
                   {f.height for f in r1.formats} >= {720, 360})
    allok &= check("NINGUN formato es imagen/avatar/pixel",
                   all("image" not in f.url and "pixel" not in f.url
                       and "avatar" not in f.url for f in r1.formats))
    allok &= check("titulo y autor desde JSON-LD",
                   r1.title and "grafos" in r1.title and r1.uploader == "Ada Lovelace")
    allok &= check("miniatura = og:image de media.licdn.com",
                   r1.thumbnail and "thumb-1280" in r1.thumbnail)

    print("\n=== FIXTURE 2: Facebook Reel (GraphQL en script no-JSON) ===")
    r2 = R.resolve_html(FACEBOOK_REEL, "https://www.facebook.com/reel/1122334455")
    print(_fmt(r2))
    top2 = r2.formats[0] if r2.formats else None
    allok &= check("resuelve algo (extrae del script GraphQL)", r2.ok)
    allok &= check("el ganador es el HD (browser_native_hd_url)",
                   top2 and "hd_reel.mp4" in top2.url)
    allok &= check("detecta height 1280 de las hermanas",
                   top2 and top2.height == 1280)
    allok &= check("NO elige el avatar ni el thumb de scontent",
                   all("avatar" not in f.url and "thumb" not in f.url
                       for f in r2.formats))
    allok &= check("dedupe: sd_url y playable_url (mismo archivo) NO duplican",
                   sum(1 for f in r2.formats if "sd_reel.mp4" in f.url) == 1)

    print("\n=== FIXTURE 3: JSON-LD generico (contentUrl anidada) ===")
    r3 = R.resolve_html(GENERIC_JSONLD, "https://blog.example.com/demo")
    print(_fmt(r3))
    top3 = r3.formats[0] if r3.formats else None
    allok &= check("encuentra el contentUrl anidado en cloudfront",
                   top3 and "cloudfront.net" in top3.url and top3.height == 1080)
    allok &= check("duracion PT1M30S -> 90s", r3.duration == 90)

    print("\n=== FIXTURE 4: login-wall (sin medio) ===")
    r4 = R.resolve_html(LOGIN_WALL, "https://www.facebook.com/watch/xyz")
    print(_fmt(r4))
    allok &= check("ok=False y razon clara (no inventa un video)",
                   (not r4.ok) and "sesion" in r4.reason.lower())

    print("\n=== resolve() CAMINO RAPIDO: el original ya resuelve, NO gasta mas red ===")
    calls = []

    def fetch_original_ok(url, ua, *, max_bytes, referer=None):
        calls.append((url, "googlebot" if "Googlebot" in ua else "browser"))
        return LINKEDIN_POST.encode("utf-8"), url

    r5 = R.resolve("https://www.linkedin.com/posts/ada_grafos-activity-7151241570371948544-4Gu7",
                   fetch=fetch_original_ok)
    print("   intentos:", calls)
    allok &= check("resolve() devuelve el 720p", r5.ok and r5.formats[0].height == 720)
    allok &= check("corta al 1er intento por alta confianza (velocidad)",
                   len(calls) == 1)

    print("\n=== resolve() CASCADA: original = login-wall -> cae a la reescritura /embed/ ===")
    calls2 = []

    def fetch_needs_embed(url, ua, *, max_bytes, referer=None):
        calls2.append(url)
        if "/embed/feed/update/urn:li:activity:7151241570371948544" in url:
            return LINKEDIN_POST.encode("utf-8"), url     # el embed SI trae el video
        return LOGIN_WALL.encode("utf-8"), url            # el /posts/ pide login

    r6 = R.resolve("https://www.linkedin.com/posts/ada_grafos-activity-7151241570371948544-4Gu7",
                   fetch=fetch_needs_embed)
    print("   intentos:", calls2)
    allok &= check("cae a la reescritura /embed/ cuando el original no trae medio",
                   any("/embed/feed/update/urn:li:activity:7151241570371948544" in c for c in calls2))
    allok &= check("y aun asi recupera el 720p", r6.ok and r6.formats[0].height == 720)

    print("\n" + ("=" * 60))
    print("RESULTADO GLOBAL:", "TODO PASA (OK)" if allok else "HAY FALLOS (FAIL)")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
