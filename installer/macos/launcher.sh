#!/bin/bash
# Lanzador de "PS5 Directo.app" (va en Contents/MacOS/PS5 Directo).
#  - Python incluido para Apple Silicon (python-arm64) e Intel (python-x86_64), en Contents/Resources.
#  - El programa se copia a ~/Library/Application Support/PS5 Directo/app y se ejecuta desde ahi,
#    para que «Actualizar» pueda escribir aunque la app este en /Applications. Si la app trae una
#    version mas nueva que la copiada, se vuelve a copiar.
#  - Si algo impide arrancar, muestra un aviso con el error (en vez de no hacer nada).
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
BUNDLE="$(cd "$RES/../.." && pwd)"
SUP="$HOME/Library/Application Support/PS5 Directo"
APP="$SUP/app"
LOG="$SUP/lanzador.log"
PLOG="$HOME/.ps5directo/ps5directo.log"
mkdir -p "$SUP" "$HOME/.ps5directo"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

# Aviso en pantalla (en Linux, para pruebas, solo lo escribe)
alert() {
  log "AVISO: $1"
  if command -v osascript >/dev/null 2>&1; then
    osascript -e 'on run argv' -e 'display alert "PS5 Directo" message (item 1 of argv) as critical' \
              -e 'end run' "$1" >/dev/null 2>&1
  else
    echo "PS5 Directo: $1" >&2
  fi
}
HELP="Si no se arregla, abre Terminal y escribe:
xattr -dr com.apple.quarantine \"$BUNDLE\"
y vuelve a abrir la app. Si sigue igual, avisa en github.com/jstrdev/ps5directo/issues con lo de arriba.

If it doesn't fix itself, open Terminal and run the command above, then open the app again. If it still fails, please report it at github.com/jstrdev/ps5directo/issues with the text above."

log "inicio: app en $BUNDLE, procesador $(uname -m), macOS $(sw_vers -productVersion 2>/dev/null)"
case "$BUNDLE" in */AppTranslocation/*) log "la app se ejecuta desde una copia temporal de macOS (no está en Aplicaciones)";; esac

# macOS marca como "descargado de internet" todo lo que trae la app y puede bloquear en silencio el
# Python de dentro. La propia app se quita esa marca (si macOS lo permite).
if command -v xattr >/dev/null 2>&1; then
  xattr -dr com.apple.quarantine "$BUNDLE" 2>/dev/null || xattr -dr com.apple.quarantine "$RES" 2>/dev/null
fi

case "$(uname -m)" in
  arm64) PY="$RES/python-arm64/bin/python3" ;;
  *)     PY="$RES/python-x86_64/bin/python3" ;;
esac
[ -x "$PY" ] || PY="$RES/python-x86_64/bin/python3"   # Rosetta, por si acaso
if [ ! -x "$PY" ]; then
  alert "No encuentro el Python incluido en la app. Vuelve a descargarla de github.com/jstrdev/ps5directo/releases

The Python included in the app is missing. Please download it again."
  exit 1
fi

# Python funciona?
OUT="$("$PY" -c 'import ssl, sqlite3, json; print("ok")' 2>&1)"; RC=$?
if [ $RC -ne 0 ] || [ "$OUT" != "ok" ]; then
  alert "macOS no deja arrancar el Python incluido en la app (código $RC).
macOS is blocking the Python included in the app (code $RC).

$(printf '%s' "$OUT" | tail -n 6)

$HELP"
  exit 1
fi

ver() { sed -n 's/^VERSION = "\([^"]*\)".*/\1/p' "$1" 2>/dev/null | head -1; }
newer() {   # 0 si $1 es mas nueva que $2
  [ -z "$2" ] && return 0
  [ "$1" = "$2" ] && return 1
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)" = "$1" ]
}
BUNDLED="$(ver "$RES/app/app.py")"
COPIED="$(ver "$APP/app.py")"
if [ ! -f "$APP/app.py" ] || newer "$BUNDLED" "$COPIED"; then
  log "copio la version $BUNDLED (habia: ${COPIED:-ninguna})"
  rm -rf "$APP.nuevo"
  if ! { cp -R "$RES/app" "$APP.nuevo" && rm -rf "$APP" && mv "$APP.nuevo" "$APP"; }; then
    alert "No pude copiar el programa a $SUP. ¿Hay espacio libre en el Mac?

Couldn't copy the program to $SUP. Is there free space on the Mac?"
    exit 1
  fi
  command -v xattr >/dev/null 2>&1 && xattr -dr com.apple.quarantine "$APP" 2>/dev/null
fi

export PATH="$RES/bin:$PATH"   # 7-Zip incluido
export PYTHONUNBUFFERED=1
cd "$APP" || exit 1

# El programa sigue en segundo plano y el lanzador termina: asi cada clic en el icono vuelve a
# entrar aqui y, si ya estaba abierto, solo trae su ventana (si no, macOS creeria que la app
# sigue abierta y no haria nada). Antes de terminar, espera a ver que arranca bien.
START=$(wc -c < "$PLOG" 2>/dev/null | tr -d ' '); START=${START:-0}
nohup "$PY" "$APP/app.py" "$@" >> "$PLOG" 2>&1 < /dev/null &
PID=$!
log "arranco el programa (proceso $PID)"
for i in $(seq 1 40); do
  sleep 0.5
  NEW="$(tail -c +$((START + 1)) "$PLOG" 2>/dev/null)"
  case "$NEW" in *"Abre en el navegador"*) log "arrancado"; exit 0;; esac
  if ! kill -0 "$PID" 2>/dev/null; then
    wait "$PID"; RC=$?
    if [ $RC -eq 0 ] || [ $RC -eq 3 ]; then   # 3 = ya estaba abierto: solo se ha traido su ventana
      log "ya estaba abierto (código $RC)"; exit 0
    fi
    alert "PS5 Directo se ha cerrado al arrancar (código $RC). Esto es lo último que escribió:
PS5 Directo closed while starting (code $RC). Last lines:

$(printf '%s' "$NEW" | tail -n 12)

$HELP"
    exit 1
  fi
done
log "sigue arrancando (tarda más de lo normal)"
exit 0
