#!/usr/bin/env python3
"""LaneHelp resource crawler.

Crawls Lane County, OR resource pages and normalizes records for spreadsheet import.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import re
import sys
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

USER_AGENT = "LaneHelpCrawler/1.0 (+https://lanehelp.org)"

DEFAULT_SEEDS = [
    "https://www.lanecounty.org/",
    "https://www.eugene-or.gov/",
    "https://www.springfield-or.gov/",
    "https://www.211info.org/",
    "https://www.whitebirdclinic.org/",
    "https://www.svdp.us/",
    "https://www.foodforlanecounty.org/",
    "https://www.ccslc.org/",
    "https://www.lookingglass.us/",
    "https://www.lcog.org/",
]

DEFAULT_ALLOWED_DOMAINS = [
    "lanecounty.org",
    "eugene-or.gov",
    "springfield-or.gov",
    "211info.org",
    "whitebirdclinic.org",
    "svdp.us",
    "foodforlanecounty.org",
    "ccslc.org",
    "lookingglass.us",
    "lcog.org",
    "navigateresources.net",
    "oregon.gov",
]

CATEGORY_KEYWORDS = {
    "Food": ["food", "pantry", "meals", "grocery", "nutrition"],
    "Housing": ["housing", "shelter", "rent", "homeless", "eviction"],
    "Health/Mental Health": ["health", "clinic", "mental", "counsel", "therapy", "medical"],
    "Substance Use": ["addiction", "recovery", "detox", "substance"],
    "Legal": ["legal", "attorney", "law", "rights"],
    "Disability": ["disability", "ada", "accessible"],
    "Youth/Family": ["youth", "children", "family", "parent"],
    "Employment": ["job", "employment", "workforce", "career"],
    "Transportation": ["transportation", "transit", "bus", "ride"],
    "Education": ["education", "school", "learning", "training"],
    "DV/Safety": ["domestic violence", "abuse", "safety", "crisis"],
    "Utilities/Financial": ["utility", "bill", "financial", "assistance"],
}

SERVICE_AREA_KEYWORDS = [
    "lane county",
    "eugene",
    "springfield",
    "cottage grove",
    "veneta",
    "junction city",
    "florence",
    "statewide",
]

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}")
HOURS_RE = re.compile(
    r"\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\b[^\n\r]{0,80}(?:\d{1,2}:?\d{0,2}\s?(?:am|pm)|closed)",
    re.I,
)
ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9.\-\s]{2,60}\s(?:St|Street|Ave|Avenue|Blvd|Road|Rd|Lane|Ln|Way|Dr|Drive|Hwy|Highway|Ct|Court)\b[^\n\r]{0,40}",
    re.I,
)


@dataclass
class Resource:
    name: str = ""
    phone: str = ""
    email: str = ""
    hours: str = ""
    address: str = ""
    tags: str = ""
    category: str = ""
    access: str = ""
    eligibility: str = ""
    service_area: str = ""
    short_description: str = ""
    links: str = ""
    source_url: str = ""

    def row_key(self) -> str:
        normalized = "|".join(
            [
                self.name.lower().strip(),
                self.phone.lower().strip(),
                self.address.lower().strip(),
                self.email.lower().strip(),
            ]
        )
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


class LinkAndTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: List[str] = []
        self.title: str = ""
        self._in_title = False
        self.text_parts: List[str] = []
        self.h1: List[str] = []
        self.meta_description: str = ""

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        d = dict(attrs)
        if tag == "a" and d.get("href"):
            self.links.append(d["href"])
        elif tag == "title":
            self._in_title = True
        elif tag == "meta" and d.get("name", "").lower() == "description" and d.get("content"):
            self.meta_description = d["content"]
        elif tag == "h1":
            self.text_parts.append("\n__H1_START__\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        chunk = data.strip()
        if not chunk:
            return
        if self._in_title:
            self.title += (" " + chunk)
        self.text_parts.append(chunk)

    def compact_text(self) -> str:
        return "\n".join(self.text_parts)


def fetch_url(url: str, timeout: int = 15) -> Tuple[Optional[str], Optional[str]]:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type and "application/json" not in content_type and "text/csv" not in content_type:
                return None, None
            body = resp.read(2_000_000)
            return body.decode("utf-8", errors="ignore"), content_type
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None, None


def in_scope(url: str, allowed_domains: List[str]) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in allowed_domains)


def normalize_url(base: str, link: str) -> Optional[str]:
    if link.startswith("mailto:") or link.startswith("tel:") or link.startswith("javascript:"):
        return None
    absolute = urljoin(base, link)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        return None
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def choose_category(text: str) -> str:
    low = text.lower()
    scores = Counter()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for k in keywords:
            if k in low:
                scores[cat] += 1
    return scores.most_common(1)[0][0] if scores else "General Support"


def infer_access(text: str) -> str:
    low = text.lower()
    if "walk-in" in low:
        return "Walk-in"
    if "appointment" in low:
        return "Appointment"
    if "call" in low or "phone" in low:
        return "Call"
    if "online" in low or "web" in low:
        return "Online"
    return "Unknown"


def infer_eligibility(text: str) -> str:
    low = text.lower()
    patterns = ["adults", "youth", "families", "veterans", "low-income", "all residents", "anyone"]
    found = [p for p in patterns if p in low]
    return ", ".join(found) if found else "Not specified"


def infer_service_area(text: str) -> str:
    low = text.lower()
    found = [k.title() for k in SERVICE_AREA_KEYWORDS if k in low]
    return ", ".join(dict.fromkeys(found)) if found else "Lane County"


def build_resource_from_page(url: str, html: str) -> Resource:
    parser = LinkAndTextParser()
    parser.feed(html)
    text = parser.compact_text()

    title = parser.title.strip()[:150]
    name = title.split("|")[0].strip() if title else ""
    if not name:
        name = urlparse(url).netloc

    phones = PHONE_RE.findall(text)
    emails = EMAIL_RE.findall(text)
    hours = HOURS_RE.findall(text)
    addresses = ADDRESS_RE.findall(text)

    description = parser.meta_description.strip()
    if not description:
        lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) > 30]
        description = lines[0][:280] if lines else ""

    category = choose_category(text)
    tags = sorted({w for w in CATEGORY_KEYWORDS.get(category, []) if w in text.lower()})

    return Resource(
        name=name,
        phone="; ".join(dict.fromkeys(phones))[:100],
        email="; ".join(dict.fromkeys(emails))[:120],
        hours="; ".join(dict.fromkeys(hours))[:120],
        address="; ".join(dict.fromkeys(addresses))[:180],
        tags=", ".join(tags),
        category=category,
        access=infer_access(text),
        eligibility=infer_eligibility(text),
        service_area=infer_service_area(text),
        short_description=description,
        links=url,
        source_url=url,
    )


def crawl_html_sources(
    seeds: List[str],
    allowed_domains: List[str],
    max_pages: int,
    workers: int,
) -> List[Resource]:
    queue: deque[str] = deque(seeds)
    seen: Set[str] = set()
    resources: List[Resource] = []

    while queue and len(seen) < max_pages:
        batch = []
        while queue and len(batch) < workers and len(seen) < max_pages:
            u = queue.popleft()
            if u in seen:
                continue
            seen.add(u)
            batch.append(u)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {pool.submit(fetch_url, u): u for u in batch}
            for fut in as_completed(future_map):
                url = future_map[fut]
                body, ctype = fut.result()
                if not body or not ctype or "text/html" not in ctype:
                    continue
                resource = build_resource_from_page(url, body)
                if resource.phone or resource.email or resource.address:
                    resources.append(resource)

                parser = LinkAndTextParser()
                parser.feed(body)
                for link in parser.links:
                    normalized = normalize_url(url, link)
                    if not normalized:
                        continue
                    if normalized in seen:
                        continue
                    if not in_scope(normalized, allowed_domains):
                        continue
                    queue.append(normalized)

    return resources


def load_feed_records(url: str) -> List[Dict[str, str]]:
    body, ctype = fetch_url(url)
    if not body:
        return []
    records: List[Dict[str, str]] = []
    if "json" in (ctype or "") or body.strip().startswith("["):
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                items = data.get("results") or data.get("data") or []
            else:
                items = data
            for item in items:
                if isinstance(item, dict):
                    records.append({k.lower(): str(v) for k, v in item.items() if v is not None})
        except json.JSONDecodeError:
            return []
    elif "csv" in (ctype or "") or "," in body[:2000]:
        reader = csv.DictReader(body.splitlines())
        for row in reader:
            records.append({(k or "").lower(): (v or "") for k, v in row.items()})
    return records


def map_feed_record(row: Dict[str, str], source_url: str) -> Resource:
    def pick(*keys: str) -> str:
        for k in keys:
            if row.get(k):
                return row[k].strip()
        return ""

    text_blob = " ".join(row.values())
    return Resource(
        name=pick("name", "organization", "agency", "program_name", "resource_name") or "Unknown resource",
        phone=pick("phone", "phone_number", "contact_phone"),
        email=pick("email", "contact_email"),
        hours=pick("hours", "service_hours"),
        address=pick("address", "street", "location", "site_address"),
        tags=pick("tags", "keywords") or "",
        category=pick("category", "service_category") or choose_category(text_blob),
        access=pick("access", "intake") or infer_access(text_blob),
        eligibility=pick("eligibility") or infer_eligibility(text_blob),
        service_area=pick("service_area", "city", "county") or infer_service_area(text_blob),
        short_description=pick("description", "short_description", "summary")[:300],
        links=pick("url", "website", "link") or source_url,
        source_url=source_url,
    )


def dedupe_resources(resources: Iterable[Resource]) -> List[Resource]:
    out: List[Resource] = []
    seen: Set[str] = set()
    for r in resources:
        key = r.row_key()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def resources_to_csv(resources: List[Resource]) -> str:
    import io

    fields = [f.name for f in dataclasses.fields(Resource)]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for resource in resources:
        writer.writerow(asdict(resource))
    return buffer.getvalue()


def save_csv(resources: List[Resource], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = [f.name for f in dataclasses.fields(Resource)]
    with output.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in resources:
            w.writerow(asdict(r))


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Crawl and normalize Lane County resources")
    p.add_argument("--config", type=Path, help="JSON config with seeds/feed_urls/allowed_domains", default=None)
    p.add_argument("--output", type=Path, default=Path("data/lanehelp_resources.csv"))
    p.add_argument("--max-pages", type=int, default=1200)
    p.add_argument("--workers", type=int, default=10)
    p.add_argument("--min-records", type=int, default=1000)
    return p.parse_args(argv)


def load_config(path: Optional[Path]) -> Dict[str, object]:
    if not path:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def crawl_resources(
    config_path: Optional[Path],
    max_pages: int,
    workers: int,
) -> List[Resource]:
    cfg = load_config(config_path)

    seeds = cfg.get("seeds", DEFAULT_SEEDS)
    allowed_domains = cfg.get("allowed_domains", DEFAULT_ALLOWED_DOMAINS)
    feed_urls = cfg.get("feed_urls", [])

    if not isinstance(seeds, list) or not isinstance(allowed_domains, list) or not isinstance(feed_urls, list):
        raise ValueError("Invalid config format")

    resources = crawl_html_sources(seeds, allowed_domains, max_pages, workers)
    for feed in feed_urls:
        rows = load_feed_records(feed)
        resources.extend(map_feed_record(r, feed) for r in rows)

    return dedupe_resources(resources)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    print(f"[*] Crawling HTML sources (max_pages={args.max_pages})")
    try:
        resources = crawl_resources(args.config, args.max_pages, args.workers)
    except ValueError:
        print("Invalid config format", file=sys.stderr)
        return 2
    save_csv(resources, args.output)

    print(f"[+] Wrote {len(resources)} unique resources -> {args.output}")
    if len(resources) < args.min_records:
        print(
            f"[!] Warning: resource count below target ({len(resources)} < {args.min_records}). Add more feed_urls/seeds in config.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
