#!/data/data/com.termux/files/usr/bin/sh
# ==========================================================================
# Cauce Soberano — auto-arranque con Termux:Boot
# --------------------------------------------------------------------------
# Copia este archivo a ~/.termux/boot/ y hazlo ejecutable:
#     mkdir -p ~/.termux/boot
#     cp ~/media-mcp/boot/start-cauce.sh ~/.termux/boot/start-cauce.sh
#     chmod +x ~/.termux/boot/start-cauce.sh
# Requiere la app "Termux:Boot" (F-Droid) instalada y ABIERTA una vez, y que
# Termux este SIN restriccion de bateria. Levanta los 3 procesos al encender.
# ==========================================================================

# Evita que Android mate Termux cuando la pantalla se apaga.
termux-wake-lock

LOGDIR="$HOME/.cauce-logs"
mkdir -p "$LOGDIR"

# 1) Proveedor de PO Token (bgutil) sobre Deno -> escucha en el puerto 4416.
( cd "$HOME/bgutil-ytdlp-pot-provider/server" && deno run -A src/main.ts ) \
    > "$LOGDIR/pot.log" 2>&1 &

sleep 3

# 2) Servidor media-mcp (Uvicorn en 0.0.0.0:8000).
( cd "$HOME/media-mcp" && python server.py ) \
    > "$LOGDIR/server.log" 2>&1 &

sleep 3

# 3) Tunel Cloudflare -> URL publica.
#    OJO: el "quick tunnel" trycloudflare CAMBIA de URL en cada arranque, asi
#    que tras un reinicio hay que actualizar el conector en claude.ai. Para una
#    URL FIJA usa un named tunnel de Cloudflare o `tailscale funnel`.
cloudflared tunnel --url http://localhost:8000 \
    > "$LOGDIR/tunnel.log" 2>&1 &
