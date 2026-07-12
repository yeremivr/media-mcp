"""
media-mcp: servidor MCP remoto que envuelve yt-dlp + API web para la PWA.

Herramientas MCP para Claude:
  - list_formats(url): metadata + formatos (rapido, no descarga)
  - download(url, format_id): descarga y registra el job
  - health_check(): estado de yt-dlp

API web para la PWA:
  - GET /api/jobs, /api/file/{job_id}, /api/health
"""

import os
import json
import uuid
import asyncio
from pathlib import Path

import yt_dlp
from mcp.server.fastmcp import FastMCP

from auto_updater import run_forever as run_auto_updater

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(exist_ok=True)
JOBS_INDEX = DOWNLOAD_DIR / "_jobs.json"

PORT = int(os.environ.get("PORT", 8000))

mcp = FastMCP("media-mcp", host="0.0.0.0", port=PORT)


def _extract_info(url: str) -> dict:
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
        return ydl.extract_info(url, download=False)


def _record_job(job: dict):
    jobs = []
    if JOBS_INDEX.exists():
        try:
            jobs = json.loads(JOBS_INDEX.read_text())
        except Exception:
            jobs = []
    jobs.insert(0, job)
    jobs = jobs[:50]
    JOBS_INDEX.write_text(json.dumps(jobs, ensure_ascii=False, indent=2))


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

    ydl_opts = {"format": format_id, "outtmpl": out_template, "quiet": True, "no_warnings": True}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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
        return {"ok": True, "yt_dlp_version": yt_dlp.version.__version__, "title": info.get("title")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import threading
    threading.Thread(target=lambda: asyncio.run(run_auto_updater()), daemon=True).start()
    mcp.run(transport="streamable-http")
