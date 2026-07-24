# HANDOFF — media-mcp (briefing para el siguiente Claude, sin contexto previo)

Eres el siguiente Claude que continúa este proyecto de **Yeremi/Piero** (académico,
pero de verdad en producción en su teléfono). Este archivo es tu punto de arranque:
está pensado para que, **sin ningún contexto previo**, entiendas TODO el proyecto y
—lo más importante— **retomes la forma de trabajo**. Léelo entero antes de tocar nada.

El usuario escribe en **español** y así le respondes. Es estudiante (ruta de
ciberseguridad/HTB, UPC en Lima). Le gusta la ingeniería bien hecha, honesta y
verificada. Se emociona con algoritmos: tu trabajo es canalizar eso hacia lo que de
verdad ayuda y decirle con claridad cuándo algo NO ayuda (ver "Metodología").

---

## 1. Qué es el proyecto (30 segundos)

Compartes un link (YouTube, Instagram, TikTok, Facebook, X/Twitter, LinkedIn,
Pinterest…) a Claude en el móvil → Claude, vía una tool MCP, **detecta qué es
(video / foto / carrusel / mixto) y lo descarga** → el archivo aparece en la
**galería del teléfono**, con notificación nativa. El caso de uso real es: *"pego
el link y cierro la app"*.

**La genialidad topológica:** el servidor corre en el **propio teléfono del usuario**
(Termux), expuesto por un túnel de Cloudflare. Por eso YouTube/Instagram no lo
bloquean: ven una **IP residencial/móvil**, no un datacenter. Ese fue el hallazgo
raíz — ningún software en la nube arregla el muro anti-bot de YouTube; la solución
era topológica (correr en IP residencial). No vuelvas a Render ni a otra nube.

- Repo: `github.com/yeremivr/media-mcp` (owner `yeremivr`, rama `main`).
- Dueños del código; firmar cada commit con `Co-Authored-By: Claude ...` (lo hace el harness).

---

## 2. LA METODOLOGÍA (lo más importante — así se trabaja aquí)

Esto es lo que el usuario más valora. Si solo lees una sección, que sea esta.

1. **Mide antes de optimizar. El cuello de botella es la RED, no la CPU.** El
   scoring del grafo del resolver son *microsegundos* sobre ~890 candidatos; cada
   *fetch* HTTP son *segundos*. Optimizar bucles o "bajar a bytes" es pulir lo que
   no se nota. Las palancas reales, en orden: (a) quitar fetches redundantes,
   (b) acertar la "puerta" al primer intento, (c) paralelizar lo que estaba en serie.

2. **Usa el algoritmo CORRECTO y RECHAZA los incorrectos — con argumentos.** El
   usuario pide a veces meter Min-Heap, Dijkstra, Quicksort, HyperLogLog, Minimax,
   KMP, K-means, A\*, etc. La respuesta honesta ya está dada: **solo el Min-Heap
   encajó** (cola de descargas por prioridad, SJF). El resto NO: Quicksort < Timsort;
   binary-search irrelevante (n<100); HLL es para miles de millones (aquí cuentas
   hasta 11); Dijkstra/A\* no hay nodo-meta (se puntúan hojas, no se buscan caminos);
   Minimax necesita juego adversarial (elegir puerta es un *bandido*, ya resuelto
   greedy); KMP no aplica (str.find ya es O(n+m) en C); **K-means reintroduciría un
   bug ya matado** (los rasgos NO separan el post de la basura, solo la ESTRUCTURA:
   commits 19d646c/5780d80). Saber qué NO meter vale más que meter algo. El código
   YA es rico: Union-Find/DSU, scorer lineal ponderado, parser de llaves con pila,
   aprendizaje online greedy (bandido), dedup exacto O(n), cache LRU+TTL, min-heap.
   Lo "estilo FAANG" aquí no es un algoritmo exótico: es **eliminar viajes de red
   redundantes** (coalescing, caché, ejecución especulativa) — que es lo hecho.

