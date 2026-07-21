# Arreglo de dos bugs de detección de medios en Facebook

Ambos bugs eran **solo de Facebook**. Instagram, LinkedIn, Pinterest y los
videos reales (IG/FB/YouTube) **no cambian de comportamiento** — está cubierto
con tests de regresión.

Caso de referencia: álbum "Tomorrowland 2011" (página Rudgr.com), ~12 fotos +
música de fondo que Facebook le pega al post (*Avicii – Levels*).

---

## BUG A — el "video fantasma" era la música de fondo

**Síntoma:** el post salía como `media_type: "video"` sin haber ningún video
real; el "video" (`cauce-v-0`) se descargaba pero nunca aparecía nada
reproducible (era la canción).

**Causa:** Facebook publica la música en `story_media_metadata.audio_url`,
servida desde `video.fbcdn.net` con path de video (`.mp4`). La detección de
audio en `score_candidate()` miraba **solo la URL**, así que disparaba las
señales de VIDEO (`vext`, `vmime`) y ninguna de audio → se clasificaba como
video con score 94, le ganaba a las fotos (76) y marcaba el post como "video".

**Cambios (`resolver.py`):**

1. `score_candidate()` — nueva señal `is_bg_audio`, que mira el **nombre** de la
   clave/ancestros, no la URL: `"audio" in key_l` (p. ej. `audio_url`) o
   `"story_media_metadata"` en los ancestros. Si es música de fondo se fuerza
   `is_video=False`, `kind="audio"` (nunca puede volverse "video", ni por una
   hermana con mime de video). Se añade el rasgo `bgaudio` a los diagnostics.
   *Un video real trae su stream por `browser_native_*`/`playable_url`, jamás
   por `audio_url`, así que esta señal no toca a los videos de verdad.*

2. Decisión de `media_type` — ahora `media_type="video"` **solo si existe un
   candidato `kind=="video"` real** (`has_real_video`), no si lo único que hay
   es audio. Sin video real: 2+ fotos = `carousel`, 1 = `image`, solo música =
   `audio`. La pista de música sigue disponible como formato `kind="audio"`
   opcional, pero no se ofrece como el medio principal.

3. `drop_video_posters()` — se le pasan **solo los videos reales**, no el audio.
   La música no tiene carátula, y colarla como "video" hacía que la red de
   seguridad por-ancla borrara la 1.ª foto del álbum (el `og:image`) creyéndola
   la portada del video → el álbum de 4 salía con 3. Corregido de paso.

4. La confianza de un álbum se calcula desde las **fotos** aunque haya una pista
   de música (`has_real_video` en vez de `media`).

---

## BUG B — solo detectaba 4 de ~12 fotos del álbum

**Causa:** a un crawler anónimo, Facebook solo incrusta las ~4 fotos del preview
en `all_subattachments.nodes`; el resto vive tras la sesión. El resolver no
mandaba cookies ni paginaba, así que nunca veía más de 4.

### Nivel 1 — arreglo honesto e inmediato (sin sesión)

- Nueva `_detect_album_total()` lee el **conteo real** del álbum
  (`all_subattachments.count`, y variantes). Es **conservador**: solo cuenta
  contenedores cuyo nombre menciona *subattachment* (lo de Facebook), así que
  Instagram —que usa `edge_sidecar_to_children` y ya entrega la lista completa—
  **no se toca**.
- Nuevo campo `ResolveResult.images_available` (y `_cauce_images_available` /
  `images_available` en el server + diagnostics).
- El server ahora avisa **"N de M" con la M correcta**: en vez de "4 de 4"
  (mentira), dice *"4 de 12 · faltan 8 (requieren iniciar sesión en Facebook)"*.

### Nivel 2 — arreglo completo SIN cookies (ataque directo al álbum) ⭐

**Este es el camino que resolvió el caso, sin iniciar sesión.** Descubierto por
ingeniería inversa con reconocimiento en vivo desde el teléfono (`fb_recon.py`):

- El link que compartes (`/share/p/...`) y el post dan **muro de login** en
  todas las puertas... **pero filtran en el JSON el ID del álbum** (`set=a.NNN`)
  y el conteo real, aunque no muestren las fotos.
- La **URL directa del álbum** — `https://www.facebook.com/media/set/?set=a.NNN`
  — es superficie pública indexable, y **Facebook SÍ se la sirve a Googlebot**.
  Medido en vivo: devuelve el álbum **completo (11/11 fotos)** sin cookies.

Implementación (`resolver.py`):
1. `_detect_album_set()` cosecha `set=a.NNN`/`set=pcb.NNN` del HTML crudo (se
   filtra incluso en páginas-muro). Se guarda en `ResolveResult.album_set`.
