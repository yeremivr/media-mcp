"""
Actualiza yt-dlp automáticamente cada N horas y corre un health check.
Se importa desde server.py y corre como tarea de fondo (no bloquea al servidor).

Filosofía: alertar por excepción, no por rutina. No notifica cuando
todo sale bien, solo deja registro en status.json para que un
endpoint o notificación externa lea el estado si algo falló.
"""

import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

STATUS_FILE = Path("./status.json")
CHECK_INTERVAL_HOURS = 4  # ajustable


def _write_status(ok: bool, detail: str, version: str | None = None):
    STATUS_FILE.write_text(json.dumps({
        "ok": ok,
        "detail": detail,
        "yt_dlp_version": version,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))


def _update_and_check() -> tuple[bool, str, str | None]:
    # 1. Intentar actualizar yt-dlp a la última versión
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
            check=True, capture_output=True, timeout=120,
        )
    except Exception as e:
        return False, f"No se pudo actualizar yt-dlp: {e}", None

    # 2. Health check real: extraer info de un video de prueba conocido
    try:
        import importlib
        import yt_dlp
        importlib.reload(yt_dlp)
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ", download=False)
        if not info:
            return False, "Health check devolvió vacío", yt_dlp.version.__version__
        return True, "OK", yt_dlp.version.__version__
    except Exception as e:
        return False, f"Health check falló tras actualizar: {e}", None


async def run_forever():
    while True:
        ok, detail, version = await asyncio.to_thread(_update_and_check)
        _write_status(ok, detail, version)
        if not ok:
            # Aquí es donde engancharías un webhook/push real (ej. a tu PWA
            # o a un bot de Telegram) para avisarte SOLO cuando algo falla.
            print(f"[ALERTA] media-mcp health check falló: {detail}", file=sys.stderr)
        await asyncio.sleep(CHECK_INTERVAL_HOURS * 3600)