3. **El ciclo de despliegue y VERIFICACIÓN EN VIVO.** Tú no tienes acceso al
   teléfono; el usuario despliega. El ciclo es: escribes código → el usuario hace
   `git pull && reload` en Termux → **tú verificas en vivo** llamando `health_check()`
   por el conector. **Truco de oro para saber si el server corre tu código nuevo:**
   agrega o busca un campo NUEVO en la salida de una tool que YA existe (p.ej. yo
   agregué `dl_pool`/`dl_queue` a health_check). Si aparece → corre lo nuevo. Si no →
   el server sigue viejo (rama mal, zombi, etc.). Esto distingue "server viejo" de
   "cliente con manifiesto congelado" sin mandar al usuario a reinstalar nada.

4. **Protocolo de prueba a ciegas (contra tu propio sesgo).** (nivel 1) ESCRIBE la
   predicción ANTES de ejecutar. (nivel 2) usa un oráculo que NO comparta código con
   lo que pruebas (`preview_image` = tus ojos, contra el scorer). (nivel 3) el
   usuario manda links SIN decir qué contienen y da la verdad de campo después.

5. **Sé honesto con los límites y los fallos silenciosos.** Login-walls, URLs
   firmadas que expiran en horas, sitios 100% JS. El peor bug de este proyecto es el
   **fallo silencioso**: guardar HTML de login como .jpg y decir "listo". Por eso se
   verifican los *magic bytes*, no el Content-Type. Contrato irrompible > declaración
   esperada. Si algo se truncó o falló, dilo con la salida real; no cantes victoria.

6. **Investigar en internet está PERMITIDO y bien visto.** Si necesitas entender una
   lógica (cómo yt-dlp resuelve X, cómo cambió el HTML de una plataforma, qué hace
   un algoritmo, foros/programación competitiva), sal a la web, entiéndelo y
   **re-adáptalo** al problema real. No reinventes la rueda; mejórala solo si aporta.

7. **Nada de vocabulario por plataforma.** El motor es genérico a propósito. No
   ramifiques por la pinta de la URL (`/reel/` vs `/p/`) ni codifiques nombres de
   claves de una plataforma: eso es el extractor-por-sitio frágil que el resolver
   existe para evitar. Aprende en runtime rasgos AGNÓSTICOS (familia de rendition,
   parentesco de ID snowflake, procedencia estructural, el contrato de `og:image`).

---

## 3. Arquitectura (los archivos y cómo se hablan)

**Flujo:** link → `_extract_info(url)` → (yt-dlp ∥ resolver) → listado de formatos/fotos
→ descarga en 2º plano (cola de prioridad + pool) → `termux-media-scan` → notificación.

- **`server.py`** — el servidor MCP (FastMCP) + orquestación de descargas + API web.
  - `_extract_info(url)`: **corre yt-dlp y el resolver EN PARALELO** para no-YouTube
    (antes en serie = suma; ahora máximo). yt-dlp gana en videos sueltos; si no da
    formatos, se cobra el resolver que ya venía resolviendo. YouTube NO usa el
    resolver (googlevideo necesita el descifrado de firma de yt-dlp; tiene su cascada).
  - **Cola de descargas por prioridad (min-heap, `heapq` bajo `Condition`) + pool de
    workers** (`MAX_PARALLEL_DL`, def 2). Orden `(tier, tamaño, seq)`: fotos (tier 0,
    KB) primero, luego videos por tamaño ascendente (SJF), `seq` desempata FIFO y hace
    la tupla siempre comparable. `_DLTask` lleva un `Event` que la tool espera.
  - Cascada YouTube auto-sanadora de `player_client` (elige el de mayor resolución) +
    PO Token (bgutil) + cookies opcionales (último recurso).
  - Descarga de fotos EN PARALELO (`ThreadPoolExecutor`, orden de carrusel preservado).
  - `_friendly_error(e, url)`: mensajes **según la plataforma** (solo YouTube habla de
    YouTube; el resto recibe explicación honesta, sin pedir cookies que no sirven).
