from __future__ import annotations

import base64
import io
import json
import re
from difflib import SequenceMatcher
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from pypdf import PdfReader

from .common import FetchError, LOG, normalise
from .llm import Extraction, prompt


def extract_doi(text):
    match = re.search(r"10\.\d{4,9}/[^\s\"<>?#]+", unquote(text or ""), re.I)
    return match.group().rstrip(".,;").lower() if match else ""


def title_score(left, right):
    return SequenceMatcher(None, normalise(left), normalise(right)).ratio()


def surname(name):
    words = normalise(name).split()
    return words[-1] if words else ""


def same_author(left, right):
    left, right = normalise(left).split(), normalise(right).split()
    if not left or not right:
        return False
    return left == right or (len(left) >= 2 and len(right) >= 2 and left[-1] == right[-1]
                            and left[0][0] == right[0][0] and (len(left[0]) == 1 or len(right[0]) == 1))


def choose_work(paper, candidates, threshold=0.93):
    """Conservative matching: title, publication year and short-title author evidence."""
    ranked = []
    seen = set()
    for candidate in candidates:
        cid = candidate.get("id") or candidate.get("doi")
        if cid in seen:
            continue
        seen.add(cid)
        score = title_score(paper.title, candidate.get("title", ""))
        year = str(candidate.get("publication_year") or "")
        if paper.year and year and abs(int(paper.year) - int(year)) > 1:
            continue
        authors = {surname(a.get("author", {}).get("display_name", "")) for a in candidate.get("authorships", [])}
        known = {surname(a) for a in paper.authors if a and "…" not in a and "..." not in a}
        if len(normalise(paper.title).split()) < 5 and not (known & authors):
            continue
        if score >= threshold:
            ranked.append((score, candidate))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    if not ranked or (len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.02):
        return None, 0.0
    return ranked[0][1], ranked[0][0]


def work_affiliations(work):
    rows = []
    for index, authorship in enumerate(work.get("authorships", []), 1):
        author = authorship.get("author") or {}
        base = {"author_index": index, "author_name": authorship.get("raw_author_name") or author.get("display_name", ""),
                "author_id": author.get("id", ""), "affiliation_source": "openalex",
                "affiliation_source_url": work.get("id", "")}
        institutions = authorship.get("institutions") or []
        raw_strings = authorship.get("raw_affiliation_strings") or []
        mapping = authorship.get("affiliations") or []
        if institutions:
            for institution in institutions:
                relevant = [m["raw_affiliation_string"] for m in mapping
                            if institution.get("id") in m.get("institution_ids", []) and m.get("raw_affiliation_string")]
                if not relevant and len(raw_strings) == 1 and len(institutions) == 1:
                    relevant = raw_strings
                rows.append({**base, "institution": institution.get("display_name", ""),
                             "institution_id": institution.get("id", ""), "institution_ror": institution.get("ror", ""),
                             "institution_country_code": institution.get("country_code", ""),
                             "raw_affiliation": " | ".join(relevant), "affiliation_status": "resolved"})
        elif raw_strings:
            # Preserve unnormalised affiliation strings for Gemini's campus/address context.
            for raw in raw_strings:
                rows.append({**base, "institution": raw, "raw_affiliation": raw,
                             "affiliation_status": "raw_only"})
        else:
            rows.append({**base, "institution": "", "affiliation_status": "missing",
                         "affiliation_note": "This author has no affiliation metadata for this paper."})
    return rows


def crossref_work(item):
    date = item.get("published") or item.get("published-print") or item.get("published-online") or {}
    parts = date.get("date-parts") or [[None]]
    return {"id": "https://doi.org/" + item.get("DOI", ""), "doi": "https://doi.org/" + item.get("DOI", ""),
            "title": (item.get("title") or [""])[0], "publication_year": parts[0][0],
            "authorships": [{"author": {"display_name": " ".join(filter(None, [a.get("given"), a.get("family")]))},
                            "raw_affiliation_strings": [f.get("name", "") for f in a.get("affiliation", []) if f.get("name")]}
                           for a in item.get("author", [])]}