2. `_expand_fb_album()` en `resolve()`: **solo si sabemos que faltan fotos**
   (`images_available > vistas`, o muro con < 2 fotos) y hay `album_set`, pide
   `/media/set/?set=a.NNN` a Googlebot, corre el mismo motor sobre esa página y
   **adopta el juego completo de fotos**. Un post normal no paga ni un fetch de
   más. Degrada limpio: si falla, se queda con lo que tenía.

Resultado: el álbum de Tomorrowland (11 fotos tras muro) ahora se resuelve
**completo, sin cookies, sin configurar nada**.

> `fb_recon.py` queda en el repo como instrumento: corre en el teléfono, mide
> qué puerta pública trae más fotos, y da un veredicto. Útil si Facebook cambia
> y hay que volver a hacer recon.

### Nivel 3 — respaldo con sesión de Facebook (cookies, opcional)

`resolver.py` ahora puede mandar el header `Cookie` en `_http_get` y
`_http_get_image`, tomando la sesión de un `cookies.txt` de Facebook.

**Por qué cookies + UA de navegador y NO seguir el cursor GraphQL:** emitir el
request paginado exige `doc_id`/`fb_dtsg`/`variables` que Facebook rota sin
aviso — justo la fragilidad de extractor-a-mano que este motor existe para
evitar. Con la sesión activa y un UA de navegador real (que el perfil de
Facebook ya prueba primero), Facebook **server-renderiza el álbum completo** en
la MISMA estructura `all_subattachments` que el walker ya entiende → el motor ve
las 12 fotos sin una línea de parsing nueva. Reutiliza el grafo, el scoring y la
deduplicación existentes.

**Degradación limpia:** si no hay cookies de Facebook configuradas, cae solo al
Nivel 1 (baja las 4 que ve + avisa). Cualquier fallo al leer/parsear el
cookies.txt se traga silenciosamente y sigue como crawler anónimo. Nunca crashea.

---

## Cómo configuro el cookies.txt de Facebook (activar el Nivel 2)

1. Exporta tus cookies de Facebook a un archivo en **formato Netscape**
   (`cookies.txt`) — el mismo formato que ya usas para YouTube. Cualquier
   extensión "Get cookies.txt" del navegador sirve; hazlo con la sesión de
   `facebook.com` iniciada.

2. Coloca el archivo en **una** de estas ubicaciones (se buscan en este orden):

   | Prioridad | Fuente |
   |-----------|--------|
   | 1 | Variable de entorno `FB_COOKIES_FILE` apuntando al archivo |
   | 2 | Variable `COOKIES_FILE` (o `YT_COOKIES_FILE`) — reusa el mismo cookies.txt |
   | 3 | `/etc/secrets/fb_cookies.txt` o `/etc/secrets/cookies.txt` |
   | 4 | `fb_cookies.txt` o `cookies.txt` junto a `resolver.py` |

   Ejemplo (Termux/servidor):
   ```bash
   export FB_COOKIES_FILE=/ruta/a/mis_cookies_facebook.txt
   ```

3. Reinicia el server (o llama a `resolver.reset_fb_cookie_cache()` para
   releer en caliente). Listo: los álbumes se bajan completos.

> Solo se envían cookies a dominios de Facebook (`facebook.com`, `fbcdn.net`,
> `fbsbx.com`, `fb.watch`, `fb.com`). A Instagram y a cualquier otro sitio
> **nunca** se les manda la sesión de Facebook.

---

## Tests

- Suite existente sigue verde: `test_resolver.py`, `test_carousel.py`,
  `test_server_integration.py` (incluye `selftest()` y `selftest_carousel()`).
- Nuevo `test_facebook_bugs.py`:
  - **Bug A:** álbum con `story_media_metadata.audio_url` + 4 fotos →
    `media_type=="carousel"` y ningún formato de video sale del `audio_url`.
  - **Bug A (regresión):** reel real de FB → `media_type=="video"`; sidecar de
    IG → `carousel` de 3, sin fantasmas.
  - **Bug B Nivel 1:** álbum con `count=12` y 4 incrustadas →
    `images_available==12`; sin `count`, cae a lo visto; IG sin cambios.
  - **Bug B Nivel 2:** con cookies mockeadas, `resolve()` ve las 12; sin
    cookies, baja 4 pero reporta 12; y el mecanismo (`_http_get` inyecta el
    header `Cookie` solo a Facebook).

```bash
python test_resolver.py && python test_carousel.py && \
python test_server_integration.py && python test_facebook_bugs.py
```
