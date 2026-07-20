#!/data/data/com.termux/files/usr/bin/sh
# ==========================================================================
# Cauce Soberano — arranca (o REARRANCA) los 3 procesos.
# --------------------------------------------------------------------------
# Sirve para DOS cosas:
#   1. Termux:Boot -> se ejecuta solo al encender el telefono.
#   2. A mano, si algo se rompe -> arregla TODO con un solo comando:
#        sh ~/media-mcp/boot/start-cauce.sh
#
# CONSERVA EL TUNEL. Antes este script mataba cloudflared siempre, y como el
# "quick tunnel" acuna una URL nueva en cada arranque, cada `git pull` obligaba
# a re-pegar la URL en el conector de Claude a mano. Innecesario: cloudflared
# solo apunta a localhost:8000 y le da igual que el proceso de Python detras se
# reinicie. Ahora el tunel solo se rearranca si de verdad esta caido (se
# comprueba de dos formas: proceso vivo + responde por HTTP).
#
#   * Cambiaste codigo (git pull)  ->  sh boot/reload-cauce.sh   (mas rapido)
#   * Algo se rompio / recien enciendes el telefono -> este script
#   * Quieres una URL nueva a proposito -> sh boot/start-cauce.sh --new-tunnel
#
# Instalar para auto-arranque (una vez):
#   mkdir -p ~/.termux/boot
#   cp ~/media-mcp/boot/start-cauce.sh ~/.termux/boot/start-cauce.sh
#   chmod +x ~/.termux/boot/start-cauce.sh
# ==========================================================================

termux-wake-lock

LOGDIR="$HOME/.cauce-logs"
mkdir -p "$LOGDIR"

FORCE_TUNNEL=0
[ "$1" = "--new-tunnel" ] && FORCE_TUNNEL=1

# 0) Limpia SOLO lo que hay que reiniciar. cloudflared NO se toca aqui:
#    se decide mas abajo, tras comprobar si sigue sano.
pkill -f "server.py"    2>/dev/null
pkill -f "src/main.ts"  2>/dev/null
sleep 1

# 1) Proveedor de PO Token (bgutil) sobre Deno -> puerto 4416.
( cd "$HOME/bgutil-ytdlp-pot-provider/server" && deno run -A src/main.ts ) > "$LOGDIR/pot.log" 2>&1 &
sleep 3

# 2) Servidor media-mcp (Uvicorn en 0.0.0.0:8000).
( cd "$HOME/media-mcp" && python server.py ) > "$LOGDIR/server.log" 2>&1 &
sleep 3

# --------------------------------------------------------------------------
# 3) TUNEL: reutilizar si sigue sano; rearrancar solo si hace falta.
# --------------------------------------------------------------------------
URL=$(grep -o "https://[a-z0-9-]*\.trycloudflare\.com" "$LOGDIR/tunnel.log" 2>/dev/null | tail -1)
TUNNEL_OK=0

if [ "$FORCE_TUNNEL" -eq 0 ] && pgrep -f "cloudflared" >/dev/null 2>&1 && [ -n "$URL" ]; then
    # El proceso vive y tenemos una URL. Comprobamos que de verdad responde:
    # un cloudflared zombi (proceso vivo, tunel muerto) es un caso real.
    if command -v curl >/dev/null 2>&1; then
        CODE=$(curl -s -m 10 -o /dev/null -w "%{http_code}" "$URL/api/health" 2>/dev/null)
        [ "$CODE" = "200" ] && TUNNEL_OK=1
    else
        TUNNEL_OK=1   # sin curl no podemos verificar; confiamos en el proceso
    fi
fi

if [ "$TUNNEL_OK" -eq 1 ]; then
    echo "Tunel sano: se REUTILIZA (tu URL no cambia)."
else
    [ "$FORCE_TUNNEL" -eq 1 ] && echo "Forzando un tunel NUEVO (--new-tunnel)." \
                              || echo "El tunel no responde: levantando uno nuevo."
    pkill -f "cloudflared" 2>/dev/null
    sleep 1
    cloudflared tunnel --url http://localhost:8000 > "$LOGDIR/tunnel.log" 2>&1 &

    echo "Esperando la URL del tunel..."
    URL=""
    i=0
    while [ -z "$URL" ] && [ "$i" -lt 15 ]; do
        sleep 2
        URL=$(grep -o "https://[a-z0-9-]*\.trycloudflare\.com" "$LOGDIR/tunnel.log" 2>/dev/null | head -1)
        i=$((i + 1))
    done
fi

# 4) Mostrar la URL, diciendo CLARAMENTE si hay que tocar el conector o no.
echo ""
if [ -n "$URL" ]; then
    echo "==================================================="
    if [ "$TUNNEL_OK" -eq 1 ]; then
        echo "  Tu URL de SIEMPRE (no toques el conector):"
    else
        echo "  URL NUEVA -> hay que pegarla en claude.ai / Conectores:"
    fi
    echo "     $URL/mcp"
    echo "==================================================="
else
    echo "La URL aun no aparece; revisa $LOGDIR/tunnel.log en unos segundos."
fi
echo "Logs: $LOGDIR (pot.log, server.log, tunnel.log)"
