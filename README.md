# media-mcp

Servidor MCP remoto que envuelve `yt-dlp` para que Claude pueda consultar
formatos disponibles y descargar video/audio desde un link (TikTok,
Instagram, YouTube, Facebook, etc.) que compartas en el chat.

## Piezas

- `server.py` — el servidor MCP en sí, expone 3 tools: `list_formats`,
  `download`, `health_check`.
- `auto_updater.py` — corre en segundo plano, actualiza `yt-dlp` cada 4h
  y guarda el estado en `status.json` (alerta solo si algo falla).
- `requirements.txt`, `Procfile`, `runtime.txt` — para desplegar en Render/Railway.

## Desplegar en Render (recomendado)

1. Ve a https://render.com y crea cuenta (puedes entrar con GitHub).
2. "New +" → "Web Service".
3. Conecta este repo `media-mcp`.
4. Configuración:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python3 server.py`
5. Deploy. Render te da una URL pública tipo `https://media-mcp.onrender.com`.

## Conectar a Claude

1. Ve a claude.ai → **Configuración → Conectores**.
2. "Agregar conector personalizado".
3. Pega la URL de Render, añadiendo `/mcp` al final:
   `https://media-mcp.onrender.com/mcp`
4. Guarda — queda disponible también en Claude Mobile automáticamente.

## Probar el flujo

En un chat con Claude (web o mobile), con el conector activado:

> [pegas un link] "Quiero descargar esto, muéstrame los formatos"

Claude llama a `list_formats`, te muestra las opciones, y al confirmar,
llama a `download`.

## Nota para el reporte académico

Este proyecto separa deliberadamente 3 capas:
- **Razonamiento** (Claude + MCP): interpreta la intención en lenguaje
  natural y decide qué formato pedir.
- **Ejecución** (`server.py` + yt-dlp): el trabajo mecánico real, sin IA.
- **Resiliencia** (`auto_updater.py`): mitiga —no elimina— la fragilidad
  de depender de APIs internas de plataformas que cambian sin aviso. Se
  documenta esto en vez de prometer disponibilidad perfecta, que sería
  técnicamente insostenible.
