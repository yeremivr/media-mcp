# -*- coding: utf-8 -*-
"""
resolver.py — Motor estructural de resolucion de medios ("Cauce Resolver").

===========================================================================
QUE PROBLEMA RESUELVE
===========================================================================
yt-dlp es excelente, pero depende de un extractor ESCRITO A MANO por sitio.
Cuando un sitio no tiene extractor (o su HTML cambia y el extractor se rompe),
yt-dlp devuelve vacio. Este modulo es la RED DE SEGURIDAD: cuando yt-dlp falla,
resolvemos el enlace nosotros mismos, de forma resiliente a cambios de HTML.

La observacion clave (verificada leyendo el codigo real de yt-dlp y como lo
hacen los downloaders comerciales) es que TODAS estas plataformas incrustan la
URL del medio dentro del HTML que sirven, y esa URL es reconocible por DOS
cosas independientes del nombre exacto de la clave:

  (1) El PATRON DEL VALOR      -> host de un CDN de medios (dms.licdn.com,
                                  *.fbcdn.net, video.twimg.com...), extension
                                  (.mp4/.m3u8/.mpd) y parametros de firma.
  (2) El CONTEXTO ESTRUCTURAL  -> las claves HERMANAS (width/height/bitrate/
                                  mimeType/duration) y las claves ANCESTRAS
                                  (video/media/stream/progressive).

Un extractor fijo busca `data["video"]["contentUrl"]`. Si LinkedIn renombra
`contentUrl` a `videoUrl`, o lo anida mas hondo, el extractor fijo MUERE. Este
motor NO busca por ruta: convierte el documento en un GRAFO, lo recorre entero,
y PUNTUA cada URL candidata por (1)+(2). Sobrevive a renombres y reanidados.

===========================================================================
COMO FUNCIONA (arquitectura del algoritmo)
===========================================================================
1. ISLAS  -> El HTML no es un solo arbol: es un BOSQUE de "islas" tipadas:
             el DOM (<video>/<source>/<meta og:video>) + N arboles JSON
             sueltos (<script type=application/ld+json>, estado GraphQL en
             <script>, blobs <code style=display:none> de LinkedIn, y JSON
             embebido en atributos como data-sources). `iter_islands()` las
             cosecha todas.

2. GRAFO  -> Cada isla JSON se recorre con un DFS que ARRASTRA PROCEDENCIA:
             por cada hoja string que parezca URL emitimos un RawCandidate con
             su cadena de claves ancestras, el conjunto de claves hermanas y la
             profundidad. Esa procedencia ES el vector de rasgos.  (`_walk`)

3. SCORE  -> Un modelo lineal ponderado sobre rasgos-de-valor U rasgos-de-
             vecindario da un puntaje y una etiqueta (video/audio) + metadata
             de calidad (height/width/tbr) leida de las hermanas. (`score_candidate`)

4. CLUSTER-> Distintas CALIDADES del mismo video son archivos distintos y las
             queremos TODAS; pero el MISMO archivo suele aparecer repetido (en
             el <video> y otra vez en el JSON-LD). Un Union-Find (conjuntos
             disjuntos) fusiona duplicados por identidad-normalizada y deja un
             representante por archivo. (`_dedupe_union_find`)

5. RANK   -> Ordenamos por (kind, height, score), agrupamos en tiers HD/SD/
             audio y calculamos una CONFIANZA (puntaje del ganador saturado,
             mezclado con su margen sobre el 2do) para que el llamador sepa si
             confiar o caer al siguiente escalon de la cascada.

6. IMAGEN -> El MISMO grafo se puntua una SEGUNDA vez con otro modelo lineal
             (`score_image_candidate`, pesos `WI`) para sacar las FOTOS del
             post: carruseles de Instagram (sidecar), pines de Pinterest,
             albumes de Facebook, documentos de LinkedIn. Dos diferencias
             finas frente al pase de video:
               * IDENTIDAD por ID-de-medio, no por path: el CDN sirve la MISMA
                 foto en varios tamanos bajo paths distintos (/s640x640/ vs
                 /p1080x1080/). `image_identity()` extrae el id estable del
                 medio para fusionar tamanos y quedarse con el mas grande.
               * ORDEN DEL DOCUMENTO, no por puntaje: en un carrusel el ORDEN
                 IMPORTA (foto 1, 2, 3...). El DFS arrastra un indice `order`
                 y las fotos se devuelven en ese orden, no rankeadas.

7. PUERTAS-> La resiliencia no es tener UN truco bueno, es tener MUCHAS
             PUERTAS. Los sitios sociales dejan pasar sin login a los robots
             que arman la tarjeta de vista previa de un link (Googlebot,
             Bingbot, facebookexternalhit, Twitterbot, Slackbot, WhatsApp,
             Discordbot). Son puertas de DUENOS DISTINTOS: no se cierran todas
             el mismo dia. `resolve()` las prueba en cascada round-robin, corta
             apenas una da confianza suficiente, y RECUERDA por-host cual gano
             para liderar con esa la proxima vez.

Todo es STDLIB PURA (html.parser, json, re, urllib): en Termux/Android compilar
dependencias con C (lxml, etc.) es un via crucis, asi que NO usamos ninguna.

Este modulo NO depende de yt-dlp ni del server: es importable y testeable solo.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterator
from urllib.parse import urlparse, urljoin
import urllib.request
import urllib.error


# ==========================================================================
# 0. CONOCIMIENTO DEL DOMINIO (los "pesos" del scorer viven aqui, explicitos)
# ==========================================================================

# Hosts (por regex) que sabemos que son CDNs de MEDIOS. Es la senal mas fuerte:
# una URL servida por dms.licdn.com o *.fbcdn.net casi seguro es el archivo.
MEDIA_CDN_HOSTS = re.compile(
    r"""(?xi)
    (
      dms\.licdn\.com                 # LinkedIn video/media
    | media\.licdn\.com               # LinkedIn imagenes (feedshare, documentos)
    | [\w.-]*\.fbcdn\.net             # Facebook / Instagram video
    | [\w.-]*\.cdninstagram\.com      # Instagram
    | scontent[\w.-]*\.(?:fbcdn\.net|cdninstagram\.com)
    | video[\w.-]*\.twimg\.com        # Twitter / X (video)
    | pbs\.twimg\.com                 # Twitter / X (imagenes)
    | [\w.-]*\.tiktokcdn[\w.-]*\.com  # TikTok
    | [\w.-]*\.tiktokv\.com
    | [\w.-]*\.muscdn\.com
    | [\w.-]*\.akamaized\.net         # CDNs genericos usados por muchas apps
    | [\w.-]*\.cloudfront\.net
    | [\w.-]*\.googlevideo\.com
    | [\w.-]*\.pinimg\.com
    )
    """,
)

# Extensiones / mimetypes de contenido reproducible.
VIDEO_EXT = re.compile(r"\.(mp4|m3u8|mpd|webm|mov|ts|m4v)(\?|$|/)", re.I)
AUDIO_EXT = re.compile(r"\.(m4a|mp3|aac|opus|ogg|wav|flac)(\?|$|/)", re.I)
IMAGE_EXT = re.compile(r"\.(jpe?g|png|webp|gif|heic|svg|ico|bmp)(\?|$|/)", re.I)

# CODIGO / TIPOGRAFIAS / DATOS: jamas son el medio que el usuario quiere.
# Descubierto EN VIVO con un post real de Instagram: su pagina de login trae un
# `rsrcMap` con todos sus .js y .css bajo claves llamadas `src`, en un host que
# termina en .cdninstagram.com. Sumaban cdn(46) + key(16) + dims(16) = 78 y
# entraban como "formatos de video". Esto los descalifica de raiz.
CODE_EXT = re.compile(
    r"\.(js|mjs|cjs|css|json|map|wasm|woff2?|ttf|eot|otf|xml|txt|php)(\?|$|/)", re.I)

# Hosts de ASSETS DE INTERFAZ (el CSS/JS/sprites del propio sitio). Comparten
# dominio con el CDN de medios, asi que el sufijo del host NO alcanza para
# distinguirlos: hay que mirar el subdominio y la ruta.
STATIC_HOST_OR_PATH = re.compile(
    r"(?i)(^|//)(static|scripts?|assets?)[\w.-]*\.(licdn|fbcdn|cdninstagram|"
    r"twimg|pinimg)\.com|/rsrc\.php/|/static/|/assets?/|/dist/|/bundles?/")

# Parametros que delatan una URL de CDN FIRMADA (token + expiracion). Su sola
# presencia sube el puntaje: los archivos reales vienen firmados.
SIGNED_PARAM = re.compile(
    r"(?i)(?:^|[?&])(e|oe|oh|efg|bytestart|byteend|_nc_|expires|signature|"
    r"key-pair-id|policy|x-amz-|token|st|ei|sig)=|/dms/|/playlist/vid/",
)

# Lexico de claves que SUELEN contener la URL del medio (case-insensitive,
# por substring). No confiamos SOLO en esto —es una senal mas—, pero pesa.
MEDIA_KEY_LEXICON = (
    "contenturl", "playable_url", "browser_native_hd_url", "browser_native_sd_url",
    "progressiveurl", "streamingurl", "master_playlist", "hd_src", "sd_src",
    "downloadurl", "video_url", "videourl", "mediaurl", "playbackurl", "src",
    "streaminglocation", "base_url", "playlist",
)

# Claves hermanas que confirman que estamos parados sobre un objeto de medio.
DIMENSION_SIBLINGS = frozenset((
    "width", "height", "bitrate", "tbr", "quality", "mimetype", "mime_type",
    "type", "data-bitrate", "duration", "framerate", "fps", "size", "mediatype",
    "contenttype", "codecs", "resolution",
))

# Claves ancestro que sugieren que el subarbol trata de VIDEO/stream.
ANCESTOR_VIDEO_HINT = ("video", "media", "stream", "playback", "progressive",
                       "format", "source", "clip", "reel", "movie", "player")
# ...y las que sugieren IMAGEN (penalizan: no queremos el poster ni el avatar).
ANCESTOR_IMAGE_HINT = ("image", "thumbnail", "thumb", "poster", "preview",
                       "avatar", "icon", "logo", "profile", "picture", "cover")

# Palabras que delatan trackers/pixeles/analitica (penalizan fuerte).
TRACKING_HINT = re.compile(
    r"(?i)(pixel|beacon|/track|/collect|doubleclick|google-analytics|"
    r"analytics|/log(?:ging)?/|/impression|/telemetry|scorecardresearch)",
)

# Pesos del modelo lineal. Explicitos y ajustables: esto ES el "cerebro".
W = {
    "host_media_cdn": 46.0,
    "ext_video": 26.0,
    "ext_audio": 20.0,
    "video_mime_param": 18.0,
    "signed_params": 12.0,
    "key_media": 16.0,
    "sibling_dims": 16.0,
    "ancestor_video": 10.0,
    "dom_video_tag": 22.0,      # vino de un <video>/<source>/og:video real
    "key_hd": 6.0,
    "ext_image": -55.0,
    "ancestor_image": -26.0,
    "tracking": -60.0,
    "is_page_url": -40.0,
    "not_http": -1000.0,        # descalifica
    "too_short": -8.0,
}

# Umbral por debajo del cual NO confiamos en un candidato (cae a la cascada).
MIN_ACCEPT_SCORE = 40.0

# Extensiones que SI son reproducibles. Sirven para saber si el ganador esta
# realmente identificado o si solo lo intuimos (ver WEAK_CONFIDENCE).
PLAYABLE_EXTS = frozenset((
    "mp4", "m3u8", "mpd", "webm", "mov", "ts", "m4v",
    "m4a", "mp3", "aac", "opus", "ogg", "wav", "flac",
))

# Techo de confianza cuando el ganador no tiene NINGUNA prueba de calidad
# (ni altura, ni bitrate, ni extension/mimetype reproducible). Debe quedar por
# DEBAJO del 0.6 con el que resolve() corta la cascada, para que una corazonada
# nunca impida probar la siguiente puerta.
WEAK_CONFIDENCE = 0.35

# Tope de formatos devueltos. Defensivo: en la pagina de login de Instagram
# llegaron a colarse ~100 "formatos" (sus scripts) y el diagnostico salio de
# 110 KB, impagable de leer. Ningun medio real tiene mas de una docena.
MAX_FORMATS = 12

# Limites de red defensivos.
_HTTP_TIMEOUT = 15
_MAX_HTML_BYTES = 8 * 1024 * 1024      # 8 MB de HTML es mas que suficiente
_MAX_THUMB_BYTES = 6 * 1024 * 1024
_MAX_IMAGE_BYTES = 24 * 1024 * 1024    # una foto de carrusel en full res

_BROWSER_UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, "
               "like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36")

# --------------------------------------------------------------------------
# LA CASCADA DE PUERTAS (multi-crawler)
# --------------------------------------------------------------------------
# Los sitios sociales tienen login-wall para personas, pero DEJAN PASAR a los
# robots que necesitan para existir comercialmente:
#   * los BUSCADORES (Googlebot, Bingbot) -> si no, no aparecen en Google.
#   * los DESPLEGADORES DE LINK (facebookexternalhit, Twitterbot, Slackbot,
#     WhatsApp, Discordbot, TelegramBot) -> si no, al pegar el link en un chat
#     no sale la tarjetita con foto y titulo, y nadie hace clic.
# Cada uno es una PUERTA DISTINTA, con dueno distinto y politica distinta.
# Depender de una sola (Googlebot) es un punto unico de fallo: el dia que el
# sitio verifique el rDNS del que dice ser Googlebot, se acabo. Depender de
# SIETE es resiliencia real: tienen que cerrarlas todas para tumbarte.
_GOOGLEBOT_UA = ("Mozilla/5.0 (compatible; Googlebot/2.1; "
                 "+http://www.google.com/bot.html)")
_BINGBOT_UA = ("Mozilla/5.0 (compatible; bingbot/2.0; "
               "+http://www.bing.com/bingbot.htm)")
_FACEBOOKBOT_UA = ("facebookexternalhit/1.1 "
                   "(+http://www.facebook.com/externalhit_uatext.php)")
_TWITTERBOT_UA = "Twitterbot/1.0"
_SLACKBOT_UA = ("Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)")
_WHATSAPP_UA = "WhatsApp/2.24.6.77 A"
_DISCORDBOT_UA = ("Mozilla/5.0 (compatible; Discordbot/2.0; "
                  "+https://discordapp.com)")
_TELEGRAMBOT_UA = "TelegramBot (like TwitterBot)"

# Nombre corto de cada puerta (para diagnostico y para la memoria por-host).
_UA_KIND = {
    _BROWSER_UA: "browser",
    _GOOGLEBOT_UA: "googlebot",
    _BINGBOT_UA: "bingbot",
    _FACEBOOKBOT_UA: "facebookbot",
    _TWITTERBOT_UA: "twitterbot",
    _SLACKBOT_UA: "slackbot",
    _WHATSAPP_UA: "whatsapp",
    _DISCORDBOT_UA: "discordbot",
    _TELEGRAMBOT_UA: "telegrambot",
}


def ua_kind(ua: str) -> str:
    """Nombre corto de la puerta (para diagnostico legible)."""
    return _UA_KIND.get(ua, "browser")


# Alias PUBLICO para que el server reutilice el mismo UA al bajar el archivo.
BROWSER_UA = _BROWSER_UA

# Cuantos intentos (target x puerta) como MAXIMO antes de rendirse. Sin tope,
# 3 reescrituras x 7 puertas = 21 fetches x 15 s = 5 minutos de espera: peor
# que fallar rapido. Con la memoria por-host, en regimen normal se gasta 1.
MAX_ATTEMPTS = 8

# MEMORIA POR-HOST (auto-sanacion): recordamos que CLASE de intento
# (original vs reescritura, y que User-Agent) resolvio por ultima vez cada
# dominio, y la probamos PRIMERO la proxima vez. En regimen normal esto hace
# que casi siempre acertemos al primer fetch aunque el sitio cambie de forma.
_HOST_MEMORY: dict = {}

# --------------------------------------------------------------------------
# QUE LO APRENDIDO SOBREVIVA AL REINICIO
# --------------------------------------------------------------------------
# Las dos memorias de arriba son dicts en RAM: se vaciaban en cada
# `reload-cauce.sh`. O sea que el sistema DESAPRENDIA cada vez que el usuario
# desplegaba codigo nuevo, y la primera peticion de cada host volvia a pagar
# la busqueda de puerta a ciegas. Aprender y olvidar en el mismo dia no es
# aprender.
#
# Se guarda el NOMBRE de la puerta ("googlebot"), no la cadena de User-Agent:
# si un dia actualizamos el UA de Googlebot, un fichero viejo seguiria
# forzando la cadena antigua. El nombre es estable; la cadena es un detalle.
#
# Si la puerta guardada resulta estar cerrada, se pierde UN intento y la
# cascada re-aprende sola. Por eso esto puede persistirse sin miedo: el peor
# caso es exactamente el comportamiento que teniamos siempre.
_MEMORY_PATH: str | None = None


def _remember_gate(memory: dict, key: str, value) -> None:
    """Guarda una puerta ganadora y persiste SOLO si algo cambio (si no,
    escribiriamos el fichero en cada foto de cada carrusel)."""
    if memory.get(key) == value:
        return
    memory[key] = value
    _save_gate_memory()


def _save_gate_memory() -> None:
    if not _MEMORY_PATH:
        return
    try:
        data = {
            "pages": {h: [bool(is_rw), kind] for h, (is_rw, kind) in _HOST_MEMORY.items()},
            "media": {h: ua_kind(ua) for h, ua in _MEDIA_GATE_MEMORY.items()},
        }
        tmp = _MEMORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _MEMORY_PATH)      # atomico: nunca un fichero a medias
    except Exception:
        pass                               # la memoria es una optimizacion,
                                           # nunca un motivo para fallar


def load_gate_memory(path: str) -> bool:
    """Carga las puertas aprendidas y deja activada la persistencia."""
    global _MEMORY_PATH
    _MEMORY_PATH = path
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for h, v in (data.get("pages") or {}).items():
            if isinstance(v, list) and len(v) == 2:
                _HOST_MEMORY[h] = (bool(v[0]), str(v[1]))   # TUPLA: se compara
        for h, kind in (data.get("media") or {}).items():   # con una tupla
            ua = _UA_BY_KIND.get(kind)
            if ua:
                _MEDIA_GATE_MEMORY[h] = ua
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


# ==========================================================================
# 1. PERFILES DE PLATAFORMA  (solo PISTAS: el motor es generico igual)
# ==========================================================================

@dataclass(frozen=True)
class Profile:
    name: str
    host_re: re.Pattern
    # UAs a probar, en orden. El motor prueba el 1o; si no saca nada, el 2o.
    user_agents: tuple = (_BROWSER_UA,)
    # Reescrituras de URL (funcion url->url) para llegar a la version que SI
    # trae el medio embebido (p.ej. la /embed/ de LinkedIn). Se prueban ademas
    # de la URL original, no en su lugar.
    rewrites: tuple = ()


def _linkedin_rewrites(url: str) -> list[str]:
    """LinkedIn expone el video tanto en el post como en su /embed/. Probamos
    ambas: la de embed suele ser mas estable y no pide login para publicos."""
    outs = []
    m = re.search(r"urn:li:activity:(\d+)", url)
    if not m:
        m = re.search(r"-(\d{10,})-\w{4}", url)   # .../posts/...-<id>-xxxx
    if m:
        act = m.group(1)
        outs.append(f"https://www.linkedin.com/embed/feed/update/urn:li:activity:{act}")
    return outs


def _instagram_rewrites(url: str) -> list[str]:
    """Instagram publica un ENDPOINT DE INCRUSTACION publico y sin login:
    `/p/<code>/embed/captioned/`. Devuelve HTML server-rendered con la
    descripcion COMPLETA y, en los carruseles, las fotos del sidecar. Es la
    misma puerta que usan Medium/Notion cuando incrustas un post."""
    m = re.search(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]{5,})", url)
    if not m:
        return []
    code = m.group(1)
    return [f"https://www.instagram.com/p/{code}/embed/captioned/",
            f"https://www.instagram.com/p/{code}/embed/"]


def _facebook_rewrites(url: str) -> list[str]:
    """`m.facebook.com` sirve una version mas simple y menos ofuscada del
    mismo post. No siempre existe, pero cuando existe es oro."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host in ("www.facebook.com", "facebook.com", "web.facebook.com"):
        return [p._replace(netloc="m.facebook.com").geturl()]
    return []


