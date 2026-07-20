#!/data/data/com.termux/files/usr/bin/sh
# ==========================================================================
# Cauce — RECARGA el codigo SIN tocar el tunel.
# --------------------------------------------------------------------------
# Este es el comando del dia a dia: el que usas despues de un `git pull`.
#
#     sh ~/media-mcp/boot/reload-cauce.sh
#
# POR QUE EXISTE: la URL publica (`*.trycloudflare.com`) la genera cloudflared
# al arrancar, y es distinta cada vez. Pero cloudflared solo apunta a
# localhost:8000 — le da exactamente igual que el proceso que escucha ahi se
# reinicie. Es decir: reiniciar `server.py` NO cambia la URL; reiniciar
# `cloudflared` SI. `start-cauce.sh` mataba los tres procesos siempre, asi que
# cada cambio de codigo costaba una URL nueva y re-configurar el conector de
# Claude a mano. No hacia falta.
#
# Este script reinicia SOLO el servidor de Python (que es lo unico que cambia
# con un `git pull`) y deja intactos el tunel y el proveedor de PO Token.
# Resultado: pull -> reload -> sigues trabajando. La URL no se mueve.
#
# Si algun dia el tunel SI se cayo, usa `start-cauce.sh` (que lo revisa y lo
# levanta solo si hace falta).
# ==========================================================================

LOGDIR="$HOME/.cauce-logs"
mkdir -p "$LOGDIR"

termux-wake-lock 2>/dev/null

echo "Recargando el servidor (el tunel NO se toca)..."

# 1) Baja SOLO el server de Python. El tunel y el proveedor siguen vivos.
pkill -f "server.py" 2>/dev/null
sleep 1

# 2) Vuelve a levantarlo con el codigo nuevo.
( cd "$HOME/media-mcp" && python server.py ) > "$LOGDIR/server.log" 2>&1 &
sleep 4

# 3) Comprobar que responde en local (no por el tunel: mas rapido y directo).
OK=0
i=0
while [ "$OK" -eq 0 ] && [ "$i" -lt 10 ]; do
    if command -v curl >/dev/null 2>&1; then
        CODE=$(curl -s -m 5 -o /dev/null -w "%{http_code}" http://localhost:8000/api/health 2>/dev/null)
        [ "$CODE" = "200" ] && OK=1
    else
        pgrep -f "server.py" >/dev/null 2>&1 && OK=1
    fi
    [ "$OK" -eq 0 ] && sleep 2
    i=$((i + 1))
done

echo ""
if [ "$OK" -eq 1 ]; then
    echo "  Servidor recargado y respondiendo."
else
    echo "  El servidor NO responde todavia. Mira: $LOGDIR/server.log"
    echo "  Si no levanta, corre: sh ~/media-mcp/boot/start-cauce.sh"
fi

# 4) Recordarte cual es TU URL (la de siempre: no ha cambiado).
URL=$(grep -o "https://[a-z0-9-]*\.trycloudflare\.com" "$LOGDIR/tunnel.log" 2>/dev/null | tail -1)
if [ -n "$URL" ]; then
    echo ""
    echo "  Tu URL sigue siendo la MISMA (no hay que tocar el conector):"
    echo "     $URL/mcp"
else
    echo ""
    echo "  No encuentro una URL de tunel en el log."
    echo "  Puede que cloudflared no este corriendo: sh ~/media-mcp/boot/start-cauce.sh"
fi
