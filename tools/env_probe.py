"""Probe the current Python environment for Phase 0 dependencies.

Run inside the container to record which libraries are actually available.
Usage: python3 tools/env_probe.py
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import platform
import sys

MODULES = [
    "pydantic",
    "pydantic_core",
    "jinja2",
    "weasyprint",
    "pytest",
    "httpx",
    "requests",
]


def probe() -> dict[str, object]:
    report: dict[str, object] = {
        "python_version": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "modules": {},
    }
    modules: dict[str, str] = {}
    for name in MODULES:
        if importlib.util.find_spec(name) is None:
            modules[name] = "MISSING"
            continue
        try:
            mod = importlib.import_module(name)
            version = getattr(mod, "VERSION", None) or getattr(mod, "__version__", "unknown")
        except Exception as exc:  # noqa: BLE001 - probe must never crash
            version = f"import-error: {exc}"
        modules[name] = str(version)
    report["modules"] = modules
    return report


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2, ensure_ascii=False))
