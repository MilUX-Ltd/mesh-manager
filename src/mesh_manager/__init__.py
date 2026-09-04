"""Mesh Manager: manage a Meshtastic mesh and its devices from the box that carries the
gateway radio, and bridge it into TAK. GPL-3.0-or-later."""
import os


def _version():
    here = os.path.dirname(os.path.abspath(__file__))
    src_tree = os.path.join(here, os.pardir, os.pardir, "VERSION")   # running from the repository
    if os.path.exists(src_tree):
        return open(src_tree).read().strip()
    try:
        from importlib.metadata import version
        return version("mesh-manager")
    except Exception:  # noqa: BLE001
        return "0.0.0"


__version__ = _version()