PROFILES = (
    Profile(
        name="linkedin",
        host_re=re.compile(r"(?i)(?:^|\.)linkedin\.com$"),
        # LinkedIn le abre a los buscadores (quiere posicionar) y a los
        # desplegadores de link (quiere que su tarjeta salga en los chats).
        user_agents=(_GOOGLEBOT_UA, _BINGBOT_UA, _SLACKBOT_UA,
                     _TWITTERBOT_UA, _FACEBOOKBOT_UA, _BROWSER_UA),
        rewrites=(_linkedin_rewrites,),
    ),
    Profile(
        name="facebook",
        host_re=re.compile(r"(?i)(?:^|\.)(facebook\.com|fb\.watch|fb\.com)$"),
        user_agents=(_BROWSER_UA, _GOOGLEBOT_UA, _TWITTERBOT_UA,
                     _SLACKBOT_UA, _WHATSAPP_UA, _BINGBOT_UA),
        rewrites=(_facebook_rewrites,),
    ),
    Profile(
        name="instagram",
        host_re=re.compile(r"(?i)(?:^|\.)instagram\.com$"),
        user_agents=(_BROWSER_UA, _GOOGLEBOT_UA, _FACEBOOKBOT_UA,
                     _TWITTERBOT_UA, _WHATSAPP_UA, _BINGBOT_UA),
        rewrites=(_instagram_rewrites,),
    ),
    Profile(
        name="pinterest",
        host_re=re.compile(r"(?i)(?:^|\.)(pinterest\.[\w.]+|pin\.it)$"),
        # Pinterest vive del SEO: a los buscadores les da el pin completo.
        user_agents=(_GOOGLEBOT_UA, _BROWSER_UA, _BINGBOT_UA,
                     _TWITTERBOT_UA, _SLACKBOT_UA),
    ),
    Profile(
        name="twitter",
        host_re=re.compile(r"(?i)(?:^|\.)(twitter\.com|x\.com)$"),
        user_agents=(_BROWSER_UA, _GOOGLEBOT_UA, _TELEGRAMBOT_UA,
                     _SLACKBOT_UA),
    ),
)

# Perfil por defecto: tambien con cascada de puertas. Un blog o un CMS
# cualquiera puede estar detras de un muro suave que si le abre a un bot.
_GENERIC_PROFILE = Profile(
    name="generic",
    host_re=re.compile(r".^"),
    user_agents=(_BROWSER_UA, _GOOGLEBOT_UA, _TWITTERBOT_UA, _SLACKBOT_UA),
)


def profile_for(url: str) -> Profile:
    host = (urlparse(url).hostname or "").lower()
    for p in PROFILES:
        if p.host_re.search(host):
            return p
    return _GENERIC_PROFILE


# ==========================================================================
# 2. COSECHA DE ISLAS  (DOM + JSON embebido)
# ==========================================================================

@dataclass
class DomMedia:
    """Un candidato que vino DIRECTO del DOM (no de JSON): <video>/<source>/
    <meta og:video>. Se le da un plus porque el navegador lo trataria como
    reproducible sin ambiguedad.

    `is_img=True` marca los que vinieron de <img>/srcset/og:image: son
    candidatos legitimos para el pase de IMAGEN, pero NO deben cobrar el bono
    `dom_video_tag` del pase de video (un <img> no es un reproducible)."""
    url: str
    attrs: dict = field(default_factory=dict)
    is_img: bool = False


