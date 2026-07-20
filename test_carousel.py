# -*- coding: utf-8 -*-
"""
test_carousel.py — pruebas del PASE DE IMAGEN (carruseles/albumes/pines), del
CAPTION COMPLETO y de la CASCADA DE PUERTAS (multi-crawler).

Todo offline y determinista: fixtures con la estructura real de cada
plataforma + un `fetch` inyectado para la cascada. Lo que se verifica:

  * Instagram sidecar: N fotos, EN ORDEN, fusionando tamanos de la misma foto,
    y sin colar el avatar del perfil (t51.2885-19).
  * LinkedIn: la identidad va por ASSET_ID, no por basename. Es la trampa mas
    facil de este motor: dos fotos DISTINTAS comparten el timestamp final del
    path, asi que deduplicar por basename las fusionaria y se perderia una.
  * Rechazo de ruido: logos, sprites, iconos, assets estaticos, imagenes chicas.
  * Caption completo + hashtags desde og:description y desde el JSON.
  * media_type correcto (video manda sobre fotos; 2+ fotos = carousel).
  * Cascada de puertas: si Googlebot cae, prueba las demas; y RECUERDA cual
    gano para liderar con esa la proxima vez.
  * parse_selection: "todas", "1,3,5", "2-4", "ultima" -> indices reales.
"""

import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import resolver as R


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    return bool(cond)


# ==========================================================================
# FIXTURE A — Carrusel de Instagram (sidecar de 3 fotos)
# Cada foto viene en 2 renditions (s640x640 y p1080x1080) = 6 URLs para 3
# fotos. Ademas: avatar del perfil, icono de la interfaz y un pixel.
# ==========================================================================
IG_CAROUSEL = r"""
<!DOCTYPE html><html><head>
<meta property="og:title" content="Ada Lovelace on Instagram">
<meta property="og:description" content="Tres graficos que explican el algoritmo de Dijkstra paso a paso.
Guardalo para despues. #algoritmos #grafos #dijkstra">
<meta property="og:image" content="https://scontent.cdninstagram.com/v/t51.2885-15/p1080x1080/111_222_333_n.jpg?_nc=A">
</head><body>
<img class="avatar" src="https://scontent.cdninstagram.com/v/t51.2885-19/s150x150/999_888_777_n.jpg?_nc=Z">
<img src="https://static.cdninstagram.com/rsrc.php/v3/yk/r/logo_glyph.png">
<img src="https://www.instagram.com/static/images/spinner.gif/abc.gif">
<script type="application/json">
{"graphql":{"shortcode_media":{
  "__typename":"GraphSidecar",
  "owner":{"username":"ada.lovelace","profile_pic_url":"https://scontent.cdninstagram.com/v/t51.2885-19/s320x320/999_888_777_n.jpg?_nc=Z"},
  "edge_media_to_caption":{"edges":[{"node":{"text":"Tres graficos que explican el algoritmo de Dijkstra paso a paso.\nGuardalo para despues. #algoritmos #grafos #dijkstra"}}]},
  "edge_sidecar_to_children":{"edges":[
    {"node":{"id":"1","display_url":"https://scontent.cdninstagram.com/v/t51.2885-15/s640x640/111_222_333_n.jpg?_nc=A",
      "display_resources":[
        {"src":"https://scontent.cdninstagram.com/v/t51.2885-15/s640x640/111_222_333_n.jpg?_nc=A","config_width":640,"config_height":640},
        {"src":"https://scontent.cdninstagram.com/v/t51.2885-15/p1080x1080/111_222_333_n.jpg?_nc=A","config_width":1080,"config_height":1080}]}},
    {"node":{"id":"2","display_url":"https://scontent.cdninstagram.com/v/t51.2885-15/s640x640/444_555_666_n.jpg?_nc=B",
      "display_resources":[
        {"src":"https://scontent.cdninstagram.com/v/t51.2885-15/p1080x1080/444_555_666_n.jpg?_nc=B","config_width":1080,"config_height":1350}]}},
    {"node":{"id":"3","display_url":"https://scontent.cdninstagram.com/v/t51.2885-15/p1080x1080/777_888_999_n.jpg?_nc=C","width":1080,"height":1080}}
  ]}}}}
</script>
<img src="https://www.facebook.com/tr?ev=PageView&noscript=1">
</body></html>
"""


# ==========================================================================
# FIXTURE B — Post de LinkedIn con 2 imagenes.
# LA TRAMPA: las dos URLs terminan en el MISMO segmento (1712345678901), que
# es un timestamp compartido. Deduplicar por basename las fusionaria en una.
# Ademas cada foto viene en 2 renditions (shrink_800 y shrink_2048).
# ==========================================================================
LINKEDIN_IMAGES = r"""
<!DOCTYPE html><html><head>
<meta property="og:title" content="Resultados del trimestre">
<meta property="og:description" content="Comparto los dos graficos del cierre. #datos #analitica">
</head><body>
<img src="https://media.licdn.com/dms/image/v2/C4D0BAQcompanylogo/company-logo_100_100/0/1600000000000?e=1&t=L">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"SocialMediaPosting",
 "author":{"@type":"Person","name":"Grace Hopper"},
 "headline":"Resultados del trimestre",
 "articleBody":"Comparto los dos graficos del cierre del trimestre. El primero es la evolucion mensual y el segundo el desglose por region. #datos #analitica",
 "images":[
   {"url":"https://media.licdn.com/dms/image/v2/D4E22AQAAA111/feedshare-shrink_800/0/1712345678901?e=2145916800&t=AAA","width":800,"height":600},
   {"url":"https://media.licdn.com/dms/image/v2/D4E22AQAAA111/feedshare-shrink_2048/0/1712345678901?e=2145916800&t=AAA","width":2048,"height":1536},
   {"url":"https://media.licdn.com/dms/image/v2/D4E22AQBBB222/feedshare-shrink_2048/0/1712345678901?e=2145916800&t=BBB","width":2048,"height":1536}
 ]}
</script>
</body></html>
"""


# ==========================================================================
# FIXTURE C — Pin de Pinterest (una sola imagen, varios tamanos del mismo hash)
# ==========================================================================
PINTEREST_PIN = r"""
<html><head>
<meta property="og:title" content="Receta de pan de masa madre">
<meta property="og:description" content="Fermentacion de 18 horas. #pan #masamadre">
<meta property="og:image" content="https://i.pinimg.com/originals/ab/cd/ef/0123456789abcdef0123.jpg">
</head><body>
<img srcset="https://i.pinimg.com/236x/ab/cd/ef/0123456789abcdef0123.jpg 236w,
             https://i.pinimg.com/736x/ab/cd/ef/0123456789abcdef0123.jpg 736w"
     src="https://i.pinimg.com/564x/ab/cd/ef/0123456789abcdef0123.jpg">
<img src="https://s.pinimg.com/webapp/style/icon-save-32x32.png">
</body></html>
"""


