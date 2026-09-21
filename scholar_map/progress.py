"""Progress messages for slow local dependency loading; standard-library only."""
from __future__ import annotations

import logging
import threading
import time

LOG = logging.getLogger("scholar_map")


class LoadingProgress:
    def __init__(self, label, interval=10):
        self.label = label
        self.interval = interval
        self.finished = threading.Event()

    def __enter__(self):
        self.started = time.monotonic()
        LOG.info("%s…", self.label)
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()
        return self

    def _heartbeat(self):
        while not self.finished.wait(self.interval):
            LOG.info("%s: waiting %.0f seconds.", self.label, time.monotonic() - self.started)

    def __exit__(self, exc_type, exc, traceback):
        self.finished.set()
        self.thread.join(timeout=0.2)
        if exc_type is None:
            LOG.info("%s completed in %.1f seconds.", self.label, time.monotonic() - self.started)
