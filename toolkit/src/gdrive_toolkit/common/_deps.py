"""Lazy optional-dependency check for the console-script entry points.

Console scripts always install (they have no heavy deps themselves); the
actual feature modules (flask/requests/rumps) are only required when the
corresponding extra was installed. `require()` gives a clear, actionable
error instead of a bare ImportError deep in some module.
"""
from __future__ import annotations

import importlib


def require(feature: str, *modules: str) -> None:
    missing = []
    for mod in modules:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        raise SystemExit(
            "Missing dependencies for '%s': %s\n"
            "Install with: pip install 'google-shared-drive-toolkit[%s]'"
            % (feature, ", ".join(missing), feature)
        )
