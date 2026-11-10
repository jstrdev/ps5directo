"""Arranque de PS5 Directo dentro de la app de Android (modo «Usar sin PC»).

El flujo de GitHub copia app.py, engine.py, features.py, web/ y los .json al paquete «ps5d» de al lado
antes de compilar la app; Chaquopy lo deja como carpeta normal en el movil (extractPackages), asi que
PS5 Directo funciona igual que en el PC, solo que en la direccion 127.0.0.1 del propio movil.
"""
import os
import sys

_URL = []


def start(files_dir):
    """Arranca el motor una sola vez y devuelve la direccion (con su codigo) que abre la app."""
    if _URL:
        return _URL[0]
    os.environ["HOME"] = files_dir             # ajustes, cola y registros en la carpeta privada de la app
    os.environ["PS5D_PHONE"] = "1"
    import ps5d
    base = os.path.dirname(os.path.abspath(ps5d.__file__))
    if base not in sys.path:
        sys.path.insert(0, base)
    try:
        log = open(os.path.join(files_dir, "ps5directo.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
    except Exception:
        pass
    import app
    _URL.append(app.phone_start())
    return _URL[0]
