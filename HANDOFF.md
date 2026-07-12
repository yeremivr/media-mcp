# HANDOFF — media-mcp (prompt de arranque para el siguiente Claude)

Eres el siguiente Claude que continua este proyecto academico de Yeremi/Piero.
Lee esto primero: es tu prompt para agarrar TODO el contexto rapido y saber
como actuar. No es la arquitectura completa; es lo esencial + la siguiente mision.

## Que es el proyecto (30 seg)
Compartir un link a Claude (movil) -> Claude lista formatos y descarga (yt-dlp)
-> el archivo aparece en la GALERIA del telefono. Corre en **tu propio telefono**
(Termux), por eso YouTube no lo bloquea (IP residencial, no datacenter).
- Repo: github.com/yeremivr/media-mcp (owner GitHub `yeremivr`, rama `main`).
- **YA NO usamos Render.** El server vive en el telefono (Termux) expuesto por un
  tunel de Cloudflare (`cloudflared tunnel --url`). El conector MCP en claude.ai
  apunta a `https://<algo>.trycloudflare.com/mcp`.
- Tools MCP: `list_formats`, `download`, `download_status`, `resolve_media`,
  `preview_thumbnail`, `health_check`.

## Como obtener contexto RAPIDO (en este orden)
1. Lee la memoria persistente `media-mcp-project.md` (estado, URLs, gotchas). Es
   la fuente de verdad mas veloz.
2. Carga las tools de GitHub (ToolSearch) y lee: `server.py`, `resolver.py`,
   `requirements.txt`, `icons.py`, `pwa/index.html`, `README.md`, `GUIA-TERMUX.md`.
3. Llama `health_check()` por el conector -> te dice js_engine (deno), cookies,
   ffmpeg, po_token, `resolver`/`resolver_ok`. Prueba `list_formats(url)` real.
4. Para probar el MOTOR sin red: `python test_resolver.py` y
   `python test_server_integration.py` (usan fixtures + stubs, no tocan internet).

## Estado actual (lo que YA funciona)
- [OK] YouTube: cascada auto-sanadora de player_client (elige el de mayor
  resolucion) + PO Token (bgutil) + IP residencial -> ya no lo limita a 360p.
- [OK] Instagram / TikTok / Facebook / Twitter via yt-dlp.
- [OK] Merge video+audio con ffmpeg (imageio-ffmpeg) -> descargas CON sonido.
- [OK] Descarga en 2do plano + notificacion nativa con miniatura + `termux-media-scan`
  (aparece en la galeria).
- [OK] **Motor estructural `resolver.py` (RED DE SEGURIDAD)**: cuando yt-dlp no
  tiene extractor o su extractor se rompe (LinkedIn, Facebook cuando cambian el
  HTML), el resolver baja el HTML, lo vuelve un GRAFO (DOM + JSON embebido) y
  PUNTUA cada URL candidata por patron-de-valor (CDN/extension/firma) + contexto
  estructural (hermanas width/height/bitrate, ancestros video/media). No depende
  del nombre de la clave -> sobrevive a renombres/reanidados. `_extract_info`
  cae a el solo para no-YouTube cuando yt-dlp no da formatos. `download`
  re-resuelve al momento (las URLs firmadas de CDN expiran) y baja la directa.
- [OK] `preview_thumbnail(url)`: devuelve la miniatura como IMAGEN (el telefono
  SI llega al CDN de fbcdn/licdn que la red de Claude tiene vetado).

## Gotchas criticos (no tropieces con esto)
- El server corre en el telefono. Para cargar codigo nuevo: en la sesion Termux
  del server -> `git pull` + `pkill -f server.py` + `python server.py` + Ctrl+Z + `bg`
  (o simplemente `sh ~/media-mcp/boot/start-cauce.sh`, que rearranca los 3
  procesos de forma idempotente). El tunel NO cambia de URL si solo reinicias el
  server. Reinicia el server tras instalar cualquier plugin de yt-dlp.
- El commit de binarios / base64 largo de 1 linea via GitHub MCP CORROMPE los
  archivos. Texto normal .py con emoji UTF-8 SI funciona. Normaliza CRLF->LF
  antes de commitear (Windows mete CRLF). Verifica comparando el git-blob-sha1.
- `gh` NO esta autenticado en la maquina del usuario -> todo commit va por la API
  del GitHub MCP como `yeremivr`.
- `resolver.py` es STDLIB PURA a proposito (Termux sufre compilando C: lxml, etc.)
  -> NO agregues dependencias que compilen. Si tocas el motor, corre sus tests.
- La URL `trycloudflare` (quick tunnel) ROTA en cada reinicio de cloudflared ->
  hay que re-pegarla en el conector. Fijarla = named tunnel o tailscale funnel.

## MISION siguiente (ideas, segun pida el usuario)
1. Probar el resolver EN VIVO desde el telefono con posts reales de LinkedIn y
   reels de Facebook (yt-dlp deberia cubrir la mayoria; el resolver es la red si
   falla). Medir tasa de exito real y afinar los pesos del scorer en resolver.py.
2. Headless de ultimo recurso: para sitios que cargan el video 100% por JS, un
   WebView/Deno headless que sniffe la peticion de red del .mp4 (el resolver ya
   deja el hueco documentado). Solo si hace falta tras medir.
3. Canario en vivo opcional (hoy `selftest()` es offline/determinista): correr el
   resolver periodicamente contra un post publico estable y alertar si baja el
   score (la estructura cambio) antes de que el usuario lo note.
4. URL FIJA del tunel (named tunnel / tailscale funnel) para no re-pegar el
   conector en cada arranque.

## Estilo de trabajo
Espanol. Commits pequenos y VERIFICADOS (los tests del resolver corren sin red;
el server se prueba en el telefono con `health_check`/`list_formats`). No
incrustar binarios. Recordar el `git pull`+restart en el telefono. Ser honesto
con los limites (login-walls, URLs firmadas que expiran, sitios 100% JS). La
firma estilo NeuroAlert NO aplica aqui salvo que lo pidan.