class _MediaHTMLParser(HTMLParser):
    """Extrae del DOM: (a) src de <video>/<source>/<audio>, (b) el atributo
    JSON `data-sources` de LinkedIn, (c) <meta property=og:video/twitter
    player stream>, y (d) el TEXTO de <script>/<code> (islas JSON candidatas).

    No construimos un arbol DOM completo (caro): solo capturamos lo util en un
    solo pase O(n) sobre el HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.dom_media: list[DomMedia] = []
        self.json_texts: list[str] = []
        self.attr_json: list[str] = []
        # metas: TODOS los <meta property|name -> content>. De aqui salen
        # og:title / og:description / og:image, que es lo que los sitios
        # sociales SI le sirven a un bot de vista previa (nuestra puerta).
        self.metas: dict = {}
        self.page_title: str | None = None
        self._capture_stack: list[str] = []   # 'script' | 'code'
        self._buf: list[str] = []
        self._in_title = False
        self._title_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("video", "source", "audio"):
            src = a.get("src") or a.get("data-src")
            if src:
                self.dom_media.append(DomMedia(src, a))
            # LinkedIn: <video data-sources='[{"src":...,"type":...}]'>
            ds = a.get("data-sources")
            if ds:
                self.attr_json.append(ds)
            for k in ("data-hd-src", "data-sd-src", "data-video-url"):
                if a.get(k):
                    self.dom_media.append(DomMedia(a[k], a))
        elif tag == "img":
            # Las FOTOS de un carrusel server-rendered (lo que ve Googlebot)
            # llegan como <img src>/<img srcset>. Antes las tirabamos.
            for k in ("src", "data-src", "data-delayed-url", "data-lazy-src"):
                if a.get(k):
                    self.dom_media.append(DomMedia(a[k], a, is_img=True))
            for u in _parse_srcset(a.get("srcset") or a.get("data-srcset") or ""):
                self.dom_media.append(DomMedia(u, a, is_img=True))
        elif tag == "link":
            rel = (a.get("rel") or "").lower()
            if "image_src" in rel and a.get("href"):
                self.dom_media.append(DomMedia(a["href"], {"from": "link:image_src"},
                                               is_img=True))
        elif tag == "meta":
            prop = (a.get("property") or a.get("name") or "").lower()
            content = a.get("content") or ""
            if prop and content:
                # nos quedamos con la PRIMERA ocurrencia (la canonica);
                # og:image se repite N veces en los carruseles -> las guardamos
                # todas en una lista aparte, en ORDEN (eso ES el carrusel).
                self.metas.setdefault(prop, content)
                if prop in ("og:image", "og:image:url", "og:image:secure_url",
                            "twitter:image", "twitter:image:src"):
                    self.dom_media.append(DomMedia(content, {"from": prop},
                                                   is_img=True))
            if prop in ("og:video", "og:video:url", "og:video:secure_url",
                        "twitter:player:stream", "og:audio"):
                if content:
                    self.dom_media.append(DomMedia(content, {"from": prop}))
        elif tag == "title":
            self._in_title = True
            self._title_buf = []
        if tag in ("script", "code"):
            self._capture_stack.append(tag)
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title = False
            t = "".join(self._title_buf).strip()
            if t and self.page_title is None:
                self.page_title = t
        if self._capture_stack and tag == self._capture_stack[-1]:
            self._capture_stack.pop()
            text = "".join(self._buf).strip()
            self._buf = []
            if text and ("{" in text or "[" in text):
                self.json_texts.append(text)

    def handle_data(self, data):
        if self._capture_stack:
            self._buf.append(data)
        elif self._in_title:
            self._title_buf.append(data)


def _parse_srcset(srcset: str) -> list[str]:
    """`srcset` es "url1 320w, url2 640w, url3 2x": nos quedamos con TODAS las
    URLs (el pase de imagen ya fusiona tamanos del mismo medio y elige el mas
    grande). Parseo tolerante: las URLs pueden llevar comas dentro del query,
    asi que cortamos por coma-seguida-de-espacio-y-algo-que-parece-URL."""
    out = []
    for part in re.split(r",(?=\s*(?:https?:)?//|\s*/)", srcset or ""):
        tok = part.strip().split()
        if tok and len(tok[0]) > 8:
            out.append(tok[0])
    return out


def _iter_json_objects(text: str) -> Iterator[object]:
    """De un blob de <script> saca los objetos/arrays JSON que contenga.

    A veces el <script> ES json puro (ld+json). A veces es JS con el JSON
    embebido (`window.__data = {...};`). Estrategia robusta y barata:
      1. Intentar json.loads del texto completo.
      2. Si falla, escanear con un contador de llaves balanceado y extraer
         cada region {...} / [...] de tope y probar json.loads de cada una.
    El escaneo respeta strings y escapes para no cortar dentro de comillas."""
    text = text.strip()
    # Quita envoltorios tipo  ld+json  con comentarios CDATA.
    if text.startswith("<!--"):
        text = text[4:]
    if text.endswith("-->"):
        text = text[:-3]
    try:
        yield json.loads(text)
        return
    except Exception:
        pass
    yield from _scan_balanced_json(text)


def _scan_balanced_json(s: str) -> Iterator[object]:
    n = len(s)
    i = 0
    while i < n:
        c = s[i]
        if c in "{[":
            region = _match_balanced(s, i)
            if region is not None:
                chunk = s[i:region]
                try:
                    yield json.loads(chunk)
                    i = region
                    continue
                except Exception:
                    pass
        i += 1


def _match_balanced(s: str, start: int) -> int | None:
    """Devuelve el indice JUSTO DESPUES del cierre que balancea s[start], o
    None si no balancea. Respeta strings JSON (comillas dobles) y escapes."""
    open_c = s[start]
    close_c = "}" if open_c == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    i = start
    n = len(s)
    while i < n:
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == open_c:
                depth += 1
            elif c == close_c:
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return None


def iter_islands(html: str) -> tuple[list[DomMedia], list[object], dict]:
    """Cosecha del HTML: (media directa del DOM, arboles JSON, meta tags).

    El 3er elemento (`metas`) es lo que hace posible la Implementacion 1 de la
    que hablamos: og:description / twitter:description traen el CAPTION COMPLETO
    del post, que es justo lo que yt-dlp no expone en Instagram."""
    parser = _MediaHTMLParser()
    try:
        parser.feed(html)
    except Exception:
        pass  # HTML roto: nos quedamos con lo capturado hasta el fallo

    json_trees: list[object] = []
    for blob in parser.attr_json:
        # data-sources viene HTML-escapado a veces; html.parser ya des-escapa
        # el texto de <script>, pero los atributos pueden traer &quot;.
        candidate = blob.replace("&quot;", '"').replace("&#34;", '"')
        for obj in _iter_json_objects(candidate):
            json_trees.append(obj)
    for text in parser.json_texts:
        for obj in _iter_json_objects(text):
            json_trees.append(obj)
    metas = dict(parser.metas)
    if parser.page_title:
        metas.setdefault("<title>", parser.page_title)
    return parser.dom_media, json_trees, metas


# ==========================================================================
# 3. RECORRIDO DEL GRAFO CON PROCEDENCIA (DFS)
# ==========================================================================

@dataclass
class RawCandidate:
    url: str
    key: str                     # la clave inmediata que apuntaba a esta URL
    ancestors: tuple             # cadena de claves desde la raiz
    siblings: frozenset          # claves hermanas (mismo objeto)
    sibling_obj: dict            # el objeto padre (para leer width/height...)
    depth: int
    from_dom: bool = False
    dom_attrs: dict = field(default_factory=dict)
    is_img: bool = False         # vino de <img>/srcset/og:image
    # Como `ancestors` pero incluyendo la POSICION dentro de cada lista. Un
    # post no es una bolsa de medios: es una SECUENCIA DE ELEMENTOS, y cada
    # elemento es o una foto o un video CON SU CARATULA. `ancestors` no
    # distingue el elemento 3 del 5 —al recorrer una lista no se guardaba el
    # indice— asi que la caratula de un video y una foto de verdad quedaban
    # indistinguibles. Por eso un post de 2 fotos + 5 videos se ofrecia como
    # "7 fotos", y bajarlas habria dado 5 fotogramas congelados en lugar de
    # los videos: archivos JPEG validos, asi que ni la verificacion de bytes
    # lo habria notado. Fallo silencioso.
    # NO se usa para puntuar ni para la procedencia (tocar esos habria movido
    # el scoring y roto el cohorte): solo para saber que dos candidatos
    # colgaban del MISMO elemento del carrusel.
    path: tuple = ()
    # ORDEN DE APARICION en el documento. Para un carrusel el orden IMPORTA
    # (foto 1, 2, 3...) y no coincide con el ranking por puntaje. El DFS
    # visita en orden de documento, asi que este indice ES el orden real.
    order: int = 0


_URLISH = re.compile(r"(?i)^(https?:)?//|^/dms/|^/playlist/")


def _looks_urlish(v: str) -> bool:
    if not isinstance(v, str) or len(v) < 8 or len(v) > 4000:
        return False
    if _URLISH.search(v):
        return True
    # URLs escapadas dentro de JSON de GraphQL suelen venir con \/\/.
    return "http" in v[:12] and "//" in v


def _walk(node, ancestors: tuple, out: list, depth: int = 0,
          parent: dict | None = None, path: tuple = ()):
    """DFS que emite un RawCandidate por cada hoja string que parezca URL,
    arrastrando la procedencia estructural (ancestros, hermanas, objeto padre).

    Complejidad O(nodos). La profundidad se acota para no morir con JSON
    patologico (referencias muy hondas)."""
    if depth > 40:
        return
    if isinstance(node, dict):
        sib_keys = frozenset(str(k).lower() for k in node.keys())
        for k, v in node.items():
            if isinstance(v, str):
                if _looks_urlish(v):
                    out.append(RawCandidate(
                        url=_normalize_escaped(v),
                        key=str(k),
                        ancestors=ancestors,
                        siblings=sib_keys,
                        sibling_obj=node,
                        depth=depth,
                        path=path + (str(k),),
                    ))
            else:
                _walk(v, ancestors + (str(k),), out, depth + 1, node,
                      path + (str(k),))
    elif isinstance(node, (list, tuple)):
        # `ancestors` NO recibe el indice a proposito: el scoring y la
        # procedencia se apoyan en el, y meterle numeros habria movido el
        # puntaje y roto el cohorte de procedencia. `path` si lo guarda, que
        # es lo unico que necesitamos para reconstruir los elementos.
        for i, v in enumerate(node):
            _walk(v, ancestors, out, depth + 1, parent, path + (f"#{i}",))


def _normalize_escaped(u: str) -> str:
    """Deshace escapes tipicos de URLs embebidas en JSON/HTML."""
    u = u.replace("\\/", "/").replace("\\u0025", "%").replace("\\u0026", "&")
    u = u.replace("&amp;", "&")
    if u.startswith("//"):
        u = "https:" + u
    return u.strip()


# ==========================================================================
# 4. SCORING
# ==========================================================================

@dataclass
class MediaCandidate:
    url: str
    score: float
    kind: str                 # 'video' | 'audio'
    height: int | None = None
    width: int | None = None
    tbr: float | None = None       # kbps
    ext: str | None = None
    mime: str | None = None
    provenance: str = ""           # de donde salio (para depurar / transparencia)
    order: int = 0                 # posicion en el documento (orden del carrusel)
    # True si hay EVIDENCIA POSITIVA de que este archivo es el medio DE ESTE
    # POST. Dos formas de conseguirla, y basta con una:
    #   (a) salir de un CONTENEDOR estructurado (carousel_media, sidecar,
    #       image_versions) -> Instagram, Facebook;
    #   (b) tener una RENDITION DE CONTENIDO en la URL (feedshare-, /dms/
    #       document/) -> LinkedIn, que sirve todo como <img> suelto y no
    #       ofrece contenedor al que deferir.
    # Ver `keep_authoritative()`.
    is_post_media: bool = False
    path: tuple = ()               # rastro con indices (ver RawCandidate.path)

    @property
    def identity(self) -> str:
        """Clave para fusionar DUPLICADOS del mismo archivo (misma url sin los
        parametros volatiles de firma). NO fusiona calidades distintas: esas
        viven en paths distintos y las queremos por separado."""
        p = urlparse(self.url)
        # el path (sin query firmada) suele identificar el archivo concreto.
        return f"{p.hostname}{p.path}"


def _int_or_none(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _extract_dims(rc: RawCandidate) -> tuple[int | None, int | None, float | None, str | None]:
    """Lee height/width/tbr/mime de las claves hermanas del candidato."""
    h = w = tbr = None
    mime = None
    obj = rc.sibling_obj or {}
    lower = {str(k).lower(): v for k, v in obj.items()} if isinstance(obj, dict) else {}
    h = _int_or_none(lower.get("height"))
    w = _int_or_none(lower.get("width"))
    tbr = _int_or_none(lower.get("bitrate") or lower.get("bitRate".lower())
                       or lower.get("data-bitrate") or lower.get("tbr"))
    if tbr and tbr > 100000:          # a veces viene en bps -> a kbps
        tbr = tbr // 1000
    mime = (lower.get("type") or lower.get("mimetype") or lower.get("mime_type")
            or lower.get("mediatype") or lower.get("contenttype"))
    if isinstance(mime, str):
        mime = mime.split(";")[0].strip() or None
    else:
        mime = None
    # Pista de altura en el propio path/URL (p.ej. .../720p/... o _1080_).
    if h is None:
        m = re.search(r"(?:^|[/_\-])(\d{3,4})p?(?:[/_\-.]|$)", urlparse(rc.url).path)
        if m:
            cand = int(m.group(1))
            if 144 <= cand <= 4320:
                h = cand
    return h, w, (float(tbr) if tbr else None), mime


def _ext_of(url: str) -> str | None:
    m = re.search(r"\.([a-z0-9]{2,5})(?:\?|$|/)", urlparse(url).path, re.I)
    return m.group(1).lower() if m else None


def score_candidate(rc: RawCandidate, page_url: str) -> MediaCandidate | None:
    """Aplica el modelo lineal. Devuelve None si la URL ni siquiera es http(s)
    valida. El puntaje puede salir negativo (candidato descartable)."""
    url = rc.url
    parsed = urlparse(url if "://" in url else urljoin(page_url, url))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    full = parsed.geturl()

    # DESCALIFICACION DURA (antes de puntuar nada): el .js de la interfaz de
    # Instagram no es un video por mucho que viva en su CDN y su clave se
    # llame `src`. Puntuarlo y luego restarle puntos era fragil; se corta aqui.
    if CODE_EXT.search(url) or STATIC_HOST_OR_PATH.search(full):
        return None

    key_l = rc.key.lower()
    anc_l = " ".join(rc.ancestors).lower()
    host = parsed.hostname.lower()

    s = 0.0
    feats = []

    if MEDIA_CDN_HOSTS.search(host):
        s += W["host_media_cdn"]; feats.append("cdn")

    is_video = bool(VIDEO_EXT.search(url)) or "mime_type=video" in url.lower() or "video/mp4" in url.lower()
    is_audio = bool(AUDIO_EXT.search(url)) or "mime_type=audio" in url.lower()
    is_image = bool(IMAGE_EXT.search(url))

    if is_video:
        s += W["ext_video"]; feats.append("vext")
    if is_audio and not is_video:
        s += W["ext_audio"]; feats.append("aext")
    if "mime_type=video" in url.lower() or "video/mp4" in url.lower():
        s += W["video_mime_param"]; feats.append("vmime")
    if is_image:
        s += W["ext_image"]; feats.append("img")

    if SIGNED_PARAM.search(url):
        s += W["signed_params"]; feats.append("signed")

    if any(tok in key_l for tok in MEDIA_KEY_LEXICON):
        s += W["key_media"]; feats.append("key")
    if any(tok in key_l for tok in ("hd", "high", "1080", "source", "_hd")):
        s += W["key_hd"]; feats.append("hd")

    if rc.siblings & DIMENSION_SIBLINGS:
        s += W["sibling_dims"]; feats.append("dims")

    if any(tok in anc_l for tok in ANCESTOR_VIDEO_HINT):
        s += W["ancestor_video"]; feats.append("anc_v")
    if any(tok in anc_l for tok in ANCESTOR_IMAGE_HINT) or any(
            tok in key_l for tok in ANCESTOR_IMAGE_HINT):
        s += W["ancestor_image"]; feats.append("anc_img")

    if rc.from_dom:
        s += W["dom_video_tag"]; feats.append("dom")

    if TRACKING_HINT.search(url):
        s += W["tracking"]; feats.append("track")

    # Penaliza si es la propia pagina (html) o el dominio principal sin CDN.
    if full.split("#")[0].rstrip("/") == page_url.split("#")[0].rstrip("/"):
        s += W["is_page_url"]; feats.append("selfpage")
    if _ext_of(url) in ("html", "htm", None) and not MEDIA_CDN_HOSTS.search(host) \
            and not is_video and not is_audio:
        # una URL sin extension de medio, no-CDN y sin senales de video: casi
        # seguro es un enlace de navegacion, no el archivo.
        s -= 30.0; feats.append("navlink")

    if len(url) < 24:
        s += W["too_short"]

    h, w, tbr, mime = _extract_dims(rc)

    # Decidir tipo. Preferimos VIDEO salvo evidencia clara de audio puro.
    # Un <img>/srcset/og:image NUNCA es un reproducible, por mas que su clave
    # se llame `src` (que esta en el lexico de medios) y viva en un CDN. Sin
    # esta salida temprana, el logo de una empresa en un <img> puntuaba 74 y
    # se colaba como "formato de video".
    if rc.is_img and not is_video and not is_audio:
        return MediaCandidate(full, s, "image", h, w, None, _ext_of(url), mime,
                              path=rc.path,
                              provenance=f"{'/'.join(rc.ancestors[-3:])}::{rc.key} "
                                         f"[{','.join(feats)}]")
    if is_video or (mime and mime.startswith("video")):
        kind = "video"
    elif is_audio or (mime and mime.startswith("audio")):
        kind = "audio"
    elif is_image:
        return MediaCandidate(full, s, "image", h, w, tbr, _ext_of(url), mime,
                              path=rc.path,
                              provenance=f"{'/'.join(rc.ancestors[-3:])}::{rc.key} [{','.join(feats)}]")
    else:
        # SIN extension ni mimetype claros. Antes esto caia a "video" siempre,
        # y con URLs sin extension (las de /dms/image/ de LinkedIn) metia FOTOS
        # en la lista de formatos reproducibles. Ahora exigimos EVIDENCIA
        # POSITIVA de video; si no la hay, lo tratamos como imagen (el pase de
        # imagen decidira si vale la pena) en vez de ensuciar los formatos.
        # La clave `src` SOLA no alcanza: en el rsrcMap de Instagram cada
        # script se llama `src` y vive en su CDN. Se exige una senal fuerte
        # (mimetype de video, o venir de un <video> real) o bien una pista de
        # nombre CORROBORADA por firma/dimensiones.
        has_video_evidence = (
            "vmime" in feats or "dom" in feats
            or (("anc_v" in feats or "key" in feats)
                and ("signed" in feats or "dims" in feats)
                and "anc_img" not in feats)
        )
        kind = "video" if has_video_evidence else "image"

    prov = f"{'/'.join(rc.ancestors[-3:])}::{rc.key} [{'+'.join(feats)}]"
    return MediaCandidate(full, round(s, 1), kind, h, w, tbr, _ext_of(url), mime,
                          prov, path=rc.path)


# ==========================================================================
# 4-bis. SCORING DE IMAGENES  (carruseles, albumes, pines)
# ==========================================================================
# El pase de VIDEO castiga las imagenes (-55) a proposito: no queremos el
# poster ni el avatar mezclados con los formatos reproducibles. Pero esas
# imagenes YA las teniamos en la mano y las estabamos tirando a la basura.
# Este segundo modelo lineal las rescata y separa las FOTOS DEL POST (lo que
# el usuario quiere bajar) del RUIDO (avatares, logos, iconos, sprites).
#
# La dificultad real no es "encontrar imagenes" —sobran—, es DISTINGUIR la
# foto del contenido del adorno de la interfaz. Las senales que de verdad
# discriminan resultaron ser tres, y ninguna depende del nombre de la clave:
#   (a) TAMANO: una foto de post mide >=600px de lado; un avatar, 50-150px.
#   (b) UBICACION EN EL CDN: las plataformas separan por prefijo de path el
#       contenido (fbcdn /t51.2885-15/) del perfil (/t51.2885-19/) y de los
#       assets estaticos de la interfaz (static.licdn.com, /rsrc.php/).
#   (c) VECINDARIO: estar colgando de `edge_sidecar_to_children`, `carousel`,
#       `slides` o `images` es practicamente una confesion.

WI = {
    "host_media_cdn": 38.0,
    "ext_image": 22.0,
    "signed_params": 10.0,
    "key_image": 18.0,
    "sibling_dims": 12.0,
    "ancestor_carousel": 26.0,   # sidecar/carousel/slides/children/gallery
    "ancestor_image": 10.0,      # image/photo/media (suave: es esperable)
    "big_dims": 20.0,            # >=600px de lado por hermanas o por el path
    "dom_img": 8.0,
    "og_image": 14.0,            # vino de og:image / twitter:image
    # castigos
    "avatar_like": -80.0,        # avatar/profile/logo/icon/sprite/emoji/badge
    "other_content": -70.0,      # recomendados/timeline/relacionados: OTRO post
    "static_asset": -70.0,       # assets de la interfaz, no contenido
    "tiny_dims": -60.0,          # <200px de lado -> es un adorno
    "vector_or_ico": -80.0,      # .svg/.ico jamas son la foto del post
    "data_uri": -1000.0,
    "tracking": -60.0,
    "not_http": -1000.0,
}

MIN_IMAGE_SCORE = 46.0
MAX_IMAGES = 24          # tope defensivo: ningun carrusel real pasa de ~20

# Claves que suelen apuntar a la foto de contenido.
IMAGE_KEY_LEXICON = (
    "display_url", "display_src", "display_resources", "image_url", "imageurl",
    "thumbnail_src", "contenturl", "src", "uri", "url", "image", "photo",
    "original", "large", "orig", "hd", "media_url",
)

# Ancestros que gritan "esto es un carrusel / album / galeria".
# OJO con lo que NO esta aqui: `edges` e `items` se quitaron tras verlo fallar
# EN VIVO. Son las palabras que usa CUALQUIER conexion GraphQL, incluido el
# timeline del perfil (`polaris_ordered_timeline_connection/edges/node`), asi
# que le regalaban +26 a las miniaturas de OTRAS publicaciones de la cuenta y
# se colaban en el carrusel. Un token generico no es una senal.
ANCESTOR_CAROUSEL_HINT = (
    "sidecar", "carousel", "slide", "children", "gallery", "album",
    "images", "photos", "attachments", "subattachments", "resources",
)

# Ancestros que delatan contenido que NO es el del post: recomendados,
# relacionados, el timeline del perfil, exploradores. Vienen del mismo CDN,
# con la misma pinta y el mismo puntaje que las fotos buenas; lo unico que los
# distingue es DE DONDE cuelgan en el documento. Descubierto en vivo: un post
# de 11 fotos devolvia 22 (11 reales + 11 de otras publicaciones del autor).
ANCESTOR_OTHER_CONTENT = (
    "timeline", "related", "suggested", "recommend", "explore", "discover",
    "similar", "more_from", "also_from", "seo_", "sidebar", "reels_tray",
    "stories_tray", "profile_grid", "shortcode_media_preview",
)

# Rutas de CDN que son ASSET DE INTERFAZ, no contenido del post.
STATIC_ASSET_HINT = re.compile(
    r"(?i)(static[\w.-]*\.(?:licdn|fbcdn|xx\.fbcdn)\.com|/rsrc\.php/|/static/|"
    r"/assets?/|/dist/|/bundles?/|sprite|/emoji/|favicon)",
)

# Tokens que delatan un avatar / logo / icono en el path o en las claves.
# `displayphoto`/`framedphoto` son los nombres REALES de LinkedIn
# (profile-displayphoto-scale_400_400): visto en vivo colandose 16 fotos de
# perfil de quienes comentaron un post. Los patrones genericos tipo
# "profile-pic" no los cubrian.
AVATAR_HINT = re.compile(
    r"(?i)(avatar|profile[-_]?pic|profile_image|display[-_]?photo|"
    r"framed[-_]?photo|member[-_]?photo|/pfp|headshot|company[-_]?logo|"
    r"\blogo\b|/icon|placeholder|ghost|default[-_]?user|badge|watermark)",
)

# RENDITIONS DE CONTENIDO: las plataformas nombran la version segun PARA QUE
# sirve el archivo, y ese nombre es una declaracion de proposito tan fuerte
# como estar dentro de un contenedor. LinkedIn usa `feedshare-*` para el medio
# del post, y `profile-displayphoto-*` / `company-logo-*` / `image-scale-*`
# (vista previa de un enlace) para todo lo demas. Instagram hace lo analogo con
# el prefijo de path (t51.2885-15 contenido vs -19 perfil).
CONTENT_RENDITION = re.compile(
    r"(?i)[/_-](feedshare|articleshare|documentshare|ugcshare|videocover)[-_/]"
    r"|/dms/document/")

# Instagram/Facebook codifican el TIPO de foto en el prefijo del path:
#   t51.2885-15 -> foto de FEED (contenido)   |  t51.2885-19 -> FOTO DE PERFIL
_IG_PROFILE_PATH = re.compile(r"/t51\.2885-19/|/t51\.\d+-19/")

# Tamano codificado en el path: /s640x640/, /p1080x1080/, /564x/, _1280.jpg
_PATH_SIZE = re.compile(r"(?i)[/_](?:[sp])?(\d{2,4})x(\d{2,4})[/_]|[/_](\d{3,4})x?/")

# Marcadores de "esta es LA version original, sin recortar". Pinterest usa
# /originals/, otros /full/ o /source/. No traen numero, pero son por
# definicion la mas grande: si no las tratamos como maximas, el agrupador
# elegiria la /736x/ (que si trae numero) y bajariamos la foto en chico.
_FULLSIZE_HINT = re.compile(r"(?i)/(originals?|orig|full|source|raw|master)/")
_FULLSIZE_PX = 100000     # sentinela: "mas grande que cualquier rendition"


# Instagram NO pone el tamano en la ruta sino dentro del parametro `stp`:
#   ?stp=c288.0.864.864a_dst-jpg_e35_s640x640_tt6
# Visto en vivo: por mirar solo la ruta, TODAS las fotos de un carrusel real
# salian sin dimensiones, y sin dimensiones no se puede elegir la version
# grande ni confiar en el resultado.
_STP_SIZE = re.compile(r"(?i)[_.&]([sp])(\d{2,4})x(\d{2,4})(?:[_.&]|$)")


def _path_size(url: str) -> tuple[int | None, int | None]:
    """Tamano (ancho, alto) que el CDN codifica en la ruta o en el query. Es la
    senal mas barata y sorprendentemente fiable para separar foto de adorno."""
    parsed = urlparse(url)
    path = parsed.path
    if _FULLSIZE_HINT.search(path):
        return _FULLSIZE_PX, _FULLSIZE_PX
    # Sufijo de rendition de X/Twitter (.jpg:large, .jpg:orig): son las
    # versiones GRANDES, y sin esto se quedaban sin tamano conocido y perdian
    # frente a una variante chica que si lo declaraba en el query.
    if re.search(r":(?:orig|large)$", path, re.I):
        return _FULLSIZE_PX, _FULLSIZE_PX
    m = _PATH_SIZE.search(path)
    if not m:
        # Fallback al query (Instagram/Facebook lo esconden en `stp`).
        q = _STP_SIZE.search(parsed.query)
        if q:
            return _int_or_none(q.group(2)), _int_or_none(q.group(3))
        return None, None
    if m.group(1) and m.group(2):
        return _int_or_none(m.group(1)), _int_or_none(m.group(2))
    if m.group(3):
        v = _int_or_none(m.group(3))
        return v, v
    return None, None


def _image_dims(rc: RawCandidate, url: str) -> tuple[int | None, int | None]:
    """(ancho, alto) de una FOTO. Deliberadamente NO reusa `_extract_dims`:
    aquella tiene una heuristica pensada para VIDEO que lee "720" de un path
    tipo `/720p/` y en un nombre de foto (`444_555_666_n.jpg`) se confunde y
    devuelve alto=444. Aqui solo confiamos en claves explicitas y en el
    tamano codificado por el CDN."""
    obj = rc.sibling_obj or {}
    lower = {str(k).lower(): v for k, v in obj.items()} if isinstance(obj, dict) else {}
    w = _int_or_none(lower.get("width") or lower.get("config_width")
                     or lower.get("image_width") or lower.get("orig_width"))
    h = _int_or_none(lower.get("height") or lower.get("config_height")
                     or lower.get("image_height") or lower.get("orig_height"))
    pw, ph = _path_size(url)
    return (w or pw), (h or ph)


def image_identity(url: str) -> str:
    """IDENTIDAD DE UNA FOTO (no de una URL).

    El mismo archivo se sirve en varios tamanos bajo paths distintos:
        .../s640x640/123_456_789_n.jpg      <- version chica
        .../p1080x1080/123_456_789_n.jpg    <- version grande
    Deduplicar por `host+path` (como hace el pase de video) los trataria como
    DOS fotos y devolveria el carrusel duplicado. Aqui extraemos el ID ESTABLE
    del medio, que es lo unico que se conserva entre renditions:

      * LinkedIn  /dms/image/v2/<ASSET_ID>/<rendition>/...  -> ASSET_ID
      * FB/IG     .../<id1>_<id2>_<id3>_n.jpg               -> el trio de ids
      * Pinterest /<size>/aa/bb/cc/<hash>.jpg               -> el hash
      * generico  el basename sin los sufijos de tamano

    Ojo con LinkedIn: NO sirve el basename, porque su ultimo segmento es un
    timestamp que varias fotos distintas comparten. Por eso va primero y por
    ASSET_ID, que si es unico por foto."""
    p = urlparse(url)
    path = p.path

    m = re.search(r"/dms/(?:image|document)/(?:v2/)?([A-Za-z0-9_-]{8,})", path)
    if m:
        return f"li:{m.group(1)}"

    base = path.rsplit("/", 1)[-1]

    # X/Twitter marca la rendition con un sufijo PEGADO al nombre en vez de un
    # parametro: HNoYYPpXUAA8b26.jpg:large / :orig / :small. Sin quitarlo, la
    # misma foto en dos tamanos se cuenta como dos fotos distintas (visto en
    # vivo: un tuit con 1 imagen devolvia 2).
    base = re.sub(r":(?:orig|large|medium|small|thumb|\d+x\d+)$", "", base, flags=re.I)

    # ...y ademas X sirve la MISMA foto con y sin extension en la ruta:
    #   /media/HNoYYPpXUAA8b26.jpg:large      (extension + sufijo)
    #   /media/HNoYYPpXUAA8b26?format=jpg     (sin extension, formato en query)
    # Quitar la extension del identificador hace que converjan. De paso fusiona
    # el mismo medio servido como .jpg y como .webp, que es lo deseable.
    base = re.sub(r"\.(jpe?g|png|webp|gif|heic|bmp|avif)$", "", base, flags=re.I)

    host = (p.hostname or "").lower()
    if re.search(r"(?:fbcdn\.net|cdninstagram\.com)$", host):
        # En los CDN de Meta el basename ES el id del medio. Solo aplicamos
        # este patron EN ESOS HOSTS: en un sitio cualquiera, un nombre tipo
        # `2024_01_15_playa.jpg` lo cumpliria y fusionaria fotos distintas.
        m = re.match(r"(\d{3,}_\d{3,}_\d{3,})", base)
        if m:
            return f"fb:{m.group(1)}"
    m = re.match(r"(\d{6,}_\d{6,}_\d{6,})", base)
    if m:
        return f"fb:{m.group(1)}"

    m = re.search(r"/([0-9a-f]{16,})\.(?:jpe?g|png|webp|gif)", path, re.I)
    if m:
        return f"h:{m.group(1).lower()}"

    # Generico: quita sufijos de tamano del basename (foto-640x480.jpg,
    # foto_1280w.jpg, foto-thumb.jpg) para que las variantes converjan.
    stem = re.sub(r"(?i)[-_](?:\d{2,4}x\d{2,4}|\d{3,4}w|thumb(?:nail)?|small|"
                  r"medium|large|orig(?:inal)?)(?=\.|$)", "", base)
    if stem:
        return f"b:{(p.hostname or '').lower()}:{stem.lower()}"

    # La ruta no identifica nada (Facebook: todas las fotos cuelgan del mismo
    # `/lookaside/crawler/media/`). El identificador esta en el query. Sin
    # esto caiamos en `u:<url entera>`, que "funciona" de casualidad mientras
    # cada foto tenga una sola version: en cuanto la plataforma sirva la MISMA
    # foto con otro parametro (`&width=`), la contaria dos veces.
    qids = _query_ids(url)
    if qids:
        return f"q:{(p.hostname or '').lower()}:{max(qids, key=len)}"
    return f"u:{url}"


def score_image_candidate(rc: RawCandidate, page_url: str) -> MediaCandidate | None:
    """Modelo lineal del pase de IMAGEN. Devuelve un MediaCandidate kind=image
    con su puntaje, o None si la URL no es http(s) utilizable."""
    url = rc.url
    if url.startswith("data:"):
        return None
    parsed = urlparse(url if "://" in url else urljoin(page_url, url))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    full = parsed.geturl()
    # Misma descalificacion dura que en el pase de video (ver CODE_EXT).
    if CODE_EXT.search(url) or STATIC_HOST_OR_PATH.search(full):
        return None

    key_l = rc.key.lower()
    anc_l = " ".join(rc.ancestors).lower()
    host = parsed.hostname.lower()
    path = parsed.path

    s = 0.0
    feats = []

    if MEDIA_CDN_HOSTS.search(host):
        s += WI["host_media_cdn"]; feats.append("cdn")

    ext = _ext_of(url)
    if IMAGE_EXT.search(url) or ext in ("jpg", "jpeg", "png", "webp", "heic"):
        s += WI["ext_image"]; feats.append("iext")
    if ext in ("svg", "ico", "gif"):
        s += WI["vector_or_ico"]; feats.append("vector")
    # Un .mp4/.m3u8 NO es una foto: fuera del pase de imagen.
    if VIDEO_EXT.search(url) or AUDIO_EXT.search(url):
        return None

    if SIGNED_PARAM.search(url):
        s += WI["signed_params"]; feats.append("signed")

    if any(tok in key_l for tok in IMAGE_KEY_LEXICON):
        s += WI["key_image"]; feats.append("key")

    if rc.siblings & DIMENSION_SIBLINGS:
        s += WI["sibling_dims"]; feats.append("dims")

    if any(tok in anc_l for tok in ANCESTOR_CAROUSEL_HINT):
        s += WI["ancestor_carousel"]; feats.append("carousel")
    elif any(tok in anc_l for tok in ANCESTOR_IMAGE_HINT):
        s += WI["ancestor_image"]; feats.append("anc_img")

    # ...pero si cuelga de un contenedor de OTRO contenido (timeline del
    # perfil, recomendados, relacionados), no es del post aunque venga del
    # mismo CDN y tenga la misma pinta. Se aplica SIEMPRE, incluso si ya cobro
    # el bono de carrusel: `carousel_bumper` o similares deben quedar en rojo.
    if any(tok in anc_l for tok in ANCESTOR_OTHER_CONTENT):
        s += WI["other_content"]; feats.append("other")

    if rc.is_img:
        frm = (rc.dom_attrs or {}).get("from", "")
        if "og:image" in frm or "twitter:image" in frm or "image_src" in frm:
            s += WI["og_image"]; feats.append("og")
        else:
            s += WI["dom_img"]; feats.append("dom")

    # --- Tamano: la senal mas discriminante ---
    eff_w, eff_h = _image_dims(rc, url)
    big = max(eff_w or 0, eff_h or 0)
    if big >= 600:
        s += WI["big_dims"]; feats.append("big")
    elif big and big < 200:
        s += WI["tiny_dims"]; feats.append("tiny")

    # --- Castigos de "esto es interfaz, no contenido" ---
    if AVATAR_HINT.search(path) or AVATAR_HINT.search(key_l) or AVATAR_HINT.search(anc_l) \
            or _IG_PROFILE_PATH.search(path):
        s += WI["avatar_like"]; feats.append("avatar")
    if STATIC_ASSET_HINT.search(full):
        s += WI["static_asset"]; feats.append("static")
    if TRACKING_HINT.search(url):
        s += WI["tracking"]; feats.append("track")

    if CONTENT_RENDITION.search(full):
        feats.append("content")

    prov = f"{'/'.join(rc.ancestors[-3:])}::{rc.key} [{'+'.join(feats)}]"
    return MediaCandidate(full, round(s, 1), "image", eff_h, eff_w, None,
                          ext, None, prov, order=rc.order,
                          is_post_media=("carousel" in feats or "content" in feats),
                          # ESTE es el constructor que fabrica las fotos que ve
                          # el usuario. Al anadir `path` parchee los otros tres
                          # y me deje justo este: las imagenes salian con el
                          # camino VACIO y `drop_video_posters` comparaba
                          # contra la nada, asi que no descartaba una sola
                          # caratula. El diagnostico lo delato: "path": "" en
                          # las siete fotos y camino completo en los videos.
                          path=rc.path)


# ==========================================================================
# EL ANCLA  (la generalizacion que evita seguir aprendiendo vocabulario)
# ==========================================================================
# Todo lo anterior —contenedores, renditions— exigio que yo supiera como llama
# CADA plataforma a sus cosas: carousel_media, feedshare, t51.2885-15,
# profile-displayphoto. Eso es exactamente la fragilidad que este motor existe
# para evitar: es un extractor-por-sitio disfrazado, y con cada plataforma
# nueva hay que volver a aprender su jerga.
#
# LA SALIDA: cada pagina nos regala un EJEMPLO ETIQUETADO y no lo estabamos
# usando. `og:image` es, por CONTRATO, el medio propio del post — el mismo
# contrato de vista-previa-de-enlace que hace funcionar la cascada de puertas.
# Ninguna plataforma puede romperlo sin que sus enlaces se vean rotos en cada
# chat del mundo.
#
# Asi que en vez de codificar vocabulario, se APRENDE LA FIRMA DEL ANCLA en
# tiempo de ejecucion y se conserva lo que se le parece. Dos rasgos bastan, y
# ninguno depende del nombre de la plataforma:
#
#   1. FAMILIA DE RENDITION: el prefijo alfabetico del segmento de ruta.
#      LinkedIn -> el ancla es `feedshare-...`; `profile-displayphoto-...` es
#      otra familia. Funciona sin saber que significa "feedshare".
#   2. PARENTESCO DE ID: las plataformas usan identificadores tipo snowflake
#      (con marca de tiempo al principio) y los medios de UN MISMO post se
#      suben juntos -> sus IDs son VECINOS. Los de otro post divergen enseguida.
#      Instagram real: 17938923786... / 17938923798... (9 digitos comunes).
#      Instagram intruso: 17898130368... (2 digitos comunes). Es aritmetica,
#      no vocabulario.

ANCHOR_MIN_AFFINITY = 0.5


def _host_family(url: str) -> str:
    """Los dos ultimos labels del host: licdn.com, cdninstagram.com, pinimg.com."""
    h = (urlparse(url).hostname or "").lower()
    return ".".join(h.split(".")[-2:]) if h else ""


def _family_tokens(url: str) -> frozenset:
    """Prefijos alfabeticos de los segmentos de ruta que nombran una FAMILIA.
    `feedshare-shrink_800` -> feedshare. `profile-displayphoto-x` -> profile.
    Los segmentos que son puro id/hash/tamano no aportan (no tienen prefijo
    alfabetico de 3+ letras seguido de separador)."""
    out = set()
    for seg in urlparse(url).path.split("/"):
        m = re.match(r"(?i)^([a-z]{3,})[-_.]", seg)
        if m:
            out.add(m.group(1).lower())
    return frozenset(out)


def _query_ids(url: str) -> list:
    """Identificadores que viajan en el QUERY en vez de en la ruta.

    El motor daba por supuesto que la identidad de un medio vive en el path.
    Facebook no: sirve TODAS sus fotos desde el mismo path
    `/lookaside/crawler/media/` y distingue por `?media_id=...`. Con esa
    suposicion, `_media_ids` devolvia [] y el ancla quedaba CIEGA — le daba
    identica afinidad (0.25) a la foto del post y a la basura de otro post,
    asi que nada alcanzaba el umbral y `keep_authoritative` caia al escalon
    "no hay autoridad, conservo todo". De ahi salio un post de UNA foto
    reportado como carrusel de NUEVE.

    Se descartan los valores cortos (`width=640`): un identificador global no
    tiene 3 digitos."""
    out = []
    for part in urlparse(url).query.split("&"):
        if "=" not in part:
            continue
        val = part.split("=", 1)[1]
        if len(val) >= 8 and val.isalnum():
            out.append(val)
    return out


def _media_ids(url: str) -> list:
    """Tokens largos que parecen identificadores.

    Se mira PRIMERO el basename de la ruta y solo se cae al query si la ruta
    no dice nada. El orden importa y es deliberado: Instagram y compania
    llevan en el query tokens de FIRMA (`_nc_ohc`, `oh`, `oe`) que son
    aleatorios por URL, no identidad. Si se mezclaran siempre, dos fotos sin
    relacion podrian compartir prefijo por casualidad y el parentesco de ID
    —que es el rasgo mas fuerte del ancla— se volveria ruidoso. Consultando
    el query SOLO cuando la ruta calla, las plataformas cuyo path ya
    identifica el medio se quedan exactamente como estaban."""
    base = urlparse(url).path.rsplit("/", 1)[-1]
    ids = [t for t in re.split(r"[^A-Za-z0-9]+", base) if len(t) >= 8]
    return ids if ids else _query_ids(url)


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def anchor_affinity(url: str, anchor: str | None) -> float:
    """0..1 — cuanto se PARECE estructuralmente `url` al ancla (og:image).

    No mira el contenido ni el nombre de la plataforma: solo la forma de la
    URL. Verificado contra las 5 plataformas capturadas en vivo."""
    if not anchor or not url:
        return 0.0
    a = 0.0

    if _host_family(url) and _host_family(url) == _host_family(anchor):
        a += 0.25

    fam_u, fam_a = _family_tokens(url), _family_tokens(anchor)
    if fam_a:
        # El ancla declara familia: pertenecer a ella suma; ser de OTRA resta.
        a += 0.45 if (fam_u & fam_a) else -0.35

    best = 0
    for x in _media_ids(url):
        for y in _media_ids(anchor):
            best = max(best, _common_prefix_len(x, y))
    # El parentesco de ID pesa MAS que el host, y a proposito: un host puede
    # cambiar sin que cambie el contenido (Instagram sirve la MISMA foto desde
    # cdninstagram.com y desde fbcdn.net, misma red de Meta), mientras que 9+
    # caracteres iniciales comunes en un identificador global no son
    # casualidad. Por eso este rasgo basta por si solo para alcanzar el umbral:
    # la alternativa era codificar "estos dos hosts son primos", que es
    # justamente el vocabulario-por-plataforma del que queremos escapar.
    if best >= 12:
        a += 0.60
    elif best >= 9:
        a += 0.50
    elif best >= 6:
        a += 0.15

    return max(0.0, min(1.0, round(a, 3)))


def _coords(path: tuple) -> list:
    """Las COORDENADAS de un camino: cada par (contenedor, indice) por el que
    pasa. De `a/#0/carousel_media/#3/video_versions/#0/url` salen
    ('a','#0'), ('carousel_media','#3'), ('video_versions','#0')."""
    out = []
    for i in range(1, len(path)):
        if path[i].startswith("#") and not path[i - 1].startswith("#"):
            out.append((path[i - 1], path[i]))
    return out


def _same_item(p1: tuple, p2: tuple) -> bool:
    """True si dos candidatos cuelgan del MISMO elemento de una coleccion.

    Primero intente comparar el PREFIJO COMUN de los dos caminos, exigiendo
    que terminara en un indice. Fallo en vivo, y la autopsia explica por que:
    Instagram sirve las fotos y los videos en RAMAS PARALELAS del mismo JSON.
    Capturado de un post real:

      .../xig_polaris_media/if_not_gated_logged_out/carousel_media/#0/video_versions/#0/url
      .../xig_polaris_media/carousel_media/#0/image_versions2/candidates/#0/url

    Sus caminos se separan ARRIBA, en `if_not_gated_logged_out`, asi que el
    prefijo comun ni siquiera llega al carrusel. Comparar prefijos absolutos
    da falso aunque ambos sean, evidentemente, el elemento 0.

    La regla que si aguanta: los dos caminos pasan por un contenedor que se
    llama IGUAL (`carousel_media`) y cada uno declara ahi su indice. Se busca
    el contenedor comun MAS PROFUNDO y se comparan esas coordenadas. Da igual
    por cuantas ramas o envoltorios llegue cada uno; lo que importa es que
    ocupen la misma posicion en la misma coleccion.

    No se nombra ninguna plataforma: no se exige que el contenedor se llame
    `carousel_media`, solo que sea el mismo en ambos caminos.

    Un matiz que costo otro fallo: no vale mirar SOLO el contenedor comun mas
    profundo. Dos fotos de elementos distintos comparten tambien la lista de
    renditions (`candidates`) y las dos son la #0 de la suya, asi que el
    contenedor mas profundo decia "mismo elemento" cuando no lo era. La regla
    es mas estricta y mas simple: de TODOS los contenedores que comparten,
    ninguno puede discrepar. En cuanto uno lo hace, son elementos distintos.

    Y un ultimo matiz, que salio al agrupar las renditions de un mismo video:
    la ULTIMA coordenada de un camino es el indice de RENDITION, no el del
    elemento. `video_versions/#0` y `video_versions/#1` son el mismo video en
    dos calidades; `candidates/#0` es la primera rendition de una foto. Todo
    lo que esta POR ENCIMA de esa ultima lista identifica QUE medio es; la
    ultima solo dice EN QUE CALIDAD. Por eso se descarta antes de comparar.
    """
    c1, c2 = _coords(p1)[:-1], _coords(p2)[:-1]
    if not c1 or not c2:
        return False
    por_clave: dict = {}
    for k, idx in c2:
        por_clave.setdefault(k, set()).add(idx)
    compartido = False
    for k, idx in c1:
        if k in por_clave:
            if idx not in por_clave[k]:
                return False             # discrepan: elementos distintos
            compartido = True
    return compartido


def drop_video_posters(images: list[MediaCandidate],
                       videos: list[MediaCandidate]) -> list[MediaCandidate]:
    """Quita de las FOTOS las que en realidad son la caratula de un video.

    Verificado en vivo con un post de Instagram de 2 fotos y 5 videos: se
    ofrecia como "7 fotos" y bajarlas habria dado 5 fotogramas congelados en
    vez de los videos. Como los fotogramas son JPEG perfectamente validos, la
    verificacion de bytes no podia notarlo: el sistema habria dicho "listo, 7
    fotos guardadas" y el usuario habria encontrado 5 archivos que no son lo
    que pidio. Otro fallo silencioso, y de una clase nueva.

    No hay nada en la URL ni en el tamano que delate a una caratula —es un
    fotograma del mismo video, misma camara, misma escena—. Lo unico que la
    delata es la ESTRUCTURA: cuelga del mismo elemento que un video."""
    if not videos or not images:
        return images
    return [im for im in images
            if not any(_same_item(im.path, v.path) for v in videos if v.path)]


def _prov_slot(c: MediaCandidate) -> str:
    """El SITIO de la estructura del que salio un candidato, sin los rasgos.

    `provenance` es "ancestros::clave [rasgos]" y los rasgos varian entre
    hermanos (uno declara dimensiones y otro no), asi que compararla entera
    separaria medios que salieron del mismo sitio. El sitio es lo estable."""
    return (c.provenance or "").split(" [")[0]


def keep_authoritative(cands: list[MediaCandidate],
                       anchor: str | None = None) -> list[MediaCandidate]:
    """SI hay evidencia POSITIVA de cuales son los medios DEL POST, esos mandan
    y todo lo demas sobra.

    Descubierto EN VIVO, dos veces y de dos formas distintas:

      * Instagram: un post de 3 fotos devolvia 12. Las 3 reales colgaban de
        `carousel_media/image_versions2/candidates`; las otras 9 eran <img>
        sueltos — la cuadricula del perfil. Autoridad = el CONTENEDOR.
      * LinkedIn: un post de 1 foto devolvia 18. La real era `feedshare-
        shrink_800`; las otras 17 eran fotos de perfil de quienes comentaron
        (`profile-displayphoto-*`) y la vista previa de un enlace. Aqui NO hay
        contenedor —LinkedIn sirve todo como <img> suelto— pero el nombre de la
        RENDITION declara el proposito igual de claro.

    En ambos casos los intrusos son indistinguibles por rasgos propios: mismo
    CDN, firmados, dimensiones plausibles. Solo los separa saber que el archivo
    fue puesto ahi PARA ESTE POST.

    Regla, en tres escalones de menor a mayor generalidad:

      1. EVIDENCIA EXPLICITA (contenedor o rendition de contenido). Es la mas
         fuerte porque la plataforma la declara; si existe, manda.
      2. EL ANCLA (`og:image`). Cuando la plataforma no nos da ni contenedor ni
         un nombre de rendition que reconozcamos —o sea, en CUALQUIER
         plataforma que todavia no hemos visto— se aprende la firma del ancla y
         se conserva lo que se le parece. Este escalon es el que evita tener
         que seguir aprendiendo vocabulario plataforma por plataforma.
      3. Si no hay ni una cosa ni la otra, no hay autoridad a la que deferir y
         se conserva todo (Pinterest, blogs, HTML plano)."""
    authoritative = [c for c in cands if c.is_post_media]
    if authoritative:
        # EL ANCLA TAMBIEN VETA, no solo suple.
        #
        # Descubierto en vivo con un post de Facebook de UNA foto que salio
        # como carrusel de nueve. La suposicion rota era que "venir de un
        # contenedor" garantiza pertenecer al post. En Instagram si:
        # `carousel_media/image_versions2/candidates` contiene exactamente las
        # fotos del post. En Facebook NO: bajo `attachment/media` cuelgan el
        # medio del post, las miniaturas de los posts vecinos, el placeholder
        # borroso y hasta el permalink de un video. El contenedor no acota
        # nada, asi que devolver su contenido entero era devolver la pagina.
        #
        # Cuando ademas hay ancla tenemos DOS evidencias independientes: el
        # contenedor dice "esto es un medio" y el ancla dice "esto pertenece a
        # ESTE post". Cruzarlas es mas fuerte que cualquiera de las dos, y
        # ninguna plataforma tiene que declarar nada nuevo para que funcione.
        #
        # Si la interseccion sale vacia NO nos quedamos sin nada: puede que el
        # og:image sea de otra naturaleza que los medios del contenedor (una
        # portada de video frente a las pistas, por ejemplo). En ese caso el
        # contenedor sigue mandando, que es el comportamiento de siempre.
        # QUE COHORTE, no que parecido. Primero intente vetar por afinidad de
        # ancla y los datos reales lo tumbaron: en un carrusel de Instagram
        # cuyas fotos se subieron en momentos distintos, los id NO son
        # hermanos (afinidad 0.00 en 3 de 5) y el veto se habria llevado por
        # delante fotos legitimas. El parentesco de id sirve para RECHAZAR
        # intrusos, no para ADMITIR miembros.
        #
        # Lo que si separa los dos casos, mirando las procedencias reales:
        #
        #   Instagram, las 5 buenas -> carousel_media/image_versions2/candidates
        #   EnterCore, el meme      -> attachment/media/photo_image
        #   EnterCore, los vecinos  -> attachment/media/image
        #   EnterCore, el borroso   -> attachment/media/blurred_image
        #
        # Los medios de UN post salen del MISMO sitio de la estructura, porque
        # los emite el mismo trozo de codigo de la plataforma. La basura sale
        # de otro. Y el ancla, que por contrato es un medio del post, nos dice
        # cual de esos grupos es el bueno: se busca al ancla entre los
        # candidatos y se conserva su cohorte entera.
        #
        # Es mas fuerte que el veto y no exige que los medios se parezcan
        # entre si: admite hermanos que no comparten ni id ni tamano, con tal
        # de que la plataforma los haya emitido juntos.
        if anchor:
            anchor_id = image_identity(anchor)
            slot = next((_prov_slot(c) for c in authoritative
                         if image_identity(c.url) == anchor_id), None)
            if slot:
                # El cohorte elige QUE FOTOS, no que URLs: la version chica y
                # la grande de una misma foto pueden salir de sitios distintos
                # de la estructura, y filtrar por URL se quedaba con la chica
                # y tiraba la de 1080 (lo caz un test que ya existia). Se
                # toman las IDENTIDADES del cohorte y luego se conservan todas
                # sus renditions, vengan de donde vengan; `group_images` ya
                # sabe quedarse con la mayor de cada una.
                ids = {image_identity(c.url) for c in authoritative
                       if _prov_slot(c) == slot}
                cohort = [c for c in authoritative
                          if image_identity(c.url) in ids]
                if cohort:
                    return cohort
        # Sin ancla, o con un ancla que no aparece entre los candidatos, no hay
        # a quien deferir: manda el contenedor, como siempre.
        return authoritative

    if anchor:
        near = [c for c in cands
                if anchor_affinity(c.url, anchor) >= ANCHOR_MIN_AFFINITY]
        if near:
            return near

    return cands


def group_images(cands: list[MediaCandidate]) -> list[MediaCandidate]:
    """Agrupa por `image_identity` (fusiona tamanos de la MISMA foto), elige
    como representante el de MAYOR resolucion (y a igualdad, mayor puntaje), y
    devuelve los grupos EN ORDEN DE DOCUMENTO — que es el orden del carrusel,
    no el ranking por calidad. Esa distincion es la que hace que "bajame la 2 y
    la 5" signifique lo mismo para el usuario que para el servidor."""
    groups: dict[str, list[MediaCandidate]] = {}
    for c in cands:
        groups.setdefault(image_identity(c.url), []).append(c)

    reps: list[MediaCandidate] = []
    for members in groups.values():
        best = max(members, key=lambda c: (max(c.height or 0, c.width or 0),
                                           c.score, len(c.url)))
        # El orden del grupo es el de su PRIMERA aparicion en el documento.
        best.order = min(m.order for m in members)
        # El puntaje del grupo es el del mejor miembro: si CUALQUIER rendition
        # tenia evidencia fuerte (dims grandes, ancestro carousel), el grupo
        # entero se beneficia. Evita perder una foto porque la variante que
        # gano por tamano venia de una isla del HTML mas pobre en contexto.
        best.score = max(m.score for m in members)
        # `_FULLSIZE_PX` es un CENTINELA para ordenar (/originals/, .jpg:large):
        # significa "mas grande que cualquier otra version", no un tamano real.
        # Si se deja puesto, sale a la interfaz como "Foto 1 · 100000x100000".
        # Se cambia por el mayor tamano REAL conocido del grupo, y si ninguno
        # se conoce, por None: no saberlo es la verdad, inventarlo no.
        if (best.width or 0) >= _FULLSIZE_PX or (best.height or 0) >= _FULLSIZE_PX:
            reales = [(m.width, m.height) for m in members
                      if m.width and m.width < _FULLSIZE_PX]
            best.width, best.height = (max(reales) if reales else (None, None))
        reps.append(best)

    reps.sort(key=lambda c: c.order)
    return reps[:MAX_IMAGES]


