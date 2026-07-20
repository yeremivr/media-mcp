"""
media-mcp: servidor MCP remoto que envuelve yt-dlp + API web para la PWA "Cauce".

Herramientas MCP para Claude (capa de razonamiento):
  - list_formats(url): metadata + formatos (rapido, no descarga)
  - download(url, format_id): dispara la descarga (en segundo plano) y avisa
  - download_status(job_id?): estado de una descarga (o de las ultimas)
  - resolve_media(url): resuelve con el MOTOR ESTRUCTURAL (grafo+scoring), util
       para LinkedIn/Facebook/sitios sin extractor, con diagnostico transparente
  - download_images(url, which): baja las FOTOS de un carrusel/album/pin
       ("all" o una seleccion tipo "1,3,5" / "2-4")
  - preview_thumbnail(url): baja la miniatura y la devuelve como IMAGEN (Claude
       la ve) — funciona porque el telefono SI llega al CDN que Claude tiene vetado
  - preview_image(url, index): igual, pero de una FOTO concreta del carrusel
  - health_check(): estado de yt-dlp + del resolver

RED DE SEGURIDAD ESTRUCTURAL (resolver.py): cuando yt-dlp no tiene extractor o
su extractor se rompe (el HTML del sitio cambio), el motor `resolver` baja el
HTML, lo convierte en un GRAFO de islas (DOM + JSON embebido), y PUNTUA cada URL
candidata por patron-de-valor (CDN, extension, firma) + contexto estructural
(claves hermanas width/height/bitrate, ancestros video/media). Robusto a que la
plataforma renombre o reanide sus claves. Ver resolver.py para el algoritmo.

API web para la PWA (capa 'ultimo kilometro', mismo origen que el servidor):
  - GET /                    -> la PWA Cauce (archivos estaticos de ./pwa)
  - GET /api/health          -> estado del servidor
  - GET /api/jobs            -> lista de descargas (con estado)
  - GET /api/file/{job_id}   -> entrega el archivo (util solo en modo remoto)
  - GET /icon-192.png, /icon-512.png -> iconos de la PWA (desde icons.py)

El endpoint MCP para Claude queda en  /mcp

--------------------------------------------------------------------------
ARQUITECTURA "Cauce Soberano" (Fase 1):
El muro anti-bot de YouTube tiene DOS causas: (1) falta de PO Token y (2) IP
de datacenter. La solucion de raiz es correr en IP residencial/movil (un
telefono con Termux). Este server es PORTABLE (Termux / PC / Render) y anade:

  * CASCADA AUTO-SANADORA de "player clients": prueba varios en orden,
    reintenta SOLO ante el muro anti-bot, APRENDE cual funciona y lidera con
    ese la proxima vez. En IP residencial la 1a suele bastar sin cookies.
  * DESCARGA EN SEGUNDO PLANO (fire-and-forget con espera breve): las
    descargas cortas responden al instante; las largas siguen en background.
  * NOTIFICACION NATIVA de Android al terminar (termux-notification), con
    tap-para-abrir el video. Fuera de Termux es un no-op (sigue portable).
  * Guarda en la carpeta Descargas COMPARTIDA del telefono -> aparece en la
    galeria y NO viaja por el tunel (plano de datos = local). Asi Cauce (la
    PWA) deja de ser necesaria: "se lo pides a Claude y aparece en tu galeria".
--------------------------------------------------------------------------
"""

import os
import re
import json
import uuid
import time
import base64
import shlex
import socket
import asyncio
import shutil
import threading
import subprocess
import urllib.request
from urllib.parse import urlparse
from pathlib import Path

import yt_dlp
from mcp.server.fastmcp import FastMCP
try:
    # Image permite que una tool devuelva una imagen INCRUSTADA (para que Claude
    # pueda "ver" la miniatura). Guardado por si cambia entre versiones de mcp.
    from mcp.server.fastmcp import Image
except Exception:
    Image = None
from starlette.responses import JSONResponse, FileResponse, Response

from auto_updater import run_forever as run_auto_updater

try:
    from icons import ICON_192_B64, ICON_512_B64
except Exception:
    ICON_192_B64 = ICON_512_B64 = ""

# Motor estructural de resolucion (resolver.py): RED DE SEGURIDAD para cuando
# yt-dlp no tiene extractor o su extractor se rompe (LinkedIn, Facebook y demas
# sitios sin soporte mantenido). Import GUARDADO: si fallara, el server arranca
# igual y solo se pierde el fallback; TODO lo de yt-dlp sigue intacto.
try:
    import resolver as _resolver
except Exception:
    _resolver = None

BASE_DIR = Path(__file__).resolve().parent


def _default_download_dir() -> Path:
    """Elige donde guardar. En Termux (telefono) guarda en la carpeta de
    Descargas COMPARTIDA: asi el video aparece en tu galeria, sobrevive al
    cierre de la app y NO viaja por el tunel (plano de datos = local)."""
    env = os.environ.get("DOWNLOAD_DIR")
    if env:
        return Path(env)
    # ~/storage/downloads existe si se corrio `termux-setup-storage`.
    termux_dl = Path(os.path.expanduser("~/storage/downloads"))
    if termux_dl.exists():
        return termux_dl / "Cauce"
    return BASE_DIR / "downloads"


DOWNLOAD_DIR = _default_download_dir()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
JOBS_INDEX = DOWNLOAD_DIR / "_jobs.json"
# Puertas aprendidas (que crawler abre cada sitio). Vive junto a las descargas
# y NO en la galeria visible: empieza por "_" igual que el indice de jobs.
GATES_MEMORY = DOWNLOAD_DIR / "_gates.json"
if _resolver is not None and hasattr(_resolver, "load_gate_memory"):
    _resolver.load_gate_memory(str(GATES_MEMORY))
PWA_DIR = BASE_DIR / "pwa"

PORT = int(os.environ.get("PORT", 8000))

# Cuantos segundos espera `download` a que termine antes de responder
# "sigue en segundo plano". Clips cortos terminan dentro de esta ventana.
DOWNLOAD_WAIT_SECONDS = float(os.environ.get("DOWNLOAD_WAIT_SECONDS", "8"))

# CORS: permite que la PWA lea el API aunque se sirva desde otro origen.
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}


