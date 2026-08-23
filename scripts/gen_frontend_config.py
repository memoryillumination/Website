#!/usr/bin/env python3
"""
Generate frontend/js/config.js from the project-root .env.

A static frontend cannot read .env at runtime -- the browser only ever sees
files the web server hands it -- so the relevant values are baked into a small
generated script instead. Run this whenever .env changes:

    python3 scripts/gen_frontend_config.py

The output is gitignored. If it is absent the frontend falls back to the
production API, so a deploy that never runs this script still works.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")
OUT_PATH = os.path.join(ROOT, "frontend", "js", "config.js")

DEFAULTS = {
    "development": "http://localhost:5000",
    "production": "https://api.memoryillumination.com",
}


def read_env(path):
    """Minimal .env reader -- avoids making python-dotenv a dependency of the
    frontend build, which may run somewhere the backend venv does not exist."""
    values = {}
    if not os.path.exists(path):
        return values
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main():
    env = read_env(ENV_PATH)
    # Real environment variables win, so CI can generate this without a .env.
    deploy_env = os.environ.get("DEPLOY_ENV", env.get("DEPLOY_ENV", "production"))
    if deploy_env not in DEFAULTS:
        print(f"error: DEPLOY_ENV must be one of {sorted(DEFAULTS)}, got {deploy_env!r}")
        return 1

    api_port = os.environ.get("API_PORT", env.get("API_PORT", "5000"))
    explicit = os.environ.get("API_BASE_URL", env.get("API_BASE_URL"))

    if explicit:
        api_expr = f'"{explicit}"'
        described = explicit
    elif deploy_env == "development":
        # Derived at load time from the page URL rather than baked in, so one
        # generated file serves localhost, 127.0.0.1 and a LAN address like
        # http://192.168.1.116:8000 alike. Baking in "localhost" would break
        # every remote browser, where localhost means that machine, not this one.
        api_expr = "`${location.protocol}//${location.hostname}:" + api_port + "`"
        described = f"<page host>:{api_port} (derived at runtime)"
    else:
        api_expr = f'"{DEFAULTS[deploy_env]}"'
        described = DEFAULTS[deploy_env]

    with open(OUT_PATH, "w") as fh:
        fh.write(
            "// GENERATED FILE -- do not edit.\n"
            "// Produced by scripts/gen_frontend_config.py from the root .env.\n"
            "// Loaded before every other page script, so window.MI_CONFIG is set\n"
            "// by the time they run.\n"
            "window.MI_CONFIG = {\n"
            f'  deployEnv: "{deploy_env}",\n'
            f"  apiBaseUrl: {api_expr},\n"
            "};\n"
        )
    print(f"wrote {os.path.relpath(OUT_PATH, ROOT)}  (DEPLOY_ENV={deploy_env}, api={described})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
