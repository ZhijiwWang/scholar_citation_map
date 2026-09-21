from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

LOG = logging.getLogger("scholar_map")

FIELDS = [
    "record_id", "profile_url", "source_paper_id", "source_paper_title",
    "source_paper_year", "source_cited_by_count", "citing_paper_id",
    "citing_paper_title", "citing_paper_year", "citing_paper_url", "citing_doi",
    "openalex_work_id", "paper_match_method", "paper_match_score", "author_index",
    "author_name", "author_id", "institution", "institution_id", "institution_ror",
    "institution_country_code", "raw_affiliation", "affiliation_source",
    "affiliation_source_url", "affiliation_evidence", "affiliation_status",
    "affiliation_note", "city", "region", "country", "latitude", "longitude",
    "geocode_status", "geocode_confidence", "geocode_reason", "geocode_sources",
    "geocode_model", "geocode_response_id",
]


def stable_id(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def normalise(text):
    return " ".join(re.findall(r"\w+", (text or "").casefold(), flags=re.UNICODE))


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def write_csv(path, rows, fields=FIELDS):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def valid_coordinate(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
        return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180
    except (TypeError, ValueError):
        return False


class Cache:
    def __init__(self, root, refresh=False):
        self.root = Path(root)
        self.refresh = refresh

    def get(self, namespace, key):
        path = self.root / namespace / (stable_id(key) + ".json")
        if self.refresh or not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def put(self, namespace, key, value):
        write_json(self.root / namespace / (stable_id(key) + ".json"), value)


class FetchError(RuntimeError):
    pass


class ScholarBlocked(FetchError):
    pass


class Http:
    """Rate-limit per host, retry transient failures, never put credentials in logs."""
    def __init__(self, cache, delay=2.0, timeout=45):
        self.cache = cache
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "ScholarCitationMap/1.0 (research metadata collection)"
        self.last_request = {}

    def get(self, url, params=None, scholar=False):
        public_params = {k: v for k, v in (params or {}).items() if k not in {"api_key", "mailto"}}
        key = {"url": url, "params": public_params}
        hit = self.cache.get("http", key)
        if hit is not None:
            return hit
        host = urlparse(url).hostname
        for attempt in range(4):
            wait = self.delay - (time.monotonic() - self.last_request.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self.last_request[host] = time.monotonic()
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException:
                if attempt == 3:
                    raise FetchError(f"Network request failed: {host}") from None
                time.sleep(2 ** attempt)
                continue
            if scholar and (response.status_code in {403, 429} or
                            any(term in response.text.lower() for term in
                                ["unusual traffic", "g-recaptcha", "not a robot", "automated queries"])):
                raise ScholarBlocked("Scholar blocked the request or requires a CAPTCHA. Cached results are retained. Retry later or use --provider serpapi.")
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 3:
                try:
                    retry = float(response.headers.get("Retry-After", 2 ** attempt))
                except ValueError:
                    retry = 2 ** attempt
                time.sleep(min(30, max(0, retry)))
                continue
            if response.status_code >= 400:
                raise FetchError(f"HTTP {response.status_code}: {host}")
            if len(response.content) > 30 * 1024 * 1024:
                raise FetchError(f"Resource exceeds 30 MB: {host}")
            content_type = response.headers.get("Content-Type", "")
            if "pdf" in content_type or response.content.startswith(b"%PDF"):
                import base64
                result = {"kind": "pdf", "body": base64.b64encode(response.content).decode(), "url": response.url.split("?")[0]}
            else:
                response.encoding = response.apparent_encoding or "utf-8"
                result = {"kind": "text", "body": response.text, "url": url}
            # Raw SerpApi responses can echo search_parameters.api_key: sanitise before caching.
            if host == "serpapi.com":
                try:
                    payload = json.loads(result["body"])
                    self._redact(payload, (params or {}).get("api_key", ""))
                    result["body"] = json.dumps(payload, ensure_ascii=False)
                    if payload.get("error"):
                        raise FetchError("SerpApi returned an error. Check your quota, API key, and search parameters.")
                except json.JSONDecodeError:
                    raise FetchError("SerpApi returned invalid JSON") from None
            self.cache.put("http", key, result)
            return result
        raise FetchError(f"Request failed: {host}")

    @classmethod
    def _redact(cls, obj, secret):
        if isinstance(obj, dict):
            for key, val in list(obj.items()):
                if key == "api_key":
                    obj[key] = "[REDACTED]"
                elif isinstance(val, str) and secret:
                    obj[key] = val.replace(secret, "[REDACTED]")
                else:
                    cls._redact(val, secret)
        elif isinstance(obj, list):
            for val in obj:
                cls._redact(val, secret)

    def json(self, url, params=None):
        result = self.get(url, params)
        try:
            return json.loads(result["body"])
        except (ValueError, KeyError):
            raise FetchError(f"Invalid JSON: {urlparse(url).hostname}") from None