- **`resolver.py`** — el MOTOR ESTRUCTURAL (RED DE SEGURIDAD). **STDLIB PURA a
  propósito** (Termux sufre compilando C → NO agregues deps que compilen). Baja el
  HTML, lo vuelve un GRAFO (DOM + islas JSON via scanner de llaves balanceadas), y
  PUNTÚA cada URL candidata por patrón-de-valor (CDN/extensión/firma) + contexto
  estructural (hermanas width/height/bitrate, ancestros). Cascada de PUERTAS
  (Googlebot/Bingbot/Slackbot/… + reescrituras `/embed/`), memoria por-host
  (`_HOST_MEMORY`, persistida) → host conocido = 1 fetch. DSU para dedup, cohorte de
  procedencia para pertenencia al post, modelo de ELEMENTOS (un post es una secuencia
  de elementos, cada uno foto o video-con-carátula), álbum completo de Facebook.
- **`fb_recon.py`** — recon/ataque directo a `/media/set/` de Facebook (álbum completo
  sin login vía lookaside). **`FACEBOOK-FIXES.md`** documenta esa fase.
- **Tests (corren SIN red, con fixtures/stubs):** `test_resolver.py`,
  `test_carousel.py` (+ canario en health), `test_server_integration.py`
  (stubs de yt-dlp/mcp/starlette), `test_facebook_bugs.py`. **Córrelos siempre antes
  de commitear.**
- **`boot/`** — `reload-cauce.sh` (el del día a día tras `git pull`: reinicia SOLO
  server.py, el túnel NO se toca → la URL no cambia), `start-cauce.sh` (arranca los 3
  procesos idempotente), `stop-cauce.sh`.
- `pwa/`, `icons.py` — la PWA "Cauce" (capa de último kilómetro, opcional; se conserva
  por la narrativa académica de las 4 capas, no es imprescindible).
- `GUIA-TERMUX.md` — guía paso a paso del setup en el teléfono.

**3 procesos vivos en el teléfono:** `server.py` (puerto 8000) · `cloudflared`
(el túnel → URL pública) · el proveedor de PO Token (`deno run` bgutil, puerto 4416).

---

## 4. Las tools MCP (lo que Claude ve)

- **`grab(url, quality="best", which="all")`** — LA POR DEFECTO cuando el usuario solo
  pega un link. Un paso: mira, decide (video/foto/carrusel/mixto) y descarga. NO
  llames list_formats antes.
- `list_formats(url)` — inspecciona sin bajar: `media_type`, `formats`, `images`,
  `full_caption`, `hashtags`. Úsala si el usuario quiere elegir calidad o solo saber.
- `download(url, format_id)` — baja un format_id concreto.
- `download_images(url, which)` — baja fotos de carrusel/álbum ("all"/"1,3,5"/"2-4").
- `download_status(job_id?)` — estado de descargas.
- `resolve_media(url)` — DIAGNÓSTICO: resuelve con el motor estructural y muestra
  score + de qué parte del HTML salió cada URL + confianza. No corre yt-dlp (a propósito).
- `preview_image(url, index)` / `preview_thumbnail(url)` — devuelve la imagen para que
  TÚ la veas (el teléfono llega al CDN que la red de Claude tiene vetado).
- `health_check()` — estado: js_engine, ffmpeg, po_token, resolver_ok,
  `dl_pool`/`dl_queue` (la cola), max_height, etc.

---

## 5. Estado actual (lo que YA funciona, verificado en vivo)

- [OK] YouTube (cascada + PO Token + IP residencial → hasta 2160p, no lo limita a 360p).
- [OK] Instagram / TikTok / Facebook / X / LinkedIn / Pinterest (video, foto, carrusel,
  MIXTO). Merge video+audio con ffmpeg. Modelo de elementos (numera "video 3 de 5").
- [OK] Álbum completo de Facebook sin login (fb_recon + lookaside).
- [OK] Extracción yt-dlp ∥ resolver; fotos en paralelo; cola de prioridad min-heap.
- [OK] Notificación tarjeta-de-medios + `termux-media-scan` (aparece en galería).
- [OK] Mensajes de error según la plataforma.

**Cómo desplegar tras un cambio (dile ESTE comando al usuario):**
```
cd ~/media-mcp && git pull --ff-only && sh boot/reload-cauce.sh
```
Solo hace falta **chat nuevo** si agregaste una TOOL nueva (el manifiesto MCP se
congela al abrir el chat); un cambio de lógica interna NO lo necesita.

---

## 6. Gotchas críticos (no tropieces con esto)