# ==========================================================================
# FIXTURE D — LOGIN-WALL REAL de Instagram (capturado EN VIVO 2026-07-19).
# Cuando Instagram no te deja ver el post, sirve su pagina de login, que trae
# un `rsrcMap` con TODOS sus .js/.css bajo claves llamadas `src`, en un host
# que termina en .cdninstagram.com y con una clave hermana `type`.
# Eso sumaba cdn(46)+key(16)+dims(16)=78 -> entraban como "formatos de video",
# y con 78 la confianza daba 0.757 > 0.6, asi que la CASCADA SE APAGABA en la
# primera puerta y nunca se probaba /embed/captioned/.
# Este fixture es la regresion de ese fallo real.
# ==========================================================================
IG_LOGIN_WALL = r"""
<!DOCTYPE html><html><head><title>Instagram</title>
<link rel="preconnect" href="https://static.cdninstagram.com">
</head><body>
<script type="application/json">
{"require":{"rsrcMap":{
  "D1NBIDO":{"src":"https://static.cdninstagram.com/rsrc.php/v5/ym/l/0,cross/aQtosRcH4EsLf4VtNQ2uUPs97xFZK--hB","type":"css"},
  "BNpx00M":{"src":"https://static.cdninstagram.com/rsrc.php/v4/yn/r/g6NfOa9EYZU.js","type":"js"},
  "E8uK18n":{"src":"https://static.cdninstagram.com/rsrc.php/v5/yw/l/0,cross/gH_BD7FcYMl.css","type":"css"},
  "NVNq+I5":{"src":"https://static.cdninstagram.com/rsrc.php/v4iQvT4/yB/l/en_US/lHg7935Wsum.js","type":"js"}
}}}
</script>
</body></html>
"""


# ==========================================================================
# FIXTURE E — CARRUSEL REAL de Instagram servido a Googlebot (capturado EN
# VIVO 2026-07-20, post /p/DaLBFzfD_yO/).
# La pagina trae DOS grupos de fotos del MISMO CDN, con la misma pinta:
#   (a) `carousel_media/image_versions2/candidates` -> las del post. SI.
#   (b) `polaris_ordered_timeline_connection/edges/node` -> el timeline del
#       perfil, o sea OTRAS publicaciones del autor. NO.
# Ambos empataban en 114 puntos porque `edges` estaba en el lexico de carrusel,
# y un post de 2 fotos devolvia 4. Lo unico que los distingue es DE DONDE
# cuelgan. Ademas el tamano viaja en `stp=`, no en la ruta.
# ==========================================================================
IG_CAROUSEL_REAL = r"""
<!DOCTYPE html><html><head>
<meta property="og:title" content="u^day on Instagram: &quot;Cosmic Love&quot;">
<meta property="og:description" content="oni. In Japanese folklore, you do not meet an oni. You become one.">
</head><body>
<script type="application/json">
{"data":{"xdt_api__v1__media__shortcode__web_info":{"items":[{
  "carousel_media":[
    {"image_versions2":{"candidates":[
      {"url":"https://instagram.flim38-1.fna.fbcdn.net/v/t51.82787-15/731390847_17933210073324584_1_n.jpg?stp=dst-jpg_e35_s640x640_tt6&_nc_cat=1&oh=AA"},
      {"url":"https://instagram.flim38-1.fna.fbcdn.net/v/t51.82787-15/731390847_17933210073324584_1_n.jpg?stp=dst-jpg_e35_p1080x1080_tt6&_nc_cat=1&oh=AA"}]}},
    {"image_versions2":{"candidates":[
      {"url":"https://instagram.flim28-2.fna.fbcdn.net/v/t51.82787-15/731068186_17933210082324584_2_n.jpg?stp=dst-jpg_e35_p1080x1080_tt6&_nc_cat=1&oh=BB"}]}}
  ]}]}},
 "polaris_ordered_timeline_connection":{"edges":[
   {"node":{"display_uri":"https://scontent.cdninstagram.com/v/t51.82787-15/624067917_17908796574324584_9_n.jpg?stp=dst-jpg_e35_s640x640_tt6&oh=CC"}},
   {"node":{"display_uri":"https://scontent.cdninstagram.com/v/t51.82787-15/610795174_17906543370324584_8_n.jpg?stp=dst-jpg_e35_s640x640_tt6&oh=DD"}},
   {"node":{"display_uri":"https://scontent.cdninstagram.com/v/t51.82787-15/619519690_17907793551324584_7_n.jpg?stp=dst-jpg_e35_s640x640_tt6&oh=EE"}}
 ]}}
</script>
</body></html>
"""


# ==========================================================================
# FIXTURE F — POST REAL de Instagram con la cuadricula del perfil mezclada
# (capturado EN VIVO 2026-07-20, post /p/Da6GnH8HGW4/).
# Un post de 3 fotos devolvia 12:
#   (a) 3 en `carousel_media/image_versions2/candidates` -> las del post.
#   (b) 9 en <img src> sueltos -> la cuadricula del perfil. Mismo CDN, mismas
#       firmas, 640x640, 116 puntos: por rasgos propios son iguales a una foto
#       buena. Solo la ESTRUCTURA las distingue.
# Ademas el og:title trae 'Autor on Instagram: "caption"' (de ahi el autor y
# el caption REAL), mientras que el JSON de la pagina incluye el texto de OTRA
# publicacion mas largo — que con la regla vieja ("el mas largo gana") ganaba.
# ==========================================================================
IG_GRID_MIXED = r"""
<!DOCTYPE html><html><head>
<meta property="og:title" content='Not Journal on Instagram: "Um jovem decidiu ignorar as vagas tradicionais e se candidatar ao cargo mais importante da OpenAI."'>
</head><body>
<img src="https://scontent.cdninstagram.com/v/t51.82787-15/543756751_17898130368276987_1_n.jpg?stp=dst-jpg_e35_s640x640_tt6&oh=AA">
<img src="https://scontent.cdninstagram.com/v/t51.71878-15/752762000_1348476560762617_2_n.jpg?stp=dst-jpg_e35_s640x640_tt6&oh=BB">
<img src="https://scontent.cdninstagram.com/v/t51.82787-15/752230107_17868021615633483_3_n.jpg?stp=dst-jpg_e35_s640x640_tt6&oh=CC">
<script type="application/json">
{"items":[{"carousel_media":[
  {"image_versions2":{"candidates":[
    {"url":"https://instagram.flim38-1.fna.fbcdn.net/v/t51.82787-15/750755233_17938923786276987_9_n.jpg?stp=dst-jpg_e35_p1080x1080_tt6&oh=DD"}]}},
  {"image_versions2":{"candidates":[
    {"url":"https://instagram.flim38-1.fna.fbcdn.net/v/t51.82787-15/749542545_17938923798276987_8_n.jpg?stp=dst-jpg_e35_p1080x1080_tt6&oh=EE"}]}},
  {"image_versions2":{"candidates":[
    {"url":"https://instagram.flim38-1.fna.fbcdn.net/v/t51.82787-15/750829478_17938923765276987_7_n.jpg?stp=dst-jpg_e35_p1080x1080_tt6&oh=FF"}]}}
]}],
 "otro_post":{"caption":"A Espanha e bicampea mundial. Depois de controlar a final por quase toda a noite, a selecao espanhola venceu a Argentina por 1 a 0 na prorrogacao e voltou ao topo do futebol 16 anos depois. O gol saiu aos 106 minutos e o titulo confirma a reconstrucao de uma selecao. Siga @notsports.ai"}}
</script>
</body></html>
"""


