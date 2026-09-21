from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from .common import FetchError, normalise, stable_id

BASE = "https://scholar.google.com"


def profile_id(url):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not (parsed.hostname or "").startswith("scholar.google."):
        raise ValueError("Provide a Google Scholar profile URL, such as https://scholar.google.com/citations?user=YOUR_USER_ID")
    user = parse_qs(parsed.query).get("user", [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", user):
        raise ValueError("The profile URL must contain a valid user parameter.")
    return user


def count(value):
    return int(re.sub(r"\D", "", str(value or "")) or "0")


def cites_id(url):
    return parse_qs(urlparse(url or "").query).get("cites", [""])[0]


@dataclass
class Paper:
    id: str
    title: str
    year: str = ""
    url: str = ""
    authors: list = field(default_factory=list)
    cited_by_count: int = 0
    cites: str = ""
    resources: list = field(default_factory=list)
    doi: str = ""

    def to_dict(self):
        return asdict(self)


def parse_profile(html):
    soup = BeautifulSoup(html, "html.parser")
    if not soup.select_one("#gsc_a_b"):
        raise FetchError("Unrecognized Scholar profile layout; an empty page cannot be treated as completion.")
    papers = []
    for row in soup.select("tr.gsc_a_tr"):
        title = row.select_one("a.gsc_a_at")
        if not title:
            continue
        url = urljoin(BASE, title.get("href", ""))
        pid = parse_qs(urlparse(url).query).get("citation_for_view", [stable_id(url)])[0]
        citation = row.select_one("a.gsc_a_ac")
        byline = row.select_one(".gs_gray")
        year = row.select_one(".gsc_a_y")
        papers.append(Paper(pid, title.get_text(" ", strip=True),
                            year.get_text(strip=True) if year else "", url,
                            [s.strip() for s in byline.get_text().split(",")] if byline else [],
                            count(citation.get_text()) if citation else 0,
                            cites_id(citation.get("href")) if citation else ""))
    more = soup.select_one("#gsc_bpf_more")
    has_more = bool(more and not more.has_attr("disabled") and more.get("aria-disabled") != "true")
    return papers, has_more


def parse_citations(html):
    soup = BeautifulSoup(html, "html.parser")
    if not soup.select_one("#gs_res_ccl_mid"):
        raise FetchError("Unrecognized Scholar citation layout; completeness cannot be confirmed.")
    papers = []
    for row in soup.select(".gs_r.gs_or"):
        title = row.select_one(".gs_rt")
        if not title:
            continue
        # [PDF] and [CITATION] labels are not part of the paper title.
        for badge in title.select(".gs_ctc, .gs_ctu"):
            badge.decompose()
        link = title.select_one("a")
        url = urljoin(BASE, link.get("href", "")) if link else ""
        byline = row.select_one(".gs_a")
        text = byline.get_text(" ", strip=True) if byline else ""
        year = re.search(r"\b(?:19|20)\d{2}\b", text)
        resources = [urljoin(BASE, a["href"]) for a in row.select(".gs_or_ggsm a[href]")]
        name = title.get_text(" ", strip=True)
        papers.append(Paper(row.get("data-cid") or stable_id(normalise(name), url), name,
                            year.group() if year else "", url,
                            [s.strip() for s in text.split(" - ")[0].split(",") if s.strip()],
                            resources=resources))
    next_start = None
    for link in soup.select("#gs_n a[href]"):
        if link.select_one(".gs_ico_nav_next"):
            next_start = int(parse_qs(urlparse(link["href"]).query).get("start", ["0"])[0])
    return papers, next_start


class DirectScholar:
    def __init__(self, http):
        self.http = http

    def profile(self, user):
        start, seen = 0, set()
        while True:
            result = self.http.get(BASE + "/citations", {"user": user, "hl": "en", "cstart": start, "pagesize": 100}, scholar=True)
            papers, more = parse_profile(result["body"])
            fresh = [p for p in papers if p.id not in seen]
            if more and not fresh:
                raise FetchError("Scholar profile pagination did not advance; stopped to avoid a loop.")
            for paper in fresh:
                seen.add(paper.id)
                yield paper
            if not more:
                break
            start += 100

    def citations(self, paper):
        cluster = paper.cites
        if not cluster and paper.cited_by_count > 0:
            result = self.http.get(paper.url, scholar=True)
            soup = BeautifulSoup(result["body"], "html.parser")
            clusters = [cites_id(a.get("href")) for a in soup.select("a[href]") if cites_id(a.get("href"))]
            if clusters:
                cluster = clusters[0]
        if not cluster:
            if paper.cited_by_count:
                raise FetchError("The paper has a citation count but no Scholar citing-papers link.")
            return
        start, seen = 0, set()
        while True:
            result = self.http.get(BASE + "/scholar", {"cites": cluster, "hl": "en", "start": start, "num": 20}, scholar=True)
            papers, next_start = parse_citations(result["body"])
            fresh = [p for p in papers if p.id not in seen]
            if next_start is not None and not fresh:
                raise FetchError("Scholar repeated a citation page; no further citations could be retrieved.")
            for citing in fresh:
                seen.add(citing.id)
                yield citing
            if next_start is None:
                break
            if next_start <= start:
                raise FetchError("Scholar citation pagination did not advance.")
            start = next_start


class SerpScholar:
    def __init__(self, http, api_key):
        if not api_key:
            raise ValueError("--provider serpapi requires SERPAPI_API_KEY in .env.")
        self.http, self.api_key = http, api_key

    def _get(self, **params):
        return self.http.json("https://serpapi.com/search.json", {**params, "api_key": self.api_key, "hl": "en"})

    def profile(self, user):
        start, seen = 0, set()
        while True:
            data = self._get(engine="google_scholar_author", author_id=user, start=start, num=100)
            if "articles" not in data:
                raise FetchError("SerpApi profile response has no articles field; completeness cannot be confirmed.")
            articles = data["articles"]
            fresh = 0
            for item in articles:
                pid = item.get("citation_id") or stable_id(item.get("title"), item.get("link"))
                if pid in seen:
                    continue
                seen.add(pid)
                fresh += 1
                cited = item.get("cited_by") or {}
                yield Paper(pid, item["title"], str(item.get("year") or ""), item.get("link", ""),
                            [s.strip() for s in item.get("authors", "").split(",")],
                            count(cited.get("value")), cites_id(cited.get("link")))
            pagination = data.get("serpapi_pagination") or {}
            more = bool(pagination.get("next") or pagination.get("next_link") or len(articles) == 100)
            if not more:
                break
            if not fresh:
                raise FetchError("SerpApi repeated a profile page.")
            next_url = pagination.get("next") or pagination.get("next_link")
            next_start = int(parse_qs(urlparse(next_url or "").query).get("start", [str(start + len(articles))])[0])
            if next_start <= start:
                raise FetchError("SerpApi profile pagination did not advance.")
            start = next_start

    def citations(self, paper):
        if not paper.cites:
            if paper.cited_by_count:
                raise FetchError("The paper has a citation count but SerpApi returned no cites link.")
            return
        start, seen = 0, set()
        while True:
            data = self._get(engine="google_scholar", cites=paper.cites, start=start, num=20)
            if "organic_results" not in data and "search_information" not in data:
                raise FetchError("SerpApi citation response has neither results nor search status.")
            items = data.get("organic_results", [])
            fresh = 0
            for item in items:
                pid = item.get("result_id") or stable_id(item.get("title"), item.get("link"))
                if pid in seen:
                    continue
                seen.add(pid)
                fresh += 1
                info = item.get("publication_info") or {}
                year = re.search(r"\b(?:19|20)\d{2}\b", info.get("summary", ""))
                authors = [a.get("name", "") for a in info.get("authors", [])]
                if not authors:
                    authors = [a.strip() for a in info.get("summary", "").split(" - ")[0].split(",") if a.strip()]
                yield Paper(pid, item["title"], year.group() if year else "", item.get("link", ""), authors,
                            resources=[r["link"] for r in item.get("resources", []) if r.get("link")])
            pagination = data.get("serpapi_pagination") or {}
            next_url = pagination.get("next") or pagination.get("next_link")
            if not next_url:
                break
            next_start = int(parse_qs(urlparse(next_url).query).get("start", [str(start + 20)])[0])
            if not fresh or next_start <= start:
                raise FetchError("SerpApi citation pagination repeated or did not advance.")
            start = next_start
