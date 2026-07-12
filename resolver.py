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

Todo es STDLIB PURA (html.parser, json, re, urllib): en Termux/Android compilar
dependencias con C (lxml, etc.) es un via crucis, asi que NO usamos ninguna.

Este modulo NO depende de yt-dlp ni del server: es importable y testeable solo.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable, Iterator
from urllib.parse import urlparse, urljoin, parse_qs
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
    | [\w.-]*\.fbcdn\.net             # Facebook / Instagram video
    | [\w.-]*\.cdninstagram\.com      # Instagram
    | scontent[\w.-]*\.(?:fbcdn\.net|cdninstagram\.com)
    | video[\w.-]*\.twimg\.com        # Twitter / X
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

# Limites de red defensivos.
_HTTP_TIMEOUT = 15
_MAX_HTML_BYTES = 8 * 1024 * 1024      # 8 MB de HTML es mas que suficiente
_MAX_THUMB_BYTES = 6 * 1024 * 1024

_BROWSER_UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, "
               "like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36")
# Googlebot recibe el HTML pre-renderizado (SSR) en sitios como LinkedIn.
_GOOGLEBOT_UA = ("Mozilla/5.0 (compatible; Googlebot/2.1; "
                 "+http://www.google.com/bot.html)")

# Alias PUBLICO para que el server reutilice el mismo UA al bajar el archivo.
BROWSER_UA = _BROWSER_UA

# MEMORIA POR-HOST (auto-sanacion): recordamos que CLASE de intento
# (original vs reescritura, y que User-Agent) resolvio por ultima vez cada
# dominio, y la probamos PRIMERO la proxima vez. En regimen normal esto hace
# que casi siempre acertemos al primer fetch aunque el sitio cambie de forma.
_HOST_MEMORY: dict = {}


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


PROFILES = (
    Profile(
        name="linkedin",
        host_re=re.compile(r"(?i)(?:^|\.)linkedin\.com$"),
        user_agents=(_GOOGLEBOT_UA, _BROWSER_UA),
        rewrites=(_linkedin_rewrites,),
    ),
    Profile(
        name="facebook",
        host_re=re.compile(r"(?i)(?:^|\.)(facebook\.com|fb\.watch|fb\.com)$"),
        user_agents=(_BROWSER_UA, _GOOGLEBOT_UA),
    ),
    Profile(
        name="instagram",
        host_re=re.compile(r"(?i)(?:^|\.)instagram\.com$"),
        user_agents=(_BROWSER_UA, _GOOGLEBOT_UA),
    ),
    Profile(
        name="twitter",
        host_re=re.compile(r"(?i)(?:^|\.)(twitter\.com|x\.com)$"),
        user_agents=(_BROWSER_UA,),
    ),
)

