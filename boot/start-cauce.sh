#!/data/data/com.termux/files/usr/bin/sh
# ==========================================================================
# Cauce Soberano — arranca (o REARRANCA) los 3 procesos.
# --------------------------------------------------------------------------
# Sirve para DOS cosas:
#   1. Termux:Boot -> se ejecuta solo al encender el telefono.
#   2. A mano, si algo se rompe -> arregla TODO con un solo comando:
#        sh ~/media-mcp/boot/start-cauce.sh
# Es IDEMPOTENTE: primero MATA cualquier instancia previa y luego arranca
# limpio, asi que nunca deja procesos duplicados. Al final imprime la URL del
# tunel (que cambia en cada arranque) lista para pegar en el conector.
#
# Instalar para auto-arranque (una vez):
#   mkdir -p ~/.termux/boot
#   cp ~/media-mcp/boot/start-cauce.sh ~/.termux/boot/start-cauce.sh
#   chmod +x ~/.termux/boot/start-cauce.sh
# ==========================================================================

termux-wake-lock

# 0) Limpia instancias previas (si no hay ninguna, no pasa nada).
pkill -f "server.py"    2>/dev/null
pkill -f "cloudflared"  2>/dev/null
pkill -f "src/main.ts"  2>/dev/null
sleep 1

LOGDIR="$HOME/.cauce-logs"
mkdir -p "$LOGDIR"

# 1) Proveedor de PO Token (bgutil) sobre Deno -> puerto 4416.
( cd "$HOME/bgutil-ytdlp-pot-provider/server" && deno run -A src/main.ts ) > "$LOGDIR/pot.log" 2>&1 &
sleep 3

# 2) Servidor media-mcp (Uvicorn en 0.0.0.0:8000).
( cd "$HOME/media-mcp" && python server.py ) > "$LOGDIR/server.log" 2>&1 &
sleep 3

# 3) Tunel Cloudflare -> URL publica (el "quick tunnel" cambia de URL cada vez).
cloudflared tunnel --url http://localhost:8000 > "$LOGDIR/tunnel.log" 2>&1 &

# 4) Espera y muestra la URL del tunel, lista para el conector de Claude.
echo "Arrancando Cauce... esperando la URL del tunel."
URL=""
i=0
while [ -z "$URL" ] && [ "$i" -lt 15 ]; do
    sleep 2
    URL=$(grep -o "https://[a-z0-9-]*\.trycloudflare\.com" "$LOGDIR/tunnel.log" 2>/dev/null | head -1)
    i=$((i + 1))
done
echo ""
if [ -n "$URL" ]; then
    echo "==================================================="
    echo "  Pega esta URL en claude.ai -> Conectores:"
    echo "     $URL/mcp"
    echo "==================================================="
else
    echo "La URL aun no aparece; revisa $LOGDIR/tunnel.log en unos segundos."
fi
echo "Logs: $LOGDIR (pot.log, server.log, tunnel.log)"
