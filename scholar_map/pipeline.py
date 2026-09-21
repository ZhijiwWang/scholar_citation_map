from __future__ import annotations

from collections import Counter
from pathlib import Path

from .common import FetchError, LOG, ScholarBlocked, stable_id, write_csv, write_json
from .scholar import profile_id


def crawl(profile_url, provider, resolver, output_dir, max_papers=0, max_citations=0):
    user = profile_id(profile_url)
    output_dir = Path(output_dir)
    rows, sources, edges = [], [], []
    report = {"profile_url": profile_url, "profile_status": "running", "limitations": [], "papers": []}

    def checkpoint():
        write_csv(output_dir / "affiliations.csv", rows)
        write_csv(output_dir / "source_papers.csv", sources,
                  ["id", "title", "year", "url", "cited_by_count", "cites"])
        write_csv(output_dir / "citation_edges.csv", edges,
                  ["source_paper_id", "citing_paper_id", "citing_paper_title", "citing_paper_year", "citing_paper_url"])
        report["source_papers"] = len(sources)
        report["citation_edges"] = len(edges)
        report["affiliation_rows"] = len(rows)
        report["affiliation_status_counts"] = dict(Counter(row.get("affiliation_status", "missing") for row in rows))
        report["rows_with_institution"] = sum(bool(row.get("institution")) for row in rows)
        report["rows_missing_institution"] = sum(not row.get("institution") for row in rows)
        report["possible_truncated_author_edges"] = len({(row["source_paper_id"], row["citing_paper_id"]) for row in rows
                                                        if "100 authors" in row.get("affiliation_note", "")})
        report["complete"] = report["profile_status"] == "exhausted" and all(p["status"] in {"exhausted", "no_citations"} for p in report["papers"])
        report["affiliations_complete"] = (report["complete"] and report["rows_missing_institution"] == 0
                                           and report["possible_truncated_author_edges"] == 0)
        write_json(output_dir / "crawl_report.json", report)

    try:
        for paper in provider.profile(user):
            if max_papers and len(sources) >= max_papers:
                report["profile_status"] = "limited"
                report["limitations"].append("The number of profile papers was limited by --max-papers.")
                break
            LOG.info("Profile paper %d: %s (%d citations)", len(sources) + 1, paper.title, paper.cited_by_count)
            sources.append(paper.to_dict())
            paper_report = {"source_paper_id": paper.id, "title": paper.title, "expected_citations": paper.cited_by_count,
                            "fetched_citations": 0, "status": "running"}
            report["papers"].append(paper_report)
            try:
                for citing in provider.citations(paper):
                    if max_citations and paper_report["fetched_citations"] >= max_citations:
                        paper_report["status"] = "limited"
                        break
                    paper_report["fetched_citations"] += 1
                    edges.append({"source_paper_id": paper.id, "citing_paper_id": citing.id, "citing_paper_title": citing.title,
                                  "citing_paper_year": citing.year, "citing_paper_url": citing.url})
                    LOG.info("  Citing paper %d: %s", paper_report["fetched_citations"], citing.title)
                    affiliations = resolver.resolve(citing)
                    for index, affiliation in enumerate(affiliations, 1):
                        rows.append({**affiliation, "record_id": stable_id(user, paper.id, citing.id, index),
                                     "profile_url": profile_url, "source_paper_id": paper.id, "source_paper_title": paper.title,
                                     "source_paper_year": paper.year, "source_cited_by_count": paper.cited_by_count,
                                     "citing_paper_id": citing.id, "citing_paper_title": citing.title,
                                     "citing_paper_year": citing.year, "citing_paper_url": citing.url})
                    checkpoint()
                else:
                    if paper_report["fetched_citations"] < paper.cited_by_count:
                        paper_report["status"] = "shortfall"
                        paper_report["note"] = "Fewer citations were retrieved than the profile count; possible causes include platform limits, merged versions, or index changes."
                    else:
                        paper_report["status"] = "exhausted" if paper_report["fetched_citations"] else "no_citations"
            except ScholarBlocked:
                paper_report["status"] = "blocked"
                raise
            except FetchError as exc:
                paper_report.update(status="error", note=str(exc))
                LOG.warning("Citation retrieval is incomplete for this paper: %s", exc)
            checkpoint()
        else:
            report["profile_status"] = "exhausted"
    except ScholarBlocked as exc:
        report.update(profile_status="blocked", error=str(exc))
        LOG.warning("%s", exc)
    except FetchError as exc:
        report.update(profile_status="error", error=str(exc))
        LOG.warning("Profile retrieval is incomplete: %s", exc)
    except KeyboardInterrupt:
        report["profile_status"] = "interrupted"
        checkpoint()
        raise
    checkpoint()
    return rows, report