# ==========================================================================
# 5. UNION-FIND (fusion de duplicados del mismo archivo)
# ==========================================================================

class _DSU:
    """Disjoint Set Union con compresion de caminos + union por rango.
    Lo usamos para agrupar candidatos que son el MISMO archivo repetido en
    varias islas del HTML, y quedarnos con el mejor representante de cada uno."""

    def __init__(self, n: int):
        self.p = list(range(n))
        self.r = [0] * n

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.r[ra] < self.r[rb]:
            ra, rb = rb, ra
        self.p[rb] = ra
        if self.r[ra] == self.r[rb]:
            self.r[ra] += 1


def _dedupe_union_find(cands: list[MediaCandidate]) -> list[MediaCandidate]:
    """Fusiona por identidad (host+path sin query). De cada grupo deja el de
    mayor puntaje (y, a igualdad, el de mayor resolucion / mas metadata)."""
    if not cands:
        return []
    dsu = _DSU(len(cands))
    by_identity: dict[str, int] = {}
    for i, c in enumerate(cands):
        key = c.identity
        if key in by_identity:
            dsu.union(by_identity[key], i)
        else:
            by_identity[key] = i

    groups: dict[int, list[int]] = {}
    for i in range(len(cands)):
        groups.setdefault(dsu.find(i), []).append(i)

    reps: list[MediaCandidate] = []
    for members in groups.values():
        best = max((cands[i] for i in members),
                   key=lambda c: (c.score, c.height or 0, c.tbr or 0,
                                  len(c.url)))
        reps.append(best)
    return reps