def page_text(resource):
    if resource["kind"] == "pdf":
        reader = PdfReader(io.BytesIO(base64.b64decode(resource["body"])))
        text = "\n".join(page.extract_text() or "" for page in reader.pages[:5])
        return text[:60000], "", []
    soup = BeautifulSoup(resource["body"], "html.parser")
    doi = ""
    metadata = []
    pdfs = []
    for meta in soup.select("meta[content]"):
        name = (meta.get("name") or meta.get("property") or "").lower()
        if name == "citation_doi":
            doi = extract_doi(meta["content"])
        if name.startswith("citation_") or name.startswith("dc.creator"):
            metadata.append(f"{name}: {meta['content']}")
        if name == "citation_pdf_url":
            pdfs.append(urljoin(resource["url"], meta["content"]))
    for node in soup.select("script, style, nav, footer"):
        node.decompose()
    text = "\n".join(metadata) + "\n" + soup.get_text("\n", strip=True)
    return text[:60000], doi, pdfs


def merge_affiliations(existing, extracted):
    """Use source-page extraction for covered authors; retain other authors and missing records."""
    names = [row["author_name"] for row in extracted]
    result = [row for row in existing if not any(same_author(row.get("author_name"), name) for name in names)]
    # Different records may share an institution; never deduplicate across authors.
    for row in extracted:
        if not any(same_author(x.get("author_name"), row["author_name"]) and
                   normalise(x.get("institution")) == normalise(row["institution"]) and
                   x.get("raw_affiliation", "") == row.get("raw_affiliation", "") for x in result):
            result.append(row)
    return result


