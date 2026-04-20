"""Throttled HTTP client with retry/backoff and a daily cap."""
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

import requests

import config

log = logging.getLogger("nptech.http")


class RateLimited(Exception):
    pass


class DailyCapReached(Exception):
    pass


class ThrottledClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})
        self.last_request_at = 0.0
        self.counter_path = config.STATE / "request_counter.json"
        self._counter = self._load_counter()

    def _load_counter(self):
        if self.counter_path.exists():
            try:
                data = json.loads(self.counter_path.read_text())
                if data.get("date") == str(date.today()):
                    return data
            except Exception:
                pass
        return {"date": str(date.today()), "count": 0}

    def _save_counter(self):
        self.counter_path.parent.mkdir(parents=True, exist_ok=True)
        self.counter_path.write_text(json.dumps(self._counter))

    def _check_cap(self):
        today = str(date.today())
        if self._counter.get("date") != today:
            self._counter = {"date": today, "count": 0}
        if config.DAILY_REQUEST_CAP and self._counter["count"] >= config.DAILY_REQUEST_CAP:
            raise DailyCapReached(
                f"Daily cap of {config.DAILY_REQUEST_CAP} requests reached"
            )

    def _throttle(self):
        elapsed = time.monotonic() - self.last_request_at
        if elapsed < config.REQUEST_DELAY_SEC:
            time.sleep(config.REQUEST_DELAY_SEC - elapsed)

    def get(self, url, params=None, stream=False):
        self._check_cap()
        for attempt in range(config.MAX_RETRIES):
            self._throttle()
            self.last_request_at = time.monotonic()
            self._counter["count"] += 1
            self._save_counter()
            try:
                resp = self.session.get(url, params=params, stream=stream, timeout=30)
            except requests.RequestException as e:
                wait = config.RETRY_BACKOFF_BASE * (2 ** attempt)
                log.warning("network error %s; sleeping %.0fs", e, wait)
                time.sleep(wait)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = config.RETRY_BACKOFF_BASE * (2 ** attempt)
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = max(wait, float(retry_after))
                log.warning(
                    "http %s on %s; sleeping %.0fs (attempt %d)",
                    resp.status_code, url, wait, attempt + 1,
                )
                time.sleep(wait)
                continue
            return resp
        raise RateLimited(f"Exhausted retries for {url}")

    @property
    def requests_today(self):
        return self._counter["count"]


def setup_logging():
    config.LOGS.mkdir(parents=True, exist_ok=True)
    logfile = config.LOGS / f"crawler-{date.today()}.log"
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[logging.FileHandler(logfile), logging.StreamHandler(sys.stdout)],
    )
