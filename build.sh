#!/usr/bin/env bash
set -e

# 1) Dependencias Python. yt-dlp[default] incluye los scripts EJS que
#    YouTube necesita para resolver su reto de JavaScript.
pip install -U -r requirements.txt

# 2) Deno = motor JavaScript que yt-dlp usa para YouTube. Se instala dentro
#    del proyecto para que persista en el runtime de Render. NO fatal: si
#    falla, TikTok/Instagram/etc. siguen funcionando (degradacion controlada).
export DENO_INSTALL="$(pwd)/.deno"
if curl -fsSL https://deno.land/install.sh | sh; then
  echo "OK: Deno en $DENO_INSTALL/bin/deno"
  "$DENO_INSTALL/bin/deno" --version || true
else
  echo "AVISO: no se pudo instalar Deno; YouTube puede degradar, el resto OK."
fi