# ==========================================================================
# 6. API PUBLICA DEL MOTOR
# ==========================================================================

@dataclass
class ResolveResult:
    ok: bool
    url: str
    strategy: str                 # que combinacion de UA/rewrite gano
    confidence: float             # 0..1
    title: str | None = None
    uploader: str | None = None
    thumbnail: str | None = None
    duration: int | None = None
    formats: list = field(default_factory=list)   # MediaCandidate ordenados
    reason: str = ""
    diagnostics: dict = field(default_factory=dict)
    # --- lo que agrega el pase de imagen / caption ---
    images: list = field(default_factory=list)    # MediaCandidate en ORDEN
    media_type: str = "none"                      # video|carousel|image|none
    full_caption: str | None = None               # el texto COMPLETO del post
    hashtags: list = field(default_factory=list)

    def to_info(self) -> dict:
        """Convierte a un 'info dict' estilo yt-dlp para que el server lo cure
        con la MISMA logica de list_formats. Los formatos del resolver son
        MUXED (video+audio juntos) salvo los audio-only."""
        fmts = []
        for i, c in enumerate(self.formats):
            muxed = c.kind == "video"
            fmts.append({
                "format_id": f"cauce{i}-{(c.height or 0)}{'p' if c.height else ''}",
                "url": c.url,
                "ext": c.ext or ("mp4" if c.kind == "video" else "m4a"),
                "height": c.height,
                "width": c.width,
                "tbr": c.tbr,
                "vcodec": "unknown" if muxed else "none",
                "acodec": "unknown",
                "protocol": "m3u8_native" if (c.ext == "m3u8") else "https",
                "_cauce_score": c.score,
                "_cauce_muxed": muxed,
                "_cauce_provenance": c.provenance,
                "_cauce_path": "/".join(c.path),
            })
        return {
            "title": self.title,
            "uploader": self.uploader,
            "thumbnail": self.thumbnail,
            "duration": self.duration,
            "formats": fmts,
            # `description` con el nombre de yt-dlp: asi el server no necesita
            # saber de donde vino el caption para mostrarlo.
            "description": self.full_caption,
            "_cauce_resolver": True,
            "_cauce_strategy": self.strategy,
            "_cauce_confidence": self.confidence,
            "_cauce_media_type": self.media_type,
            "_cauce_hashtags": list(self.hashtags),
            "_cauce_images": [{
                "index": i + 1,                  # 1-based: lo que ve el humano
                "url": c.url,
                "width": c.width,
                "height": c.height,
                "ext": c.ext or "jpg",
                "score": c.score,
                "provenance": c.provenance,
                # El camino CON indices de lista. Sin verlo de verdad no se
                # puede emparejar un video con su caratula: Instagram sirve
                # las fotos y los videos en estructuras PARALELAS.
                "path": "/".join(c.path),
            } for i, c in enumerate(self.images)],
            "webpage_url": self.url,
        }


