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

    print("\n" + "=" * 60)
    print("RESULTADO:", "TODO PASA (OK)" if ok else "HAY FALLOS (FAIL)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
