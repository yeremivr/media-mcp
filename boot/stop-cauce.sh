#!/data/data/com.termux/files/usr/bin/sh
# ==========================================================================
# Cauce Soberano — detiene los 3 procesos y suelta el wake-lock.
# Uso:  sh ~/media-mcp/boot/stop-cauce.sh
# (Para volver a levantar todo: sh ~/media-mcp/boot/start-cauce.sh)
# ==========================================================================
pkill -f "server.py"    2>/dev/null
pkill -f "cloudflared"  2>/dev/null
pkill -f "src/main.ts"  2>/dev/null
termux-wake-unlock 2>/dev/null
echo "Cauce detenido: servidor, tunel y proveedor PO Token cerrados."
