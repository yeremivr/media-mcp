# -*- coding: utf-8 -*-
"""
test_server_integration.py — prueba la INTEGRACION del resolver en server.py
sin necesitar yt-dlp/mcp/starlette instalados (los stubbeamos). Verifica el
cableado real:
  1. _curate_resolver: convierte info del resolver al esquema de list_formats
     (muxed, sin +bestaudio, etiquetas por altura, format_id cauce-v-/cauce-a-).
  2. _extract_info: para no-YouTube, si yt-dlp falla cae al resolver.
  3. _do_download_resolved: re-resuelve y elige la calidad pedida por altura,
     enriquece con titulo/miniatura del resolver, baja la URL DIRECTA.
"""
import sys
import types

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# --------------------------------------------------------------------------
# Stubs de los modulos pesados que server.py importa (no los tenemos aqui).
# --------------------------------------------------------------------------
def _install_stubs(fake_ydl_factory=None, resolve_raises=False):
    # yt_dlp
    ydl = types.ModuleType("yt_dlp")
    ver = types.ModuleType("yt_dlp.version")
    ver.__version__ = "0.0-test"
    ydl.version = ver

    class _FakeYDL:
        last_opts = None
        last_url = None

        def __init__(self, opts):
            _FakeYDL.last_opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            _FakeYDL.last_url = url
            if fake_ydl_factory:
                return fake_ydl_factory(url, download)
            raise RuntimeError("Unsupported URL: no extractor")

        def prepare_filename(self, info):
            return "/tmp/out.mp4"

    ydl.YoutubeDL = _FakeYDL
    sys.modules["yt_dlp"] = ydl

    # mcp.server.fastmcp con FastMCP + Image
    fastmcp = types.ModuleType("mcp.server.fastmcp")

    class _FastMCP:
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            return lambda fn: fn

        def custom_route(self, *a, **k):
            return lambda fn: fn

        def streamable_http_app(self):
            return None

    class _Image:
        def __init__(self, data=None, format=None):
            self.data = data
            self.format = format

    fastmcp.FastMCP = _FastMCP
    fastmcp.Image = _Image
    mcp_pkg = types.ModuleType("mcp")
    mcp_server = types.ModuleType("mcp.server")
    sys.modules["mcp"] = mcp_pkg
    sys.modules["mcp.server"] = mcp_server
    sys.modules["mcp.server.fastmcp"] = fastmcp

    # starlette.responses
    st = types.ModuleType("starlette")
    str_ = types.ModuleType("starlette.responses")
    for name in ("JSONResponse", "FileResponse", "Response"):
        setattr(str_, name, type(name, (), {"__init__": lambda self, *a, **k: None}))
    st.responses = str_
    sys.modules["starlette"] = st
    sys.modules["starlette.responses"] = str_

    # auto_updater e icons
    au = types.ModuleType("auto_updater")
    au.run_forever = lambda: None
    sys.modules["auto_updater"] = au
    ic = types.ModuleType("icons")
    ic.ICON_192_B64 = ic.ICON_512_B64 = ""
    sys.modules["icons"] = ic

    return _FakeYDL


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    return bool(cond)


