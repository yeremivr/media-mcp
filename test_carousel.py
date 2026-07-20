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
              f"cont={c.from_container} {c.url[:62]}")
    ok &= check("SOLO las 3 del contenedor (descarta los 3 <img> sueltos)",
                len(o.images) == 3)
    ok &= check("todas vienen del contenedor estructurado",
                all(c.from_container for c in o.images))
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

    print("\n=== J. selftests offline del health_check ===")
    ok &= check("selftest() (video) OK", R.selftest())
    ok &= check("selftest_carousel() (fotos) OK", R.selftest_carousel())

    print("\n" + "=" * 62)
    print("RESULTADO:", "TODO PASA (OK)" if ok else "HAY FALLOS (FAIL)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