- **Verifica en qué RAMA está el teléfono.** Lección dura: el teléfono estuvo en una
  rama `claude/...` (no `main`) y todos los `git pull` decían "Already up to date" tras
  traer commits (firma de rama distinta) → corría código viejo. Antes de dar por hecho
  que un push llegó: `git -C ~/media-mcp status -sb`. El teléfono debe seguir `main`.
- **Zombi de server.py:** si `reload` dice "respondiendo" pero health no muestra tu
  campo nuevo, puede haber un `server.py` viejo agarrado al puerto. `pgrep -af server.py`;
  si hay >1 o el reinicio no tomó: `pkill -9 -f server.py; sleep 1; pgrep -af server.py`
  (debe quedar vacío) y luego `sh boot/reload-cauce.sh`. El túnel es otro proceso → la URL no cambia.
- **git push YA funciona** como `yeremivr` (el usuario corrió `gh auth login`). Commitea
  con `git push` normal desde un clon del repo. NO uses la API del GitHub MCP (obliga a
  reescribir el archivo entero y gasta contexto). Cuida: commitea con **LF, no CRLF**
  (verifica `git show HEAD:archivo | tr -cd '\r' | wc -c` == 0). El `.gitignore` cubre
  `downloads/`, `__pycache__/`, `cookies*.txt`, `_gates.json` → revisa `git status` antes.
- **URLs firmadas de CDN expiran** (horas) → la descarga RE-RESUELVE al momento; hay una
  caché TTL 90s para no bajar la misma página dos veces en segundos.
- **`resolver.py` = STDLIB PURA.** No agregues deps que compilen en Termux.
- **URL del túnel (quick tunnel) ROTA** si reinicias `cloudflared` (no si solo reinicias
  server.py). Fijarla = named tunnel (dominio propio) o tailscale funnel. Pendiente.
- No incrustes binarios (PNG/base64) por la API del MCP: corrompe. Los iconos se generan
  por código (Pillow) a propósito.

---

## 7. Palancas abiertas / próximas misiones (según pida el usuario)

1. **Eliminar la doble extracción de `grab` (la palanca real de los ~10s).** Hoy `grab`
   extrae para elegir formato Y `download` re-extrae para bajar (2 golpes a la API por
   video). Descargar del `info` ya extraído (`process_ie_result`) ahorra ~2-4s por
   video. RIESGO: puede producir archivo corrupto SIN error (fallo silencioso) y no se
   puede probar offline. Hazlo con FALLBACK a re-extraer + verificación EN VIVO.
2. **`noplaylist:True` condicionado a YouTube.** Para IG, un carrusel ES una playlist
   para yt-dlp → lo colapsa y siempre caemos al resolver. Condicionarlo daría metadata
   real (altura/tamaño) de los videos IG. Es CALIDAD, no velocidad; podría añadir
   latencia. Prueba controlada antes.
3. **Paralelizar el resolver** solo si mides que un host frío duele — con cuidado: 8
   requests simultáneos a una plataforma arriesgan bloqueo de IP (lo valioso es la IP
   residencial). El `_HOST_MEMORY` ya hace 1-fetch para hosts conocidos.
4. **URL FIJA del túnel** (named tunnel / tailscale funnel) para no re-pegar el conector.
5. **Canario en vivo** (hoy los selftest son offline): correr el resolver periódicamente
   contra un post público estable y alertar si baja el score (la estructura cambió).

---

## 8. Cómo obtener contexto rápido (en este orden)

1. Lee este HANDOFF entero.
2. Carga las tools de GitHub (ToolSearch) y lee `server.py`, `resolver.py`,
   `FACEBOOK-FIXES.md`, `GUIA-TERMUX.md`. Lee solo lo que necesites; no todo.
3. Llama `health_check()` por el conector → confirma que responde y con qué código
   (busca `dl_pool`/`dl_queue`). Prueba `list_formats(url)` o `resolve_media(url)` real.
4. Sin red: `python test_server_integration.py` + `test_resolver.py` + `test_carousel.py`
   + `test_facebook_bugs.py` (todos deben dar "TODO PASA (OK)").
5. Pregúntale al usuario cuál es la misión de hoy. Trabaja como dice la §2.