_GENERIC_PROFILE = Profile(name="generic", host_re=re.compile(r".^"))


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
    reproducible sin ambiguedad."""
    url: str
    attrs: dict = field(default_factory=dict)


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
        self._capture_stack: list[str] = []   # 'script' | 'code'
        self._buf: list[str] = []

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
        elif tag == "meta":
            prop = (a.get("property") or a.get("name") or "").lower()
            if prop in ("og:video", "og:video:url", "og:video:secure_url",
                        "twitter:player:stream", "og:audio"):
                if a.get("content"):
                    self.dom_media.append(DomMedia(a["content"], {"from": prop}))
        if tag in ("script", "code"):
            self._capture_stack.append(tag)
            self._buf = []

    def handle_endtag(self, tag):
        if self._capture_stack and tag == self._capture_stack[-1]:
            self._capture_stack.pop()
            text = "".join(self._buf).strip()
            self._buf = []
            if text and ("{" in text or "[" in text):
                self.json_texts.append(text)

    def handle_data(self, data):
        if self._capture_stack:
            self._buf.append(data)


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


def iter_islands(html: str) -> tuple[list[DomMedia], list[object]]:
    """Cosecha del HTML: (media directa del DOM, lista de arboles JSON)."""
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
    return parser.dom_media, json_trees


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


_URLISH = re.compile(r"(?i)^(https?:)?//|^/dms/|^/playlist/")


def _looks_urlish(v: str) -> bool:
    if not isinstance(v, str) or len(v) < 8 or len(v) > 4000:
        return False
    if _URLISH.search(v):
        return True
    # URLs escapadas dentro de JSON de GraphQL suelen venir con \/\/.
    return "http" in v[:12] and "//" in v


def _walk(node, ancestors: tuple, out: list, depth: int = 0, parent: dict | None = None):
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
                    ))
            else:
                _walk(v, ancestors + (str(k),), out, depth + 1, node)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _walk(v, ancestors, out, depth + 1, parent)


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
    if is_video or (mime and mime.startswith("video")):
        kind = "video"
    elif is_audio or (mime and mime.startswith("audio")):
        kind = "audio"
    elif is_image:
        return MediaCandidate(full, s, "image", h, w, tbr, _ext_of(url), mime,
                              provenance=f"{'/'.join(rc.ancestors[-3:])}::{rc.key} [{','.join(feats)}]")
    else:
        # sin extension clara: si vive en CDN de medios y tiene dims, es video.
        if MEDIA_CDN_HOSTS.search(host) and (rc.siblings & DIMENSION_SIBLINGS):
            kind = "video"
        else:
            kind = "video"  # por defecto lo tratamos como video candidato

    prov = f"{'/'.join(rc.ancestors[-3:])}::{rc.key} [{'+'.join(feats)}]"
    return MediaCandidate(full, round(s, 1), kind, h, w, tbr, _ext_of(url), mime, prov)


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
            })
        return {
            "title": self.title,
            "uploader": self.uploader,
            "thumbnail": self.thumbnail,
            "duration": self.duration,
            "formats": fmts,
            "_cauce_resolver": True,
            "_cauce_strategy": self.strategy,
            "_cauce_confidence": self.confidence,
            "webpage_url": self.url,
        }


def _find_meta(dom_and_json, page_url) -> dict:
    """Saca titulo/uploader/miniatura/duracion recorriendo el JSON-LD y OG."""
    dom_media, json_trees = dom_and_json
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
    dom_media, json_trees = iter_islands(html)

    raws: list[RawCandidate] = []
    # (a) media directa del DOM
    for dm in dom_media:
        raws.append(RawCandidate(
            url=_normalize_escaped(dm.url), key=dm.attrs.get("from", "src"),
            ancestors=("<dom>",), siblings=frozenset(dm.attrs.keys()),
            sibling_obj=dm.attrs, depth=0, from_dom=True, dom_attrs=dm.attrs))
    # (b) todo lo que haya en los arboles JSON
    for tree in json_trees:
        _walk(tree, tuple(), raws)

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

    meta = _find_meta((dom_media, json_trees), page_url)
    # Si no hubo miniatura en el meta, usa la imagen mejor puntuada como thumb.
    if not meta.get("thumbnail"):
        imgs = sorted((c for c in scored if c.kind == "image"),
                      key=lambda c: -c.score)
        if imgs:
            meta["thumbnail"] = imgs[0].url

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

    ok = bool(media)
    reason = "" if ok else (
        "No encontre ninguna URL de medio en el HTML. Probablemente el "
        "contenido exige inicio de sesion, o la pagina carga el video por "
        "JavaScript (haria falta un navegador headless).")
    return ResolveResult(
        ok=ok, url=page_url, strategy=strategy or "html", confidence=conf,
        title=meta.get("title"), uploader=meta.get("uploader"),
        thumbnail=meta.get("thumbnail"), duration=meta.get("duration"),
        formats=media, reason=reason,
        diagnostics={
            "dom_media": len(dom_media), "json_trees": len(json_trees),
            "raw_candidates": len(raws), "scored_media": len(scored),
            "top_scores": [(c.url[:80], c.score, c.height) for c in media[:5]],
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
            fetch=_http_get) -> ResolveResult:
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

    attempts: list[dict] = []
    seen = set()
    for is_rw, group in ((False, [url]), (True, rewrites)):
        for t in group:
            for ua in profile.user_agents:
                k = (t, ua)
                if k in seen:
                    continue
                seen.add(k)
                attempts.append({
                    "target": t, "ua": ua, "is_rw": is_rw,
                    "ua_kind": "googlebot" if "Googlebot" in ua else "browser",
                })

    # Auto-sanacion: si recordamos que clase gano en este host, va PRIMERO.
    remembered = _HOST_MEMORY.get(host)
    if remembered:
        attempts.sort(key=lambda a: 0 if (a["is_rw"], a["ua_kind"]) == remembered else 1)

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
        if best is None or res.confidence > best.confidence or (res.ok and not best.ok):
            best = res
        if res.ok and res.confidence >= 0.6:
            _HOST_MEMORY[host] = (a["is_rw"], a["ua_kind"])   # recordar el ganador
            break   # suficientemente bueno: no gastamos mas red (RAPIDO)
    return best if best else ResolveResult(False, url, "none", 0.0,
                                           reason="Sin intentos posibles.")


# HTML minimo con la estructura real de un <video data-sources> de LinkedIn.
_SELFTEST_HTML = (
    "<video data-sources='[{\"src\":"
    "\"https://dms.licdn.com/playlist/vid/v2/SELFTEST/mp4-720p-30fp/0/1?e=1&t=x\","
    "\"type\":\"video/mp4\",\"data-bitrate\":\"1000000\"}]'></video>"
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


def fetch_thumbnail_bytes(url: str, *, referer: str | None = None) -> tuple[bytes, str] | None:
    """Baja los BYTES de una miniatura (para devolversela a Claude como imagen).
    Solo tiene sentido desde el telefono (IP residencial que SI llega al CDN).
    Devuelve (bytes, content_type) o None."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _BROWSER_UA,
            **({"Referer": referer} if referer else {}),
        })
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            data = r.read(_MAX_THUMB_BYTES)
            ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
        return (data, ctype) if data else None
    except Exception:
        return None
