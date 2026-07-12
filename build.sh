#!/usr/bin/env bash
set -e

# Instalar dependencias de Python
pip install -r requirements.txt

# Instalar Deno (motor de JavaScript que yt-dlp necesita para YouTube).
curl -fsSL https://deno.land/install.sh | sh
echo "Deno instalado en $HOME/.deno/bin"
