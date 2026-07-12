# media-mcp

Servidor MCP remoto que envuelve `yt-dlp` para que Claude pueda consultar
formatos disponibles y descargar video/audio desde un link (TikTok,
Instagram, YouTube, Facebook, etc.) que compartas en el chat. Incluye una
PWA ("Cauce") que muestra las descargas listas y las guarda en tu telefono.

## Piezas

- `server.py` — servidor MCP + API web:
  - 3 tools MCP para Claude: `list_formats`, `download`, `health_check`.
  - API para la PWA: `/api/jobs`, `/api/file/{job_id}`, `/api/health`.
  - Sirve la PWA Cauce en la raiz `/`.
- `pwa/` — la app "Cauce" (HTML/JS + manifest + service worker + iconos).
- `auto_updater.py` — corre en segundo plano, actualiza `yt-dlp[default]`
  cada 4h y guarda el estado en `status.json` (alerta solo si algo falla).
- `build.sh` — instala dependencias Python + Deno (motor JS para YouTube).
- `requirements.txt`, `Procfile`, `runtime.txt` — para desplegar en Render.

## Desplegar en Render

1. https://render.com → entra con GitHub.
2. "New +" → "Web Service" → conecta el repo `media-mcp`.
3. Configuracion:
   - **Build Command:** `bash build.sh`   ← (instala Python + Deno)
   - **Start Command:** `python3 server.py`
4. Deploy. Render te da una URL tipo `https://media-mcp.onrender.com`.

> El plan gratis de Render duerme el servicio tras inactividad: la primera
> peticion puede tardar ~30-50s en "despertar". Es normal.

## Conectar a Claude (web y mobile)

1. claude.ai → **Configuracion → Conectores** → "Agregar conector personalizado".
2. URL de Render + `/mcp`:  `https://media-mcp.onrender.com/mcp`
3. Guarda. Queda disponible tambien en Claude Mobile automaticamente.

## Instalar la PWA "Cauce" en tu telefono

1. En el telefono, abre en Chrome: `https://media-mcp.onrender.com/`
   (la misma URL de Render, **sin** `/mcp`).
2. Menu (⋮) → **"Agregar a pantalla de inicio" / "Instalar app"**.
3. Se instala como app con su icono. Abrela cuando Claude termine una descarga.

## Probar el flujo completo

1. En Claude (web o mobile) con el conector activo, pega un link:
   > "Descarga esto" — Claude llama a `list_formats`, te muestra opciones,
   > y al confirmar llama a `download`.
2. Abre la app **Cauce**: aparece la descarga como tarjeta.
3. Toca **"Guardar en mi telefono"** → el archivo se descarga a tu dispositivo.

> Nota: en el plan gratis de Render el disco es efimero; los archivos viven
> hasta el siguiente reinicio/redeploy. Guarda desde Cauce poco despues de
> descargar. (Para produccion se usaria almacenamiento persistente / S3.)

## Nota para el reporte academico

4 capas deliberadamente separadas:
- **Razonamiento** (Claude + MCP): interpreta la intencion en lenguaje
  natural y decide que formato pedir.
- **Ejecucion** (`server.py` + yt-dlp): el trabajo mecanico real, sin IA.
- **Ultimo kilometro** (PWA Cauce + API): entrega el archivo al dispositivo.
- **Resiliencia** (`auto_updater.py` + `build.sh`): mitiga —no elimina— la
  fragilidad de depender de APIs internas que cambian sin aviso. YouTube es
  la plataforma mas fragil: requiere Deno + scripts EJS; si fallan, el resto
  sigue funcionando (degradacion controlada, no promesa de disponibilidad
  perfecta).