class AffiliationResolver:
    def __init__(self, http, cache, openalex_key="", email="", llm=None, publisher_fallback=True):
        self.http, self.cache = http, cache
        self.openalex_key, self.email = openalex_key, email
        self.llm, self.publisher_fallback = llm, publisher_fallback
        self.extract_prompt = prompt("extract_affiliations.txt")

    def _oa(self, path, **params):
        if self.openalex_key:
            params["api_key"] = self.openalex_key
        if self.email:
            params["mailto"] = self.email
        return self.http.json("https://api.openalex.org/" + path, params)

    def resolve(self, paper):
        cache_key = {"version": 2, "provider": "gemini-adc", "paper": paper.to_dict(), "fallback": self.publisher_fallback,
                     "model": self.llm.model if self.llm else None, "prompt": self.extract_prompt}
        hit = self.cache.get("affiliations", cache_key)
        if hit is not None:
            return hit
        notes = []
        work, score, method = None, 0, ""
        doi = paper.doi or extract_doi(paper.url)
        try:
            if doi:
                try:
                    work, method, score = self._oa("works/https://doi.org/" + doi), "doi", 1.0
                except FetchError:
                    pass
            if work is None:
                data = self._oa("works", search=paper.title, **{"per-page": 10})
                work, score = choose_work(paper, data.get("results", []))
                if work:
                    method = "title_year"
        except FetchError as exc:
            notes.append("OpenAlex: " + str(exc))
        rows = work_affiliations(work) if work else []
        oa_work_id = work.get("id", "") if work else ""
        if work:
            doi = extract_doi(work.get("doi")) or doi
            if len(work.get("authorships", [])) >= 100:
                notes.append("OpenAlex returned 100 authors; the author list may be truncated.")
        # Crossref supplies affiliation text independently when OpenAlex has gaps.
        if not rows or any(not row.get("institution") for row in rows):
            try:
                if doi:
                    cross_work = crossref_work(self.http.json("https://api.crossref.org/works/" + doi).get("message", {}))
                    cross_score = 1.0
                else:
                    data = self.http.json("https://api.crossref.org/works", {"query.title": paper.title, "rows": 5})
                    candidates = [crossref_work(item) for item in data.get("message", {}).get("items", [])]
                    cross_work, cross_score = choose_work(paper, candidates)
                if cross_work:
                    cross_rows = work_affiliations(cross_work)
                    for row in cross_rows:
                        row["affiliation_source"] = "crossref"
                    if not work:
                        method, score = ("crossref_doi" if doi else "crossref_title_year"), cross_score
                        doi = extract_doi(cross_work.get("doi")) or doi
                    supplemental = [r for r in cross_rows if r.get("institution") and not any(
                        existing.get("institution") and same_author(existing.get("author_name"), r.get("author_name")) for existing in rows)]
                    rows = merge_affiliations(rows, supplemental) or cross_rows
            except FetchError as exc:
                notes.append("Crossref: " + str(exc))
        incomplete = not rows or any(not r.get("institution") for r in rows) or bool(work and len(work.get("authorships", [])) >= 100)
        if self.publisher_fallback and self.llm and incomplete:
            urls = [paper.url, *paper.resources]
            if work:
                for loc in [work.get("primary_location"), work.get("best_oa_location")]:
                    if loc:
                        urls.extend([loc.get("landing_page_url"), loc.get("pdf_url")])
            if doi:
                urls.append("https://doi.org/" + doi)
            visited = set()
            while urls and len(visited) < 6:
                url = urls.pop(0)
                if not url or url in visited or urlparse(url).scheme not in {"https", "http"}:
                    continue
                visited.add(url)
                try:
                    resource = self.http.get(url)
                    text, found_doi, pdfs = page_text(resource)
                    urls.extend(pdfs)
                    if not text.strip():
                        notes.append("No extractable text in this page or PDF (OCR may be required).")
                        continue
                    result, response_id, _ = self.llm.parse(self.extract_prompt,
                        json.dumps({"paper_title": paper.title, "source_url": url, "text": text}, ensure_ascii=False), Extraction)
                    extracted = []
                    for item in result["affiliations"]:
                        # Require literal source evidence, including institution and author strings.
                        if not item["evidence"].strip() or item["evidence"] not in text:
                            continue
                        if normalise(item["institution"]) not in normalise(text) or normalise(item["author_name"]) not in normalise(text):
                            continue
                        extracted.append({**item, "affiliation_evidence": item["evidence"],
                                          "affiliation_source": "publisher_pdf_llm" if resource["kind"] == "pdf" else "publisher_html_llm",
                                          "affiliation_source_url": url, "affiliation_status": "extracted",
                                          "affiliation_note": "LLM evidence checked; response=" + response_id})
                    if extracted:
                        rows = merge_affiliations(rows, extracted)
                        doi = doi or found_doi
                        # A page may only cover some authors; continue to PDF if metadata still has gaps.
                        if not any(not r.get("institution") for r in rows):
                            break
                except Exception as exc:
                    # Do not log API bodies or credentials; audit the failing source and exception type.
                    notes.append(f"Publisher page/PDF extraction failed: {urlparse(url).hostname} ({type(exc).__name__})")
        if not rows:
            rows = [{"author_index": i, "author_name": author, "institution": "", "affiliation_status": "missing"}
                    for i, author in enumerate(paper.authors, 1)] or [{"author_name": "", "institution": "", "affiliation_status": "missing"}]
            notes.append("Author affiliations could not be confirmed for this citing paper; manual review is needed.")
        # A publisher page may reveal only a subset of the authors visible in Scholar.
        for author in paper.authors:
            if author and not any(marker in author for marker in ("…", "...", "et al")) and not any(same_author(author, r.get("author_name")) for r in rows):
                rows.append({"author_name": author, "institution": "", "affiliation_status": "missing",
                             "affiliation_note": "Scholar lists this author, but no affiliation was retrieved."})
        names = []
        for row in rows:
            matches = [index for index, name in enumerate(names) if same_author(name, row.get("author_name"))]
            if matches:
                row["author_index"] = matches[0] + 1
            else:
                names.append(row.get("author_name", ""))
                row["author_index"] = len(names)
            row.update({"citing_doi": doi, "openalex_work_id": oa_work_id,
                        "paper_match_method": method, "paper_match_score": score or ""})
            if notes:
                row["affiliation_note"] = " | ".join(filter(None, [row.get("affiliation_note"), *notes]))
        # Failures are retried next run; successful metadata is resumable.
        if any(r.get("institution") for r in rows) and not notes:
            self.cache.put("affiliations", cache_key, rows)
        return rows
