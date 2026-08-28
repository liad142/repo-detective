from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import Settings
from .github import GitHubResponse

# GitHub advisory ecosystem names → OSV ecosystem names.
OSV_ECOSYSTEMS = {
    "npm": "npm", "pip": "PyPI", "go": "Go", "rust": "crates.io", "maven": "Maven",
    "nuget": "NuGet", "rubygems": "RubyGems", "composer": "Packagist", "pub": "Pub",
    "erlang": "Hex", "actions": "GitHub Actions", "swift": "SwiftURL",
}


class OSVClient:
    """Keyless POST to the OSV vulnerability database. Returns the same response shape
    as GitHubClient so tools can record evidence through one path."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def query(self, *, ecosystem: str, package: str, version: str | None) -> GitHubResponse:
        payload: dict[str, Any] = {"package": {"name": package, "ecosystem": ecosystem}}
        if version:
            payload["version"] = version
        url = f"{self.settings.osv_api_url}/v1/query"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "repo-detective/0.1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.http_timeout_seconds) as response:
                raw = response.read()
                headers = {key.lower(): value for key, value in response.headers.items()}
                return GitHubResponse(response.status, self._decode(raw), headers, url, url)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            headers = {key.lower(): value for key, value in exc.headers.items()}
            return GitHubResponse(exc.code, self._decode(raw), headers, url, url)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return GitHubResponse(0, {"message": f"Network error: {type(exc).__name__}: {exc}"}, {}, url, url)

    @staticmethod
    def _decode(raw: bytes) -> Any:
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"text": raw.decode("utf-8", errors="replace")[:4_000]}
