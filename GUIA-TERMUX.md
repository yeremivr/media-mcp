# 📱 Guía "Cauce Soberano": corre el descargador en tu propio teléfono (Termux)

Esta guía te lleva **paso a paso** para montar `media-mcp` dentro de tu teléfono
Android con **Termux**, exponerlo a Claude por un túnel, y descargar videos/música
con solo pasarle un link a Claude. Está escrita con los **errores reales** que
salen en el camino y, sobre todo, con bloques **"✅ Si ves esto, vas por buen
camino"** para que sepas en todo momento que vas bien.

> **¿Por qué en el teléfono y no en la nube?**
> YouTube bloquea las descargas desde IPs de **datacenter** (todas las nubes:
> Render, AWS, etc.) con un muro anti-bot (`Sign in to confirm you're not a bot`).
> Un teléfono usa una **IP móvil/residencial**, que YouTube **no puede banear**.
> Por eso el backend corre en tu bolsillo: es la solución de raíz, no un parche.
> (TikTok, Instagram y Facebook no tienen este muro; el problema es solo YouTube.)

---

## 🗺️ Cómo funciona (panorama en 10 segundos)

```
   Tú (Claude en el móvil o laptop)
            │  "descarga este link"
            ▼
   Claude  ──MCP──►  Túnel Cloudflare  ──►  Tu teléfono (Termux)
                       (URL pública)          server.py + yt-dlp
                                                    │
                                                    ▼
                                        El video se guarda en TU galería
                                        (NO viaja por el túnel = rápido)
```

El **control** (la orden de Claude) viaja por internet; los **datos** (el video)
se quedan **local** en el teléfono. Por eso es rápido y no gasta tu túnel.

---

## 📋 Índice

