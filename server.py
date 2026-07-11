"""
media-mcp: servidor MCP remoto que envuelve yt-dlp.

Expone herramientas a Claude:
  - list_formats(url): consulta metadata + formatos disponibles (rápido, no descarga)
  - download(url, format_id): descarga y convierte en el formato elegido
  - health_check(): prueba que yt-dlp sigue funcionando

Diseñado para correr como servidor HTTP (transporte 'streamable-http'),
que es lo que necesita un Custom Connector remoto en Claude.
"""

import os
import uuid
import asyncio
from pathlib import Path

import yt_dlp
from mcp.server.fastmcp import FastMCP

from auto_updater import run_forever as run_auto_updater

# --- Configuración ---
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(exist_ok=True)

# El puerto lo define la plataforma de hosting (Render/Railway) via env var PORT.
PORT = int(os.environ.get("PORT", 8000))

mcp = FastMCP("media-mcp", host="0.0.0.0", port=PORT)


def _extract_info(url: str) -> dict:
    """Consulta metadata + formatos SIN descargar. Es la llamada rápida (1-3s)."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


@mcp.tool()
def list_formats(url: str) -> dict:
    """
    Consulta un link (TikTok, Instagram, YouTube, Facebook, etc.) y devuelve
    metadata (título, autor, miniatura, duración) + una lista curada de
    formatos disponibles para descargar, con tamaño estimado.

    Args:
        url: el link compartido por el usuario.
    """
    try:
        info = _extract_info(url)
    except Exception as e:
        return {"ok": False, "error": f"No se pudo leer el link: {e}"}

    # yt-dlp devuelve decenas de formatos técnicos; los curamos a algo legible.
    raw_formats = info.get("formats", []) or []
    curated = []

    # Mejor formato de audio solo
    audio_only = [f for f in raw_formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
    if audio_only:
        best_audio = max(audio_only, key=lambda f: f.get("abr") or 0)
        curated.append({
            "format_id": best_audio["format_id"],
            "kind": "audio",
            "label": f"Audio {int(best_audio.get('abr') or 0)}kbps ({best_audio.get('ext')})",
            "filesize_mb": round((best_audio.get("filesize") or best_audio.get("filesize_approx") or 0) / 1_000_000, 1),
        })

    # Formatos de video con audio incluido, deduplicados por resolución
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
        if len(seen_res) >= 4:  # top 4 resoluciones basta
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
    Descarga el link en el format_id elegido (obtenido previamente de list_formats)
    y lo guarda en el servidor. Devuelve un job_id y la ruta del archivo final.

    Args:
        url: el link original.
        format_id: el format_id devuelto por list_formats.
    """
    job_id = str(uuid.uuid4())[:8]
    out_template = str(DOWNLOAD_DIR / f"{job_id}_%(title).80s.%(ext)s")

    ydl_opts = {
        "format": format_id,
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
    except Exception as e:
        return {"ok": False, "error": f"Fallo la descarga: {e}"}

    return {
        "ok": True,
        "job_id": job_id,
        "file_path": filepath,
        "title": info.get("title"),
    }


@mcp.tool()
def health_check() -> dict:
    """Prueba rápida de que yt-dlp sigue funcionando (para el cron de monitoreo)."""
    test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    try:
        info = _extract_info(test_url)
        return {"ok": True, "yt_dlp_version": yt_dlp.version.__version__, "title": info.get("title")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    # Nota: mcp.run() maneja su propio event loop internamente, así que el
    # auto-actualizador se lanza en un hilo aparte para no pelear con él.
    import threading

    def _updater_thread():
        asyncio.run(run_auto_updater())

    threading.Thread(target=_updater_thread, daemon=True).start()

    # Transporte HTTP streamable: esto es lo que Claude necesita para
    # conectarse como Custom Connector remoto (no stdio local).
    mcp.run(transport="streamable-http")