# Claves de texto largo que suelen contener el CAPTION del post. LinkedIn lo
# llama `commentary`, Instagram `edge_media_to_caption.edges[].node.text`,
# schema.org `articleBody`/`description`, Facebook `message`.
CAPTION_KEYS = ("caption", "description", "articlebody", "commentary",
                "message", "text", "content", "summary", "title_text")

# Basura tipica que las plataformas ponen en og:description cuando NO hay
# caption real (la frase de relleno). Si el candidato es solo esto, no sirve.
_CAPTION_NOISE = re.compile(
    r"(?i)^\s*(\d+[\d.,]*\s*(likes?|me gusta|comments?|comentarios?|views?|"
    r"reproducciones)\b.*){1,3}\s*$",
)


# LinkedIn cierra su og:description con metricas: "<caption> | 432 comments on
# LinkedIn". Igual que la envoltura de Instagram, es ruido en cada post.
_SOCIAL_TAIL = re.compile(
    r"(?i)\s*[|·-]\s*[\d.,]+[KMB]?\s+(?:comments?|reactions?|likes?|shares?)"
    r"(?:\s+on\s+\w+)?\s*\.?\s*$")


# ESLOGANES DE PLATAFORMA: texto identico en CADA pagina del sitio, que no
# dice nada del contenido. Pinterest devuelve su tagline traducido ("Scopri (e
# salva) i tuoi Pin su Pinterest."), Facebook e Instagram tienen los suyos.
# Rechazarlos es de bajo riesgo: solo descartamos ruido, nunca contenido.
_BOILERPLATE = re.compile(
    r"(?i)^\s*("
    r"(scopri|discover|descubre|d[ée]couvrez|entdecke|descobre|ontdek)\b[^.]{0,60}"
    r"\bpin(e?s)?\b[^.]{0,40}\bpinterest"
    r"|see\s+(posts|photos)[^.]{0,60}\b(facebook|instagram)"
    r"|log\s+in\s+or\s+sign\s+up"
    r"|(inicia\s+sesi[óo]n|reg[íi]strate)\b"
    r"|\d+[\d.,]*[KMB]?\s+(followers?|seguidores)\b"
    r")")


def clean_caption(t) -> str | None:
    """Version PUBLICA: limpia un caption venga de donde venga (tambien del
    `description` de yt-dlp, que el server usa en la via rapida)."""
    return _clean_caption(t)


def _clean_caption(t) -> str | None:
    if not isinstance(t, str):
        return None
    if _BOILERPLATE.match(t.strip()):
        return None
    t = re.sub(r"\s+\n", "\n", t.replace("\r", "")).strip()
    # Puede haber mas de una metrica encadenada al final.
    for _ in range(3):
        t2 = _SOCIAL_TAIL.sub("", t).strip()
        if t2 == t:
            break
        t = t2
    if len(t) < 12 or len(t) > 6000:
        return None
    if _CAPTION_NOISE.match(t):
        return None
    return t[:4000]


# Las redes envuelven el caption dentro del og:title con un prefijo de autor:
#   'Not Journal on Instagram: "Um jovem decidiu ignorar as vagas..."'
# De ahi salen DOS datos: el AUTOR (que hoy llegaba null) y el caption REAL de
# ESTA publicacion — que es justo el que hay que preferir sobre cualquier otro
# texto largo del documento.
_OG_TITLE_WRAPPER = re.compile(
    r'(?is)^\s*(.{1,80}?)\s+on\s+(?:instagram|facebook|threads|linkedin|x|'
    r'twitter)\s*:\s*[\"“”](.*?)[\"“”]\s*(?:/\s*\w{1,10}\s*)?$')


