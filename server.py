"""
media-mcp: servidor MCP remoto que envuelve yt-dlp + API web para la PWA "Cauce".

Herramientas MCP para Claude (capa de razonamiento):
  - list_formats(url): metadata + formatos (rapido, no descarga)
  - download(url, format_id): descarga y registra el job
  - health_check(): estado de yt-dlp

API web para la PWA (capa 'ultimo kilometro', mismo origen que el servidor):
  - GET /                    -> la PWA Cauce (archivos estaticos de ./pwa)
  - GET /api/health          -> estado del servidor
  - GET /api/jobs            -> lista de descargas listas
  - GET /api/file/{job_id}   -> entrega el archivo al telefono

El endpoint MCP para Claude queda en  /mcp
"""

import os
import json
import uuid
import asyncio
import shutil
from pathlib import Path

import yt_dlp
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse, FileResponse

from auto_updater import run_forever as run_auto_updater

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", BASE_DIR / "downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
JOBS_INDEX = DOWNLOAD_DIR / "_jobs.json"
PWA_DIR = BASE_DIR / "pwa"

PORT = int(os.environ.get("PORT", 8000))

# CORS: permite que la PWA lea el API aunque se sirva desde otro origen
# (ej. GitHub Pages). Si se sirve desde el mismo servidor, no estorba.
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
        "/opt/render/.deno/bin/deno",
        "/opt/render/project/src/.deno/bin/deno",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


DENO_PATH = _find_deno()
if DENO_PATH:
    # Con Deno en el PATH, yt-dlp lo detecta y lo usa solo para YouTube.
    os.environ["PATH"] = os.path.dirname(DENO_PATH) + os.pathsep + os.environ.get("PATH", "")


def _base_opts() -> dict:
    """Opciones base de yt-dlp. Deno (si existe) se detecta solo via PATH."""
    return {"quiet": True, "no_warnings": True}


mcp = FastMCP("media-mcp", host="0.0.0.0", port=PORT)


def _extract_info(url: str) -> dict:
    opts = _base_opts()
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _load_jobs() -> list:
    if JOBS_INDEX.exists():
        try:
            return json.loads(JOBS_INDEX.read_text())
        except Exception:
            return []
    return []


def _record_job(job: dict):
    jobs = _load_jobs()
    jobs.insert(0, job)
    jobs = jobs[:50]
    JOBS_INDEX.write_text(json.dumps(jobs, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------
# Herramientas MCP (capa de razonamiento: las llama Claude)
# --------------------------------------------------------------------------

@mcp.tool()
def list_formats(url: str) -> dict:
    """
    Consulta un link (TikTok, Instagram, YouTube, Facebook, etc.) y devuelve
    metadata (titulo, autor, miniatura, duracion) + formatos disponibles.

    Args:
        url: el link compartido por el usuario.
    """
    try:
        info = _extract_info(url)
    except Exception as e:
        return {"ok": False, "error": f"No se pudo leer el link: {e}"}

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
            "format_id": f["format_id"],
            "kind": "video",
            "label": f"{height}p ({f.get('ext')})",
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
    Descarga el link en el format_id elegido y registra el job para la PWA.

    Args:
        url: el link original.
        format_id: el format_id devuelto por list_formats.
    """
    job_id = str(uuid.uuid4())[:8]
    out_template = str(DOWNLOAD_DIR / f"{job_id}_%(title).80s.%(ext)s")

    opts = _base_opts()
    opts["format"] = format_id
    opts["outtmpl"] = out_template

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
    except Exception as e:
        return {"ok": False, "error": f"Fallo la descarga: {e}"}

    job = {
        "job_id": job_id,
        "file_path": filepath,
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader") or info.get("channel"),
        "ext": Path(filepath).suffix.lstrip("."),
    }
    _record_job(job)

    return {
        "ok": True,
        "job_id": job_id,
        "title": info.get("title"),
        "message": "Descarga lista. Abre la app Cauce para guardarla en tu telefono.",
    }


@mcp.tool()
def health_check() -> dict:
    """Prueba rapida de que yt-dlp sigue funcionando."""
    try:
        info = _extract_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        return {
            "ok": True,
            "yt_dlp_version": yt_dlp.version.__version__,
            "js_engine": "deno" if DENO_PATH else "none",
            "title": info.get("title"),
        }
    except Exception as e:
        return {"ok": False, "js_engine": "deno" if DENO_PATH else "none", "error": str(e)}


# --------------------------------------------------------------------------
# API web (capa 'ultimo kilometro': la consume la PWA Cauce)
# --------------------------------------------------------------------------

@mcp.custom_route("/api/health", methods=["GET"])
async def api_health(request):
    return JSONResponse({
        "ok": True,
        "yt_dlp_version": yt_dlp.version.__version__,
        "js_engine": "deno" if DENO_PATH else "none",
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
                {"ok": False, "error": "El archivo ya no esta en el servidor (se reinicio). Vuelve a pedir la descarga."},
                status_code=410, headers=CORS_HEADERS,
            )
    return JSONResponse({"ok": False, "error": "job no encontrado"}, status_code=404, headers=CORS_HEADERS)


if __name__ == "__main__":
    import threading
    import uvicorn
    from starlette.routing import Mount
    from starlette.staticfiles import StaticFiles

    # Auto-updater de yt-dlp en segundo plano (no bloquea al servidor).
    threading.Thread(target=lambda: asyncio.run(run_auto_updater()), daemon=True).start()

    # App ASGI: incluye /mcp (para Claude) y /api/* (custom_route).
    app = mcp.streamable_http_app()

    # Sirve la PWA Cauce en la raiz. Se agrega al final para que /mcp y /api/*
    # tengan prioridad; cualquier otra ruta cae en los archivos estaticos.
    if PWA_DIR.exists():
        app.router.routes.append(
            Mount("/", app=StaticFiles(directory=str(PWA_DIR), html=True), name="pwa")
        )

    uvicorn.run(app, host="0.0.0.0", port=PORT)
