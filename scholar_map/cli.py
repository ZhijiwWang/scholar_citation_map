from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .progress import LoadingProgress

LOG = logging.getLogger("scholar_map")


# Load expensive dependencies only when a command actually needs them.
def LLM(*args, **kwargs):
    from .llm import LLM as implementation
    return implementation(*args, **kwargs)


def geocode_rows(*args, **kwargs):
    from .geocode import geocode_rows as implementation
    return implementation(*args, **kwargs)


def create_map(*args, **kwargs):
    with LoadingProgress("Loading map dependencies"):
        from .mapping import create_map as implementation
    return implementation(*args, **kwargs)


def crawl(*args, **kwargs):
    from .pipeline import crawl as implementation
    return implementation(*args, **kwargs)

ROOT = Path(__file__).resolve().parent.parent


def add_map_options(parser):
    parser.add_argument("--cluster-km", type=float, default=25, help="Merge nearby coordinates within this distance in km; 0 merges identical coordinates only")
    parser.add_argument("--count-mode", choices=["rows", "paper-institution", "unique-papers"], default="rows")
    parser.add_argument("--min-confidence", choices=["high", "medium", "low"], default="medium")
    parser.add_argument("--color-scale", choices=["linear", "log"], default="linear")


def add_api_options(parser):
    parser.add_argument("--model", default=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    parser.add_argument("--no-web-search", action="store_true", help="Use model knowledge without online verification of cities and coordinates")


def add_crawl_options(parser):
    parser.add_argument("profile_url", help="Google Scholar profile URL")
    parser.add_argument("--provider", choices=["direct", "serpapi"], default="direct")
    parser.add_argument("--delay", type=float, default=3, help="Minimum seconds between requests to the same service")
    parser.add_argument("--max-papers", type=int, default=0, help="0 means unlimited")
    parser.add_argument("--max-citations", type=int, default=0, help="Maximum citing papers per profile paper; 0 means unlimited")
    parser.add_argument("--no-publisher-fallback", action="store_true", help="Use OpenAlex/Crossref metadata only; skip Gemini extraction from publisher pages/PDFs")


def main(argv=None):
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Scholar citation affiliations -> CSV -> Gemini city coordinates -> world frequency map")
    parser.add_argument("--env-file", type=Path, help="Additional .env file; place this option before the subcommand")
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "crawl", "geocode", "map"):
        sub = commands.add_parser(command)
        sub.add_argument("--output-dir", type=Path, default=Path("output"))
        if command in {"run", "crawl", "geocode"}:
            sub.add_argument("--cache-dir", type=Path, default=Path(".cache"))
            sub.add_argument("--refresh", action="store_true", help="Ignore cached results and request again (may incur additional API charges)")
        if command in {"run", "crawl"}:
            add_crawl_options(sub)
        if command in {"run", "geocode", "crawl"}:
            add_api_options(sub)
        if command in {"run", "map"}:
            add_map_options(sub)
        if command in {"geocode", "map"}:
            sub.add_argument("input_csv", type=Path)
    args = parser.parse_args(argv)
    if args.env_file:
        load_dotenv(args.env_file, override=True)
        # argparse defaults were resolved before custom env loading.
        actual_argv = argv if argv is not None else __import__("sys").argv[1:]
        if hasattr(args, "model") and not any(arg == "--model" or arg.startswith("--model=") for arg in actual_argv):
            args.model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LOG.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    # SDK debug logs may include request payloads; verbosity is restricted to our own logger.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("google.genai").setLevel(logging.WARNING)
    try:
        if hasattr(args, "cluster_km") and not 0 <= args.cluster_km <= 1000:
            raise ValueError("--cluster-km must be between 0 and 1000.")
        if hasattr(args, "max_papers") and (args.max_papers < 0 or args.max_citations < 0 or args.delay < 0):
            raise ValueError("Record limits and request delays cannot be negative.")
        if args.command in {"map", "geocode"}:
            from .common import read_csv, Cache
            rows = read_csv(args.input_csv)
            if args.command == "geocode":
                llm = LLM(os.getenv("GOOGLE_CLOUD_PROJECT", ""), args.model, os.getenv("GOOGLE_CLOUD_LOCATION", "global"))
                report = geocode_rows(rows, llm, Cache(args.cache_dir, args.refresh),
                                      args.output_dir / "institutions_geocoded.csv", not args.no_web_search,
                                      args.output_dir / "geocode_report.json")
                LOG.info("Geocoding finished: %s", report["status_counts"])
                return 2 if report["status_counts"].get("error") else 0
        else:
            with LoadingProgress("Loading paper retrieval dependencies"):
                from .scholar import DirectScholar, SerpScholar, profile_id
                from .common import Cache, Http
                from .affiliations import AffiliationResolver
            profile_id(args.profile_url)
            LOG.info("Citation source: %s", "SerpApi" if args.provider == "serpapi" else "Direct Google Scholar access")
            if args.provider == "direct" and os.getenv("SERPAPI_API_KEY"):
                LOG.warning("SerpApi is not enabled. Add --provider serpapi to use your configured key.")
            if args.provider == "serpapi" and not os.getenv("SERPAPI_API_KEY"):
                raise ValueError("--provider serpapi requires SERPAPI_API_KEY in .env.")
            llm = None
            if args.command == "run" or not args.no_publisher_fallback:
                llm = LLM(os.getenv("GOOGLE_CLOUD_PROJECT", ""), args.model, os.getenv("GOOGLE_CLOUD_LOCATION", "global"))
            cache = Cache(args.cache_dir, args.refresh)
            http = Http(cache, args.delay)
            provider = DirectScholar(http) if args.provider == "direct" else SerpScholar(http, os.getenv("SERPAPI_API_KEY", ""))
            resolver = AffiliationResolver(http, cache, os.getenv("OPENALEX_API_KEY", ""), os.getenv("CONTACT_EMAIL", ""),
                                           llm, not args.no_publisher_fallback)
            LOG.info("Reading the profile from %s or the cache...", "SerpApi" if args.provider == "serpapi" else "Google Scholar")
            rows, crawl_report = crawl(args.profile_url, provider, resolver, args.output_dir, args.max_papers, args.max_citations)
            LOG.info("Retrieval finished: %d profile papers, %d citation edges, %d affiliation rows", crawl_report["source_papers"], crawl_report["citation_edges"], len(rows))
            if args.command == "crawl":
                return 0 if crawl_report["complete"] else 2
            if not rows and not crawl_report["complete"]:
                LOG.error("Citation retrieval is incomplete with no affiliation rows. Stopped before geocoding or map generation.")
                if crawl_report["profile_status"] == "blocked":
                    LOG.error("Set SERPAPI_API_KEY in .env and retry with --provider serpapi.")
                LOG.info("Retrieval report: %s", args.output_dir / "crawl_report.json")
                if (args.output_dir / "world_map.html").exists():
                    LOG.warning("The existing map was not updated and does not represent this run.")
                return 2
            geo_report = geocode_rows(rows, llm, cache, args.output_dir / "institutions_geocoded.csv",
                                     not args.no_web_search, args.output_dir / "geocode_report.json")
        if args.command in {"run", "map"}:
            report = create_map(rows, args.output_dir, args.cluster_km, args.count_mode, args.min_confidence, args.color_scale)
            LOG.info("Map saved: %s (%d locations, %d eligible rows)", args.output_dir / "world_map.html", report["cluster_count"], report["mapped_rows"])
        if args.command == "run" and (not crawl_report["complete"] or geo_report["status_counts"].get("error")):
            LOG.warning("Partial results saved; retrieval or API requests are incomplete. Review the reports and retry.")
            return 2
        return 0
    except KeyboardInterrupt:
        LOG.warning("Interrupted. Cached results and saved CSV files are available for the next run.")
        return 130
    except (ValueError, OSError) as exc:
        LOG.error("%s", exc)
        return 1
