#!/bin/sh
# Abre PS5 Directo en Linux (y en Mac, si lo usas desde la carpeta).
# Usa el Python que viene incluido (carpeta python/) o, si no esta, el python3 del sistema.
# Puedes pasarle archivos: ./ps5directo.sh juego.rar
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1
# 7-Zip incluido (carpeta bin/), para "Descomprimir en el PC"
[ -d "$DIR/bin" ] && PATH="$DIR/bin:$PATH" && export PATH
if [ -x "$DIR/python/bin/python3" ]; then
  PY="$DIR/python/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  MSG="No encuentro Python 3. Instálalo (por ejemplo: sudo apt install python3) o baja la versión de PS5 Directo para Linux, que ya lo trae."
  command -v zenity >/dev/null 2>&1 && zenity --error --title="PS5 Directo" --text="$MSG" 2>/dev/null
  echo "$MSG" >&2
  exit 1
fi
export PYTHONUNBUFFERED=1
# Abierto desde el menu (sin terminal): el registro va al mismo archivo que en Windows
if [ ! -t 1 ]; then
  mkdir -p "$HOME/.ps5directo"
  exec "$PY" "$DIR/app.py" "$@" >> "$HOME/.ps5directo/ps5directo.log" 2>&1
fi
exec "$PY" "$DIR/app.py" "$@"
