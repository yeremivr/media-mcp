# media-mcp

Servidor MCP remoto que envuelve `yt-dlp` para que Claude pueda consultar
formatos disponibles y descargar video/audio desde un link (TikTok,
Instagram, YouTube, Facebook, etc.) que compartas en el chat. Corre en **tu
propio telefono** (Termux), asi YouTube no te bloquea.

> ⭐ **Guia paso a paso para montarlo en tu telefono** (cada comando, los
> errores comunes y que deberias ver en cada paso):
> ### 👉 **[GUIA-TERMUX.md](GUIA-TERMUX.md)**

## Por que en el telefono

YouTube bloquea las descargas desde IPs de **datacenter** (cualquier nube) con
un muro anti-bot. Un telefono usa una **IP movil/residencial** que YouTube no
puede banear -> es la solucion de raiz. Ademas, con un **PO Token** (bgutil, ver
la guia) se elimina el throttle intermitente a 360p. TikTok, Instagram y
Facebook no tienen este problema.

## Piezas

- `server.py` — servidor MCP + API web:
  - tools MCP para Claude: `list_formats`, `download`, `download_status`, `health_check`.
  - API web (opcional): `/api/jobs`, `/api/file/{job_id}`, `/api/health`.
  - Cascada auto-sanadora para YouTube (elige el cliente de MAYOR resolucion),
    descarga en segundo plano, notificacion nativa con miniatura y escaneo de
    galeria (`termux-media-scan`). Detecta el proveedor de PO Token en health.
- `pwa/` — la app "Cauce" (opcional: historial/biblioteca).
- `icons.py` — genera los iconos de la PWA con Pillow (no binarios en el repo).
- `auto_updater.py` — actualiza `yt-dlp` en segundo plano cada 4h.
- `boot/start-cauce.sh` — script de **Termux:Boot** que auto-arranca los 3
  procesos (proveedor PO Token + servidor + tunel) al encender el telefono.

## Puesta en marcha (resumen)

Detalle completo en **[GUIA-TERMUX.md](GUIA-TERMUX.md)**. En breve:

1. Instala **Termux** + **Termux:API** desde **F-Droid** (NO Play Store).
2. `pkg install -y python git ffmpeg deno termux-api python-pillow clang` y
   `termux-setup-storage`.
3. `git clone` este repo e instala deps con el indice de Termux (TUR).
4. `python server.py` (arranca Uvicorn en `0.0.0.0:8000`).
5. `cloudflared tunnel --url http://localhost:8000` -> te da una URL publica.
6. Conecta esa URL **+ `/mcp`** en Claude (claude.ai -> Conectores).
7. (Opcional) PO Token con bgutil + auto-arranque con Termux:Boot: ver la guia.

## Conectar a Claude (web y mobile)

1. claude.ai -> **Configuracion -> Conectores** -> "Agregar conector personalizado".
2. La URL de tu tunel + `/mcp`:  `https://<...>.trycloudflare.com/mcp`
3. Guarda. Queda disponible tambien en Claude Mobile automaticamente.

> 🔴 La URL **debe terminar en `/mcp`**. Si no, Claude falla con "no apunta a un
> servidor MCP valido" / pide un login OAuth que este servidor no tiene.

## Probar el flujo completo

Pega un link en Claude: *"descarga esto en la maxima calidad"*. Claude llama a
`list_formats`, te muestra titulo/autor/miniatura y las calidades, y al
confirmar llama a `download`. En el telefono salta una **notificacion** con
miniatura y el archivo aparece en tu **galeria** (album *Descargas/Cauce*).

## Cookies de YouTube (opcional)

Desde el telefono (IP residencial) casi nunca hacen falta. Si un video puntual
las pidiera: exporta un `cookies.txt` (extension open source "Get cookies.txt
LOCALLY") y apunta la variable `YT_COOKIES_FILE` a ese archivo, o dejalo junto a
`server.py`. `GET /api/health` mostrara `"cookies": true`.

> Seguridad: el `cookies.txt` es tu sesion de YouTube; conviene rotarlo y no
> subirlo al repo.

## Nota para el reporte academico

4 capas deliberadamente separadas:
- **Razonamiento** (Claude + MCP): interpreta la intencion en lenguaje natural
  y decide que formato pedir.
- **Ejecucion** (`server.py` + yt-dlp): el trabajo mecanico real, sin IA.
- **Ultimo kilometro** (galeria del telefono / PWA Cauce): el worker vive EN el
  dispositivo, asi que el archivo se guarda local (co-localizacion = sin
  "hairpin" por el tunel).
- **Resiliencia** (cascada de clientes + PO Token bgutil + `auto_updater.py`):
  mitiga —no elimina— la fragilidad de depender de APIs internas que cambian sin
  aviso. La solucion de raiz al muro de YouTube es **topologica**: correr en una
  IP residencial (el telefono), que YouTube no puede banear, reforzada con un PO
  Token para que ni siquiera limite la calidad.
