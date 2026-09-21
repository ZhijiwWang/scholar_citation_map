#!/usr/bin/env python3
"""Google Scholar citation affiliations -> CSV -> city coordinates -> world map."""
import logging


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("scholar_map")
    try:
        from scholar_map.progress import LoadingProgress
        with LoadingProgress("Loading Python dependencies"):
            from scholar_map.cli import main
        return main()
    except KeyboardInterrupt:
        log.warning("Interrupted by Ctrl+C.")
        return 130
    except ModuleNotFoundError as exc:
        log.error("Missing dependency %s. Run: python -m pip install -r requirements.txt", exc.name)
        return 1

if __name__ == "__main__":
    raise SystemExit(run())
