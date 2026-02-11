from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Optional

from flask import Flask, Response, jsonify, request

from lanehelp_crawler import crawl_resources, resources_to_csv

app = Flask(__name__)

# api/export-csv.py -> repo root
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_REL = Path("config/lanehelp_sources.example.json")

# Reasonable-ish defaults for web-triggered runs
DEFAULT_MAX_PAGES = 250
DEFAULT_WORKERS = 3

# Hard caps to prevent “oops I crawled the whole internet”
MAX_MAX_PAGES_CAP = 2000
MAX_WORKERS_CAP = 10


def _parse_int(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = request.args.get(name, str(default))
    try:
        v = int(raw)
    except (TypeError, ValueError):
        v = default
    return max(min_value, min(max_value, v))


def _resolve_config_path(config_arg: str) -> Optional[Path]:
    """
    Supports:
      - config=none  -> no config file, use crawler defaults
      - config=config/lanehelp_sources.example.json  -> resolved relative to repo root
      - config=/abs/path/to/file.json -> absolute path (local only)
    Rejects path traversal attempts like ../../
    """
    if not config_arg:
        config_arg = str(DEFAULT_CONFIG_REL)

    if config_arg.strip().lower() == "none":
        return None

    cfg = Path(config_arg.strip())

    # If absolute path, allow it (useful locally)
    if cfg.is_absolute():
        return cfg

    # Basic anti-traversal: don’t allow leading ".." segments
    # (You can tighten further if you ever expose this publicly.)
    if any(part == ".." for part in cfg.parts):
        raise ValueError("Invalid config path (path traversal not allowed)")

    # Resolve relative to repo root
    return (ROOT / cfg).resolve()


@app.get("/api/export-csv")
def export_csv() -> Response:
    # Safer defaults for web. Still overrideable, but capped.
    max_pages = _parse_int(
        name="maxPages",
        default=DEFAULT_MAX_PAGES,
        min_value=1,
        max_value=MAX_MAX_PAGES_CAP,
    )
    workers = _parse_int(
        name="workers",
        default=DEFAULT_WORKERS,
        min_value=1,
        max_value=MAX_WORKERS_CAP,
    )

    # Optional soft time guard (doesn't kill threads, just prevents infinite misery)
    timeout_seconds = _parse_int(
        name="timeoutSeconds",
        default=0,   # 0 = no soft timeout
        min_value=0,
        max_value=600,
    )

    config_arg = request.args.get("config", str(DEFAULT_CONFIG_REL))
    started = time.time()

    try:
        config_path = _resolve_config_path(config_arg)

        # Soft timeout check BEFORE starting (useful if someone sets 0 workers etc.)
        if timeout_seconds and (time.time() - started) > timeout_seconds:
            return jsonify({"error": "Request timed out before crawl started"}), 504

        resources = crawl_resources(config_path, max_pages=max_pages, workers=workers)

        if timeout_seconds and (time.time() - started) > timeout_seconds:
            return jsonify({"error": "Request timed out during crawl"}), 504

        csv_text = resources_to_csv(resources)

    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        # Print full traceback to server logs so you can actually debug.
        print("EXPORT CSV FAILED:", repr(exc))
        print(traceback.format_exc())
        return jsonify({"error": f"Failed to export CSV: {exc}"}), 500

    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=lane-county-resources.csv"},
    )


@app.get("/api/health")
def health() -> Response:
    return jsonify({"ok": True, "service": "lanehelp-export"})
