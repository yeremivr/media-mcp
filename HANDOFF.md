# HANDOFF — media-mcp (prompt de arranque para el siguiente Claude)

Eres el siguiente Claude que continua este proyecto academico de Yeremi/Piero.
Lee esto primero: es tu prompt para agarrar TODO el contexto rapido y saber
como actuar. No es la arquitectura completa; es lo esencial + la siguiente mision.

## Que es el proyecto (30 seg)
Compartir un link a Claude (movil) -> Claude lista formatos y descarga (yt-dlp)
-> la PWA "Cauce" guarda el archivo en el telefono.
- Repo: github.com/yeremivr/media-mcp (owner GitHub `yeremivr`, rama `main`).
- Servidor en vivo: https://media-mcp-c9h6.onrender.com (Render). MCP en `/mcp`,
  PWA en `/`, API en `/api/health|jobs|file/{id}`.
- Conector MCP "media-mcp" activo en la app del usuario (tools: list_formats,
  download, health_check).

## Como obtener contexto RAPIDO (en este orden)
1. Lee la memoria persistente `media-mcp-project.md` (estado, URLs, gotchas). Es
   la fuente de verdad mas veloz.
2. Carga las tools de GitHub (ToolSearch) y lee: `server.py`, `requirements.txt`,
   `build.sh`, `icons.py`, `pwa/index.html`, `README.md`.
3. Llama `health_check()` por el conector -> te dice js_engine (deno), cookies,
   ffmpeg. Prueba `list_formats(url)` con un link real.
4. Para probar de verdad: clona el repo, crea venv, instala
   `mcp[cli] yt-dlp[default] starlette uvicorn pillow imageio-ffmpeg`, corre
   `python server.py` y curl los endpoints. Patron ya usado y confiable.

## Estado actual
- [OK] API web + PWA Cauce servida en `/`; iconos generados por codigo (Pillow).
- [OK] YouTube base: Deno + scripts EJS (`yt-dlp[default]`) -> funciona con muchos videos.
- [OK] Cookies de YouTube: Secret File `/etc/secrets/cookies.txt` copiado a ruta
  ESCRIBIBLE (`DOWNLOAD_DIR/cookies_active.txt`) porque yt-dlp reescribe el cookiefile.
- [OK] Instagram / TikTok / etc. funcionan.
- [OK] Merge video+audio con ffmpeg (imageio-ffmpeg) -> descargas CON sonido
  (DASH viene con pistas separadas). list_formats devuelve `idVideo+bestaudio/best`.
- [PEND] Auto-deploy en Render APAGADO: cada push necesita Manual Deploy ->
  "Deploy latest commit". Sugerir activarlo (Settings > Build & Deploy).
- [LIMITE] YouTube: algunos videos siguen dando "Sign in to confirm you're not a
  bot" AUN con cookies validas (mismatch IP datacenter vs cookie residencial +
  po_token). Es best-effort, no 100%.

## Gotchas criticos (no tropieces con esto)
- El commit de binarios / base64 largo via GitHub MCP CORROMPE los archivos
  (cambia bytes/caracteres). Nada de PNGs incrustados: generalos por codigo.
  `gh` NO esta autenticado -> no hay `git push` local; todo va por la API MCP.
- Tras cada push, el usuario debe Manual Deploy (auto-deploy off).
- Nunca apuntes yt-dlp a un cookiefile de solo lectura (/etc/secrets); usa copia escribible.
- VERIFICA re-clonando y booteando antes de afirmar que algo funciona. Se honesto con los limites.

## MISION siguiente (lo que pidio el usuario: minar yt-dlp)
Estudiar github.com/yt-dlp/yt-dlp (el motor que YA usamos) para ampliar y robustecer.
Orden sugerido:
1. PRIMERO verificar, no "replicar": nuestro wrapper ya es GENERICO. Prueba
   `list_formats` con links de **Pinterest**, X/Twitter, Facebook, Twitch. Es muy
   probable que YA funcionen; entonces no hay que reconstruir nada, solo confirmar
   y pulir labels/tamaños. Reporta cuales funcionan tal cual.
2. YouTube anti-bot: en yt-dlp revisa `extractor_args youtube:player_client`
   (probar `default`, `mweb`, `tv`, `web_safari`), el sistema `po_token` /
   `--remote-components ejs:npm`, y buenas practicas de cookies (idealmente
   generadas/renovadas para reducir el bloqueo por IP). Meter esas opciones en
   `_base_opts()` de forma configurable y medir si sube la tasa de exito. Sin
   prometer 100%.
3. UX: que Claude muestre miniatura + formatos por defecto; soportar playlists /
   varios reels; mostrar tamaños reales; mensajes claros.

## Estilo de trabajo
Espanol. Commits pequenos y VERIFICADOS (re-clonar + bootear). No incrustar
binarios. Recordar el Manual Deploy. Ser honesto con lo que YouTube permite.
La firma estilo NeuroAlert NO aplica aqui salvo que lo pidan.
