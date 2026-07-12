"""
media-mcp: servidor MCP remoto que envuelve yt-dlp + API web para la PWA "Cauce".

Herramientas MCP para Claude (capa de razonamiento):
  - list_formats(url): metadata + formatos (rapido, no descarga)
  - download(url, format_id): dispara la descarga (en segundo plano) y avisa
  - download_status(job_id?): estado de una descarga (o de las ultimas)
  - health_check(): estado de yt-dlp

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
import json
import uuid
import time
import base64
import shlex
import asyncio
import shutil
import threading
import subprocess
import urllib.request
from pathlib import Path

import yt_dlp
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse, FileResponse, Response

from auto_updater import run_forever as run_auto_updater

try:
    from icons import ICON_192_B64, ICON_512_B64
except Exception:
    ICON_192_B64 = ICON_512_B64 = ""

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
# Anadir PO Token (plugin bgutil) seria una estrategia mas aqui si hiciera falta.
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


def _extract_info(url: str) -> dict:
    """Extrae metadata + formatos.

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

def _do_download(job_id: str, url: str, format_id: str):
    """Descarga real con yt-dlp, bajo el semaforo y via la cascada."""
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
        is_audio = "+" not in (job.get("format_id") or "")   # video = "ID+bestaudio"
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


# --------------------------------------------------------------------------
# Herramientas MCP (capa de razonamiento: las llama Claude)
# --------------------------------------------------------------------------

@mcp.tool()
def list_formats(url: str) -> dict:
    """
    Muestra los formatos y calidades disponibles de un link de video/audio.

    USA ESTA HERRAMIENTA siempre que el usuario comparta un ENLACE (URL) de
    YouTube, TikTok, Instagram, Facebook, Twitter/X, etc. y quiera verlo,
    guardarlo o descargarlo, o pida ver las calidades/formatos o la miniatura.
    Devuelve titulo, autor, miniatura, duracion y la lista de formatos con su
    `format_id` (para pasarselo luego a `download`). Para YouTube elige el
    cliente que da la MAYOR resolucion disponible.

    Args:
        url: el link compartido por el usuario.
    """
    try:
        info = _extract_info(url)
    except Exception as e:
        return _friendly_error(e)

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

    return {
        "ok": True,
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "thumbnail": info.get("thumbnail"),
        "duration_seconds": info.get("duration"),
        "formats": curated,
        "js_engine": "deno" if DENO_PATH else "none",
    }


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
        return {"ok": True, "job": {k: v for k, v in j.items() if k != "file_path"}}
    jobs = _load_jobs()
    return {"ok": True, "jobs": [{k: v for k, v in j.items() if k != "file_path"} for j in jobs[:10]]}


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
        "youtube_strategy": _CHAMPION["name"],
    }, headers=CORS_HEADERS)


@mcp.custom_route("/api/jobs", methods=["GET"])
async def api_jobs(request):
    jobs = _load_jobs()
    # No exponemos file_path (ruta interna del servidor) al cliente.
    safe = [{k: v for k, v in j.items() if k != "file_path"} for j in jobs]
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