1. [Requisitos](#1-requisitos)
2. [Instalar las apps (F-Droid, NO Play Store)](#2-instalar-las-apps-f-droid-no-play-store)
3. [Preparar Termux](#3-preparar-termux)
4. [Descargar el proyecto](#4-descargar-el-proyecto)
5. [Instalar las dependencias de Python (aquí aparece el error famoso)](#5-instalar-las-dependencias-de-python)
6. [Arrancar el servidor](#6-arrancar-el-servidor)
7. [Exponerlo a internet con el túnel](#7-exponerlo-a-internet-con-el-túnel)
8. [Conectarlo a Claude (el paso del `/mcp`)](#8-conectarlo-a-claude)
9. [Probar el flujo completo](#9-probar-el-flujo-completo)
10. [Que aparezca en la galería (media-scan)](#10-que-aparezca-en-la-galería)
11. [Dejarlo trabajando en segundo plano](#11-dejarlo-trabajando-en-segundo-plano)
12. [Reiniciar el servidor tras un cambio](#12-reiniciar-el-servidor-tras-un-cambio)
13. [Usarlo desde varias cuentas de Claude](#13-usarlo-desde-varias-cuentas-de-claude)
14. [Pendiente: que "nunca se caiga" (URL fija + auto-arranque)](#14-pendiente-que-nunca-se-caiga)
15. [Tabla de señales "vas por buen camino"](#15-tabla-de-señales-vas-por-buen-camino)
16. [Solución de problemas](#16-solución-de-problemas)

---

## 1. Requisitos

- Un teléfono **Android** (probado en Honor/realme; sirve cualquiera moderno).
- Espacio libre (~1–2 GB) y batería/cargador (la primera instalación calienta).
- **Datos móviles o Wi-Fi.** Curiosamente, para YouTube conviene tener también
  datos móviles disponibles: es tu IP residencial la que derriba el muro.

---

## 2. Instalar las apps (F-Droid, NO Play Store)

> 🔴 **IMPORTANTE:** instala Termux desde **[F-Droid](https://f-droid.org)**, NO
> desde Play Store. La versión de Play Store está **descontinuada y rota**; los
> comandos de esta guía fallarán con ella.

Instala **F-Droid** primero (bájalo de su web) y desde F-Droid instala estas 3 apps:

| App | Para qué sirve |
|---|---|
| **Termux** | La terminal Linux donde corre todo. |
| **Termux:API** | Da acceso a funciones del teléfono: **notificaciones**, escaneo de galería, etc. |
| **Termux:Boot** | (Opcional, para después) arranca el servidor solo al reiniciar el teléfono. |

> ✅ **Vas por buen camino si:** las 3 apps aparecen instaladas y Termux abre una
> pantalla negra con un cursor y un teclado.

---

## 3. Preparar Termux

Abre Termux y ejecuta (una línea a la vez, Enter al final de cada una):

```bash
pkg update && pkg upgrade -y
```
> Puede preguntarte por reemplazar archivos de config: acepta con `Y` o Enter.

Instala los paquetes base:

```bash
pkg install -y python git ffmpeg deno termux-api python-pillow clang
```

> 💡 **Por qué estos:** `python`+`git` (obvio), `ffmpeg` (une video+audio),
> `deno` (motor JS que yt-dlp necesita para YouTube; hay binario listo para
> Android, no compila), `termux-api` (notificaciones/galería), `python-pillow`
> (evita compilar Pillow), `clang` (compilador, hace falta más adelante).

Da permiso de almacenamiento (crea el puente a tu galería):

```bash
termux-setup-storage
```

> ⚠️ **Aparecerá un POPUP de Android** pidiendo permiso de "Archivos y multimedia".
> **Toca "Permitir".** Esto crea la carpeta `~/storage/downloads`, que es la
> carpeta de **Descargas compartida** del teléfono (la que ve tu galería).

> ✅ **Vas por buen camino si:** al escribir `ls ~/storage` ves carpetas como
> `downloads`, `dcim`, `music`, `movies`, `shared`.

---

## 4. Descargar el proyecto

```bash
cd ~
git clone https://github.com/yeremivr/media-mcp
cd media-mcp
```

> ✅ **Vas por buen camino si:** el prompt cambia a `~/media-mcp $` y al escribir
> `ls` ves `server.py`, `requirements.txt`, `pwa`, etc.

---

## 5. Instalar las dependencias de Python

> 🔴 **NO ejecutes** `pip install -U pip`. Termux **prohíbe** actualizar su pip
> y lo rompe. Si lo hiciste, tendrás que reinstalar python. **Sáltatelo.**

Instala las dependencias usando el repositorio de paquetes de Termux (TUR), que
trae ruedas ya compiladas para Android:

```bash
pip install --extra-index-url https://termux-user-repository.github.io/pypi/ -r requirements.txt
```

### ❌ El error que probablemente verás (y cómo arreglarlo)

Es muy común que aquí falle al llegar a **`pydantic-core`** (o `rpds-py`,
`brotli`, `pycryptodomex`). El síntoma es que **pip intenta compilar** y verás
que **baja `rustup`** y luego revienta con algo como:

```
error: could not download file ... rustup ...
error: failed to run custom build command for `pydantic-core`
... target 'aarch64-linux-android' ... not supported
```

**Causa:** ese paquete no estaba pre-compilado, así que pip quiso compilarlo con
Rust, pero `rustup` **no soporta Android**. La solución es usar el **Rust nativo
de Termux** (ese sí compila para Android):

```bash
pkg install -y rust python-cryptography patchelf
```

Y **vuelve a lanzar** el mismo pip de antes:

```bash
pip install --extra-index-url https://termux-user-repository.github.io/pypi/ -r requirements.txt
```

> ⏳ **Ahora SÍ compila.** Tarda **~10 minutos**, usa bastante RAM y calienta el
> teléfono. **Mantén la pantalla encendida y Termux abierto** durante la
> compilación (si la pantalla se apaga mucho rato, Android puede matar el
> proceso). Verás muchas líneas `Building wheel for pydantic-core ...`.

> ✅ **Vas por buen camino si:** al final aparece
> `Successfully installed pydantic-core-... yt-dlp-... fastmcp-... uvicorn-...`
> **sin** ningún `ERROR` en rojo al terminar.

---

## 6. Arrancar el servidor

```bash
python server.py
```

> ✅ **Vas por buen camino si ves algo así** (esta es la señal clave):
>
> ```
> INFO:     Started server process [xxxxx]
> INFO:     Waiting for application startup.
> INFO:     Application startup complete.
> INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
> ```
>
> La línea **`Uvicorn running on http://0.0.0.0:8000`** significa que el servidor
> está vivo y escuchando. 🎉

### Dejarlo en segundo plano (para poder seguir usando la terminal)

Con el servidor corriendo, presiona **`Ctrl+Z`** (lo pausa) y luego escribe:

```bash
bg
```

> Esto lo manda a segundo plano como *job* `[1]`. El servidor sigue corriendo y
> te devuelve el prompt. (Para detenerlo luego: `kill %1`, o `pkill -f server.py`
> para matar cualquier instancia.)

### (Opcional) Comprobarlo localmente

Abre **otra sesión** de Termux (desliza desde el borde izquierdo → *NEW SESSION*)
y prueba:

```bash
curl http://localhost:8000/api/health
```

> ✅ **Vas por buen camino si** responde un JSON parecido a:
>
> ```json
> {"ok": true, "js_engine": "deno", "cookies": false, "ffmpeg": true,
>  "notifications": true, "media_scan": true, "youtube_strategy": null}
> ```
>
> - `js_engine: "deno"` → el motor de YouTube está listo.
> - `ffmpeg: true` → puede unir video+audio (si no, revisa el `pkg install ffmpeg`).
> - `notifications: true` y `media_scan: true` → Termux:API funciona.

---

## 7. Exponerlo a internet con el túnel

Instala cloudflared:

```bash
pkg install -y cloudflared
```

Levanta el túnel (en una sesión aparte, **déjala abierta**):

```bash
cloudflared tunnel --url http://localhost:8000
```

> ✅ **Vas por buen camino si ves un recuadro con TU URL pública:**
>
> ```
> +--------------------------------------------------------------------+
> |  Your quick Tunnel has been created! Visit it at:                  |
> |  https://<palabras-aleatorias>.trycloudflare.com                   |
> +--------------------------------------------------------------------+
> ```
>
> y más abajo los **prechecks en verde**:
>
> ```
> precheck component="DNS Resolution"  status=pass
> precheck component="UDP Connectivity" details="QUIC connection successful" status=pass
> precheck component="TCP Connectivity" details="HTTP/2 connection successful" status=pass
> precheck component="Cloudflare API"   details="API is reachable" status=pass
> SUMMARY: Environment is healthy.
> ```

**Copia esa URL** (`https://<...>.trycloudflare.com`). Ese es tu endpoint base.

> ⚠️ **Esa URL es TEMPORAL:** cambia cada vez que reinicias `cloudflared`. Para
> una URL fija, ver la [sección 14](#14-pendiente-que-nunca-se-caiga).
>
> 💡 Un `WRN` sobre `ping_group_range`/ICMP al arrancar es **inofensivo** (es solo
> sobre `ping`, no afecta al HTTP). Ignóralo.
>
> 💡 Es **normal** ver de vez en cuando errores rojos tipo
> `failed to serve tunnel connection ... timeout: no recent network activity`
> seguidos de `Registered tunnel connection`: son micro-cortes de la red móvil y
> **cloudflared se reconecta solo**. Mientras se reconecte, la URL sigue igual.

---

## 8. Conectarlo a Claude

Tu endpoint para Claude es la URL del túnel **+ `/mcp` al final**:

```
https://<...>.trycloudflare.com/mcp
```

> 🔴 **EL DETALLE MÁS IMPORTANTE:** la URL **DEBE terminar en `/mcp`.** Sin eso,
> nada funciona (ver el error abajo).

Pasos en **claude.ai**:

1. Foto/menú → **Configuración → Conectores**.
2. **"Agregar conector personalizado"**.
3. Pega la URL **con `/mcp`**.
4. **Conectar**.

> ✅ **Vas por buen camino si** en los logs de tu servidor (en Termux) aparece:
>
> ```
> INFO:  <ip>:0 - "POST /mcp HTTP/1.1" 200 OK
> INFO:  <ip>:0 - "POST /mcp HTTP/1.1" 202 Accepted
> Processing request of type ListToolsRequest
> ```
>
> Ese **`POST /mcp ... 200 OK`** + `ListToolsRequest` es Claude conectándose. 🎉

### ❌ Si olvidas el `/mcp` (error clásico)

Claude mostrará *"Problema de conexión — la URL no apunta a un servidor MCP
válido"* o *"No se pudo registrar con el servicio de inicio de sesión"*, y en los
logs verás peticiones a la **raíz** en vez de a `/mcp`:

```
GET  /.well-known/oauth-protected-resource    404 Not Found
GET  /.well-known/oauth-authorization-server   404 Not Found
POST /register                                 405 Method Not Allowed
POST /                                          405 Method Not Allowed
```

**Causa:** al no encontrar el MCP en la raíz, Claude intenta un login OAuth que
este servidor **no tiene**. **Solución:** borra el conector y créalo de nuevo con
la URL **terminada en `/mcp`**. (No necesitas ningún "OAuth Client ID": el
servidor es abierto, sin login.)

> 💡 Si tras poner `/mcp` aún falla, espera unos segundos (el túnel pudo estar en
> un micro-corte) y dale **Conectar** otra vez.

---

## 9. Probar el flujo completo

En Claude (con el conector activo), pásale un link y pide la descarga. Ejemplo:

> *"https://youtube.com/watch?v=... descarga este video en la máxima calidad y
> muéstrame la miniatura y los formatos."*

Lo que pasa:

1. Claude llama a **`list_formats`** → te muestra **título, autor, miniatura** y
   la tabla de **calidades** (1080p, 720p, …, solo audio).
2. Claude llama a **`download`** con la calidad elegida.
3. En tu teléfono **salta una notificación** y el archivo cae en la galería.

> ✅ **Vas por buen camino si:**
> - En los logs ves `POST /mcp ... 200 OK` cuando Claude llama a cada tool.
> - Te llega la **notificación** de descarga (con miniatura).
> - El video aparece en tu **galería** (álbum *Descargas/Cauce*).

Así se ve la **notificación** (tarjeta de medios):

```
┌─────────────────────────────────────┐
│ 🎬 Termux:API                        │
│ ┌─────────────────────────────────┐ │
│ │                                 │ │
│ │      [ miniatura grande ]       │ │
│ │                                 │ │
│ └─────────────────────────────────┘ │
│ I'll find you                       │
│ VXLLAIN · 1080p · Guardado en tu    │
│ galería                             │
└─────────────────────────────────────┘
```

---

## 10. Que aparezca en la galería

El servidor ya llama a **`termux-media-scan`** tras cada descarga, así que los
archivos nuevos aparecen **solos** en la Galería y el reproductor de música.

> **¿Por qué hace falta?** yt-dlp escribe el archivo "por debajo" y Android no se
> entera. La Galería lee de un índice (**MediaStore**) que solo se actualiza si
> alguien avisa. `termux-media-scan` es ese aviso.

Si tienes archivos **viejos** (de antes de este fix) que no aparecen, fuérzalos
una vez a mano:

```bash
termux-media-scan -rv ~/storage/downloads/Cauce
```

> ✅ **Vas por buen camino si** imprime `Scanning ...` por cada archivo y, al
> abrir la Galería, los videos aparecen.

---

## 11. Dejarlo trabajando en segundo plano

Para que el servidor siga vivo aunque bloquees la pantalla o uses otras apps:

**a) Activa el wake-lock** (evita que Android mate Termux por batería):
- Baja la barra de notificaciones → busca la notificación fija de **Termux** →
  toca **"ACQUIRE WAKELOCK"**.
- ✅ La notificación cambiará a `wake lock held`.

**b) Quita la optimización de batería:**
- Ajustes de Android → **Aplicaciones → Termux → Batería → "Sin restricciones"**.
- (En algunos teléfonos: apaga *"Gestionar automáticamente"* y deja encendidos
  *Inicio automático*, *Inicio secundario* y *Ejecutar en segundo plano*.)

> ⚠️ **La verdad honesta sobre "cerrar todo":** si **deslizas Termux fuera de las
> apps recientes** o le das **"Forzar detención"**, el servidor **muere** — es un
> límite de Android, no hay truco. Pero **no necesitas cerrarlo**: solo minimízalo
> (botón de inicio) y déjalo. Con el wake-lock aguanta días.
>
> 💡 Si tu teléfono ofrece **bloquear la app en recientes** (candado 🔒: mantén
> pulsada la tarjeta de Termux, o deslízala hacia abajo), bloquéala y entonces sí
> podrás usar "cerrar todo" sin matarla. No todos los teléfonos lo traen.

---

## 12. Reiniciar el servidor tras un cambio

Cuando actualices el código (`git pull`) tienes que reiniciar el servidor para
que cargue lo nuevo. Hazlo en la **sesión del servidor** (NO en la de cloudflared):

```bash
cd ~/media-mcp
git pull
pkill -f server.py        # mata TODAS las instancias (evita duplicados)
pgrep -f server.py        # no debe imprimir nada = todas cerradas
python server.py          # arranca con el código nuevo
```
Luego **`Ctrl+Z`** y **`bg`** para mandarlo a segundo plano.

> ✅ **Vas por buen camino si:** `pgrep -f server.py` no imprime nada tras el
> `pkill`, y luego ves de nuevo `Uvicorn running on http://0.0.0.0:8000`.

> 💡 **No toques la sesión de cloudflared.** Al reiniciar SOLO el servidor, el
> túnel sigue vivo y **la URL NO cambia**; Claude se reconecta solo en 2-3 s.
>
> **¿Cómo distingo las dos sesiones?** La del **servidor** muestra el prompt
> `~/media-mcp $`. La del **túnel** está llena de logs de colores con palabras
> como `tunnel`, `quic`, `argotunnel`. Cambia entre sesiones deslizando desde el
> borde izquierdo.

---

## 13. Usarlo desde varias cuentas de Claude

El mismo teléfono puede servir a **varias cuentas de Claude a la vez** (no hay
límite): solo agrega el conector en cada cuenta con la URL **terminada en `/mcp`**.

> 🔒 **Nota de seguridad:** como el servidor **no tiene login**, cualquiera que
> tenga tu URL del túnel puede usar tu teléfono para descargar. Para un proyecto
> académico suele estar bien, pero tenlo presente y no publiques la URL.

---

## 14. Pendiente: que "nunca se caiga"

Dos mejoras opcionales que rematan la resiliencia:

**a) URL fija.** El túnel `trycloudflare` cambia de URL cada vez que reinicias
`cloudflared`. Para una URL permanente:
- **Cloudflare named tunnel** (necesita cuenta de Cloudflare + un dominio):
  `cloudflared tunnel login`, `cloudflared tunnel create cauce`, y correr con el
  token del túnel. La URL queda fija para siempre.
- **Alternativa:** `tailscale funnel` (más simple si ya usas Tailscale).

**b) Auto-arranque al reiniciar el teléfono.** Con **Termux:Boot**:
- Crea `~/.termux/boot/start-cauce.sh` con: `termux-wake-lock`, arrancar el
  servidor y arrancar el túnel.
- Dale permiso de ejecución (`chmod +x`). Al reiniciar el teléfono, todo arranca
  solo.

---

## 15. Tabla de señales "vas por buen camino"

| Paso | Lo que DEBES ver |
|---|---|
| `termux-setup-storage` | Popup de Android → *Permitir* |
| `pip install ... requirements.txt` | `Successfully installed ...` sin ERROR final |
| `python server.py` | `Uvicorn running on http://0.0.0.0:8000` |
| `curl localhost:8000/api/health` | JSON con `js_engine:"deno"`, `ffmpeg:true` |
| `cloudflared tunnel --url ...` | Recuadro con la URL + `Environment is healthy` |
| Conectar en Claude | `POST /mcp ... 200 OK` + `ListToolsRequest` en los logs |
| Descargar un video | Notificación con miniatura + archivo en la galería |

---

## 16. Solución de problemas

| Síntoma | Causa probable | Solución |
|---|---|---|
| `pip` falla en `pydantic-core` bajando `rustup` | No hay wheel; rustup no soporta Android | `pkg install -y rust python-cryptography patchelf` y reintenta el pip |
| Rompiste pip | Hiciste `pip install -U pip` (prohibido) | Reinstala: `pkg reinstall python` |
| `health` da `js_engine:"none"` | Falta Deno | `pkg install -y deno` y reinicia el server |
| `health` da `ffmpeg:false` | Falta ffmpeg | `pkg install -y ffmpeg` y reinicia el server |
| Claude: "no apunta a un servidor MCP válido" | URL sin `/mcp` | Recrea el conector con `.../mcp` al final |
| Claude pide OAuth / "servicio de inicio de sesión" | Igual que arriba (falta `/mcp`) | Usa `.../mcp`; NO necesitas OAuth Client ID |
| El video no sale en la galería | Archivo viejo (antes del fix) | `termux-media-scan -rv ~/storage/downloads/Cauce` |
| El servidor se murió al cerrar Termux | Deslizaste/forzaste el cierre de la app | Reinícialo (sección 12); usa wake-lock (sección 11) |
| La URL del túnel cambió | Reiniciaste `cloudflared` | Actualiza el conector con la URL nueva, o monta URL fija (sección 14) |
| YouTube: `needs_cookies` / muro anti-bot | Estás en IP de datacenter, no del teléfono | Corre en el teléfono (esta guía). En residencial casi nunca pasa |
| Errores rojos `quic timeout` en cloudflared | Micro-corte de red móvil | Es normal, se reconecta solo (`Registered tunnel connection`) |

---

> Hecho con constancia sobre un teléfono real. Si algo de esta guía no coincide
> con lo que ves, abre un *issue* — probablemente Android/Termux cambió algo y
> conviene actualizar estas notas. 🚀
