#!/usr/bin/env python3
"""LaneHelp verification tool.

Re-checks scraped resources by validating contact artifacts from source pages.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USER_AGENT = "LaneHelpVerifier/1.0 (+https://lanehelp.org)"

PHONE_RE = re.compile(r"(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}")
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
HOURS_HINT_RE = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun|hour|open|closed)\b", re.I)


@dataclass
class VerificationResult:
    status: str
    score: int
    notes: str


def fetch(url: str, timeout: int = 20) -> str:
    if not url:
        return ""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "text/html" not in ctype:
                return ""
            return resp.read(2_000_000).decode("utf-8", errors="ignore")
    except (HTTPError, URLError, TimeoutError, ValueError):
        return ""


def contains_any(text: str, fields: List[str]) -> Tuple[int, List[str]]:
    hits = []
    low = text.lower()
    for field in fields:
        field = (field or "").strip()
        if not field:
            continue
        if field.lower() in low:
            hits.append(field)
    return len(hits), hits


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def verify_row(row: Dict[str, str]) -> VerificationResult:
    source = row.get("source_url") or row.get("links") or row.get("website") or ""
    html = fetch(source)
    if not html:
        return VerificationResult("unverified", 0, "Could not fetch source page")

    score = 0
    notes: List[str] = []

    name = row.get("name", "").strip()
    if name and name.lower() in html.lower():
        score += 25
    else:
        notes.append("name not found")

    phone = row.get("phone", "")
    if phone:
        expected = [normalize_phone(p) for p in re.split(r";|,", phone) if p.strip()]
        found = {normalize_phone(p) for p in PHONE_RE.findall(html)}
        hits = [p for p in expected if p and p in found]
        if hits:
            score += 25
        else:
            notes.append("phone mismatch")

    email = row.get("email", "")
    if email:
        found_emails = {e.lower() for e in EMAIL_RE.findall(html)}
        expected = [e.strip().lower() for e in re.split(r";|,", email) if e.strip()]
        hits = [e for e in expected if e in found_emails]
        if hits:
            score += 20
        else:
            notes.append("email mismatch")

    address = row.get("address", "")
    if address:
        n, _ = contains_any(html, [a.strip() for a in re.split(r";", address) if a.strip()])
        if n:
            score += 15
        else:
            notes.append("address not found")

    hours = row.get("hours", "")
    if hours:
        if HOURS_HINT_RE.search(html):
            score += 15
        else:
            notes.append("hours not found")

    if score >= 70:
        status = "verified"
    elif score >= 35:
        status = "partial"
    else:
        status = "unverified"

    return VerificationResult(status=status, score=score, notes="; ".join(notes) or "ok")


def run(input_csv: Path, output_csv: Path, report_json: Optional[Path]) -> None:
    with input_csv.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        raise ValueError("Input CSV contains no records")

    out_rows = []
    stats = {"verified": 0, "partial": 0, "unverified": 0}

    for row in rows:
        vr = verify_row(row)
        row["verification_status"] = vr.status
        row["verification_score"] = str(vr.score)
        row["verification_notes"] = vr.notes
        out_rows.append(row)
        stats[vr.status] += 1

    fields = list(out_rows[0].keys())
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(out_rows)

    if report_json:
        report_json.parent.mkdir(parents=True, exist_ok=True)
        report_json.write_text(json.dumps({"total": len(out_rows), "stats": stats}, indent=2), encoding="utf-8")

    print(f"[+] Verification complete: {len(out_rows)} rows -> {output_csv}")
    print(f"[+] Stats: {stats}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Verify LaneHelp resource CSV")
    p.add_argument("--input", type=Path, required=True, help="Input CSV produced by lanehelp_crawler.py")
    p.add_argument("--output", type=Path, default=Path("data/lanehelp_resources_verified.csv"))
    p.add_argument("--report-json", type=Path, default=Path("data/verification_report.json"))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    run(args.input, args.output, args.report_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