def _find_deno() -> str | None:
    """Busca el ejecutable de Deno (motor JS que yt-dlp usa para YouTube)."""
    candidates = [
        shutil.which("deno"),
        str(BASE_DIR / ".deno" / "bin" / "deno"),
        os.path.expanduser("~/.deno/bin/deno"),
        os.path.expanduser("~/.local/bin/deno"),
        "/data/data/com.termux/files/usr/bin/deno",   # Termux (telefono)
        "/opt/render/.deno/bin/deno",                  # Render (datacenter)
        "/opt/render/project/src/.deno/bin/deno",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


DENO_PATH = _find_deno()
if DENO_PATH:
    os.environ["PATH"] = os.path.dirname(DENO_PATH) + os.pathsep + os.environ.get("PATH", "")


def _find_cookies_source() -> str | None:
    """Ubicacion del cookies.txt para YouTube. En IP residencial/movil casi
    NUNCA hace falta; se mantiene como ultimo recurso de la cascada."""
    candidates = [
        os.environ.get("YT_COOKIES_FILE"),
        "/etc/secrets/cookies.txt",
        str(BASE_DIR / "cookies.txt"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _prepare_cookies() -> str | None:
    """Devuelve un cookies.txt ESCRIBIBLE. yt-dlp reescribe el cookiefile al
    cerrar; si apunta a un Secret File de solo lectura (/etc/secrets) revienta
    con 'Read-only file system'. Por eso copiamos a una ruta escribible."""
    src = _find_cookies_source()
    if not src:
        return None
    try:
        dst = DOWNLOAD_DIR / "cookies_active.txt"
        shutil.copyfile(src, dst)
        return str(dst)
    except Exception:
        return None


COOKIES_FILE = _prepare_cookies()


try:
    import imageio_ffmpeg
    _IMAGEIO_FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    _IMAGEIO_FFMPEG = None


def _find_ffmpeg() -> str | None:
    """Ruta a ffmpeg (une video+audio de formatos DASH). En Termux:
    `pkg install ffmpeg`. Si no, imageio-ffmpeg trae un binario via pip."""
    return shutil.which("ffmpeg") or _IMAGEIO_FFMPEG


FFMPEG_PATH = _find_ffmpeg()


# ==========================================================================
# PO TOKEN (bgutil). OPCIONAL: si el proveedor bgutil esta corriendo, el plugin
# `bgutil-ytdlp-pot-provider` (instalado aparte) dota a yt-dlp de PO Tokens
# frescos para los clientes web/tv/mweb -> YouTube deja de limitar a 360p. Aqui
# solo DETECTAMOS si el proveedor escucha, para reportarlo en health. La
# extraccion no cambia: la cascada ya usa esos clientes. Si el proveedor no
# esta, todo sigue igual (4K por client-juggling). Deteccion best-effort.
# ==========================================================================

POT_PROVIDER_URL = os.environ.get("BGUTIL_POT_BASE_URL", "http://127.0.0.1:4416")


def _pot_provider_reachable() -> bool:
    """True si el proveedor de PO Token (bgutil) esta escuchando en su host:
    puerto. Best-effort: abre un socket con timeout corto y no lanza nunca."""
    try:
        u = urlparse(POT_PROVIDER_URL)
        host = u.hostname or "127.0.0.1"
        port = u.port or 4416
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except Exception:
        return False


# ==========================================================================
# NOTIFICACION NATIVA (Termux). Fuera de Termux -> no-op (portable).
# ==========================================================================

_TERMUX_NOTIFY = shutil.which("termux-notification")
# Ruta ABSOLUTA de termux-open: la accion de la notificacion corre con
# `dash -c` SIN $PATH, asi que no podemos depender del nombre suelto.
_TERMUX_OPEN = shutil.which("termux-open")
# Escaner de medios de Android (Termux:API): registra el archivo en el
# MediaStore para que aparezca en la Galeria/reproductor (ver _media_scan).
_TERMUX_MEDIA_SCAN = shutil.which("termux-media-scan")

# Cache PRIVADA para las miniaturas de las notificaciones. Va en el home de
# Termux (NO en el almacenamiento compartido) para no ensuciar la galeria.
_THUMB_CACHE = Path(os.path.expanduser("~/.cache/cauce_thumbs"))


def _notify(job_id: str, title: str, body: str, filepath: str | None = None,
            image_path: str | None = None, icon: str | None = None):
    """Lanza una notificacion de Android tipo 'tarjeta de medios'. Nunca debe
    tumbar la descarga: si falla o no hay Termux, simplemente no hace nada."""
    if not _TERMUX_NOTIFY:
        return
    try:
        nid = str(int(job_id[:6], 16) % 100000)   # id estable por job
        cmd = [_TERMUX_NOTIFY,
               "--id", nid,
               "--priority", "high",
               "--title", title,
               "--content", body]              # obligatorio: si no, espera stdin 3s
        if icon:
            cmd += ["--icon", icon]             # icono de estado (Material, snake_case)
        if image_path and os.path.exists(image_path):
            cmd += ["--image-path", str(image_path)]   # miniatura grande (BigPicture)
        if filepath and _TERMUX_OPEN:
            # `--action` se ejecuta con dash -c: ruta ABSOLUTA de termux-open
            # + path con comillas seguras (anti-inyeccion via el titulo).
            cmd += ["--action", f"{_TERMUX_OPEN} {shlex.quote(str(filepath))}"]
        subprocess.run(cmd, timeout=20, check=False,
                       stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _media_scan(filepath: str | None):
    """Avisa a Android (MediaStore) que hay un archivo NUEVO para que aparezca
    de inmediato en la Galeria y el reproductor de musica.

    yt-dlp escribe el archivo por la via de shell y eso NO dispara el
    MediaScanner de Android: el archivo existe en el disco pero el MediaStore
    (el indice del que leen Galeria/Musica) no lo conoce, asi que no se ve
    hasta reiniciar el telefono. `termux-media-scan` fuerza ese indexado.
    Fuera de Termux (o si falla) es un no-op: jamas debe tumbar la descarga."""
    if not _TERMUX_MEDIA_SCAN or not filepath:
        return
    try:
        subprocess.run([_TERMUX_MEDIA_SCAN, "-r", str(filepath)],
                       timeout=30, check=False,
                       stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _fetch_thumb(url: str | None, job_id: str) -> str | None:
    """Descarga la miniatura del video/cancion a la cache PRIVADA y devuelve
    su ruta local, para adjuntarla como imagen grande en la notificacion.
    Best-effort: si falla, devuelve None y la notificacion sale sin imagen."""
    if not url:
        return None
    try:
        _THUMB_CACHE.mkdir(parents=True, exist_ok=True)
        dst = _THUMB_CACHE / f"{job_id}.jpg"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
        if not data:
            return None
        dst.write_bytes(data)
        return str(dst)
    except Exception:
        return None


# ==========================================================================
# CASCADA AUTO-SANADORA DE YOUTUBE
# ==========================================================================

def _is_youtube(url: str) -> bool:
    u = (url or "").lower()
    return "youtube.com" in u or "youtu.be" in u or "youtube-nocookie.com" in u


# Estrategias de 'player_client' que probamos para YouTube. Distintos clientes
# exponen distintos formatos: cuando YouTube limita la sesion (SABR / falta de
# PO Token) el cliente 'web' suele quedarse solo con el 360p progresivo (itag
# 18), pero 'tv'/'ios'/'mweb' muchas veces AUN entregan los DASH de 1080p+. Por
# eso _extract_info recorre varios y se queda con la MAYOR resolucion (no con el
# primero que responda). 'tv' a veces cae en el experimento DRM "solo
# miniaturas" (yt-dlp #12563) -> _has_real_formats lo detecta y lo descarta.
# Si el proveedor bgutil (PO Token) esta activo, los clientes web/tv/mweb
# reciben tokens frescos automaticamente -> ya no los limitan a 360p.
YOUTUBE_STRATEGIES = [
    {"name": "default", "player_client": None,               "use_cookies": False},
    {"name": "tv",      "player_client": ["tv"],             "use_cookies": False},
    {"name": "ios",     "player_client": ["ios"],            "use_cookies": False},
    {"name": "mweb",    "player_client": ["mweb"],           "use_cookies": False},
    {"name": "web",     "player_client": ["web"],            "use_cookies": False},
    {"name": "web_tv_cookies", "player_client": ["web", "tv"], "use_cookies": True},
]

# Auto-aprendizaje: la ultima estrategia que gano se prueba PRIMERO la proxima
# vez -> en regimen normal, pocas llamadas aunque YouTube cambie.
_CHAMPION = {"name": None}

# Cache por-URL en la sesion: la estrategia ganadora de list_formats la reusa
# download (para pedir el mismo format_id al mismo cliente).
_URL_STRATEGY: dict = {}

# El telefono tiene recursos limitados: no dejar que descargas pesadas se
# peleen la CPU/red a la vez. Semaforo simple (2 en paralelo, configurable).
_DL_SEMAPHORE = threading.Semaphore(int(os.environ.get("MAX_PARALLEL_DL", "2")))

# Altura "suficientemente buena": si un cliente ya da >= esto, dejamos de
# probar mas clientes (evita latencia en el caso normal).
GOOD_ENOUGH_HEIGHT = int(os.environ.get("GOOD_ENOUGH_HEIGHT", "720"))


def _ordered_strategies() -> list:
    """Estrategias con la 'campeona' (ultima que gano) al frente."""
    champ = _CHAMPION["name"]
    if not champ:
        return list(YOUTUBE_STRATEGIES)
    lead = [s for s in YOUTUBE_STRATEGIES if s["name"] == champ]
    rest = [s for s in YOUTUBE_STRATEGIES if s["name"] != champ]
    return lead + rest


def _classify_error(e: Exception) -> str:
    """Clasifica el error de yt-dlp para decidir el siguiente paso:
      - 'hard'    : privado/borrado/geo -> NO reintentar (no tiene arreglo)
      - 'auth'    : edad/inicio de sesion -> hacen falta cookies
      - 'botwall' : muro anti-bot / SABR -> probar el siguiente cliente"""
    m = str(e).lower()
    if any(k in m for k in (
        "private video", "video unavailable", "has been removed", "removed by",
        "does not exist", "not available in your country", "blocked it in your country",
        "members-only", "join this channel", "this live event",
    )):
        return "hard"
    if any(k in m for k in (
        "confirm your age", "age-restricted", "sign in to confirm your age",
        "inappropriate for some users",
    )):
        return "auth"
    if any(k in m for k in (
        "confirm you're not a bot", "confirm you’re not a bot", "not a bot",
        "sign in to confirm", "only images are available", "forcing sabr",
        "failed to extract any player response", "unable to extract",
    )):
        return "botwall"
    return "botwall"   # desconocido: una oportunidad mas con otro cliente


def _base_opts(strategy: dict | None = None) -> dict:
    """Opciones base de yt-dlp, endurecidas para red movil inestable.
    strategy=None (TikTok/IG/etc.): cookies si existen, sin forzar cliente.
    Para YouTube se pasa la estrategia de la cascada."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,       # comparten un video dentro de playlist -> solo el video
        "retries": 5,             # red movil se corta -> reintentar
        "fragment_retries": 10,
        "continuedl": True,       # reanudar en vez de reempezar
        "socket_timeout": 30,
    }
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH

    use_cookies = True if strategy is None else strategy.get("use_cookies", False)
    if COOKIES_FILE and use_cookies:
        opts["cookiefile"] = COOKIES_FILE

    if strategy and strategy.get("player_client"):
        opts["extractor_args"] = {"youtube": {"player_client": strategy["player_client"]}}
    return opts


def _run_resilient(url: str, action, remember_for_url: bool = False):
    """Ejecuta action(opts).
    - YouTube: recorre la cascada (campeona/ cache-por-URL primero); reintenta
      SOLO ante muro anti-bot; respeta errores 'duros'.
    - Otras plataformas: un solo intento (con cookies si hay)."""
    if not _is_youtube(url):
        return action(_base_opts(None))

    cached = _URL_STRATEGY.get(url)
    if cached:
        strategies = [cached] + [s for s in _ordered_strategies() if s["name"] != cached["name"]]
    else:
        strategies = _ordered_strategies()

    last_err = None
    for st in strategies:
        try:
            result = action(_base_opts(st))
            _CHAMPION["name"] = st["name"]
            if remember_for_url:
                _URL_STRATEGY[url] = st
            return result
        except Exception as e:
            if _classify_error(e) == "hard":
                raise
            last_err = e
            continue
    raise last_err if last_err else RuntimeError("YouTube: ninguna estrategia funciono")


def _friendly_error(e: Exception) -> dict:
    """Traduce el error crudo de yt-dlp segun su clasificacion."""
    kind = _classify_error(e)
    if kind == "hard":
        return {"ok": False,
                "error": "Ese video no se puede descargar: es privado, fue borrado, "
                         "es solo para miembros o no esta disponible en tu region."}
    if kind == "auth":
        return {"ok": False, "needs_cookies": True,
                "error": "Este video tiene restriccion de edad y pide inicio de sesion. "
                         "Hacen falta cookies de una cuenta de YouTube."}
    if kind == "botwall":
        return {"ok": False, "needs_cookies": True,
                "error": ("YouTube pidio verificacion anti-bot y ninguna estrategia lo paso. "
                          "Desde IP residencial/movil es raro; desde un datacenter es esperado "
                          "(por eso Cauce corre mejor en tu telefono). TikTok, Instagram y "
                          "Facebook no tienen este problema.")}
    return {"ok": False, "error": f"No se pudo leer el link: {e}"}


mcp = FastMCP("media-mcp", host="0.0.0.0", port=PORT)


def _has_real_formats(info: dict) -> bool:
    """True si hay al menos un formato de video o audio REAL (no storyboard).
    YouTube a veces (experimento DRM en 'tv'/'web_safari') solo devuelve
    miniaturas; eso lo tratamos como fallo para probar el siguiente cliente."""
    for f in (info.get("formats") or []):
        if f.get("vcodec") not in (None, "none") or f.get("acodec") not in (None, "none"):
            return True
    return False


def _max_height(info: dict) -> int:
    """Mayor altura (resolucion) de video disponible en el resultado; 0 si no
    hay formatos de video. Sirve para comparar que cliente da mejor calidad."""
    heights = [f.get("height") or 0 for f in (info.get("formats") or [])
               if f.get("vcodec") not in (None, "none")]
    return max(heights) if heights else 0


def _extract_once(url: str, strategy: dict | None) -> dict:
    """Una extraccion (sin descargar) con una estrategia concreta. Si YouTube
    devuelve solo miniaturas (experimento DRM), lo tratamos como fallo para que
    el caller pruebe el siguiente cliente."""
    o = _base_opts(strategy)
    o["skip_download"] = True
    with yt_dlp.YoutubeDL(o) as ydl:
        info = ydl.extract_info(url, download=False)
    if _is_youtube(url) and not _has_real_formats(info):
        raise RuntimeError("only images are available (DRM/storyboard)")
    return info


# --------------------------------------------------------------------------
# MEMORIA CORTA DEL RESOLVER (no bajar dos veces la misma pagina)
# --------------------------------------------------------------------------
# `grab` con fotos resolvia el enlace DOS veces con segundos de diferencia:
# una en _extract_info (para decidir si es video o fotos) y otra dentro del
# worker de download_images. Son dos descargas COMPLETAS de la misma pagina,
# y la pagina es la parte cara: el scoring del grafo son microsegundos, la
# red son segundos. El cuello de botella nunca estuvo en el CPU.
#
# Por que un TTL corto y no un cache normal: las URLs de los CDN vienen
# FIRMADAS y caducan. Ese es justo el motivo por el que el worker re-resuelve
# — y hace bien. Pero caducan en HORAS, y aqui hablamos de reusar durante
# segundos. 90 s esta dos ordenes de magnitud por debajo del riesgo: elimina
# el viaje redundante sin tocar la garantia de frescura. Si el usuario mira
# las fotos y decide bajarlas cinco minutos despues, el cache ya expiro y se
# resuelve de nuevo, que es exactamente lo correcto.
_RESOLVE_TTL = 90.0
_RESOLVE_CACHE: dict = {}
_RESOLVE_CACHE_MAX = 32
_RESOLVE_LOCK = threading.Lock()


def _resolve_cached(url: str, *, fresh: bool = False):
    """`_resolver.resolve(url)` reusando el resultado si es de hace nada.

    `fresh=True` fuerza la resolucion real (lo usa la descarga de video, que
    necesita la URL firmada mas nueva posible porque la va a consumir entera).
    """
    now = time.time()
    if not fresh:
        with _RESOLVE_LOCK:
            hit = _RESOLVE_CACHE.get(url)
            if hit and (now - hit[0]) < _RESOLVE_TTL:
                return hit[1]
    res = _resolver.resolve(url)
    with _RESOLVE_LOCK:
        if len(_RESOLVE_CACHE) >= _RESOLVE_CACHE_MAX:
            oldest = min(_RESOLVE_CACHE, key=lambda k: _RESOLVE_CACHE[k][0])
            _RESOLVE_CACHE.pop(oldest, None)
        _RESOLVE_CACHE[url] = (now, res)
    return res


def _extract_info(url: str) -> dict:
    """Extrae metadata + formatos, con RED DE SEGURIDAD estructural.

    yt-dlp va PRIMERO (rapido y mantenido). Para enlaces que NO son YouTube, si
    yt-dlp no tiene extractor o su extractor se rompio (no devuelve formatos
    usables), caemos al `resolver` estructural (grafo + scoring del HTML).
    YouTube NO usa el resolver: sus URLs de googlevideo necesitan el descifrado
    de firma que solo yt-dlp sabe hacer, y ese camino ya tiene su cascada."""
    ytdlp_err = None
    try:
        info = _extract_info_ytdlp(url)
        if _is_youtube(url) or _has_real_formats(info):
            return info
    except Exception as e:
        if _classify_error(e) == "hard":
            raise
        ytdlp_err = e

    if not _is_youtube(url) and _resolver is not None:
        try:
            res = _resolve_cached(url)
        except Exception:
            res = None
        if res is not None and getattr(res, "ok", False):
            return res.to_info()

    if ytdlp_err is not None:
        raise ytdlp_err
    raise RuntimeError("No encontre formatos descargables para este enlace "
                       "(ni con yt-dlp ni con el resolver estructural).")


def _extract_info_ytdlp(url: str) -> dict:
    """Extrae metadata + formatos SOLO con yt-dlp (la logica original).

    - Otras plataformas (TikTok/IG/etc.): una sola extraccion.
    - YouTube: prueba varias estrategias de 'player_client' y SE QUEDA CON LA
      DE MAYOR RESOLUCION, no con la primera que devuelva algo. Motivo: cuando
      YouTube limita la sesion (SABR / falta de PO Token) algunos clientes solo
      entregan el 360p progresivo (itag 18) aunque el video tenga 1080p+. Al
      recorrer clientes (default/tv/ios/mweb/web) y comparar la altura maxima,
      recuperamos la calidad alta si ALGUN cliente aun la sirve. Corta apenas
      encuentra >= GOOD_ENOUGH_HEIGHT (rapido en el caso normal) y recuerda la
      estrategia ganadora para que la descarga use ese mismo cliente."""
    if not _is_youtube(url):
        return _extract_once(url, None)

    best, best_h, last_err = None, -1, None
    for st in _ordered_strategies():
        try:
            info = _extract_once(url, st)
        except Exception as e:
            if _classify_error(e) == "hard":
                raise
            last_err = e
            continue
        h = _max_height(info)
        if h > best_h:
            best, best_h = info, h
            _CHAMPION["name"] = st["name"]
            _URL_STRATEGY[url] = st
        if best_h >= GOOD_ENOUGH_HEIGHT:
            break
    if best is None:
        raise last_err if last_err else RuntimeError("YouTube: ninguna estrategia funciono")
    return best


def _load_jobs() -> list:
    with _JOBS_LOCK:
        return _read_jobs_unlocked()


# ---- Registro de jobs (con lock: varios hilos escriben el mismo JSON) ----

_JOBS_LOCK = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read_jobs_unlocked() -> list:
    if JOBS_INDEX.exists():
        try:
            return json.loads(JOBS_INDEX.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _upsert_job(job: dict):
    """Inserta o actualiza un job por job_id (read-modify-write bajo lock)."""
    with _JOBS_LOCK:
        jobs = [j for j in _read_jobs_unlocked() if j.get("job_id") != job["job_id"]]
        jobs.insert(0, job)
        jobs = jobs[:50]
        JOBS_INDEX.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_job(job_id: str) -> dict | None:
    with _JOBS_LOCK:
        for j in _read_jobs_unlocked():
            if j.get("job_id") == job_id:
                return j
    return None


# ---- Motor de descarga (corre en un hilo de fondo) ----

def _do_download_resolved(job_id: str, url: str, format_id: str):
    """Descarga para formatos del RESOLVER estructural (`cauce-...`).

    Re-resuelve el enlace EN EL MOMENTO: las URLs firmadas de los CDN
    (fbcdn/licdn) expiran en horas, asi que no se puede reusar la que se mostro
    en list_formats. Elige la calidad pedida y baja la URL DIRECTA con yt-dlp
    (que igual hace el merge/HLS/DASH). El titulo/miniatura/autor los pone el
    resolver, porque yt-dlp sobre una URL pelada de CDN no los conoce."""
    res = _resolve_cached(url, fresh=True)
    if not getattr(res, "ok", False):
        raise RuntimeError(res.reason or "El resolver no encontro el medio al descargar.")

    kind = "audio" if format_id.startswith("cauce-a") else "video"
    tail = format_id.rsplit("-", 1)[-1]
    want_h = int(tail) if (kind == "video" and tail.isdigit()) else None

    fmts = [f for f in res.formats if f.kind == kind] or list(res.formats)
    if not fmts:
        raise RuntimeError("El resolver no devolvio formatos al descargar.")
    if kind == "video" and want_h:
        chosen = min(fmts, key=lambda f: abs((f.height or 0) - want_h))
    else:
        chosen = fmts[0]                 # ya vienen ordenados: el mejor primero

    direct_url = chosen.url
    out_template = str(DOWNLOAD_DIR / f"{job_id}_%(title).80s.%(ext)s")
    ua = getattr(_resolver, "BROWSER_UA", "Mozilla/5.0")

    def action(opts):
        o = dict(opts)
        o["outtmpl"] = out_template
        o["merge_output_format"] = "mp4"
        o["windowsfilenames"] = True
        # Algunos CDN (fbcdn) exigen Referer del sitio original; lo pasamos.
        o["http_headers"] = {"User-Agent": ua, "Referer": url}
        with yt_dlp.YoutubeDL(o) as ydl:
            di = ydl.extract_info(direct_url, download=True)
            reqd = di.get("requested_downloads") or []
            filepath = reqd[-1].get("filepath") if reqd else ydl.prepare_filename(di)
        # Enriquecer con la metadata del resolver (yt-dlp sobre URL pelada no la tiene).
        di = dict(di or {})
        di["title"] = res.title or di.get("title")
        di["thumbnail"] = res.thumbnail or di.get("thumbnail")
        di["uploader"] = res.uploader or di.get("uploader")
        if chosen.height:
            di["height"] = chosen.height
        return di, filepath

    with _DL_SEMAPHORE:
        return action(_base_opts(None))


def _do_download(job_id: str, url: str, format_id: str):
    """Descarga real con yt-dlp, bajo el semaforo y via la cascada."""
    if format_id.startswith("cauce-") and _resolver is not None:
        return _do_download_resolved(job_id, url, format_id)
    out_template = str(DOWNLOAD_DIR / f"{job_id}_%(title).80s.%(ext)s")

    def action(opts):
        o = dict(opts)
        o["format"] = format_id
        o["outtmpl"] = out_template
        o["merge_output_format"] = "mp4"
        o["windowsfilenames"] = True   # nombres seguros para almacenamiento del telefono
        with yt_dlp.YoutubeDL(o) as ydl:
            info = ydl.extract_info(url, download=True)
            reqd = info.get("requested_downloads") or []
            filepath = reqd[-1].get("filepath") if reqd else ydl.prepare_filename(info)
            return info, filepath

    with _DL_SEMAPHORE:
        return _run_resilient(url, action)


def _download_worker(job: dict):
    """Ejecuta la descarga, actualiza el estado del job y notifica al terminar.
    Captura TODO: un fallo aqui jamas debe matar el hilo en silencio."""
    job_id = job["job_id"]
    try:
        info, filepath = _do_download(job_id, job["url"], job["format_id"])
        in_gallery = "storage/downloads" in str(filepath).replace("\\", "/")
        job.update({
            "status": "done",
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader") or info.get("channel"),
            "ext": Path(filepath).suffix.lstrip("."),
            "file_path": filepath,
            "updated_at": _now(),
        })
        _upsert_job(job)
        # Registrar en el MediaStore ANTES de notificar: asi, al tocar la
        # notificacion, el video ya es visible en la Galeria/reproductor.
        _media_scan(filepath)

        # Notificacion tipo "tarjeta de medios": titulo limpio + subtitulo
        # elegante (Autor · Calidad · destino) + miniatura grande + icono.
        media_title = job.get("title") or "Tu descarga"
        fid = job.get("format_id") or ""
        if fid.startswith("cauce-"):
            is_audio = fid.startswith("cauce-a")     # formato del resolver
        else:
            is_audio = "+" not in fid                # yt-dlp: video = "ID+bestaudio"
        bits = []
        uploader = info.get("uploader") or info.get("channel")
        if uploader:
            bits.append(uploader)
        if is_audio:
            bits.append("Audio")
        elif info.get("height"):
            bits.append(f"{info.get('height')}p")
        bits.append("Guardado en tu galería" if in_gallery else "Descarga lista")
        subtitle = "  ·  ".join(bits)

        thumb = _fetch_thumb(info.get("thumbnail"), job_id)
        icon = "music_note" if is_audio else "movie"
        _notify(job_id, media_title, subtitle, filepath=filepath,
                image_path=thumb, icon=icon)
    except Exception as e:
        fe = _friendly_error(e)
        job.update({"status": "error", "error": fe.get("error", str(e)), "updated_at": _now()})
        _upsert_job(job)
        _notify(job_id, "No se pudo descargar", (fe.get("error") or str(e))[:120],
                icon="error_outline")


# ==========================================================================
# DESCARGA DE FOTOS (carruseles / albumes / pines)
# ==========================================================================
# Una foto NO necesita yt-dlp: es un GET a un CDN. Bajarlas nosotros con
# urllib es mas rapido, no arranca subprocesos y no depende de que yt-dlp
# tenga extractor para ese sitio. Lo unico que importa de verdad es que el
# telefono (IP residencial) SI llega a fbcdn/licdn/pinimg.

_SAFE_NAME = re.compile(r"[^\w\-. ]+", re.U)

# Plataformas donde un mismo enlace puede traer varias fotos ademas del video.
_CAROUSEL_CAPABLE = re.compile(
    r"(?i)(instagram\.com|facebook\.com|fb\.watch|linkedin\.com|"
    r"pinterest\.[\w.]+|pin\.it|threads\.net|x\.com|twitter\.com)$")


def _safe_stem(title: str | None, fallback: str = "cauce") -> str:
    """Nombre de archivo seguro para el almacenamiento del telefono."""
    t = _SAFE_NAME.sub("", (title or "").strip())[:60].strip() or fallback
    return t.replace(" ", "_")


def parse_selection(which: str, n: int) -> list:
    """Traduce lo que dijo el humano a indices concretos (1-based).

    Acepta "all"/"todas"/"" (todas), "1,3,5", "2-4", "1-3,7", "ultima"/"last".
    Ignora lo que no exista en vez de reventar: si pide la 9 de un carrusel de
    5, se baja lo que si hay y se le avisa. Robusto a proposito: el input viene
    de una persona hablando, no de un formulario."""
    if n <= 0:
        return []
    w = (which or "").strip().lower()
    if w in ("", "all", "todas", "todos", "toda", "todo", "*", "everything"):
        return list(range(1, n + 1))
    if w in ("last", "ultima", "última", "la ultima", "la última"):
        return [n]
    if w in ("first", "primera", "la primera"):
        return [1]
    out: list = []
    for part in re.split(r"[,;/ ]+", w):
        if not part:
            continue
        m = re.match(r"^(\d+)\s*[-–a]\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            out.extend(range(min(a, b), max(a, b) + 1))
        elif part.isdigit():
            out.append(int(part))
    seen = set()
    picked = []
    for i in out:
        if 1 <= i <= n and i not in seen:
            seen.add(i)
            picked.append(i)
    return picked


def _img_format(mime: str | None) -> str:
    """Formato para incrustar la imagen. El mime ya viene VERIFICADO de los
    magic bytes, asi que aqui solo se traduce: antes se decia "jpeg" para todo
    lo que no fuera png y un webp llegaba mal etiquetado."""
    sub = (mime or "image/jpeg").split("/")[-1].lower()
    return sub if sub in ("png", "jpeg", "webp", "gif") else "jpeg"


def _download_images_worker(job: dict):
    """Baja las fotos elegidas, una a una, y notifica UNA vez al final.

    RE-RESUELVE el enlace en el momento: igual que con el video, las URLs de
    los CDN vienen FIRMADAS y con expiracion, asi que la que se mostro en
    `list_formats` puede estar muerta cuando el usuario decide bajarla."""
    job_id = job["job_id"]
    url = job["url"]
    try:
        res = _resolve_cached(url)
        imgs = list(getattr(res, "images", []) or [])
        if not imgs:
            raise RuntimeError(res.reason or
                               "No encontre fotos descargables en ese enlace.")

        picked = parse_selection(job.get("which") or "all", len(imgs))
        if not picked:
            raise RuntimeError(
                f"Ese enlace tiene {len(imgs)} foto(s) y la seleccion "
                f"'{job.get('which')}' no coincide con ninguna.")

        stem = _safe_stem(res.title, f"cauce_{job_id}")
        files, errors = [], []
        for i in picked:
            cand = imgs[i - 1]
            # `prefer_gate`: la puerta que abrio la pagina abre tambien la
            # foto (Facebook sirve las suyas por un endpoint de crawler).
            got = _resolver.fetch_image_bytes(
                cand.url, referer=url, prefer_gate=res.strategy)
            if not got:
                errors.append(i)
                continue
            data, ctype = got
            # `ctype` viene de los magic bytes, no de la cabecera: si llego
            # aqui es una imagen de verdad.
            ext = "png" if "png" in (ctype or "") else (
                "webp" if "webp" in (ctype or "") else "jpg")
            # El numero va en el nombre para que la galeria las ordene igual
            # que el carrusel original.
            dst = DOWNLOAD_DIR / f"{stem}_{i:02d}_{job_id}.{ext}"
            dst.write_bytes(data)
            files.append(str(dst))
            _media_scan(str(dst))

        if not files:
            raise RuntimeError("Encontre las fotos pero el CDN no me dejo "
                               "bajar ninguna (pueden haber expirado).")

        job.update({
            "status": "done",
            "title": res.title or f"{len(files)} foto(s)",
            "thumbnail": res.thumbnail,
            "uploader": res.uploader,
            "file_path": files[0],
            "files": files,
            "downloaded": len(files),
            "requested": len(picked),
            "failed": errors,
            "updated_at": _now(),
        })
        _upsert_job(job)

        bits = [f"{len(files)} de {len(imgs)} fotos"]
        if res.uploader:
            bits.insert(0, res.uploader)
        bits.append("Guardadas en tu galería")
        _notify(job_id, res.title or "Fotos descargadas", "  ·  ".join(bits),
                filepath=files[0], image_path=files[0], icon="photo_library")
    except Exception as e:
        job.update({"status": "error", "error": str(e), "updated_at": _now()})
        _upsert_job(job)
        _notify(job_id, "No se pudieron descargar las fotos", str(e)[:120],
                icon="error_outline")


def _curate_resolver(info: dict) -> dict:
    """Cura la salida del resolver estructural al MISMO esquema que list_formats
    (title/uploader/thumbnail/formats[{format_id,kind,label,filesize_mb}]) para
    que Claude y la PWA no noten diferencia. Los formatos del resolver son
    MUXED: se etiquetan por altura y NO llevan `+bestaudio`. El tamano se estima
    con tbr*duracion cuando ambos se conocen. Los format_id llevan el prefijo
    `cauce-v-<altura>` / `cauce-a-<idx>` para que download() sepa re-resolver."""
    duration = info.get("duration")
    vids: list = []
    auds: list = []
    seen_h = set()
    for f in info.get("formats", []) or []:
        muxed = f.get("_cauce_muxed", True)
        h = f.get("height") or 0
        tbr = f.get("tbr")
        size_mb = round(tbr * 1000 / 8 * duration / 1_000_000, 1) if (tbr and duration) else 0
        if muxed:
            if h in seen_h:
                continue
            seen_h.add(h)
            vids.append((h, {
                "format_id": f"cauce-v-{h}",
                "kind": "video",
                "label": (f"{h}p (mp4)" if h else "Video (mp4)"),
                "filesize_mb": size_mb,
            }))
        else:
            auds.append({
                "format_id": f"cauce-a-{len(auds)}",
                "kind": "audio",
                "label": f"Audio ({f.get('ext') or 'm4a'})",
                "filesize_mb": size_mb,
            })
    vids.sort(key=lambda t: -t[0])                 # mayor resolucion primero
    curated = [v for _, v in vids] + auds

    # FOTOS del post (carrusel/album/pin). Van NUMERADAS desde 1 y en el orden
    # en que el usuario las ve en la app: asi "bajame la 2 y la 5" es
    # inequivoco entre el humano, Claude y el servidor.
    imgs = info.get("_cauce_images") or []
    images = [{
        "index": im.get("index"),
        "label": (f"Foto {im.get('index')}"
                  + (f" · {im.get('width')}x{im.get('height')}"
                     if im.get("width") and im.get("height") else "")),
        "width": im.get("width"),
        "height": im.get("height"),
        "ext": im.get("ext") or "jpg",
    } for im in imgs]

    return {
        "ok": True,
        "media_type": info.get("_cauce_media_type") or ("video" if curated else "none"),
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "thumbnail": info.get("thumbnail"),
        "duration_seconds": duration,
        "formats": curated,
        "images": images,
        "image_count": len(images),
        "full_caption": info.get("description"),
        "hashtags": info.get("_cauce_hashtags") or [],
        "js_engine": "deno" if DENO_PATH else "none",
        "resolver": True,
        "confidence": info.get("_cauce_confidence"),
        "strategy": info.get("_cauce_strategy"),
    }


# --------------------------------------------------------------------------
# Herramientas MCP (capa de razonamiento: las llama Claude)
# --------------------------------------------------------------------------

@mcp.tool()
def list_formats(url: str) -> dict:
    """
    Inspecciona un enlace y dice QUE ES y como bajarlo (video, foto o carrusel).

    USA ESTA HERRAMIENTA SIEMPRE que el usuario comparta un ENLACE (URL) de
    YouTube, TikTok, Instagram, Facebook, LinkedIn, Pinterest, Twitter/X, etc.
    y quiera verlo, guardarlo o descargarlo. Es el PRIMER paso: no adivines por
    la pinta del link si es video o fotos — esta herramienta te lo dice.

    Campos que te importan para decidir:
      * `media_type`: "video" | "carousel" | "image" | "none".
          - "video"    -> elige un format_id de `formats` y llama `download`.
          - "carousel" -> hay VARIAS fotos: mira `images` (numeradas 1..N, en
                          el mismo orden en que el usuario las ve) y llama
                          `download_images`. PREGUNTALE al usuario si quiere
                          todas o solo algunas antes de bajar.
          - "image"    -> una sola foto: `download_images(url, "all")`.
      * `full_caption`: el TEXTO COMPLETO del post (no solo el titulo generico
        tipo "Video by usuario"). Usalo para saber DE QUE trata el contenido.
      * `hashtags`: los hashtags del post, ya extraidos.
      * `formats`: calidades de video/audio con su `format_id` para `download`.
      * `images`: fotos con su `index` (1-based) para `download_images`.

    Para YouTube elige el cliente que da la MAYOR resolucion disponible.

    Args:
        url: el link compartido por el usuario.
    """
    try:
        info = _extract_info(url)
    except Exception as e:
        return _friendly_error(e)

    # Si el resultado vino del resolver estructural, se cura aparte: sus
    # formatos ya son MUXED (video+audio juntos) -> NO se les agrega +bestaudio.
    if info.get("_cauce_resolver"):
        return _curate_resolver(info)

    raw_formats = info.get("formats", []) or []
    curated = []

    audio_only = [f for f in raw_formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
    if audio_only:
        best_audio = max(audio_only, key=lambda f: f.get("abr") or 0)
        curated.append({
            "format_id": best_audio["format_id"],
            "kind": "audio",
            "label": f"Audio {int(best_audio.get('abr') or 0)}kbps ({best_audio.get('ext')})",
            "filesize_mb": round((best_audio.get("filesize") or best_audio.get("filesize_approx") or 0) / 1_000_000, 1),
        })

    video_formats = [f for f in raw_formats if f.get("vcodec") != "none"]
    seen_res = set()
    for f in sorted(video_formats, key=lambda f: f.get("height") or 0, reverse=True):
        height = f.get("height")
        if not height or height in seen_res:
            continue
        seen_res.add(height)
        curated.append({
            # Selector que une el video con el mejor audio (los DASH vienen
            # con las pistas separadas); asi la descarga sale CON sonido.
            "format_id": f"{f['format_id']}+bestaudio/best",
            "kind": "video",
            "label": f"{height}p (mp4)",
            "filesize_mb": round((f.get("filesize") or f.get("filesize_approx") or 0) / 1_000_000, 1),
        })
        if len(seen_res) >= 4:
            break

    # Caption completo tambien por la via de yt-dlp: su `description` ya trae
    # el texto del post en Facebook/TikTok/YouTube. Es informacion que estaba
    # ahi y no exponiamos, y le ahorra a Claude tener que adivinar el tema.
    # Se limpia con el MISMO criterio que el resolver: yt-dlp devuelve tal cual
    # lo que ponga la pagina, incluidos los esloganes genericos de plataforma
    # (Pinterest: "Scopri (e salva) i tuoi Pin su Pinterest.", identico en cada
    # pin y sin ninguna informacion del contenido).
    caption = (_resolver.clean_caption(info.get("description"))
               if _resolver else (info.get("description") or None))
    hashtags = _resolver.extract_hashtags(caption) if _resolver else []

    # LIMITE HONESTO: si yt-dlp resolvio el enlace, NO sabemos si el post tenia
    # ademas un carrusel de fotos — yt-dlp devuelve el primer elemento y calla
    # el resto. Consultar al resolver aqui costaria un fetch extra en CADA
    # link, y el caso comun (un video suelto) no lo necesita. En vez de pagar
    # ese peaje siempre, le decimos a Claude cuando VALE LA PENA preguntar.
    hint = None
    if _CAROUSEL_CAPABLE.search(urlparse(url).hostname or ""):
        hint = ("Este sitio puede tener carruseles de fotos y yt-dlp solo ve el "
                "primer elemento. Si el usuario menciona fotos, imagenes, "
                "album o carrusel, llama a resolve_media(url) o directamente a "
                "download_images(url) — usan el motor estructural y SI las ven.")

    return {
        "ok": True,
        "media_type": "video" if curated else "none",
        "carousel_hint": hint,
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "thumbnail": info.get("thumbnail"),
        "duration_seconds": info.get("duration"),
        "formats": curated,
        "images": [],
        "image_count": 0,
        "full_caption": caption,
        "hashtags": hashtags,
        "js_engine": "deno" if DENO_PATH else "none",
    }


@mcp.tool()
def grab(url: str, quality: str = "best", which: str = "all") -> dict:
    """
    UN SOLO PASO: mira el enlace, decide QUE ES y lo descarga. Sin preguntar.

    ESTA ES LA HERRAMIENTA POR DEFECTO cuando el usuario comparte un enlace y
    NO pide nada en concreto — por ejemplo si solo pega el link, o dice
    "bajame esto", "guardalo", "descarga". Usala directamente: NO llames antes
    a list_formats. Este es el caso comun y esta pensado para que el usuario
    pueda pegar el link y cerrar la app.

    Que hace por dentro:
      * Si el enlace es un VIDEO   -> lo baja en la MAXIMA calidad disponible.
      * Si es un CARRUSEL o FOTO   -> baja TODAS las imagenes.
      * Todo en segundo plano, a la galeria del telefono, con notificacion.

    Cuando NO usar esta herramienta (usa las especificas):
      * El usuario pide una calidad concreta -> list_formats + download.
      * El usuario quiere elegir fotos sueltas -> list_formats + download_images.
      * El usuario solo quiere SABER que es, sin bajar nada -> list_formats.

    Args:
        url: el enlace compartido.
        quality: "best" (por defecto) o "worst" para la mas ligera. Solo aplica
                 a video; se ignora en fotos.
        which: que fotos bajar si resulta ser un carrusel. "all" por defecto.
    """
    try:
        info = _extract_info(url)
    except Exception as e:
        return _friendly_error(e)

    curated = (_curate_resolver(info) if info.get("_cauce_resolver")
               else None)

    # --- Caso FOTOS: el resolver ya dijo que hay carrusel/imagen ---
    if curated and curated.get("image_count"):
        res = download_images(url, which)
        res["media_type"] = curated.get("media_type")
        res["title"] = res.get("title") or curated.get("title")
        res["full_caption"] = curated.get("full_caption")
        res["uploader"] = curated.get("uploader")
        return res

    # --- Caso VIDEO ---
    listing = curated or list_formats(url)
    fmts = [f for f in (listing.get("formats") or []) if f.get("kind") == "video"]
    if not fmts:
        fmts = [f for f in (listing.get("formats") or []) if f.get("kind") == "audio"]
    if not fmts:
        # Ni video ni fotos: puede ser un post de solo texto o pedir login.
        return {"ok": False, "media_type": listing.get("media_type", "none"),
                "error": "No encontre nada descargable en ese enlace "
                         "(puede ser solo texto, o exigir inicio de sesion).",
                "title": listing.get("title"),
                "full_caption": listing.get("full_caption")}

    # `formats` viene ordenado de mayor a menor calidad desde list_formats.
    chosen = fmts[-1] if str(quality).lower() in ("worst", "peor", "baja") else fmts[0]
    res = download(url, chosen["format_id"])
    res["media_type"] = listing.get("media_type")
    res["quality"] = chosen.get("label")
    res["title"] = res.get("title") or listing.get("title")
    res["full_caption"] = listing.get("full_caption")
    res["uploader"] = listing.get("uploader")
    return res


@mcp.tool()
def download(url: str, format_id: str) -> dict:
    """
    Descarga el link en el format_id elegido (viene de `list_formats`).

    USA ESTA HERRAMIENTA cuando el usuario quiera efectivamente descargar o
    guardar el video/audio de un enlace. Normalmente se llama DESPUES de
    `list_formats` (para tener el format_id de la calidad deseada, p.ej. la mas
    alta). La descarga corre en SEGUNDO PLANO en el telefono: si termina rapido
    responde "listo"; si tarda, sigue en background y al terminar salta una
    notificacion nativa con la miniatura y se guarda en la galeria.

    Args:
        url: el link original.
        format_id: el format_id devuelto por list_formats (ej. la mejor calidad).
    """
    job_id = str(uuid.uuid4())[:8]
    job = {
        "job_id": job_id,
        "url": url,
        "format_id": format_id,
        "status": "downloading",
        "title": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _upsert_job(job)

    t = threading.Thread(target=_download_worker, args=(job,), daemon=True)
    t.start()
    t.join(timeout=DOWNLOAD_WAIT_SECONDS)   # espera breve: clips cortos terminan aqui

    fresh = _get_job(job_id) or job
    status = fresh.get("status")

    if status == "done":
        return {"ok": True, "job_id": job_id, "status": "done",
                "title": fresh.get("title"),
                "message": "Descarga lista y guardada en tu telefono."}
    if status == "error":
        return {"ok": False, "job_id": job_id, "status": "error",
                "error": fresh.get("error")}
    return {"ok": True, "job_id": job_id, "status": "downloading",
            "message": "Bajandolo en segundo plano. Te llega una notificacion al terminar."}


@mcp.tool()
def download_images(url: str, which: str = "all") -> dict:
    """
    Descarga las FOTOS de un post: carrusel de Instagram, album de Facebook,
    pin de Pinterest, documento de LinkedIn, o una imagen suelta.

    USALA cuando `list_formats` haya devuelto `media_type` = "carousel" o
    "image" (o cuando el usuario pida "bajame las fotos" de un enlace).

    FLUJO RECOMENDADO con el usuario:
      1. `list_formats(url)` -> te dice cuantas fotos hay y en que orden.
      2. Si son varias, MUESTRASELAS y preguntale si quiere todas o cuales.
      3. Llama a esta herramienta con su respuesta.
    Los numeros son los MISMOS que ve el usuario en la app (1 = la primera).

    Las fotos se guardan numeradas en la galeria del telefono, en el orden del
    carrusel, y al terminar salta una notificacion.

    Args:
        url: el link del post.
        which: "all" para todas (por defecto), o una seleccion: "1,3,5",
               "2-4", "1-3,7", "primera", "ultima".
    """
    if _resolver is None:
        return {"ok": False, "error": "El motor resolver no esta disponible en este server."}

    job_id = str(uuid.uuid4())[:8]
    job = {
        "job_id": job_id,
        "url": url,
        "kind": "images",
        "which": which or "all",
        "format_id": f"cauce-img:{which or 'all'}",
        "status": "downloading",
        "title": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _upsert_job(job)

    t = threading.Thread(target=_download_images_worker, args=(job,), daemon=True)
    t.start()
    t.join(timeout=DOWNLOAD_WAIT_SECONDS)

    fresh = _get_job(job_id) or job
    status = fresh.get("status")
    if status == "done":
        return {"ok": True, "job_id": job_id, "status": "done",
                "downloaded": fresh.get("downloaded"),
                "requested": fresh.get("requested"),
                "title": fresh.get("title"),
                "message": f"Listo: {fresh.get('downloaded')} foto(s) guardadas en tu galeria."}
    if status == "error":
        return {"ok": False, "job_id": job_id, "status": "error",
                "error": fresh.get("error")}
    return {"ok": True, "job_id": job_id, "status": "downloading",
            "message": "Bajando las fotos en segundo plano. Te llega una notificacion al terminar."}


@mcp.tool()
def download_status(job_id: str = "") -> dict:
    """
    Consulta el estado de una descarga por job_id, o las ultimas si no se pasa.

    Args:
        job_id: (opcional) el id devuelto por download().
    """
    if job_id:
        j = _get_job(job_id)
        if not j:
            return {"ok": False, "error": "job no encontrado"}
        return {"ok": True, "job": {k: v for k, v in j.items() if k not in ("file_path", "files")}}
    jobs = _load_jobs()
    return {"ok": True, "jobs": [{k: v for k, v in j.items() if k not in ("file_path", "files")} for j in jobs[:10]]}


@mcp.tool()
def resolve_media(url: str) -> dict:
    """
    Resuelve un enlace con el MOTOR ESTRUCTURAL (grafo + scoring del HTML), sin
    depender del extractor de yt-dlp. Util para LinkedIn, Facebook y sitios sin
    extractor, o para ver POR QUE se eligio cada URL (diagnostico transparente).

    Devuelve titulo, autor, miniatura, la lista de formatos encontrados (con su
    puntaje y de que parte del HTML salio cada uno) y una confianza 0..1. Los
    `format_id` que devuelve se pueden pasar directo a `download`.

    Args:
        url: el enlace a inspeccionar (post/reel/video).
    """
    if _resolver is None:
        return {"ok": False, "error": "El motor resolver no esta disponible en este server."}
    try:
        res = _resolve_cached(url)
    except Exception as e:
        return {"ok": False, "error": f"Fallo el resolver: {e}"}
    return {
        "ok": res.ok,
        "media_type": getattr(res, "media_type", "none"),
        "title": res.title,
        "uploader": res.uploader,
        "thumbnail": res.thumbnail,
        "duration_seconds": res.duration,
        "confidence": res.confidence,
        "strategy": res.strategy,
        "full_caption": getattr(res, "full_caption", None),
        "hashtags": list(getattr(res, "hashtags", []) or []),
        "images": [{
            "index": i + 1,
            "width": c.width,
            "height": c.height,
            "score": c.score,
            "url_preview": c.url[:90],
            "from": c.provenance,
            "path": "/".join(getattr(c, "path", ()) or ()),
        } for i, c in enumerate(getattr(res, "images", []) or [])],
        "formats": [{
            "format_id": (f"cauce-a-0" if f.kind == "audio" else f"cauce-v-{f.height or 0}"),
            "kind": f.kind,
            "height": f.height,
            "score": f.score,
            "url_preview": f.url[:90],
            "from": f.provenance,
            "path": "/".join(getattr(f, "path", ()) or ()),
        } for f in res.formats],
        "reason": res.reason,
        "diagnostics": res.diagnostics,
    }


@mcp.tool()
def preview_thumbnail(url: str):
    """
    Baja la MINIATURA de un enlace y la devuelve como IMAGEN incrustada para que
    Claude pueda VERLA y analizarla (de que trata, colores, texto en pantalla).

    Funciona porque este server corre en tu telefono (IP residencial) y SI puede
    llegar al CDN de Facebook/LinkedIn/etc., que la red de Claude tiene vetado.
    Resuelve el problema de "solo me llega el link de la miniatura, no la imagen".

    Args:
        url: el enlace del video/reel/post.
    """
    thumb = None
    strategy = None
    try:
        info = _extract_info(url)
        thumb = info.get("thumbnail")
    except Exception:
        pass
    if not thumb and _resolver is not None:
        try:
            res = _resolve_cached(url)
            thumb, strategy = res.thumbnail, res.strategy
        except Exception:
            pass
    if not thumb:
        return {"ok": False, "error": "No encontre miniatura para ese enlace."}
    if _resolver is None:
        return {"ok": True, "thumbnail_url": thumb,
                "note": "No puedo bajar la imagen en este server; te paso la URL."}
    got = _resolver.fetch_thumbnail_bytes(thumb, referer=url, prefer_gate=strategy)
    if not got:
        return {"ok": False, "thumbnail_url": thumb,
                "error": "Tengo la URL de la miniatura pero lo que devuelve el "
                         "CDN no es una imagen (login-wall o enlace expirado)."}
    data, ctype = got
    if Image is None:
        return {"ok": True, "thumbnail_url": thumb,
                "note": "Esta version no puede incrustar imagenes; te paso la URL."}
    return Image(data=data, format=_img_format(ctype))


@mcp.tool()
def preview_image(url: str, index: int = 1):
    """
    Devuelve UNA foto concreta de un carrusel como IMAGEN incrustada, para que
    Claude pueda VERLA y describirsela al usuario antes de bajar nada.

    Es lo que permite decirle al usuario "la 3 es el grafico de barras, la 5 es
    la conclusion" en vez de ofrecerle una lista de numeros a ciegas. Llamala
    con distintos `index` (1..N) si necesitas ver varias.

    Args:
        url: el link del post.
        index: numero de la foto, empezando en 1 (el mismo que ve el usuario).
    """
    if _resolver is None:
        return {"ok": False, "error": "El motor resolver no esta disponible en este server."}
    try:
        res = _resolve_cached(url)
    except Exception as e:
        return {"ok": False, "error": f"Fallo el resolver: {e}"}
    imgs = list(getattr(res, "images", []) or [])
    if not imgs:
        return {"ok": False, "error": "No encontre fotos en ese enlace.",
                "reason": res.reason}
    if not (1 <= index <= len(imgs)):
        return {"ok": False,
                "error": f"Ese post tiene {len(imgs)} foto(s); pediste la {index}."}
    got = _resolver.fetch_image_bytes(imgs[index - 1].url, referer=url,
                                      prefer_gate=res.strategy)
    if not got:
        return {"ok": False,
                "error": "Lo que devuelve el CDN para esa foto no es una "
                         "imagen (login-wall o enlace expirado)."}
    data, ctype = got
    if Image is None:
        return {"ok": True, "image_url": imgs[index - 1].url,
                "note": "Esta version no puede incrustar imagenes; te paso la URL."}
    return Image(data=data, format=_img_format(ctype))


@mcp.tool()
def health_check() -> dict:
    """Prueba rapida de que yt-dlp sigue funcionando (via la cascada)."""
    try:
        info = _extract_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        return {
            "ok": True,
            "yt_dlp_version": yt_dlp.version.__version__,
            "js_engine": "deno" if DENO_PATH else "none",
            "cookies": bool(COOKIES_FILE),
            "ffmpeg": bool(FFMPEG_PATH),
            "notifications": bool(_TERMUX_NOTIFY),
            "media_scan": bool(_TERMUX_MEDIA_SCAN),
            "po_token": _pot_provider_reachable(),
            "resolver": _resolver is not None,
            "resolver_ok": bool(_resolver and _resolver.selftest()),
            "resolver_carousel_ok": bool(_resolver and _resolver.selftest_carousel()),
            "youtube_strategy": _CHAMPION["name"],
            "max_height": _max_height(info),
            "download_dir": str(DOWNLOAD_DIR),
            "title": info.get("title"),
        }
    except Exception as e:
        return {"ok": False, "js_engine": "deno" if DENO_PATH else "none",
                "cookies": bool(COOKIES_FILE), "ffmpeg": bool(FFMPEG_PATH),
                "notifications": bool(_TERMUX_NOTIFY),
                "media_scan": bool(_TERMUX_MEDIA_SCAN),
                "po_token": _pot_provider_reachable(),
                "resolver": _resolver is not None,
                "resolver_ok": bool(_resolver and _resolver.selftest()),
                "resolver_carousel_ok": bool(_resolver and _resolver.selftest_carousel()),
                "youtube_strategy": _CHAMPION["name"], **_friendly_error(e)}


# --------------------------------------------------------------------------
# API web (capa 'ultimo kilometro': la consume la PWA Cauce, ahora opcional)
# --------------------------------------------------------------------------

@mcp.custom_route("/api/health", methods=["GET"])
async def api_health(request):
    return JSONResponse({
        "ok": True,
        "yt_dlp_version": yt_dlp.version.__version__,
        "js_engine": "deno" if DENO_PATH else "none",
        "cookies": bool(COOKIES_FILE),
        "ffmpeg": bool(FFMPEG_PATH),
        "notifications": bool(_TERMUX_NOTIFY),
        "media_scan": bool(_TERMUX_MEDIA_SCAN),
        "po_token": _pot_provider_reachable(),
        "resolver": _resolver is not None,
        "resolver_ok": bool(_resolver and _resolver.selftest()),
        "resolver_carousel_ok": bool(_resolver and _resolver.selftest_carousel()),
        "youtube_strategy": _CHAMPION["name"],
    }, headers=CORS_HEADERS)


@mcp.custom_route("/api/jobs", methods=["GET"])
async def api_jobs(request):
    jobs = _load_jobs()
    # No exponemos file_path (ruta interna del servidor) al cliente.
    safe = [{k: v for k, v in j.items() if k not in ("file_path", "files")} for j in jobs]
    return JSONResponse({"jobs": safe}, headers=CORS_HEADERS)


@mcp.custom_route("/api/file/{job_id}", methods=["GET"])
async def api_file(request):
    job_id = request.path_params["job_id"]
    for j in _load_jobs():
        if j.get("job_id") == job_id:
            fp = j.get("file_path")
            if fp and os.path.exists(fp):
                return FileResponse(fp, filename=os.path.basename(fp), headers=CORS_HEADERS)
            return JSONResponse(
                {"ok": False, "error": "El archivo ya no esta disponible. Vuelve a pedir la descarga."},
                status_code=410, headers=CORS_HEADERS,
            )
    return JSONResponse({"ok": False, "error": "job no encontrado"}, status_code=404, headers=CORS_HEADERS)


@mcp.custom_route("/icon-192.png", methods=["GET"])
async def icon_192(request):
    return Response(base64.b64decode(ICON_192_B64), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@mcp.custom_route("/icon-512.png", methods=["GET"])
async def icon_512(request):
    return Response(base64.b64decode(ICON_512_B64), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


if __name__ == "__main__":
    import uvicorn
    from starlette.routing import Mount
    from starlette.staticfiles import StaticFiles

    # Auto-updater de yt-dlp en segundo plano (no bloquea al servidor).
    threading.Thread(target=lambda: asyncio.run(run_auto_updater()), daemon=True).start()

    # App ASGI: incluye /mcp (para Claude) y /api/* + /icon-*.png (custom_route).
    app = mcp.streamable_http_app()

    # Sirve la PWA Cauce en la raiz (opcional: historial/biblioteca). Se agrega
    # al final para que /mcp, /api/* e /icon-*.png tengan prioridad.
    if PWA_DIR.exists():
        app.router.routes.append(
            Mount("/", app=StaticFiles(directory=str(PWA_DIR), html=True), name="pwa")
        )

    uvicorn.run(app, host="0.0.0.0", port=PORT)
