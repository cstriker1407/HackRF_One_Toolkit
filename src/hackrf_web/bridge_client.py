"""Thin HTTP client for RF_Bridge. The Web frontend never touches hackrf_*;
everything goes through here (and the /api/* transparent proxy in app.py)."""
from __future__ import annotations
import requests


class BridgeClient:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    # ---- reads ----
    def health(self) -> dict:
        return requests.get(f"{self.base}/health", timeout=5).json()

    def device(self) -> dict:
        return requests.get(f"{self.base}/device", timeout=12).json()

    def captures(self) -> dict:
        return requests.get(f"{self.base}/captures", timeout=10).json()

    # ---- writes (also available via transparent proxy) ----
    def stream(self, job_id: str):
        """Return a streaming requests.Response for SSE proxying."""
        return requests.get(f"{self.base}/jobs/{job_id}/stream",
                            stream=True, timeout=(10, 3600))
