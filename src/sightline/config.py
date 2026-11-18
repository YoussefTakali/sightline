"""Profiles: the ONLY place that knows whether a run is headed or headless.

sightline.toml
--------------
    [defaults]
    assets = ["assets"]
    timeout = 10
    ocr_lang = "fra+eng"

    [profiles.dev]                 # headed, this machine
    backend = "local"

    [profiles.headless]            # Xvfb container, Windows VM, remote box...
    backend = "vnc"
    [profiles.headless.connection]
    host = "127.0.0.1"
    port = 5900
    password_env = "SIGHTLINE_VNC_PASSWORD"
    target_os = "linux"

Select with Session.from_profile("headless"), or SIGHTLINE_PROFILE=headless.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .errors import SightlineError

SESSION_KEYS = {
    "assets", "timeout", "poll", "similarity", "color_similarity", "text_similarity",
    "strict", "ambiguity_margin", "stable", "settle", "screen_scale", "scale_search",
    "ocr_lang", "theme", "trace", "trace_dir", "park_mouse",
    "recovery",                                      # steps run after a failed test
    "on_artifact", "ocr",                            # programmatic only (callbacks, engines)
}


def _load_toml(path: Path) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:                      # Python 3.10
        try:
            import tomli as tomllib  # type: ignore
        except ModuleNotFoundError as exc:
            raise SightlineError("Python 3.10 needs: pip install tomli") from exc
    with open(path, "rb") as f:
        return tomllib.load(f)


def find_config(path: str | None = None) -> Path:
    if path:
        return Path(path)
    if os.environ.get("SIGHTLINE_CONFIG"):
        return Path(os.environ["SIGHTLINE_CONFIG"])
    here = Path.cwd()
    for d in [here, *here.parents]:
        if (d / "sightline.toml").exists():
            return d / "sightline.toml"
    raise SightlineError("No sightline.toml found (cwd and parents); set SIGHTLINE_CONFIG")


def load_profile(name: str | None = None, path: str | None = None) -> tuple[str, dict, Path]:
    cfg_path = find_config(path)
    data = _load_toml(cfg_path)
    name = name or os.environ.get("SIGHTLINE_PROFILE") or data.get("default_profile") or "dev"
    profiles = data.get("profiles", {})
    if name not in profiles:
        raise SightlineError(f"Profile '{name}' not in {cfg_path} (have: {sorted(profiles)})")
    merged: dict[str, Any] = dict(data.get("defaults", {}))
    conn = dict(merged.pop("connection", {}))
    prof = dict(profiles[name])
    conn.update(prof.pop("connection", {}))
    merged.update(prof)
    merged["connection"] = conn
    return name, merged, cfg_path.parent


def session_from_profile(name: str | None = None, path: str | None = None, **overrides):
    from .backends import create_backend
    from .session import Session

    pname, cfg, base = load_profile(name, path)
    cfg.update({k: v for k, v in overrides.items() if k != "connection"})
    conn = dict(cfg.pop("connection", {}))
    conn.update(overrides.get("connection", {}))
    for key in list(conn):
        if key.endswith("_env"):                     # secrets/overrides from the environment
            var, base_key = conn.pop(key), key[:-4]
            if var in os.environ:
                conn[base_key] = os.environ[var]
            elif base_key not in conn:               # no default in the profile either
                raise SightlineError(f"profile '{pname}': environment variable {var} is not set")
    kind = cfg.pop("backend", "local")
    assets = [str((base / a).resolve()) if not Path(a).is_absolute() else a
              for a in cfg.get("assets", [])]
    cfg["assets"] = assets
    if "trace_dir" in cfg and not Path(cfg["trace_dir"]).is_absolute():
        cfg["trace_dir"] = str(base / cfg["trace_dir"])
    unknown = set(cfg) - SESSION_KEYS
    if unknown:
        raise SightlineError(f"profile '{pname}': unknown setting(s) {sorted(unknown)}")
    backend = create_backend(kind, **conn)
    return Session(backend, **cfg)