def main():
    ok = True

    # ---------------------------------------------------------------- A
    print("\n=== A. Carrusel de Instagram (sidecar de 3 fotos) ===")
    a = R.resolve_html(IG_CAROUSEL, "https://www.instagram.com/p/ABC123/")
    for i, c in enumerate(a.images, 1):
        print(f"    {i}. [{c.score:6.1f}] {c.width}x{c.height} {c.url[:78]}")
    ok &= check("media_type = carousel", a.media_type == "carousel")
    ok &= check("exactamente 3 fotos (fusiono los 2 tamanos de cada una)",
                len(a.images) == 3)
    ok &= check("EN ORDEN del carrusel: 111, 444, 777",
                len(a.images) == 3
                and "111_222_333" in a.images[0].url
                and "444_555_666" in a.images[1].url
                and "777_888_999" in a.images[2].url)
    ok &= check("de cada foto eligio la version GRANDE (p1080x1080)",
                all("p1080x1080" in c.url for c in a.images))
    ok &= check("NO cuela el avatar del perfil (t51.2885-19)",
                not any("2885-19" in c.url for c in a.images))
    ok &= check("NO cuela logos/sprites/assets estaticos",
                not any(("rsrc.php" in c.url or "spinner" in c.url
                         or "/static/" in c.url) for c in a.images))
    ok &= check("NO cuela el pixel de tracking",
                not any("facebook.com/tr" in c.url for c in a.images))
    ok &= check("caption COMPLETO (no 'Video by usuario')",
                a.full_caption and "Dijkstra" in a.full_caption)
    ok &= check("hashtags extraidos en orden",
                a.hashtags[:3] == ["#algoritmos", "#grafos", "#dijkstra"])
    ok &= check("no hay formatos de video (es un post de fotos)", not a.formats)
    ok &= check("ok=True aunque NO haya video", a.ok)

    # ---------------------------------------------------------------- B
    print("\n=== B. LinkedIn: identidad por ASSET_ID, no por basename ===")
    b = R.resolve_html(LINKEDIN_IMAGES,
                       "https://www.linkedin.com/posts/grace_q4-activity-7480678504318267393-abcd")
    for i, c in enumerate(b.images, 1):
        print(f"    {i}. [{c.score:6.1f}] {c.width}x{c.height} {c.url[:82]}")
    ok &= check("2 fotos distintas (AAA111 y BBB222), NO fusionadas por el "
                "timestamp compartido", len(b.images) == 2)
    ok &= check("cada una en su rendition de 2048 (fusiono 800 con 2048)",
                all("shrink_2048" in c.url for c in b.images))
    ok &= check("descarta el logo de empresa (company-logo_100_100)",
                not any("company-logo" in c.url for c in b.images))
    ok &= check("media_type = carousel", b.media_type == "carousel")
    ok &= check("caption largo desde articleBody del JSON-LD",
                b.full_caption and "desglose por region" in b.full_caption)
    ok &= check("autor desde JSON-LD", b.uploader == "Grace Hopper")

    print("   identidades:", sorted({R.image_identity(c.url) for c in b.images}))
    ok &= check("image_identity de LinkedIn usa el ASSET_ID",
                R.image_identity(
                    "https://media.licdn.com/dms/image/v2/D4E22AQAAA111/"
                    "feedshare-shrink_800/0/1712345678901?e=1") == "li:D4E22AQAAA111")
    ok &= check("image_identity de IG/FB usa el trio de ids",
                R.image_identity(
                    "https://scontent.cdninstagram.com/v/t51.2885-15/s640x640/"
                    "111_222_333_n.jpg?x=1") == "fb:111_222_333")

    # ---------------------------------------------------------------- C
    print("\n=== C. Pin de Pinterest (1 foto, varios tamanos) ===")
    c_ = R.resolve_html(PINTEREST_PIN, "https://www.pinterest.com/pin/12345/")
    for i, c in enumerate(c_.images, 1):
        print(f"    {i}. [{c.score:6.1f}] {c.url[:78]}")
    ok &= check("1 sola foto (fusiono 236/564/736/originals por el hash)",
                len(c_.images) == 1)
    ok &= check("eligio 'originals' (la mas grande)",
                c_.images and "originals" in c_.images[0].url)
    ok &= check("media_type = image (una sola)", c_.media_type == "image")
    ok &= check("descarta el icono de la interfaz (s.pinimg webapp)",
                not any("webapp" in c.url for c in c_.images))

    # ------------------------------------------------- D (no regresion)
    print("\n=== D. Un post CON video sigue siendo media_type=video ===")
    import test_resolver as T
    d = R.resolve_html(T.LINKEDIN_POST,
                       "https://www.linkedin.com/posts/ada_grafos-activity-7151241570371948544-4Gu7")
    ok &= check("media_type = video (el video manda sobre el poster)",
                d.media_type == "video")
    ok &= check("los formatos de video siguen intactos (720 y 360)",
                {f.height for f in d.formats} >= {720, 360})

    # ---------------------------------------------------------------- E
    print("\n=== E. CASCADA DE PUERTAS: Googlebot cae -> entra otra puerta ===")
    R._HOST_MEMORY.clear()
    tried = []

    def fetch_only_slackbot(url, ua, *, max_bytes, referer=None):
        kind = R.ua_kind(ua)
        tried.append((kind, "embed" if "/embed/" in url else "original"))
        if kind == "slackbot":
            return LINKEDIN_IMAGES.encode("utf-8"), url
        raise OSError("HTTP Error 999: la puerta esta cerrada")

    e = R.resolve("https://www.linkedin.com/posts/grace_q4-activity-7480678504318267393-abcd",
                  fetch=fetch_only_slackbot)
    print("   puertas probadas:", tried)
    ok &= check("probo varias puertas distintas antes de rendirse",
                len({k for k, _ in tried}) >= 3)
    ok &= check("Googlebot fue la PRIMERA (sigue siendo la mejor apuesta)",
                tried and tried[0][0] == "googlebot")
    ok &= check("entro por Slackbot y resolvio", e.ok and "slackbot" in e.strategy)
    ok &= check("y trajo las 2 fotos", len(e.images) == 2)

    print("\n=== F. MEMORIA: la 2a vez lidera con la puerta que gano ===")
    tried2 = []

    def fetch_track(url, ua, *, max_bytes, referer=None):
        kind = R.ua_kind(ua)
        tried2.append(kind)
        if kind == "slackbot":
            return LINKEDIN_IMAGES.encode("utf-8"), url
        raise OSError("HTTP Error 999: cerrada")

    R.resolve("https://www.linkedin.com/posts/otro-activity-999-xy", fetch=fetch_track)
    print("   puertas probadas (2a vez):", tried2)
    ok &= check("aprendio: arranca por Slackbot, no por Googlebot",
                tried2 and tried2[0] == "slackbot")
    ok &= check("y por eso resolvio en 1 solo fetch", len(tried2) == 1)

    print("\n=== G. TOPE de intentos (no se cuelga probando 20 puertas) ===")
    R._HOST_MEMORY.clear()
    tried3 = []

    def fetch_all_closed(url, ua, *, max_bytes, referer=None):
        tried3.append(R.ua_kind(ua))
        raise OSError("cerrada")

    g = R.resolve("https://www.linkedin.com/posts/x-activity-1-a",
                  fetch=fetch_all_closed, max_attempts=4)
    ok &= check("respeta max_attempts=4", len(tried3) == 4)
    ok &= check("falla limpio, sin inventar nada", not g.ok)

    # ---------------------------------------------------------------- H
    print("\n=== H. parse_selection: lo que dice el humano -> indices ===")
    from test_server_integration import _install_stubs
    _install_stubs()
    if "server" in sys.modules:
        del sys.modules["server"]
    import server as S

    cases = [
        ("all", 5, [1, 2, 3, 4, 5]),
        ("", 3, [1, 2, 3]),
        ("todas", 4, [1, 2, 3, 4]),
        ("1,3,5", 6, [1, 3, 5]),
        ("2-4", 6, [2, 3, 4]),
        ("1-3,6", 6, [1, 2, 3, 6]),
        ("ultima", 7, [7]),
        ("primera", 7, [1]),
        ("3, 1", 5, [3, 1]),          # respeta el orden que pidio el usuario
        ("2,2,2", 5, [2]),            # sin repetidos
        ("9", 5, []),                 # fuera de rango -> no revienta
        ("todas", 0, []),             # sin fotos -> vacio
    ]
    for which, n, expected in cases:
        got = S.parse_selection(which, n)
        ok &= check(f"parse_selection({which!r}, {n}) -> {expected}", got == expected)

    print("\n=== I. _curate_resolver expone fotos numeradas y caption ===")
    info = R.resolve_html(IG_CAROUSEL, "https://www.instagram.com/p/ABC123/").to_info()
    cur = S._curate_resolver(info)
    print("    ", cur["media_type"], "| fotos:", cur["image_count"],
          "|", [im["label"] for im in cur["images"]])
    ok &= check("media_type llega a list_formats", cur["media_type"] == "carousel")
    ok &= check("3 fotos numeradas 1,2,3",
                [im["index"] for im in cur["images"]] == [1, 2, 3])
    ok &= check("etiquetas legibles con resolucion",
                cur["images"][0]["label"].startswith("Foto 1"))
    ok &= check("full_caption llega a list_formats",
                cur.get("full_caption") and "Dijkstra" in cur["full_caption"])
    ok &= check("hashtags llegan a list_formats",
                "#grafos" in (cur.get("hashtags") or []))

    print("\n=== K. REGRESION EN VIVO: login-wall de Instagram (rsrcMap) ===")
    k = R.resolve_html(IG_LOGIN_WALL, "https://www.instagram.com/p/DaLBFzfD_yO/")
    print(f"    ok={k.ok} media_type={k.media_type} conf={k.confidence} "
          f"formatos={len(k.formats)} fotos={len(k.images)}")
    ok &= check("NINGUN .js/.css entra como formato de video",
                not any(c.url.endswith((".js", ".css")) for c in k.formats))
    ok &= check("no devuelve NINGUN formato (la pagina no tiene medios)",
                len(k.formats) == 0)
    ok &= check("no devuelve fotos falsas", len(k.images) == 0)
    ok &= check("ok=False: admite que no encontro nada", not k.ok)
    ok &= check("confianza por DEBAJO de 0.6 -> la cascada NO se apaga",
                k.confidence < 0.6)

    print("\n=== L. Un ganador sin altura ni bitrate NO da alta confianza ===")
    weak = R.resolve_html(
        '<script type="application/json">{"video":{"playable_url":'
        '"https://video.xx.fbcdn.net/v/t42/algo?_nc_cat=1","type":"x"}}}</script>',
        "https://www.facebook.com/reel/1")
    print(f"    conf={weak.confidence} formatos={len(weak.formats)}")
    ok &= check("techo de confianza aplicado (<= 0.35) para que siga la cascada",
                weak.confidence <= R.WEAK_CONFIDENCE)

    print("\n=== M. Tope de formatos (diagnostico legible) ===")
    ok &= check("MAX_FORMATS existe y es razonable",
                1 < R.MAX_FORMATS <= 30)

    print("\n=== N. REGRESION EN VIVO: carrusel real vs timeline del perfil ===")
    n = R.resolve_html(IG_CAROUSEL_REAL, "https://www.instagram.com/p/DaLBFzfD_yO/")
    for i, c in enumerate(n.images, 1):
        print(f"    {i}. [{c.score:6.1f}] {c.width}x{c.height} {c.url[:70]}")
        print(f"        {c.provenance}")
    ok &= check("SOLO las 2 del carrusel (descarta las 3 del timeline)",
                len(n.images) == 2)
    ok &= check("NINGUNA viene del timeline del perfil",
                not any("timeline" in c.provenance for c in n.images))
    ok &= check("todas salen de carousel_media",
                all("carousel_media" in c.provenance for c in n.images))
    ok &= check("fusiono los 2 tamanos de la foto 1 y eligio p1080x1080",
                n.images and "p1080x1080" in n.images[0].url)
    ok &= check("AHORA si conoce las dimensiones (las lee de stp=)",
                all(c.width and c.height for c in n.images))
    ok &= check("dimensiones correctas (1080x1080)",
                n.images[0].width == 1080 and n.images[0].height == 1080)
    ok &= check("media_type = carousel", n.media_type == "carousel")
    ok &= check("con dimensiones conocidas la confianza YA no tiene techo",
                n.confidence > R.WEAK_CONFIDENCE)

    print("\n=== O. REGRESION EN VIVO: cuadricula del perfil + caption ajeno ===")
    o = R.resolve_html(IG_GRID_MIXED, "https://www.instagram.com/p/Da6GnH8HGW4/")
    for i, c in enumerate(o.images, 1):
        print(f"    {i}. [{c.score:6.1f}] {c.width}x{c.height} "
              f"post={c.is_post_media} {c.url[:62]}")
    ok &= check("SOLO las 3 del contenedor (descarta los 3 <img> sueltos)",
                len(o.images) == 3)
    ok &= check("todas vienen del contenedor estructurado",
                all(c.is_post_media for c in o.images))
    ok &= check("ninguna es de 640x640 (esas eran la cuadricula)",
                all(c.width == 1080 for c in o.images))
    ok &= check("EL CAPTION ES EL DE ESTE POST, no el mas largo del documento",
                o.full_caption and "OpenAI" in o.full_caption)
    ok &= check("NO se cuela el caption del otro post (el del Mundial)",
                o.full_caption and "Espanha" not in o.full_caption)
    ok &= check("saca el AUTOR del og:title (antes llegaba null)",
                o.uploader == "Not Journal")
    ok &= check("el titulo ya no arrastra la envoltura 'on Instagram:'",
                o.title and "on Instagram" not in o.title)

    print("\n=== P. keep_authoritative: sin contenedor NO filtra nada ===")
    # Pinterest/blogs no tienen contenedor: ahi no hay autoridad a la que
    # deferir y hay que conservar todo.
    p = R.resolve_html(PINTEREST_PIN, "https://www.pinterest.com/pin/12345/")
    ok &= check("Pinterest sigue devolviendo su foto", len(p.images) == 1)
    ok &= check("y sigue siendo la 'originals'",
                "originals" in p.images[0].url)

    print("\n=== Q. Envoltura de estadisticas del og:description (EN VIVO) ===")
    # Capturado del post /p/DaaEZJ7Fsmx/ de Cosmopolitan: Instagram mete el
    # caption dentro de "N likes, M comments - handle on <fecha>: ...".
    q = R.resolve_html(
        '<meta property="og:description" content=\'273K likes, 880 comments - '
        'cosmopolitan on July 5, 2026: "@monicabarbaro and #AndrewGarfield '
        'making a STRONG case for respectful PDA while at #Wimbledon '
        'yesterday.".\'>'
        '<script type="application/json">{"carousel_media":[{"image_versions2":'
        '{"candidates":[{"url":"https://scontent.cdninstagram.com/v/t51.82787-15/'
        '731787561_18607566088037517_1_n.jpg?stp=dst-jpg_e35_p1080x1080_tt6&oh=A"}]}}]}'
        '</script>',
        "https://www.instagram.com/p/DaaEZJ7Fsmx/")
    print(f"    caption={q.full_caption!r}")
    print(f"    uploader={q.uploader!r}  hashtags={q.hashtags}")
    ok &= check("el caption NO arrastra las estadisticas",
                q.full_caption and "likes" not in q.full_caption
                and "comments" not in q.full_caption)
    ok &= check("el caption SI conserva el texto real",
                q.full_caption and "Wimbledon" in q.full_caption)
    ok &= check("saca el handle como autor", q.uploader == "cosmopolitan")
    ok &= check("los hashtags siguen saliendo",
                q.hashtags == ["#AndrewGarfield", "#Wimbledon"])

    print("\n=== R. LinkedIn REAL: feedshare manda sobre 16 fotos de perfil ===")
    # Capturado EN VIVO del post de OpenAI sobre su chip Jalapeño. LinkedIn
    # sirve TODO como <img> suelto (no hay contenedor), asi que la autoridad
    # tiene que salir del nombre de la rendition: `feedshare-` es el post,
    # `profile-displayphoto-` son quienes comentaron, `image-scale_` es la
    # vista previa del enlace.
    LI_REAL = (
        '<meta property="og:title" content="OpenAI on LinkedIn: '
        '&quot;We have designed and built our first AI chip: Jalapeno.&quot;">'
        '<meta property="og:description" content="We have designed and built our '
        'first AI chip: Jalapeno. Purpose-built for the LLM workloads powering '
        'ChatGPT and the API. | 432 comments on LinkedIn">'
        '<meta property="og:image" content="https://media.licdn.com/dms/image/v2/'
        'D5622AQF-wmKFPe2xdg/feedshare-shrink_800/B56Z750Bn_IQAc-/0/1782307626233?e=1&t=A">'
        '<img src="https://media.licdn.com/dms/image/v2/D4D03AQHfWVqf-HH67g/'
        'profile-displayphoto-scale_400_400/B4E/0/1700000000001?e=1&t=B">'
        '<img src="https://media.licdn.com/dms/image/v2/D5603AQGY2L4pJXbhrQ/'
        'profile-displayphoto-shrink_400_400/B4E/0/1700000000002?e=1&t=C">'
        '<img src="https://media.licdn.com/dms/image/v2/D563DAQEiLbFUDLFr1g/'
        'image-scale_191_1128/image-scale_191_1128/0/1700000000003?e=1&t=D">'
    )
    r_ = R.resolve_html(LI_REAL, "https://www.linkedin.com/posts/openai-chip-7475540008238538752-a7tn/")
    for i, c in enumerate(r_.images, 1):
        print(f"    {i}. [{c.score:6.1f}] {c.width}x{c.height} post={c.is_post_media}")
        print(f"        {c.url[:88]}")
    ok &= check("SOLO la foto del post (1), no las 3 intrusas", len(r_.images) == 1)
    ok &= check("es la feedshare", r_.images and "feedshare" in r_.images[0].url)
    ok &= check("descarta las profile-displayphoto (avatares de LinkedIn)",
                not any("displayphoto" in c.url for c in r_.images))
    ok &= check("descarta la vista previa del enlace (image-scale_)",
                not any("image-scale" in c.url for c in r_.images))
    ok &= check("uploader desde 'OpenAI on LinkedIn:'", r_.uploader == "OpenAI")
    ok &= check("el caption NO arrastra '| 432 comments on LinkedIn'",
                r_.full_caption and "comments on LinkedIn" not in r_.full_caption)
    ok &= check("el caption SI conserva el texto real",
                r_.full_caption and "Jalapeno" in r_.full_caption)

    print("\n=== S. EL ANCLA: generaliza sin conocer la plataforma ===")
    # Firmas REALES capturadas en vivo. anchor_affinity no sabe que es
    # "feedshare" ni que es Instagram: solo compara la forma de la URL.
    li_anchor = ("https://media.licdn.com/dms/image/v2/D5622AQF-wmKFPe2xdg/"
                 "feedshare-shrink_800/B56Z750Bn_IQAc-/0/1782307626233?e=1")
    li_otra = ("https://media.licdn.com/dms/image/v2/D4E22AQH2Kp-tS4b5Pg/"
               "feedshare-image-high-res/B4EZ92N19_JYAU-/0/1784394768540?e=1")
    li_avatar = ("https://media.licdn.com/dms/image/v2/D4D03AQHfWVqf-HH67g/"
                 "profile-displayphoto-scale_400_400/B4E/0/1700000000001?e=1")
    li_preview = ("https://media.licdn.com/dms/image/v2/D563DAQEiLbFUDLFr1g/"
                  "image-scale_191_1128/image-scale_191_1128/0/1700000000003?e=1")
    for nombre, u, esperado in (
            ("otra feedshare", li_otra, True),
            ("avatar de perfil", li_avatar, False),
            ("vista previa de enlace", li_preview, False)):
        af = R.anchor_affinity(u, li_anchor)
        print(f"    LinkedIn {nombre:24} afinidad={af}")
        ok &= check(f"LinkedIn: {nombre} -> {'cerca' if esperado else 'lejos'} del ancla",
                    (af >= R.ANCHOR_MIN_AFFINITY) == esperado)

    ig_anchor = ("https://scontent.cdninstagram.com/v/t51.82787-15/"
                 "750755233_17938923786276987_7558221027793493537_n.jpg?stp=x")
    ig_hermana = ("https://instagram.flim38-1.fna.fbcdn.net/v/t51.82787-15/"
                  "749542545_17938923798276987_7501157_n.jpg?stp=x")
    ig_otropost = ("https://scontent.cdninstagram.com/v/t51.82787-15/"
                   "543756751_17898130368276987_8489574_n.jpg?stp=x")
    for nombre, u, esperado in (
            ("foto hermana del post", ig_hermana, True),
            ("foto de OTRO post", ig_otropost, False)):
        af = R.anchor_affinity(u, ig_anchor)
        print(f"    Instagram {nombre:23} afinidad={af}")
        ok &= check(f"Instagram: {nombre} -> {'cerca' if esperado else 'lejos'}",
                    (af >= R.ANCHOR_MIN_AFFINITY) == esperado)

    print("\n=== T. El ancla actua cuando NO hay contenedor ni rendition ===")
    # Plataforma imaginaria: ni contenedor JSON ni nombres que conozcamos.
    # Solo <img> sueltos. El ancla tiene que bastar para separar el grano.
    DESCONOCIDA = (
        '<meta property="og:image" content="https://cdn.plataforma-nueva.com/'
        'media/9001234567890123_a.jpg">'
        '<img src="https://cdn.plataforma-nueva.com/media/9001234567891456_b.jpg">'
        '<img src="https://cdn.plataforma-nueva.com/media/9001234567892789_c.jpg">'
        '<img src="https://cdn.plataforma-nueva.com/media/1200000000000001_z.jpg">'
    )
    t_ = R.resolve_html(DESCONOCIDA, "https://plataforma-nueva.com/post/1")
    for c in t_.images:
        print(f"    [{c.score:6.1f}] {c.url[-28:]}")
    ok &= check("conserva las 3 del post (IDs vecinos del ancla)",
                len(t_.images) == 3)
    ok &= check("descarta la de ID lejano (otro post)",
                not any("1200000000000001" in c.url for c in t_.images))

    print("\n=== U. Esloganes de plataforma rechazados como caption ===")
    ok &= check("Pinterest (italiano)",
                R.clean_caption("Scopri (e salva) i tuoi Pin su Pinterest.") is None)
    ok &= check("Pinterest (espanol)",
                R.clean_caption("Descubre (y guarda) tus propios Pines en Pinterest") is None)
    ok &= check("un caption REAL no se descarta",
                R.clean_caption("Receta de pan de masa madre con 18 horas de fermentacion"))

    print("\n=== V. X/Twitter: el sufijo :large es rendition, no otra foto ===")
    # Capturado EN VIVO: un tuit con UNA imagen devolvia dos, porque X marca
    # el tamano con un sufijo pegado al nombre (.jpg:large) en vez de un
    # parametro, y el agrupador las tomaba por archivos distintos.
    X_POST = (
        '<meta property="og:title" content=\'Marcelo Chepillo on X: "un meme" / X\'>'
        '<meta property="og:image" content="https://pbs.twimg.com/media/'
        'HNoYYPpXUAA8b26.jpg:large">'
        # OJO: la forma REAL de X (capturada en vivo) NO lleva extension en la
        # ruta, el formato va en el query. La primera version de este fixture
        # se la invento con `.jpg?name=small` y por eso el test PASABA mientras
        # la realidad fallaba. Fixture inventado = test que miente.
        '<img src="https://pbs.twimg.com/media/HNoYYPpXUAA8b26?format=jpg'
        '&name=small" width="546" height="680">'
    )
    v = R.resolve_html(X_POST, "https://x.com/JRafela63855/status/2079001071393890584")
    for c in v.images:
        print(f"    [{c.score:6.1f}] {c.url}")
    ok &= check("UNA sola foto (fusiona :large con la variante del query)",
                len(v.images) == 1)
    ok &= check("se queda con la version GRANDE (:large)",
                v.images and ":large" in v.images[0].url)
    ok &= check("media_type = image, no carousel", v.media_type == "image")
    ok &= check("identity ignora el sufijo :large Y la extension",
                R.image_identity("https://pbs.twimg.com/media/ABC12345.jpg:large")
                == R.image_identity("https://pbs.twimg.com/media/ABC12345?format=jpg"))
    ok &= check("el centinela de tamano NO se filtra a la interfaz",
                all((c.width or 0) < 100000 and (c.height or 0) < 100000
                    for c in v.images))
    ok &= check("reporta el tamano REAL conocido del grupo (546x680)",
                v.images and v.images[0].width == 546 and v.images[0].height == 680)
    ok &= check("desenvuelve el og:title de X y su cola ' / X'",
                v.uploader == "Marcelo Chepillo")

    print("\n=== W. bajar el MEDIO: verificar bytes + cascada de puertas ===")
    # Capturado EN VIVO con Facebook: la pagina se gana con Googlebot y las
    # fotos vienen por `/lookaside/crawler/media/?media_id=...`, un endpoint
    # que sirve el archivo a los CRAWLERS. Se pedia con UA de navegador -> el
    # CDN devolvia su HTML de login con codigo 200 y se guardaba como .jpg.
    # El job decia "1 foto guardada" y en la galeria quedaba basura: el
    # sistema fallaba EN SILENCIO.
    JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 32
    LOGIN_HTML = b"<!DOCTYPE html><html><body>Inicia sesion</body></html>"

    ok &= check("sniff reconoce JPEG / PNG / WebP",
                R.sniff_image_mime(JPEG) == "image/jpeg"
                and R.sniff_image_mime(PNG) == "image/png"
                and R.sniff_image_mime(WEBP) == "image/webp")
    ok &= check("sniff RECHAZA el HTML del login-wall",
                R.sniff_image_mime(LOGIN_HTML) is None)

    tried: list = []

    def fake_fetch(url, ua, *, max_bytes, referer=None):
        """Solo las puertas de crawler reciben la imagen; el navegador se come
        el login-wall, exactamente como hace Facebook."""
        tried.append(R.ua_kind(ua))
        return JPEG if R.ua_kind(ua) in ("facebookbot", "googlebot") else LOGIN_HTML

    R._MEDIA_GATE_MEMORY.clear()
    tried.clear()
    got = R.fetch_image_bytes("https://lookaside.fbsbx.com/lookaside/crawler/"
                              "media/?media_id=1221", fetch=fake_fetch)
    ok &= check("NO devuelve el HTML disfrazado de imagen",
                got is not None and got[0] == JPEG)
    ok &= check("el mime sale de los BYTES, no de la cabecera",
                got is not None and got[1] == "image/jpeg")
    ok &= check("empieza por el navegador y cae a la puerta de crawler",
                tried[0] == "browser" and "facebookbot" in tried)

    # La memoria por-host: en un carrusel solo la 1a foto paga la busqueda.
    tried.clear()
    R.fetch_image_bytes("https://lookaside.fbsbx.com/lookaside/crawler/"
                        "media/?media_id=9999", fetch=fake_fetch)
    ok &= check("recuerda la puerta ganadora POR HOST (1 solo fetch)",
                tried == ["facebookbot"])

    # La pista del llamante: la puerta que abrio la PAGINA lidera.
    R._MEDIA_GATE_MEMORY.clear()
    tried.clear()
    R.fetch_image_bytes("https://lookaside.fbsbx.com/x.jpg",
                        prefer_gate="facebook:googlebot:original",
                        fetch=fake_fetch)
    ok &= check("prefer_gate acepta la cadena de estrategia completa",
                tried == ["googlebot"])

    # Un CDN sano no debe pagar ni un fetch de mas (sin regresion de latencia).
    R._MEDIA_GATE_MEMORY.clear()
    tried.clear()
    R.fetch_image_bytes("https://scontent.cdninstagram.com/v/foto.jpg",
                        fetch=lambda u, ua, **k: (tried.append(R.ua_kind(ua)), PNG)[1])
    ok &= check("un CDN sano acierta al primer intento", tried == ["browser"])

    # Si NINGUNA puerta da una imagen, falla HONESTAMENTE (None), no devuelve
    # bytes que acabarian escritos como .jpg en la galeria del usuario.
    R._MEDIA_GATE_MEMORY.clear()
    ok &= check("si ninguna puerta sirve la imagen, devuelve None",
                R.fetch_image_bytes("https://ejemplo.com/x.jpg",
                                    fetch=lambda u, ua, **k: LOGIN_HTML) is None)

    print("\n=== Y. el identificador puede vivir en el QUERY, no en la ruta ===")
    # Capturado EN VIVO: un post de Facebook con UNA sola foto se reporto como
    # carrusel de NUEVE (iconos de reacciones, miniaturas de otros posts y el
    # logo de un anuncio). El motor daba por supuesto que la identidad de un
    # medio vive en el PATH; Facebook sirve todo desde el mismo
    # `/lookaside/crawler/media/` y distingue por `?media_id=`.
    #
    # Por que el post de 5 fotos SI funcionaba y este no: aquel era un
    # carrusel, tenia CONTENEDOR, y `keep_authoritative` resolvia en el
    # escalon 1 sin llegar a consultar el ancla. Con UNA foto no hay
    # contenedor -> el ancla es la unica defensa -> y estaba ciega.
    #
    # HONESTIDAD SOBRE ESTE FIXTURE: las URLs de lookaside son REALES
    # (capturadas en vivo); el HTML que las envuelve lo escribi yo. Vale para
    # fijar la regresion del ancla, NO como prueba de que la pagina real de
    # Facebook tenga esta forma.
    FB_ANCLA = ("https://lookaside.fbsbx.com/lookaside/crawler/media/"
                "?media_id=122141642061152120")
    FB_OTRO = ("https://lookaside.fbsbx.com/lookaside/crawler/media/"
               "?media_id=987654321098765432")

    ok &= check("el id se encuentra aunque la ruta no lo lleve",
                R._media_ids(FB_ANCLA) == ["122141642061152120"])
    ok &= check("hermana del MISMO post supera el umbral",
                R.anchor_affinity(
                    FB_ANCLA.replace("061152120", "073152120"),
                    FB_ANCLA) >= R.ANCHOR_MIN_AFFINITY)
    ok &= check("basura de OTRO post NO lo supera (antes empataban en 0.25)",
                R.anchor_affinity(FB_OTRO, FB_ANCLA) < R.ANCHOR_MIN_AFFINITY)
    ok &= check("la identidad sale del id, no de la URL entera",
                R.image_identity(FB_ANCLA)
                == R.image_identity(FB_ANCLA + "&width=640"))

    # La regla es "el query SOLO si la ruta calla": las plataformas cuyo path
    # ya identifica el medio no deben cambiar en nada. Si esto se rompe, los
    # tokens de FIRMA de Instagram (_nc_ohc, oh, oe) entrarian en la
    # comparacion de ids y la volverian ruidosa.
    IG = ("https://scontent.cdninstagram.com/v/t51.2885-15/"
          "111111111_222222222_333333333_n.jpg?stp=dst-jpg_s1080x1080"
          "&_nc_ohc=AAAAAAAAAAAA&oh=00_ZZZZZZZZ&oe=6A63")
    ok &= check("Instagram sigue sacando el id del PATH, no del query",
                R._media_ids(IG) == ["111111111", "222222222", "333333333"])
    ok &= check("X sigue sacando el id del PATH",
                R._media_ids("https://pbs.twimg.com/media/HNoYYPpXUAA8b26.jpg"
                             ":large") == ["HNoYYPpXUAA8b26"])
    ok &= check("un valor corto del query no es un id (width=640)",
                R._query_ids("https://x.com/a/?width=640&h=480") == [])

    print("\n=== Z. el ancla tambien VETA lo que el contenedor mete de mas ===")
    # DATOS REALES capturados en vivo del post de Facebook que lo destapo
    # (EnterCore, "Que fue xd"): UNA sola foto publicada, reportada como
    # carrusel de NUEVE. Estas 9 URLs y sus dimensiones salieron tal cual de
    # `resolve_media` contra el servidor del telefono.
    #
    # Lo importante: las NUEVE traian la marca de contenedor
    # (`[key+dims+carousel]`), asi que `keep_authoritative` resolvia en el
    # escalon 1 y no llegaba a consultar el ancla NUNCA. En Instagram el
    # contenedor acota de verdad; en Facebook `attachment/media` es un cajon
    # de sastre donde cuelgan el post, los vecinos, el placeholder borroso y
    # hasta el permalink de un video ajeno.
    LK = "https://lookaside.fbsbx.com/lookaside/crawler/media/?media_id="
    IG = "https://instagram.flim38-1.fna.fbcdn.net/v/"

    def _c(u, prov, w=None, h=None, s=76):
        return R.MediaCandidate(url=u, score=s, kind="image", width=w,
                                height=h, is_post_media=True, provenance=prov)

    ENTERCORE = [
        _c(LK + "1523758276433683",
           "attachment/media/photo_image::uri [key,dims,carousel,big]",
           512, 640),                                   # EL MEME (= og:image)
        _c(LK + "1395159759095153",
           "attachment/media/image::uri [key,dims,carousel]", 225, 225, 56),
        _c(LK + "1029224036749977",
           "attachment/media/image::uri [key,dims,carousel]", 261, 163, 56),
        _c(LK + "27531537473140125",
           "attachment/media/image::uri [key,dims,carousel]", 168, 209, 56),
        _c("https://scontent.flim38-1.fna.fbcdn.net/v/t15.5256-10/"
           "752576154_1046134134732361_217287752.jpg",
           "attachment/media/blurred_image::uri [cdn,signed,carousel,big]",
           960, 960, 134),                              # placeholder borroso
        _c("https://www.facebook.com/61585103860771/videos/1334593682178915/",
           "style_type_renderer/attachment/media::permalink_url "
           "[key,dims,carousel,big]", 576, 1024),       # permalink de VIDEO
        _c(LK + "1334593682178915",
           "style_type_renderer/attachment/media::seo_web_crawler_lookaside_url"
           " [key,dims,carousel,big]", 576, 1024),
        _c(LK + "1078546248176237",
           "attachment/media/image::uri [key,dims,carousel]", 128, 225, 56),
        _c(LK + "2919417081754717",
           "attachment/media/image::uri [key,dims,carousel]", 261, 224, 56),
    ]
    kept = R.keep_authoritative(ENTERCORE, LK + "1523758276433683")
    ok &= check("un post de UNA foto devuelve UNA foto (antes: nueve)",
                len(kept) == 1)
    ok &= check("y queda el medio del post, no el mas puntuado (el borroso "
                "puntuaba 134 y el bueno 76)",
                kept and kept[0].url.endswith("1523758276433683"))

    # EL CASO QUE TUMBO MI PRIMERA REGLA. Carrusel MIXTO de Instagram (5 fotos
    # + video), datos reales. Sus fotos se subieron en momentos distintos, asi
    # que los id NO son hermanos: la afinidad de ancla daba 0.00 en tres de
    # las cinco. Un veto por parecido se las habria llevado por delante. El
    # parentesco de id sirve para RECHAZAR intrusos, no para ADMITIR miembros.
    P = "carousel_media/image_versions2/candidates::url"
    MIXTO = [
        _c(IG + "t51.82787-15/654593291_18079113764102329_667247_n.jpg",
           P + " [cdn,iext,signed,key,carousel,big]", 1080, 1080),
        _c(IG.replace("38", "28") + "t51.75761-15/"
           "472987024_18049131551473496_2300132375648910107_n.jpg",
           P + " [cdn,iext,signed,key,dims,carousel,big]", 720, 720),
        _c(IG + "t51.82787-15/655325362_18154963231446696_163327_n.jpg",
           P + " [cdn,iext,signed,key,carousel,big]", 1080, 1080),
        _c(IG.replace("38", "28") +
           "t51.82787-15/669878108_18262800781295936_554816_n.jpg",
           P + " [cdn,iext,signed,key,carousel,big]", 1080, 1080),
        _c(IG + "t51.75761-15/473058021_18049131533473496_524758_n.jpg",
           P + " [cdn,iext,signed,key,dims,carousel,big]", 720, 720),
    ]
    ANC_MIXTO = ("https://scontent.cdninstagram.com/v/t51.75761-15/"
                 "472987024_18049131551473496_2300132375648910107_n.jpg"
                 "?stp=cmp1_dst-jpg_e35_s640x640_tt6")
    ok &= check("carrusel mixto: las 5 fotos SOBREVIVEN aunque sus id no sean "
                "hermanos (un veto por parecido perdia 3)",
                len(R.keep_authoritative(MIXTO, ANC_MIXTO)) == 5)
    ok &= check("y esos hermanos de verdad NO se parecen entre si",
                R.anchor_affinity(MIXTO[0].url, ANC_MIXTO)
                < R.ANCHOR_MIN_AFFINITY)

    # NO ROMPER el carrusel que ya funcionaba (5/5 descargadas en vivo).
    CINCO = [_c(LK + i, "nodes/media/viewer_image::uri "
                        "[key,dims,carousel,big]", 1080, 1080) for i in
             ("122141642061152120", "122141642073152120", "122141642085152120",
              "122141642067152120", "122141642079152120")]
    ok &= check("un carrusel real de 5 sigue devolviendo 5",
                len(R.keep_authoritative(CINCO, LK + "122141642061152120")) == 5)

    # Red de seguridad: si el ancla no aparece entre los candidatos no hay a
    # quien deferir y manda el contenedor, como siempre.
    ok &= check("si el ancla no esta entre los candidatos, manda el contenedor",
                len(R.keep_authoritative(
                    CINCO, "https://otrositio.com/imagen-sin-relacion.jpg")) == 5)

    print("\n=== V2. la caratula de un video NO es una foto del post ===")
    # Verdad de campo dada por el usuario: el post /p/DaJIuiznJjV/ tiene
    # 2 FOTOS y 5 VIDEOS. El motor devolvia "7 fotos" -- el conteo total
    # acertaba, pero 5 de esas "fotos" eran las caratulas de los videos.
    # Bajarlas habria dado 5 fotogramas congelados en vez de los videos, y
    # como un fotograma es un JPEG perfectamente valido, la verificacion de
    # bytes no podia notarlo. Fallo silencioso de una clase nueva.
    #
    # Nada en la URL ni en el tamano delata a una caratula: es un fotograma
    # del propio video, misma camara y misma escena. Solo la delata la
    # ESTRUCTURA, y solo si se conserva la POSICION dentro de las listas --
    # que es justo lo que el DFS tiraba.
    # CAMINOS REALES capturados del post con el diagnostico instrumentado.
    # Los videos salieron en los elementos 0,2,3,4,5 y no hay video en el 1 ni
    # en el 6 -- que es EXACTAMENTE la verdad de campo del usuario: 5 videos y
    # 2 fotos. La reconstruccion cierra sin un solo hueco.
    RAIZ = ("require/#0/#3/#0/__bbox/require/#7/#3/#1/__bbox/result/data/"
            "xig_polaris_media")

    def _vid(i):
        return tuple((f"{RAIZ}/if_not_gated_logged_out/carousel_media/#{i}"
                      "/video_versions/#0/url").split("/"))

    def _img(i, gated=False):
        mid = "if_not_gated_logged_out/" if gated else ""
        return tuple((f"{RAIZ}/{mid}carousel_media/#{i}"
                      "/image_versions2/candidates/#0/url").split("/"))

    # La 1a version comparaba el PREFIJO COMUN exigiendo que terminara en
    # indice, y fallo EN VIVO: Instagram envuelve solo los videos en
    # `if_not_gated_logged_out`, asi que fotos y videos van por RAMAS
    # PARALELAS y su prefijo comun ni llega al carrusel. Debe aguantar las dos
    # formas, porque no sabemos cual usara la plataforma manana.
    for gated in (False, True):
        etq = "misma rama" if gated else "ramas paralelas"
        ok &= check(f"[{etq}] el video #0 y SU caratula son el mismo elemento",
                    R._same_item(_vid(0), _img(0, gated)))
        ok &= check(f"[{etq}] el video #0 y la foto del #1, no",
                    not R._same_item(_vid(0), _img(1, gated)))

    # La 2a version miraba solo el contenedor comun MAS PROFUNDO, y dos fotos
    # de elementos distintos comparten la lista de renditions (`candidates`)
    # siendo ambas la #0 de la suya -> falso positivo. Por eso ahora ningun
    # contenedor compartido puede discrepar.
    ok &= check("dos fotos de elementos distintos no se confunden",
                not R._same_item(_img(1), _img(3)))

    VIDS = [R.MediaCandidate(url=f"v{i}", score=110, kind="video", path=_vid(i))
            for i in (0, 2, 3, 4, 5)]
    FOTOS = [R.MediaCandidate(url=f"f{i}", score=140, kind="image",
                              path=_img(i)) for i in range(7)]
    ok &= check("el post real: de 7 'fotos' quedan las 2 de verdad (#1 y #6)",
                [c.url for c in R.drop_video_posters(FOTOS, VIDS)]
                == ["f1", "f6"])
    ok &= check("sin videos en el post no se toca ninguna foto",
                len(R.drop_video_posters(FOTOS, [])) == 7)
    ok &= check("candidatos sin rastro de posicion no se descartan por error",
                len(R.drop_video_posters(
                    [R.MediaCandidate(url="x", score=1, kind="image")],
                    VIDS)) == 1)

    print("\n=== X. que lo aprendido SOBREVIVA al reinicio ===")
    # Las memorias de puertas eran dicts en RAM: se vaciaban en cada
    # `reload-cauce.sh`, asi que el sistema desaprendia cada vez que el usuario
    # desplegaba. Se persisten guardando el NOMBRE de la puerta, no la cadena
    # de User-Agent (esa puede cambiar de version; el nombre es estable).
    import json as _json
    import tempfile
    import os as _os
    tmpdir = tempfile.mkdtemp()
    path = _os.path.join(tmpdir, "_gates.json")

    R._HOST_MEMORY.clear()
    R._MEDIA_GATE_MEMORY.clear()
    R.load_gate_memory(path)                      # activa la persistencia
    R._remember_gate(R._HOST_MEMORY, "www.facebook.com", (False, "googlebot"))
    R._remember_gate(R._MEDIA_GATE_MEMORY, "lookaside.fbsbx.com",
                     R._FACEBOOKBOT_UA)
    ok &= check("escribe el fichero de puertas", _os.path.exists(path))
    on_disk = _json.load(open(path, encoding="utf-8"))
    ok &= check("guarda el NOMBRE de la puerta, no el User-Agent entero",
                on_disk["media"]["lookaside.fbsbx.com"] == "facebookbot")

    # Simular el reinicio: memorias vacias, volver a cargar.
    R._HOST_MEMORY.clear()
    R._MEDIA_GATE_MEMORY.clear()
    ok &= check("carga el fichero al arrancar", R.load_gate_memory(path))
    ok &= check("la puerta de PAGINA vuelve como TUPLA (se compara con tupla)",
                R._HOST_MEMORY.get("www.facebook.com") == (False, "googlebot"))
    ok &= check("la puerta de MEDIO vuelve como User-Agent real",
                R._MEDIA_GATE_MEMORY.get("lookaside.fbsbx.com")
                == R._FACEBOOKBOT_UA)

    # Tras el "reinicio", la 1a foto ya no busca: va directa a la ganadora.
    tried2: list = []

    def fake2(url, ua, **k):
        tried2.append(R.ua_kind(ua))
        return JPEG if R.ua_kind(ua) == "facebookbot" else LOGIN_HTML

    R.fetch_image_bytes("https://lookaside.fbsbx.com/lookaside/crawler/"
                        "media/?media_id=1", fetch=fake2)
    ok &= check("tras reiniciar, acierta al PRIMER intento (no re-aprende)",
                tried2 == ["facebookbot"])

    ok &= check("no escribe si el valor no cambio (no un fichero por foto)",
                (lambda before: (R._remember_gate(R._MEDIA_GATE_MEMORY,
                                                  "lookaside.fbsbx.com",
                                                  R._FACEBOOKBOT_UA),
                                 _os.path.getmtime(path) == before)[1])(
                    _os.path.getmtime(path)))

    ok &= check("un fichero corrupto NO tumba el arranque",
                (open(path, "w").write("{no es json"),
                 R.load_gate_memory(path) is False)[1])
    R._MEMORY_PATH = None                          # no persistir en el resto

    print("\n=== J. selftests offline del health_check ===")
    ok &= check("selftest() (video) OK", R.selftest())
    ok &= check("selftest_carousel() (fotos) OK", R.selftest_carousel())

    print("\n" + "=" * 62)
    print("RESULTADO:", "TODO PASA (OK)" if ok else "HAY FALLOS (FAIL)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
