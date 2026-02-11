from __future__ import annotations

from pathlib import Path

from flask import Flask, Response, jsonify, request

from lanehelp_crawler import crawl_resources, resources_to_csv

app = Flask(__name__)


@app.get('/api/export-csv')
def export_csv() -> Response:
    max_pages = int(request.args.get('maxPages', '1200'))
    workers = int(request.args.get('workers', '10'))
    config_arg = request.args.get('config', 'config/lanehelp_sources.example.json')

    config_path = None if config_arg.lower() == 'none' else Path(config_arg)

    try:
        resources = crawl_resources(config_path, max_pages=max_pages, workers=workers)
        csv_text = resources_to_csv(resources)
    except FileNotFoundError as exc:
        return jsonify({'error': str(exc)}), 400
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({'error': f'Failed to export CSV: {exc}'}), 500

    return Response(
        csv_text,
        mimetype='text/csv',
        headers={
            'Content-Disposition': 'attachment; filename=lane-county-resources.csv'
        },
    )


@app.get('/api/health')
def health() -> Response:
    return jsonify({'ok': True, 'service': 'lanehelp-export'})