# El og:description de Instagram envuelve el caption en estadisticas:
#   '273K likes, 880 comments - cosmopolitan on July 5, 2026: "texto".'
# Es ruido en CADA post: ensucia el caption, se arrastra al nombre del archivo
# y le da a Claude datos irrelevantes. De paso, el `- <handle> on` trae el
# usuario REAL (cosmopolitan), mas util que el nombre para mostrar.
_OG_DESC_WRAPPER = re.compile(
    r'(?is)^\s*[\d.,]+[KMB]?\s+likes?\s*,\s*[\d.,]+[KMB]?\s+comments?\s*'
    r'[-–—]\s*(\S+?)\s+on\s+[^:]{3,60}?:\s*[\"“](.*?)[\"”]\s*\.?\s*$')


def unwrap_og_title(t) -> tuple[str | None, str | None]:
    """De 'Autor on Instagram: "texto"' o de '<N> likes, <M> comments - autor
    on <fecha>: "texto"' saca (autor, texto). Si no cuadra ningun patron,
    devuelve (None, el texto tal cual)."""
    if not isinstance(t, str):
        return None, None
    t = t.strip()
    m = _OG_DESC_WRAPPER.match(t)
    if m:
        return m.group(1).strip() or None, m.group(2).strip() or None
    m = _OG_TITLE_WRAPPER.match(t)
    if m:
        return m.group(1).strip() or None, m.group(2).strip() or None
    return None, t


def _caption_key(t: str) -> str:
    """Firma normalizada para decidir si dos textos son EL MISMO caption (uno
    quiza truncado). Solo letras/numeros en minuscula: inmune a comillas
    tipograficas, saltos de linea y espacios raros."""
    return re.sub(r"[^0-9a-zà-ɏ]+", "", (t or "").lower())[:120]


def find_caption(metas: dict, json_trees: list) -> str | None:
    """IMPLEMENTACION 1 (la del TXT): el CAPTION COMPLETO del post.

    yt-dlp en Instagram devuelve "Video by <usuario>" y nada mas; el texto real
    del post —el que dice de que trata— esta en el HTML, en og:description y en
    el JSON embebido. Lo buscamos en las dos fuentes y nos quedamos con el MAS
    LARGO que no sea relleno: mas largo = mas informacion para razonar.

    Coste: CERO fetches extra. Ya bajamos ese HTML para buscar el video.

    OJO CON "EL MAS LARGO GANA": era la regla original y fallo EN VIVO. La
    pagina de un post trae TAMBIEN el texto de otras publicaciones (la
    cuadricula del perfil, relacionados). En un post sobre OpenAI, el motor
    devolvio como caption la cronica de una final del Mundial de OTRA cuenta,
    simplemente porque era mas larga.

    Regla corregida, por AUTORIDAD: las meta-etiquetas describen ESTA pagina,
    asi que mandan. Un texto del JSON solo puede reemplazarlas si es el MISMO
    caption sin truncar (comparten firma normalizada) — que es el caso util:
    og:description suele venir cortado y el JSON trae el texto completo."""
    meta_cands: list[str] = []
    for k in ("og:description", "twitter:description", "description"):
        # og:description tambien puede venir envuelto en estadisticas
        # ("273K likes, 880 comments - user on <fecha>: ..."): se desenvuelve.
        _author, inner = unwrap_og_title(metas.get(k))
        c = _clean_caption(inner)
        if c:
            meta_cands.append(c)
    # og:title suele traer el caption envuelto: 'Autor on Instagram: "..."'.
    for k in ("og:title", "twitter:title", "<title>"):
        _author, inner = unwrap_og_title(metas.get(k))
        c = _clean_caption(inner)
        if c:
            meta_cands.append(c)

    json_cands: list[str] = []

    def visit(node, depth=0):
        if depth > 30:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and str(k).lower() in CAPTION_KEYS:
                    c = _clean_caption(v)
                    if c:
                        json_cands.append(c)
                else:
                    visit(v, depth + 1)
        elif isinstance(node, (list, tuple)):
            for v in node:
                visit(v, depth + 1)

    for tree in json_trees:
        visit(tree)

    if meta_cands:
        best = max(meta_cands, key=lambda c: (len(c), "\n" in c, "#" in c))
        key = _caption_key(best)
        # ¿Hay en el JSON una version MAS LARGA DEL MISMO texto? Esa gana.
        # Un texto DISTINTO, por largo que sea, no: sera de otro post.
        #
        # Se comparan PREFIJOS, no contenido: og:description no siempre es un
        # recorte literal del caption (a veces la plataforma lo abrevia y mueve
        # los hashtags), asi que exigir que el largo CONTENGA al corto fallaba.
        # Lo que si comparten es el ARRANQUE. 30 caracteres normalizados son
        # de sobra especificos: dos posts distintos casi nunca empiezan igual.
        if len(key) >= 24:
            pref = key[:30]
            extended = [c for c in json_cands
                        if len(c) > len(best) and _caption_key(c).startswith(pref)]
            if extended:
                return max(extended, key=len)
        return best

    if not json_cands:
        return None
    return max(json_cands, key=lambda c: (len(c), "\n" in c, "#" in c))


def extract_hashtags(text: str | None) -> list:
    """Hashtags en orden de aparicion, sin repetir. Son el ancla mas barata
    para que Claude sepa DE QUE trata el contenido sin ver el video."""
    if not text:
        return []
    seen = set()
    out = []
    for h in re.findall(r"#([^\s#.,;:!?()\[\]{}\"']{2,60})", text):
        k = h.lower()
        if k not in seen:
            seen.add(k)
            out.append("#" + h)
    return out[:40]


def _find_meta(dom_and_json, page_url) -> dict:
    """Saca titulo/uploader/miniatura/duracion recorriendo el JSON-LD y OG."""
    dom_media, json_trees = dom_and_json[0], dom_and_json[1]
    metas = dom_and_json[2] if len(dom_and_json) > 2 else {}
    meta = {"title": None, "uploader": None, "thumbnail": None, "duration": None}

    # 1) JSON-LD estandar (schema.org VideoObject / SocialMediaPosting).
    def visit(node):
        if isinstance(node, dict):
            t = node.get("@type") or node.get("__typename")
            if t and any(str(t).lower().find(x) >= 0 for x in ("video", "posting", "media", "clip")):
                meta["title"] = meta["title"] or node.get("name") or node.get("headline") or node.get("title")
                thumb = node.get("thumbnailUrl") or node.get("thumbnail") or node.get("image")
                if isinstance(thumb, list) and thumb:
                    thumb = thumb[0]
                if isinstance(thumb, dict):
                    thumb = thumb.get("url") or thumb.get("uri")
                meta["thumbnail"] = meta["thumbnail"] or (thumb if isinstance(thumb, str) else None)
                auth = node.get("author") or node.get("creator") or node.get("uploader")
                if isinstance(auth, dict):
                    auth = auth.get("name")
                if isinstance(auth, list) and auth:
                    auth = auth[0].get("name") if isinstance(auth[0], dict) else auth[0]
                meta["uploader"] = meta["uploader"] or (auth if isinstance(auth, str) else None)
                dur = node.get("duration") or node.get("durationInSeconds") or node.get("video_duration")
                meta["duration"] = meta["duration"] or _parse_duration(dur)
            for v in node.values():
                visit(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                visit(v)

    for tree in json_trees:
        visit(tree)

    # FALLBACK A OPEN GRAPH. Antes solo miraramos el JSON-LD, y muchos sitios
    # (Instagram, Pinterest, casi todo lo servido a un bot de vista previa) NO
    # traen JSON-LD pero SI traen og:*. Es informacion gratis que teniamos
    # delante y no estabamos leyendo.
    # og:title de las redes viene envuelto: 'Autor on Instagram: "caption"'.
    # De ahi salen el AUTOR y un titulo legible (la 1a linea del caption), en
    # vez de volcar la envoltura entera como titulo.
    raw_title = metas.get("og:title") or metas.get("twitter:title") or \
        metas.get("<title>")
    og_author, og_text = unwrap_og_title(raw_title)
    if og_author and not meta["uploader"]:
        meta["uploader"] = og_author
    if not meta["uploader"]:
        # Fallback: el handle que va dentro del og:description envuelto
        # ("... - cosmopolitan on July 5, 2026: ...").
        desc_author, _ = unwrap_og_title(metas.get("og:description"))
        if desc_author:
            meta["uploader"] = desc_author
    if not meta["title"]:
        if og_text:
            first = next((ln.strip() for ln in og_text.splitlines() if ln.strip()), "")
            meta["title"] = (first[:120] or None) if first else (raw_title or None)
        else:
            meta["title"] = raw_title
    meta["thumbnail"] = meta["thumbnail"] or metas.get("og:image") or \
        metas.get("og:image:secure_url") or metas.get("twitter:image")
    if not meta["uploader"]:
        # `author` de Twitter cards y el patron "@usuario" del og:title.
        au = metas.get("twitter:creator") or metas.get("author") or \
            metas.get("article:author")
        if isinstance(au, str) and au.strip():
            meta["uploader"] = au.strip()
    if meta["duration"] is None:
        meta["duration"] = _parse_duration(metas.get("og:video:duration") or
                                           metas.get("video:duration"))
    return meta


def _parse_duration(d) -> int | None:
    if d is None:
        return None
    if isinstance(d, (int, float)):
        v = int(d)
        return v // 1000 if v > 100000 else v      # ms -> s heuristica
    if isinstance(d, str):
        m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d)   # ISO-8601
        if m:
            h, mi, se = (int(x) if x else 0 for x in m.groups())
            return h * 3600 + mi * 60 + se
        if d.isdigit():
            return int(d)
    return None


def resolve_html(html: str, page_url: str, *, strategy: str = "") -> ResolveResult:
    """NUCLEO PURO (sin red): dado el HTML, devuelve el resultado. Esto es lo
    que testeamos con fixtures — es determinista y no toca la red."""
    dom_media, json_trees, metas = iter_islands(html)

    raws: list[RawCandidate] = []
    # (a) media directa del DOM
    for dm in dom_media:
        raws.append(RawCandidate(
            url=_normalize_escaped(dm.url), key=dm.attrs.get("from", "src"),
            ancestors=("<dom>",), siblings=frozenset(dm.attrs.keys()),
            sibling_obj=dm.attrs, depth=0,
            # Un <img> NO cobra el bono de "es un <video> real".
            from_dom=not dm.is_img, dom_attrs=dm.attrs, is_img=dm.is_img))
    # (b) todo lo que haya en los arboles JSON
    for tree in json_trees:
        _walk(tree, tuple(), raws)

    # Sellar el ORDEN DE DOCUMENTO. El DOM va primero y el DFS visita en orden
    # de aparicion, asi que el indice en `raws` ES el orden del carrusel.
    for i, rc in enumerate(raws):
        rc.order = i

    scored: list[MediaCandidate] = []
    for rc in raws:
        mc = score_candidate(rc, page_url)
        if mc is not None:
            scored.append(mc)

    # separa medios reproducibles de imagenes; las imagenes sirven de fallback
    # para la miniatura pero no como "formato".
    media = [c for c in scored if c.kind in ("video", "audio") and c.score >= MIN_ACCEPT_SCORE]
    media = _dedupe_union_find(media)
    media.sort(key=lambda c: (c.kind == "audio", -(c.score), -(c.height or 0),
                              -(c.tbr or 0)))
    media = media[:MAX_FORMATS]

    # ---- PASE DE IMAGEN (carrusel / album / pin) -------------------------
    # Mismo grafo, segundo modelo. No cuesta ni un fetch mas.
    img_scored: list[MediaCandidate] = []
    for rc in raws:
        ic = score_image_candidate(rc, page_url)
        if ic is not None:
            img_scored.append(ic)
    # El ANCLA: og:image es, por contrato de vista previa, un medio DEL POST.
    # Es el ejemplo etiquetado que la propia pagina nos regala.
    anchor = (metas.get("og:image") or metas.get("og:image:secure_url")
              or metas.get("twitter:image"))
    images = group_images(
        drop_video_posters(
            keep_authoritative([c for c in img_scored
                                if c.score >= MIN_IMAGE_SCORE], anchor=anchor),
            media))

    meta = _find_meta((dom_media, json_trees, metas), page_url)
    # Si no hubo miniatura en el meta, usa la imagen mejor puntuada como thumb.
    if not meta.get("thumbnail"):
        best_img = max(img_scored, key=lambda c: c.score, default=None)
        if best_img is not None:
            meta["thumbnail"] = best_img.url

    caption = find_caption(metas, json_trees)
    hashtags = extract_hashtags(caption)

    # Confianza: satura el puntaje del ganador y lo mezcla con su margen sobre
    # el 2do archivo DISTINTO. Alta cuando el ganador es claro y bien puntuado.
    conf = 0.0
    if media:
        top = media[0].score
        sat = min(1.0, top / 90.0)
        margin = 0.0
        if len(media) > 1:
            margin = min(1.0, max(0.0, (top - media[1].score) / 40.0))
        conf = round(0.7 * sat + 0.3 * (0.5 + 0.5 * margin), 3)

        # TECHO POR FALTA DE EVIDENCIA DE CALIDAD (aprendido EN VIVO con un
        # post real de Instagram). Un ganador SIN altura, SIN bitrate y SIN
        # extension/mimetype reproducible no es un archivo identificado: es
        # una corazonada. Antes eso puntuaba 0.757 y, como el umbral de corte
        # es 0.6, la cascada se APAGABA en la primera puerta y el motor nunca
        # llegaba al /embed/captioned/ donde estaba el carrusel de verdad.
        # Con el techo bajo, una corazonada ya no calla a las demas puertas:
        # es justo para lo que construimos la cascada.
        win = media[0]
        if not (win.height or win.tbr or (win.ext or "") in PLAYABLE_EXTS
                or (win.mime or "").startswith(("video", "audio"))):
            conf = min(conf, WEAK_CONFIDENCE)

    # Sin video pero CON fotos, la confianza la marca el pase de imagen.
    if not media and images:
        top_i = max(c.score for c in images)
        conf = round(min(1.0, top_i / 100.0) * 0.85, 3)
        # Mismo criterio: si de NINGUNA foto conocemos su tamano, no sabemos
        # si son las del post o adornos -> que la cascada siga buscando.
        if not any((c.width or c.height) for c in images):
            conf = min(conf, WEAK_CONFIDENCE)

    # QUE ES ESTO: la respuesta que Claude necesita para no adivinar mirando
    # el link. `video` manda si hay algo reproducible (el usuario que comparte
    # un reel quiere el reel); si no hay video, 2+ fotos = carrusel.
    if media:
        media_type = "video"
    elif len(images) >= 2:
        media_type = "carousel"
    elif images:
        media_type = "image"
    else:
        media_type = "none"

    ok = bool(media or images)
    reason = "" if ok else (
        "No encontre ninguna URL de medio en el HTML. Probablemente el "
        "contenido exige inicio de sesion, o la pagina carga el video por "
        "JavaScript (haria falta un navegador headless).")
    return ResolveResult(
        ok=ok, url=page_url, strategy=strategy or "html", confidence=conf,
        title=meta.get("title"), uploader=meta.get("uploader"),
        thumbnail=meta.get("thumbnail"), duration=meta.get("duration"),
        formats=media, reason=reason,
        images=images, media_type=media_type,
        full_caption=caption, hashtags=hashtags,
        diagnostics={
            "dom_media": len(dom_media), "json_trees": len(json_trees),
            "raw_candidates": len(raws), "scored_media": len(scored),
            "scored_images": len(img_scored), "images_kept": len(images),
            "media_type": media_type,
            "top_scores": [(c.url[:80], c.score, c.height) for c in media[:5]],
            "top_images": [(c.url[:80], c.score, c.width, c.height) for c in images[:5]],
        },
    )


