"""Thin wrapper over the Firecrawl v2 Monitor REST API.

Endpoints used (https://docs.firecrawl.dev/features/monitoring):
  POST   /v2/monitor                          create a monitor
  GET    /v2/monitor/{id}                     get a monitor
  DELETE /v2/monitor/{id}                     delete a monitor
  POST   /v2/monitor/{id}/run                 trigger a check immediately
  GET    /v2/monitor/{id}/checks              list checks
  GET    /v2/monitor/{id}/checks/{checkId}    check detail with per-page diffs
"""

import os
import re

import requests

API_BASE = "https://api.firecrawl.dev/v2"


class FirecrawlError(Exception):
    pass


class FirecrawlRateLimitError(FirecrawlError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class FirecrawlClient:
    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ.get("FIRECRAWL_API_KEY")
        if not self.api_key:
            raise FirecrawlError("FIRECRAWL_API_KEY is not set")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
        )

    def _request(self, method, path, **kwargs):
        resp = self.session.request(method, f"{API_BASE}{path}", timeout=60, **kwargs)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code == 429:
            detail = body.get("error") or body.get("message") or resp.text[:300]
            retry_after = None
            header = resp.headers.get("Retry-After", "")
            if header.isdigit():
                retry_after = int(header)
            else:
                match = re.search(r"retry after (\d+)s", detail)
                if match:
                    retry_after = int(match.group(1))
            raise FirecrawlRateLimitError(detail, retry_after=retry_after)
        if resp.status_code >= 400 or body.get("success") is False:
            detail = body.get("error") or body.get("message") or resp.text[:300]
            raise FirecrawlError(f"{method} {path} failed ({resp.status_code}): {detail}")
        return body.get("data", body)

    def create_monitor(
        self,
        name,
        urls,
        goal=None,
        schedule_text="every 6 hours",
        timezone="UTC",
        country="US",
    ):
        # Pin the scrape location: without it the proxy region can vary
        # between checks, and a geo-localized page (currency, language)
        # diffs as "changed" even when nothing real happened.
        payload = {
            "name": name,
            "schedule": {"text": schedule_text, "timezone": timezone},
            "targets": [
                {
                    "type": "scrape",
                    "urls": urls,
                    "scrapeOptions": {
                        "formats": ["markdown"],
                        "onlyMainContent": True,
                        "location": {"country": country},
                    },
                }
            ],
        }
        if goal:
            payload["goal"] = goal
        return self._request("POST", "/monitor", json=payload)

    def list_monitors(self, limit=100):
        return self._request("GET", "/monitor", params={"limit": limit})

    def get_monitor(self, monitor_id):
        return self._request("GET", f"/monitor/{monitor_id}")

    def delete_monitor(self, monitor_id):
        return self._request("DELETE", f"/monitor/{monitor_id}")

    def run_monitor(self, monitor_id):
        return self._request("POST", f"/monitor/{monitor_id}/run")

    def list_checks(self, monitor_id, limit=20):
        return self._request("GET", f"/monitor/{monitor_id}/checks", params={"limit": limit})

    def get_check(self, monitor_id, check_id, limit=50):
        return self._request(
            "GET", f"/monitor/{monitor_id}/checks/{check_id}", params={"limit": limit}
        )
