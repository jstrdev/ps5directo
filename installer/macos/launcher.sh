#!/bin/bash
# Lanzador de "PS5 Directo.app" (va en Contents/MacOS/PS5 Directo).
#  - Python incluido para Apple Silicon (python-arm64) e Intel (python-x86_64), en Contents/Resources.
#  - El programa se copia a ~/Library/Application Support/PS5 Directo/app y se ejecuta desde ahi,
#    para que «Actualizar» pueda escribir aunque la app este en /Applications. Si la app trae una
#    version mas nueva que la copiada, se vuelve a copiar.
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
SUP="$HOME/Library/Application Support/PS5 Directo"
APP="$SUP/app"
LOG="$SUP/lanzador.log"
mkdir -p "$SUP"

case "$(uname -m)" in
  arm64) PY="$RES/python-arm64/bin/python3" ;;
  *)     PY="$RES/python-x86_64/bin/python3" ;;
esac
[ -x "$PY" ] || PY="$RES/python-x86_64/bin/python3"   # Rosetta, por si acaso

ver() { sed -n 's/^VERSION = "\([^"]*\)".*/\1/p' "$1" 2>/dev/null | head -1; }
newer() {   # 0 si $1 es mas nueva que $2
  [ -z "$2" ] && return 0
  [ "$1" = "$2" ] && return 1
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)" = "$1" ]
}
BUNDLED="$(ver "$RES/app/app.py")"
COPIED="$(ver "$APP/app.py")"
if [ ! -f "$APP/app.py" ] || newer "$BUNDLED" "$COPIED"; then
  echo "$(date) copio la version $BUNDLED (habia: ${COPIED:-ninguna})" >> "$LOG"
  rm -rf "$APP.nuevo"
  cp -R "$RES/app" "$APP.nuevo" && rm -rf "$APP" && mv "$APP.nuevo" "$APP"
fi

export PATH="$RES/bin:$PATH"   # 7-Zip incluido
export PYTHONUNBUFFERED=1
cd "$APP" || exit 1
if [ ! -x "$PY" ]; then
  osascript -e 'display alert "PS5 Directo" message "No encuentro el Python incluido en la app. Vuelve a descargarla de github.com/jstrdev/ps5directo/releases"' >/dev/null 2>&1
  exit 1
fi
mkdir -p "$HOME/.ps5directo"
# El programa sigue en segundo plano y el lanzador termina: asi cada clic en el icono vuelve a
# entrar aqui y, si ya estaba abierto, solo trae su ventana (si no, macOS creeria que la app
# sigue abierta y no haria nada).
nohup "$PY" "$APP/app.py" "$@" >> "$HOME/.ps5directo/ps5directo.log" 2>&1 < /dev/null &
exit 0