# ==========================================================================
# 7. RED (baja el HTML; solo esto toca internet — testeado aparte)
# ==========================================================================

def _http_get(url: str, ua: str, *, max_bytes: int, referer: str | None = None) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        **({"Referer": referer} if referer else {}),
    })
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
        raw = r.read(max_bytes + 1)
        enc = (r.headers.get("Content-Encoding") or "").lower()
        final_url = r.geturl()
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
    if "gzip" in enc:
        try:
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        except Exception:
            pass
    elif "deflate" in enc:
        try:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            pass
    charset = "utf-8"
    m = re.search(rb'charset=["\']?([\w-]+)', raw[:4096], re.I)
    if m:
        try:
            charset = m.group(1).decode("ascii")
        except Exception:
            charset = "utf-8"
    return raw, final_url


def resolve(url: str, *, profile: Profile | None = None,
            fetch=_http_get, max_attempts: int = MAX_ATTEMPTS) -> ResolveResult:
    """Resuelve un enlace de verdad (con red). Construye la lista de intentos
    (URL original + reescrituras) x (User-Agents del perfil), la REORDENA segun
    la memoria por-host (lo que gano la ultima vez va primero), y se queda con
    el primer resultado de CONFIANZA suficiente (>=0.6) para no gastar red de
    mas. Si nada llega a esa confianza, devuelve el MEJOR resultado visto.

    `fetch` es inyectable para poder testear la cascada sin red."""
    profile = profile or profile_for(url)
    host = (urlparse(url).hostname or "").lower()

    # Intentos: primero la URL original, luego las reescrituras; cada una con
    # cada UA del perfil. Cada intento lleva su "clase" (para la memoria).
    rewrites: list[str] = []
    for rw in profile.rewrites:
        try:
            rewrites.extend(rw(url) or [])
        except Exception:
            pass

    # ORDEN ROUND-ROBIN POR PUERTA, no por objetivo. Es decir:
    #   original+googlebot, embed+googlebot, original+bingbot, embed+bingbot...
    # y NO: original x las 6 puertas, y recien despues el embed.
    # Motivo: cuando el original esta tras login-wall, la REESCRITURA (el
    # /embed/ de LinkedIn, el /embed/captioned/ de Instagram) suele ser mucho
    # mas determinante que insistir con otra puerta sobre la misma pagina. Asi
    # el tope de intentos se gasta en las combinaciones que mas informacion
    # nueva aportan.
    attempts: list[dict] = []
    seen = set()
    targets = [(False, url)] + [(True, t) for t in rewrites]
    for ua_rank, ua in enumerate(profile.user_agents):
        for tgt_rank, (is_rw, t) in enumerate(targets):
            k = (t, ua)
            if k in seen:
                continue
            seen.add(k)
            attempts.append({
                "target": t, "ua": ua, "is_rw": is_rw,
                "ua_kind": ua_kind(ua),
                "rank": (ua_rank, tgt_rank),
            })
    attempts.sort(key=lambda a: a["rank"])

    # Auto-sanacion: si recordamos que puerta gano en este host, va PRIMERO.
    # Guardamos la puerta CONCRETA (no solo "bot vs navegador"): si el dia que
    # LinkedIn cierre Googlebot resulta que Slackbot sigue abierto, la memoria
    # aprende sola a liderar con Slackbot sin que nadie toque el codigo.
    remembered = _HOST_MEMORY.get(host)
    if remembered:
        attempts.sort(key=lambda a: 0 if (a["is_rw"], a["ua_kind"]) == remembered else 1)

    if max_attempts and max_attempts > 0:
        attempts = attempts[:max_attempts]

    best: ResolveResult | None = None
    for a in attempts:
        try:
            raw, _final = fetch(a["target"], a["ua"], max_bytes=_MAX_HTML_BYTES, referer=url)
            html = raw.decode("utf-8", errors="replace")
        except Exception as e:
            if best is None:
                best = ResolveResult(False, url, "network-error", 0.0,
                                     reason=f"No pude bajar la pagina: {e}")
            continue
        strat = f"{profile.name}:{a['ua_kind']}:{'embed' if a['is_rw'] else 'original'}"
        res = resolve_html(html, a["target"], strategy=strat)
        res.url = url  # reportamos siempre la URL original que dio el usuario

        # MEZCLA DE PUERTAS: una puerta puede darnos el video y otra el caption
        # completo (Googlebot ve el SSR; el /embed/captioned/ trae el texto).
        # Nos quedamos con lo MEJOR de cada intento en vez de descartar el
        # anterior entero. Es gratis y sube la calidad del resultado final.
        if best is not None:
            if not res.full_caption and best.full_caption:
                res.full_caption = best.full_caption
                res.hashtags = best.hashtags
            if not res.thumbnail and best.thumbnail:
                res.thumbnail = best.thumbnail
            if not res.title and best.title:
                res.title = best.title
            if not res.images and best.images:
                res.images = best.images
                # Si este intento no encontro NADA propio, hereda tambien la
                # etiqueta; si encontro video, "video" manda y no se toca.
                if res.media_type in ("none", "image"):
                    res.media_type = best.media_type

        if best is None or res.confidence > best.confidence or (res.ok and not best.ok):
            best = res
        if res.ok and res.confidence >= 0.6:
            _remember_gate(_HOST_MEMORY, host,
                           (a["is_rw"], a["ua_kind"]))       # recordar el ganador
            break   # suficientemente bueno: no gastamos mas red (RAPIDO)
    return best if best else ResolveResult(False, url, "none", 0.0,
                                           reason="Sin intentos posibles.")


# HTML minimo con la estructura real de un <video data-sources> de LinkedIn.
_SELFTEST_HTML = (
    "<video data-sources='[{\"src\":"
    "\"https://dms.licdn.com/playlist/vid/v2/SELFTEST/mp4-720p-30fp/0/1?e=1&t=x\","
    "\"type\":\"video/mp4\",\"data-bitrate\":\"1000000\"}]'></video>"
)


# Carrusel minimo con la forma real de un sidecar de Instagram: DOS fotos, cada
# una en dos tamanos, mas un avatar de perfil (t51.2885-19) que NO debe colarse.
_SELFTEST_CAROUSEL_HTML = (
    '<meta property="og:description" content="Probando el carrusel #uno #dos">'
    '<img src="https://scontent.cdninstagram.com/v/t51.2885-19/'
    'avatar_150x150.jpg?_nc=1">'
    '<script type="application/json">'
    '{"edge_sidecar_to_children":{"edges":['
    '{"node":{"display_url":"https://scontent.cdninstagram.com/v/t51.2885-15/'
    's640x640/111111_222222_333333_n.jpg?_nc=a","display_resources":['
    '{"src":"https://scontent.cdninstagram.com/v/t51.2885-15/p1080x1080/'
    '111111_222222_333333_n.jpg?_nc=a","config_width":1080,"config_height":1080}]}},'
    '{"node":{"display_url":"https://scontent.cdninstagram.com/v/t51.2885-15/'
    'p1080x1080/444444_555555_666666_n.jpg?_nc=b","width":1080,"height":1350}}'
    ']}}</script>'
)


def selftest() -> bool:
    """Canario OFFLINE y determinista para el health_check: corre el motor
    contra un HTML minimo y verifica que EXTRAE la URL del CDN correctamente.
    Si esto falla, el motor tiene una regresion de CODIGO (no de red): es una
    alerta temprana independiente de que el sitio remoto haya cambiado."""
    try:
        r = resolve_html(_SELFTEST_HTML, "https://www.linkedin.com/posts/x-1-a")
        return bool(r.ok and r.formats and "dms.licdn.com" in r.formats[0].url
                    and r.formats[0].height == 720)
    except Exception:
        return False


def selftest_carousel() -> bool:
    """Canario OFFLINE del pase de IMAGEN: dos fotos distintas, en orden, sin
    el avatar, y fusionando los dos tamanos de la primera en una sola."""
    try:
        r = resolve_html(_SELFTEST_CAROUSEL_HTML,
                         "https://www.instagram.com/p/ABC123/")
        if r.media_type != "carousel" or len(r.images) != 2:
            return False
        if any("avatar" in c.url for c in r.images):
            return False
        # la 1a foto debe salir en su version GRANDE (p1080x1080), no la chica
        if "p1080x1080" not in r.images[0].url:
            return False
        return "111111" in r.images[0].url and "444444" in r.images[1].url
    except Exception:
        return False


# --------------------------------------------------------------------------
# LA FIRMA DEL ARCHIVO (verificar los bytes, no creerle a la cabecera)
# --------------------------------------------------------------------------
# El `Content-Type` es una DECLARACION del servidor, y los servidores mienten:
# cuando Facebook no te deja pasar devuelve una pagina de "inicia sesion" con
# codigo 200, y la guardabamos como .jpg. El job decia "1 foto guardada" y en
# la galeria quedaba un archivo roto — el sistema fallaba EN SILENCIO, que es
# la peor forma de fallar.
#
# Los primeros bytes de un archivo SON el formato: eso no lo puede falsear
# nadie sin dejar de ser una imagen. Es el mismo criterio que el ancla —
# apoyarse en un contrato que la plataforma no puede romper, en vez de en una
# declaracion que esperamos que sea cierta.
def sniff_image_mime(data: bytes) -> str | None:
    """Devuelve el mime REAL segun los magic bytes, o None si no es imagen."""
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    # AVIF / HEIC: contenedor ISO-BMFF, la marca va en bytes 4..8.
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"mif1", b"msf1"):
            return "image/heic"
    return None


# LA MISMA CASCADA DE PUERTAS, AHORA PARA EL MEDIO.
# Descubierto en vivo con Facebook: la pagina se gana con Googlebot y las
# fotos vienen como `/lookaside/CRAWLER/media/?media_id=...` — un endpoint
# que, por su propio nombre, sirve el archivo a los CRAWLERS. Lo pediamos con
# UA de navegador, o sea con una credencial distinta de la que abrio la
# puerta. La regla general, sin jerga de ningun sitio:
#
#     la puerta que abrio la pagina abre tambien el medio.
#
# Y como la verificacion de arriba nos dice si lo que llego es una imagen de
# verdad, la cascada tiene realimentacion: si no lo es, prueba la siguiente
# puerta. Igual que con el HTML, se recuerda la ganadora POR HOST de imagen,
# asi que en un carrusel solo la 1a foto paga el coste de buscar.
_MEDIA_GATES = (_BROWSER_UA, _FACEBOOKBOT_UA, _GOOGLEBOT_UA,
                _TWITTERBOT_UA, _WHATSAPP_UA)

_UA_BY_KIND = {kind: ua for ua, kind in _UA_KIND.items()}
_MEDIA_GATE_MEMORY: dict = {}


def _http_get_image(url: str, ua: str, *, max_bytes: int,
                    referer: str | None) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        **({"Referer": referer} if referer else {}),
    })
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
        return r.read(max_bytes)


def fetch_image_bytes(url: str, *, referer: str | None = None,
                      max_bytes: int = _MAX_IMAGE_BYTES,
                      prefer_gate: str | None = None,
                      fetch=_http_get_image) -> tuple[bytes, str] | None:
    """Baja los BYTES de una imagen del CDN y VERIFICA que sean una imagen.

    Solo tiene sentido desde el telefono (IP residencial que SI llega a
    fbcdn/licdn, que la red de Claude tiene vetada). Devuelve (bytes, mime)
    con el mime deducido de los propios bytes, o None si ninguna puerta
    devolvio una imagen de verdad. Nunca devuelve HTML disfrazado.

    `prefer_gate` acepta el nombre de una puerta ("googlebot") o la cadena de
    estrategia completa que devuelve resolve() ("facebook:googlebot:original")
    para liderar con la que ya abrio la pagina.

    El `Referer` importa: varios CDN devuelven 403 sin el del sitio de origen.
    """
    host = urlparse(url).netloc.lower()

    # Orden de puertas: la que pide el llamante, luego la que funciono la vez
    # pasada en este host, luego el resto. Un CDN sano acierta a la primera y
    # no gasta ni un fetch de mas.
    hinted = None
    if prefer_gate:
        kind = prefer_gate.split(":")[1] if ":" in prefer_gate else prefer_gate
        hinted = _UA_BY_KIND.get(kind)
    gates: list[str] = []
    for ua in (hinted, _MEDIA_GATE_MEMORY.get(host), *_MEDIA_GATES):
        if ua and ua not in gates:
            gates.append(ua)

    for ua in gates:
        try:
            data = fetch(url, ua, max_bytes=max_bytes, referer=referer)
        except Exception:
            continue
        if not data:
            continue
        mime = sniff_image_mime(data)
        if mime:
            _remember_gate(_MEDIA_GATE_MEMORY, host, ua)   # puerta ganadora
            return (data, mime)
        # Llego algo que NO es una imagen (login-wall, redireccion, error
        # HTML). No lo devolvemos: probamos la siguiente puerta.
    return None


def fetch_thumbnail_bytes(url: str, *, referer: str | None = None,
                          prefer_gate: str | None = None) -> tuple[bytes, str] | None:
    """Alias historico de `fetch_image_bytes` (lo usa preview_thumbnail)."""
    return fetch_image_bytes(url, referer=referer, prefer_gate=prefer_gate,
                             max_bytes=_MAX_THUMB_BYTES)