def main():
    ok = True
    _install_stubs()

    # importar server DESPUES de instalar stubs
    import importlib
    if "server" in sys.modules:
        del sys.modules["server"]
    import server
    import resolver

    # ---- 1. _curate_resolver ----
    print("\n=== 1. _curate_resolver (esquema list_formats, muxed) ===")
    fake_info = {
        "title": "Charla grafos", "uploader": "Ada", "duration": 60,
        "thumbnail": "https://media.licdn.com/x.jpg",
        "_cauce_resolver": True, "_cauce_confidence": 0.85, "_cauce_strategy": "linkedin:googlebot:original",
        "formats": [
            {"format_id": "cauce0-720p", "height": 720, "tbr": 2400, "ext": "mp4", "_cauce_muxed": True},
            {"format_id": "cauce1-360p", "height": 360, "tbr": 600, "ext": "mp4", "_cauce_muxed": True},
        ],
    }
    cur = server._curate_resolver(fake_info)
    print("   ", cur["formats"])
    ok &= check("resolver=True y confidence propagada", cur.get("resolver") and cur.get("confidence") == 0.85)
    ok &= check("2 formatos video, ordenados 720 antes que 360",
                [f["format_id"] for f in cur["formats"]] == ["cauce-v-720", "cauce-v-360"])
    ok &= check("NINGUN format_id lleva +bestaudio (son muxed)",
                all("+" not in f["format_id"] for f in cur["formats"]))
    ok &= check("estima tamano con tbr*duracion (720p ~18MB)",
                17 <= cur["formats"][0]["filesize_mb"] <= 19)
    ok &= check("etiquetas '720p (mp4)'", cur["formats"][0]["label"] == "720p (mp4)")

    # ---- 2. _extract_info cae al resolver cuando yt-dlp falla ----
    print("\n=== 2. _extract_info: no-YouTube + yt-dlp falla -> resolver ===")
    called = {"resolve": 0}

    class _FakeRes:
        ok = True
        title = "Reel FB"; uploader = "Autor"; thumbnail = "https://scontent.fbcdn.net/t.jpg"
        duration = 34; confidence = 0.87; strategy = "facebook:browser:original"; reason = ""; url = "u"
        diagnostics = {}
        formats = []

        def to_info(self):
            return {"_cauce_resolver": True, "title": self.title, "formats": [
                {"format_id": "cauce-x", "url": "https://video.xx.fbcdn.net/hd.mp4",
                 "height": 1280, "vcodec": "unknown", "acodec": "unknown", "_cauce_muxed": True}]}

    def fake_resolve(url):
        called["resolve"] += 1
        return _FakeRes()

    resolver.resolve = fake_resolve  # el server llama _resolver.resolve
    info = server._extract_info("https://www.facebook.com/reel/123")
    ok &= check("cae al resolver y marca _cauce_resolver", info.get("_cauce_resolver") is True)
    ok &= check("uso el resolver exactamente 1 vez", called["resolve"] == 1)
    ok &= check("_has_real_formats acepta el formato muxed del resolver",
                server._has_real_formats(info))

    # y para YouTube NO debe tocar el resolver
    print("\n=== 2b. YouTube NO usa el resolver ===")
    called["resolve"] = 0
    try:
        server._extract_info("https://www.youtube.com/watch?v=abc")
    except Exception:
        pass
    ok &= check("con YouTube el resolver NO se llamo", called["resolve"] == 0)

    # ---- 3. _do_download_resolved: re-resuelve y elige por altura ----
    print("\n=== 3. _do_download_resolved: elige calidad por altura + enriquece ===")

    class _F:
        def __init__(self, url, kind, height):
            self.url, self.kind, self.height = url, kind, height

    class _FakeRes2:
        ok = True
        title = "Mi Reel"; uploader = "Creador"; thumbnail = "https://cdn/thumb.jpg"; reason = ""
        formats = [
            _F("https://video.xx.fbcdn.net/HD_1080.mp4?sig=a", "video", 1080),
            _F("https://video.xx.fbcdn.net/SD_360.mp4?sig=b", "video", 360),
        ]

    resolver.resolve = lambda url: _FakeRes2()

    # yt-dlp "descarga" la URL directa: devolvemos info con el filepath.
    def fake_download(url, download=False):
        return {"title": "pelado", "requested_downloads": [{"filepath": "/tmp/" + url.split("/")[-1]}]}

    # reinstalar stubs con el downloader que responde
    _install_stubs(fake_ydl_factory=fake_download)
    del sys.modules["server"]
    import server as server2
    server2._resolver = resolver
    resolver.resolve = lambda url: _FakeRes2()

    di, filepath = server2._do_download_resolved("job1", "https://fb.com/reel/1", "cauce-v-360")
    print("    elegido:", server2.yt_dlp.YoutubeDL.last_url, "| filepath:", filepath)
    ok &= check("pidio 360p -> bajo la URL directa SD_360 (no la HD)",
                "SD_360" in server2.yt_dlp.YoutubeDL.last_url)
    ok &= check("paso Referer del sitio original a yt-dlp",
                server2.yt_dlp.YoutubeDL.last_opts.get("http_headers", {}).get("Referer") == "https://fb.com/reel/1")
    ok &= check("enriquecio titulo/miniatura/uploader desde el resolver",
                di["title"] == "Mi Reel" and di["thumbnail"] == "https://cdn/thumb.jpg" and di["uploader"] == "Creador")
    ok &= check("fijo height del formato elegido (360)", di.get("height") == 360)

    di2, _ = server2._do_download_resolved("job2", "https://fb.com/reel/1", "cauce-v-1080")
    ok &= check("pidio 1080p -> bajo la HD_1080", "HD_1080" in server2.yt_dlp.YoutubeDL.last_url)

    print("\n=== MEMORIA CORTA: no bajar dos veces la misma pagina ===")
    # `grab` con fotos resolvia el enlace DOS veces con segundos de
    # diferencia (una para decidir que es, otra dentro del worker de
    # descarga), y `preview_image` una vez POR FOTO. Son descargas completas
    # de la misma pagina: el coste esta en la RED, no en el scoring.
    calls = {"n": 0}

    class _FakeRes:
        ok = True
        strategy = "x:browser:original"

    def fake_resolve(u):
        calls["n"] += 1
        return _FakeRes()

    real_resolve = server2._resolver.resolve
    server2._resolver.resolve = fake_resolve
    server2._RESOLVE_CACHE.clear()

    a = server2._resolve_cached("https://sitio.com/post/1")
    b = server2._resolve_cached("https://sitio.com/post/1")
    ok &= check("dos llamadas seguidas = UNA sola bajada de pagina",
                calls["n"] == 1 and a is b)

    server2._resolve_cached("https://sitio.com/post/2")
    ok &= check("otra URL si vuelve a resolver (no confunde enlaces)",
                calls["n"] == 2)

    server2._resolve_cached("https://sitio.com/post/1", fresh=True)
    ok &= check("fresh=True IGNORA el cache (lo usa la descarga de video, "
                "que consume la URL firmada entera)", calls["n"] == 3)

    # Caducidad: envejecemos la entrada a mano en vez de dormir el test.
    ts, val = server2._RESOLVE_CACHE["https://sitio.com/post/2"]
    server2._RESOLVE_CACHE["https://sitio.com/post/2"] = (
        ts - server2._RESOLVE_TTL - 1, val)
    server2._resolve_cached("https://sitio.com/post/2")
    ok &= check("pasado el TTL vuelve a resolver (las URLs firmadas caducan)",
                calls["n"] == 4)

    for i in range(server2._RESOLVE_CACHE_MAX + 5):
        server2._resolve_cached(f"https://sitio.com/otro/{i}")
    ok &= check("el cache tiene tope (no crece sin limite en un server que "
                "vive semanas)",
                len(server2._RESOLVE_CACHE) <= server2._RESOLVE_CACHE_MAX)
    server2._resolver.resolve = real_resolve

    print("\n=== CARRUSEL DE VIDEOS: uno por ELEMENTO, no uno en total ===")

    # Un post de Instagram con 5 videos los servia TODOS con altura
    # desconocida (h=0). `_curate_resolver` deduplicaba por altura, asi que se
    # quedaba con UNO y tiraba cuatro EN SILENCIO. Caminos REALES capturados.
    RZ = ("require/#0/#3/#0/__bbox/require/#7/#3/#1/__bbox/result/data/"
          "xig_polaris_media")

    def _vp(i, r=0):
        return (f"{RZ}/if_not_gated_logged_out/carousel_media/#{i}"
                f"/video_versions/#{r}/url")

    fmts = [{"_cauce_muxed": True, "height": None, "_cauce_path": _vp(i)}
            for i in (0, 2, 3, 4, 5)]
    fmts.append({"_cauce_muxed": True, "height": None,
                 "_cauce_path": _vp(0, 1)})        # otra rendition del #0
    cur = server2._curate_resolver({"formats": fmts})
    vids = [f for f in cur["formats"] if f["kind"] == "video"]
    ok &= check("los 5 videos del carrusel sobreviven (antes quedaba 1)",
                len(vids) == 5)
    ok &= check("las 2 renditions del mismo video NO cuentan como dos",
                len({v["format_id"] for v in vids}) == 5)
    ok &= check("cada uno lleva su numero de elemento en el format_id",
                any(v["format_id"].startswith("cauce-v1-") for v in vids))
    ok &= check("y la etiqueta se lo dice al usuario",
                any("de 5" in v["label"] for v in vids))

    # Un solo video en varias calidades: los ids de SIEMPRE, sin numero.
    uno = server2._curate_resolver({"formats": [
        {"_cauce_muxed": True, "height": 1080, "_cauce_path": _vp(0)},
        {"_cauce_muxed": True, "height": 360, "_cauce_path": _vp(0, 1)},
    ]})
    ok &= check("un solo video conserva los ids de siempre",
                {f["format_id"] for f in uno["formats"]}
                == {"cauce-v-1080", "cauce-v-360"})

    # REGRESION REAL (LinkedIn): un post con UN video en dos calidades dejaba
    # DOS mp4 en la galeria, mas la pista de subtitulos como "unknown_video".
    # `grab` lanzaba un trabajo por FORMATO cuando debe lanzarlo por ELEMENTO.
    li = server2._curate_resolver({"formats": [
        {"_cauce_muxed": True, "height": 720, "ext": "mp4"},
        {"_cauce_muxed": True, "height": 640, "ext": "mp4"},
        {"_cauce_muxed": True, "height": None, "ext": "vtt"},   # subtitulos
    ]})
    ok &= check("los subtitulos no entran como video",
                all(f.get("format_id") != "cauce-v-0" for f in li["formats"]))

    def _trabajos(curado):
        """Lo que `grab` lanzaria: el mejor formato de CADA elemento."""
        mejor = {}
        for f in curado["formats"]:
            if f["kind"] != "video":
                continue
            p = str(f["format_id"]).split("-")
            e = (p[1][1:] if len(p) >= 3 and p[1][:1] == "v"
                 and p[1][1:].isdigit() else "0")
            mejor.setdefault(e, f)
        return list(mejor.values())

    t_li = _trabajos(li)
    ok &= check("un video en 2 calidades = UN trabajo (no dos)", len(t_li) == 1)
    ok &= check("y se queda con la mejor calidad",
                t_li and t_li[0]["format_id"] == "cauce-v-720")
    ok &= check("pero un carrusel de 5 videos sigue lanzando 5",
                len(_trabajos(cur)) == 5)

    # ---- 4. EXTRACCION PARALELA: yt-dlp y el resolver corren A LA VEZ ----
    # Antes era en SERIE (yt-dlp entero -> si no sirve, resolver entero = SUMA
    # de dos viajes de red). Ahora se solapan y se paga el MAXIMO. El contrato
    # observable no cambia (yt-dlp gana si da formatos; si no, el resolver), y
    # ademas un resolver especulativo que reviente jamas debe tumbar la extraccion.
    print("\n=== EXTRACCION PARALELA: yt-dlp gana; resolver especulativo que peta no estorba ===")

    def ydl_real(url, download):
        if "private" in url:
            raise RuntimeError("ERROR: Video unavailable: this content is not available")
        return {"title": "Reel", "formats": [
            {"format_id": "hd", "vcodec": "h264", "acodec": "aac", "height": 1080}]}

    _install_stubs(fake_ydl_factory=ydl_real)
    del sys.modules["server"]
    import server as server3
    server3._resolver = resolver

    # (a) yt-dlp da formatos -> gana al instante. El resolver corre en paralelo
    #     y aqui REVIENTA a proposito: no debe tumbar lo que yt-dlp resolvio.
    def _boom(u):
        raise RuntimeError("el resolver especulativo peto a proposito")
    resolver.resolve = _boom
    info3 = server3._extract_info("https://www.instagram.com/reel/XYZ/")
    ok &= check("yt-dlp con formatos reales gana (no cae al resolver)",
                not info3.get("_cauce_resolver") and server3._has_real_formats(info3))
    ok &= check("un resolver especulativo que PETA no tumba la extraccion",
                info3.get("title") == "Reel")

    # (b) error DURO (privado/borrado): se propaga y el resolver NO lo enmascara,
    #     aunque tuviera algo que ofrecer (no se reintenta lo que no tiene arreglo).
    class _ResConAlgo:
        ok = True
        def to_info(self):
            return {"_cauce_resolver": True, "title": "no deberia verse"}
    resolver.resolve = lambda u: _ResConAlgo()
    duro = False
    try:
        server3._extract_info("https://www.instagram.com/reel/private/1/")
    except Exception as e:
        duro = "unavailable" in str(e).lower()
    ok &= check("un error DURO se propaga; el resolver no lo enmascara", duro)

    # ---- 5. FOTOS EN PARALELO: orden de carrusel preservado, fallos aislados ----
    # Las fotos eran GET en fila (N viajes). Ahora salen en paralelo, pero el
    # nombre lleva el indice y se escriben en orden -> la galeria las ordena
    # igual que el carrusel, y una foto que falle en el CDN no arrastra al resto.
    print("\n=== FOTOS EN PARALELO: orden de carrusel preservado, fallos aislados ===")
    import re as _re
    import tempfile as _tmp
    import pathlib as _pl
    d = _pl.Path(_tmp.mkdtemp())
    server3.DOWNLOAD_DIR = d
    server3.JOBS_INDEX = d / "jobs.json"
    server3._resolver = resolver

    class _Img:
        def __init__(self, u):
            self.url = u

    class _ResImgs:
        ok = True
        title = "Album"; uploader = "Autor"; thumbnail = "t"; reason = ""
        strategy = "instagram:googlebot:original"
        images = [_Img(f"https://cdn/img{i}.jpg") for i in range(1, 6)]   # 5 fotos

    server3._RESOLVE_CACHE.clear()
    resolver.resolve = lambda u: _ResImgs()

    JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32                 # magic bytes JPEG

    def _fake_fetch(url, referer=None, prefer_gate=None, **k):
        if "img3.jpg" in url:                                 # la 3 "falla" en el CDN
            return None
        return (JPEG, "image/jpeg")

    resolver.fetch_image_bytes = _fake_fetch
    job = {"job_id": "imgs01", "url": "https://www.instagram.com/p/ABC/", "which": "all"}
    server3._download_images_worker(job)
    fin = server3._get_job("imgs01") or {}
    ok &= check("bajo 4 de 5 fotos (la 3 fallo en el CDN)", fin.get("downloaded") == 4)
    ok &= check("la foto que fallo queda aislada, no rompe el lote",
                fin.get("failed") == [3])
    names = [_pl.Path(f).name for f in (fin.get("files") or [])]
    idxs = [int(_re.search(r"_(\d\d)_", n).group(1)) for n in names]
    ok &= check("los ficheros conservan el ORDEN del carrusel (1,2,4,5)",
                idxs == [1, 2, 4, 5])

    # ---- 6. COLA DE PRIORIDAD (min-heap): orden de ejecucion ----
    # Antes: un hilo por job + semaforo -> el orden lo decidia el planeador del
    # SO. Ahora un min-heap ordena por (tier, tamano, secuencia): fotos primero
    # (tier 0, aparecen ya), luego videos del mas liviano al mas pesado (SJF),
    # y FIFO para desempatar. Se congela el 'notify' del pool para que enqueue
    # empuje al heap sin que los workers roben items -> pop determinista.
    print("\n=== COLA DE PRIORIDAD (min-heap): fotos primero, videos por tamano (SJF) ===")
    server3._DL_HEAP.clear()
    server3._DL_SEQ = 0
    server3._DL_CV.notify = lambda *a, **k: None   # congela el pool durante la prueba
    try:
        # Llegan DESORDENADOS: video grande, foto1, video chico, video medio, foto2.
        _, t_big = server3._submit_video("u", "big", size_mb=200)
        _, t_ph1 = server3._submit_images("u", "all")
        _, t_sm = server3._submit_video("u", "small", size_mb=5)
        _, t_med = server3._submit_video("u", "med", size_mb=50)
        _, t_ph2 = server3._submit_images("u2", "all")
        popped = [server3._dl_pop() for _ in range(5)]
    finally:
        del server3._DL_CV.notify                  # restaura el pool real
    ok &= check("las 2 FOTOS salen primero (tier 0), en orden de llegada (FIFO)",
                popped[0] is t_ph1 and popped[1] is t_ph2)
    ok &= check("luego los VIDEOS por tamano ascendente (SJF): small<med<big",
                popped[2] is t_sm and popped[3] is t_med and popped[4] is t_big)
    ok &= check("un video de tamano DESCONOCIDO cae en el medio (ni 1o ni ultimo)",
                0 < server3._DL_UNKNOWN_MB < 200)

    # ---- 7. CONTRATO DE download() intacto: el Event lo resuelve el pool ----
    # download() ya no lanza su propio hilo: encola y espera el Event que el
    # worker levanta al terminar. Debe seguir devolviendo 'done' si el trabajo
    # termina dentro de la ventana, y 'downloading' si se pasa (sigue en el pool).
    print("\n=== CONTRATO download(): 'done' rapido, 'downloading' si tarda (via el pool) ===")
    import time as _t

    def _fast_done(job):
        job.update({"status": "done", "title": "OK"})
        server3._upsert_job(job)

    server3._download_worker = _fast_done
    server3.DOWNLOAD_WAIT_SECONDS = 3.0
    r = server3.download("https://x/v", "137+bestaudio/best")
    ok &= check("download() responde 'done' cuando el worker termina a tiempo",
                r.get("status") == "done" and r.get("ok") is True)

    def _slow(job):
        _t.sleep(1.0)                              # mas que la ventana de espera
        job.update({"status": "done"})
        server3._upsert_job(job)

    server3._download_worker = _slow
    server3.DOWNLOAD_WAIT_SECONDS = 0.3
    r2 = server3.download("https://x/v2", "137+bestaudio/best")
    ok &= check("download() responde 'downloading' si excede la ventana (sigue en el pool)",
                r2.get("status") == "downloading")

    # ---- 8. MENSAJE DE ERROR CONSCIENTE DE LA PLATAFORMA ----
    # Bug real capturado en vivo: un reel de Facebook que fallaba mostraba
    # "YouTube pidio verificacion anti-bot" + needs_cookies (por el default
    # 'botwall' de _classify_error, cuyo mensaje asumia YouTube SIEMPRE). El
    # mensaje ahora depende del dominio del enlace.
    print("\n=== MENSAJE DE ERROR SEGUN LA PLATAFORMA (el bug del 'YouTube' en Facebook) ===")
    err = RuntimeError("Cannot parse data from the page")   # desconocido -> 'botwall'
    fb = server3._friendly_error(err, "https://www.facebook.com/share/r/193ijQfq6C/")
    ok &= check("un fallo de Facebook NO menciona YouTube",
                "youtube" not in (fb.get("error") or "").lower())
    ok &= check("un fallo de Facebook NO pide cookies de YouTube",
                not fb.get("needs_cookies"))
    ok &= check("y nombra el dominio REAL (facebook.com)",
                "facebook.com" in (fb.get("error") or ""))
    yt = server3._friendly_error(err, "https://www.youtube.com/watch?v=abc")
    ok &= check("el MISMO error en YouTube SI habla de YouTube/anti-bot + cookies",
                "youtube" in (yt.get("error") or "").lower() and yt.get("needs_cookies") is True)
    hard = server3._friendly_error(RuntimeError("Private video"),
                                   "https://www.facebook.com/x")
    ok &= check("un error DURO (privado/borrado) sigue con su mensaje generico",
                "privado" in (hard.get("error") or "").lower())

    print("\n" + "=" * 60)
    print("RESULTADO:", "TODO PASA (OK)" if ok else "HAY FALLOS (FAIL)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
