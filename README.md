# gather-verify (LaneHelp)

LaneHelp crawler + verification tools with a Vercel web export endpoint.

## What this does

1. Crawls **multiple Lane County sources** (not only 211) including county/city government, nonprofits, and 211.
2. Normalizes records into CSV columns used by LaneHelp.
3. Optionally verifies output using the verifier CLI.
4. Exposes a Vercel page + API to generate/download CSV.

## Main files

- `lanehelp_crawler.py` — multi-source Lane County crawler + feed ingestion.
- `lanehelp_verify.py` — verification pass with `verified/partial/unverified` scoring.
- `api/export-csv.py` — Vercel API endpoint returning CSV.
- `public/index.html` — Vercel page for triggering exports.
- `config/lanehelp_sources.example.json` — editable seed/domain/feed config.

## CLI usage

### Crawl resources

```bash
python3 lanehelp_crawler.py \
  --config config/lanehelp_sources.example.json \
  --output data/lanehelp_resources.csv \
  --max-pages 1800 \
  --workers 12 \
  --min-records 1000
```

### Verify resources

```bash
python3 lanehelp_verify.py \
  --input data/lanehelp_resources.csv \
  --output data/lanehelp_resources_verified.csv \
  --report-json data/verification_report.json
```

## Vercel deployment

1. Import this repository into Vercel.
2. Deploy.
3. Open `/` for the export page.
4. API endpoint: `/api/export-csv?maxPages=1200&workers=10&config=config/lanehelp_sources.example.json`

> I cannot provide your final production Vercel URL from this environment because deployment happens in your Vercel account. After deploy, it will be visible in the Vercel dashboard and in the deployment output.
