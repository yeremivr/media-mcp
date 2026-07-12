# media-mcp

Servidor MCP remoto que envuelve `yt-dlp` para que Claude pueda consultar
formatos disponibles y descargar video/audio desde un link (TikTok,
Instagram, YouTube, Facebook, etc.) que compartas en el chat. Incluye una
PWA ("Cauce") que muestra las descargas listas y las guarda en tu telefono.

> ⭐ **¿Quieres correrlo en TU PROPIO TELEFONO? (recomendado)**
> Asi YouTube **no te bloquea** (usas una IP movil/residencial en vez de una IP
> de datacenter). La guia es paso a paso, con cada comando, los errores comunes
> que salen en el camino y **que deberias ver en cada paso** para saber que vas
> bien:
> ### 👉 **[GUIA-TERMUX.md — montarlo en el telefono](GUIA-TERMUX.md)**
>
> El despliegue en **Render** (mas abajo) sigue funcionando como alternativa en
> la nube, pero desde ahi YouTube a veces exige cookies (muro anti-bot por IP de
> datacenter). Para YouTube fiable, usa el telefono.

## Piezas

- `server.py` — servidor MCP + API web:
  - tools MCP para Claude: `list_formats`, `download`, `download_status`, `health_check`.
  - API para la PWA: `/api/jobs`, `/api/file/{job_id}`, `/api/health`.
  - Sirve la PWA Cauce en la raiz `/`.
  - Corre igual en **telefono (Termux)**, PC o Render (es portable): cascada
    auto-sanadora para YouTube, descarga en segundo plano, notificacion nativa
    con miniatura y escaneo de galeria (`termux-media-scan`) cuando corre en Termux.
- `pwa/` — la app "Cauce" (HTML/JS + manifest + service worker).
- `icons.py` — genera los iconos de la PWA con Pillow (no binarios en el repo).
- `auto_updater.py` — corre en segundo plano, actualiza `yt-dlp[default]`
  cada 4h y guarda el estado en `status.json` (alerta solo si algo falla).
- `build.sh` — instala dependencias Python + Deno (motor JS para YouTube).
- `requirements.txt`, `Procfile`, `runtime.txt` — para desplegar en Render.

## Correr en el telefono (recomendado)

Ver la guia completa: **[GUIA-TERMUX.md](GUIA-TERMUX.md)**. En resumen:

1. Instala **Termux** + **Termux:API** desde **F-Droid** (NO Play Store).
2. `pkg install -y python git ffmpeg deno termux-api python-pillow clang` y
   `termux-setup-storage`.
3. `git clone` este repo e instala deps con el indice de Termux (TUR).
4. `python server.py` (arranca Uvicorn en `0.0.0.0:8000`).
5. `cloudflared tunnel --url http://localhost:8000` → te da una URL publica.
6. Conecta esa URL **+ `/mcp`** en Claude (Conectores).

## Desplegar en Render (alternativa nube)

1. https://render.com → entra con GitHub.
2. "New +" → "Web Service" → conecta el repo `media-mcp`.
3. Configuracion:
   - **Build Command:** `bash build.sh`   ← (instala Python + Deno)
   - **Start Command:** `python3 server.py`
4. Deploy. Render te da una URL tipo `https://media-mcp-xxxx.onrender.com`.

> El plan gratis de Render duerme el servicio tras inactividad: la primera
> peticion puede tardar ~30-50s en "despertar". Es normal.

## Conectar a Claude (web y mobile)

1. claude.ai → **Configuracion → Conectores** → "Agregar conector personalizado".
2. La URL de tu servidor + `/mcp`:
   - Telefono: `https://<...>.trycloudflare.com/mcp`
   - Render:   `https://media-mcp-xxxx.onrender.com/mcp`
3. Guarda. Queda disponible tambien en Claude Mobile automaticamente.

> 🔴 La URL **debe terminar en `/mcp`**. Si no, Claude falla con "no apunta a un
> servidor MCP valido" / pide un login OAuth que este servidor no tiene.

## Instalar la PWA "Cauce" en tu telefono (opcional)

> En el modo "telefono" el video ya cae directo en tu galeria, asi que la PWA es
> opcional (historial/biblioteca). En modo Render si es util para bajar el archivo.

1. En el telefono, abre en Chrome la URL base **sin** `/mcp`.
2. Menu (⋮) → **"Agregar a pantalla de inicio" / "Instalar app"**.
3. Se instala como app con su icono.

## Probar el flujo completo

1. En Claude (web o mobile) con el conector activo, pega un link:
   > "Descarga esto" — Claude llama a `list_formats`, te muestra opciones,
   > y al confirmar llama a `download`.
2. En el telefono: salta una **notificacion** con miniatura y el archivo aparece
   en tu **galeria** (album *Descargas/Cauce*). (En modo Render, abre la PWA
   **Cauce** y toca "Guardar en mi telefono".)

## Cookies de YouTube (opcional, solo si YouTube falla)

YouTube es la plataforma mas dura: desde una IP de datacenter (como Render)
a veces responde `Sign in to confirm you're not a bot`. **TikTok, Instagram,
Facebook, etc. NO tienen este problema.** Desde el **telefono** (IP residencial)
tampoco suele hacer falta. Si aun asi lo necesitas:

1. En tu navegador (con sesion de YouTube iniciada) instala una extension
   tipo **"Get cookies.txt LOCALLY"** (open source).
2. Entra a `youtube.com` y exporta el archivo `cookies.txt`.
3. Ubicalo donde el servidor lo detecta solo:
   - Render → **Environment → Secret Files** → archivo `cookies.txt`
     (se monta en `/etc/secrets/cookies.txt`).
   - Telefono/PC → variable `YT_COOKIES_FILE` con la ruta, o un `cookies.txt`
     junto a `server.py`.
4. `GET /api/health` debe mostrar `"cookies": true`.

> Seguridad: el `cookies.txt` es tu sesion de YouTube. Va como Secret File
> (no en el repo) y conviene rotarlo/expirarlo; puede caducar con el tiempo.

## Nota para el reporte academico

4 capas deliberadamente separadas:
- **Razonamiento** (Claude + MCP): interpreta la intencion en lenguaje
  natural y decide que formato pedir.
- **Ejecucion** (`server.py` + yt-dlp): el trabajo mecanico real, sin IA.
- **Ultimo kilometro** (galeria del telefono / PWA Cauce + API): entrega el
  archivo al dispositivo. En el modo "telefono" el worker vive EN el dispositivo,
  asi que el archivo se guarda local (co-localizacion = sin "hairpin" por el tunel).
- **Resiliencia** (`auto_updater.py` + `build.sh` + cascada + cookies): mitiga
  —no elimina— la fragilidad de depender de APIs internas que cambian sin aviso.
  YouTube es la mas fragil: pide Deno + scripts EJS, y desde servidores a veces
  exige cookies (muro anti-bot por IP). La solucion de raiz es **topologica**:
  correr en una IP residencial (el telefono), que YouTube no puede banear. El
  sistema degrada de forma controlada en vez de prometer disponibilidad perfecta.
```