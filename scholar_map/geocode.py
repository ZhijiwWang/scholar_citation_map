from __future__ import annotations

from collections import Counter
import json
from urllib.parse import urlparse

from .common import LOG, normalise, stable_id, valid_coordinate, write_csv, write_json
from .llm import Location, prompt

GEO_FIELDS = ["city", "region", "country", "latitude", "longitude", "geocode_status",
              "geocode_confidence", "geocode_reason", "geocode_sources", "geocode_model", "geocode_response_id"]


def geocode_context(row):
    # Include the campus/address, so different campuses of a shared institution are not conflated.
    return {"institution": row.get("institution", ""), "institution_id": row.get("institution_id", ""),
            "country_code": row.get("institution_country_code", ""), "raw_affiliation": row.get("raw_affiliation", "")}


def geocode_rows(rows, llm, cache, output, web_search=True, report_path=None):
    geo_prompt = prompt("geocode.txt")
    in_memory = {}
    for index, row in enumerate(rows, 1):
        for key in GEO_FIELDS:
            row[key] = ""
        if not row.get("institution", "").strip():
            row.update(geocode_status="skipped", geocode_reason="Institution missing; no API request made.")
            continue
        context = geocode_context(row)
        cache_key = {"version": 2, "provider": "gemini-adc", "context": context, "model": llm.model, "web_search": web_search, "prompt": geo_prompt}
        key = stable_id(cache_key)
        data = in_memory.get(key) or cache.get("geocode", cache_key)
        if data is None:
            LOG.info("Locating institution %d/%d: %s", index, len(rows), row["institution"])
            try:
                result, response_id, searched_sources = llm.parse(geo_prompt,
                    json.dumps({**context, "web_search_enabled": web_search}, ensure_ascii=False), Location, web_search=web_search)
                if result["status"] == "resolved" and (not result["city"] or not result["country"] or
                                                       not valid_coordinate(result["latitude"], result["longitude"])):
                    raise ValueError("Invalid resolved city/coordinates")
                sources = [s for s in result["sources"] if urlparse(s).scheme in {"http", "https"}]
                if web_search:
                    # Accept only URLs that the API actually returned from search/citations.
                    canonical = lambda url: url.split("#")[0].rstrip("/")
                    used = {canonical(s) for s in searched_sources}
                    sources = [s for s in sources if canonical(s) in used]
                    if result["status"] == "resolved" and not sources:
                        result.update(status="unknown", city=None, latitude=None, longitude=None,
                                      reason="The API returned no verifiable search sources; location left blank for review.", confidence="low")
                elif result["confidence"] == "high":
                    result["confidence"] = "medium"
                if result["status"] != "resolved":
                    result.update(city=None, latitude=None, longitude=None)
                data = {"city": result["city"] or "", "region": result["region"] or "", "country": result["country"] or "",
                        "latitude": result["latitude"] if result["latitude"] is not None else "",
                        "longitude": result["longitude"] if result["longitude"] is not None else "",
                        "geocode_status": result["status"], "geocode_confidence": result["confidence"],
                        "geocode_reason": result["reason"], "geocode_sources": json.dumps(sources, ensure_ascii=False),
                        "geocode_model": llm.model, "geocode_response_id": response_id}
                cache.put("geocode", cache_key, data)
            except Exception as exc:
                data = {"geocode_status": "error", "geocode_reason": "Gemini request failed: " + type(exc).__name__, "geocode_model": llm.model}
                LOG.warning("Institution geocoding failed (%s). The record is retained for retry on the next run.", type(exc).__name__)
        in_memory[key] = data
        row.update(data)
        # Checkpoint contains every input row, including rows not processed yet.
        if index % 10 == 0:
            write_csv(output, rows)
    write_csv(output, rows)
    report = {"rows": len(rows), "unique_institution_contexts": len(in_memory), "web_search": web_search,
              "model": llm.model, "status_counts": dict(Counter(r.get("geocode_status", "pending") for r in rows))}
    if report_path:
        write_json(report_path, report)
    return report
