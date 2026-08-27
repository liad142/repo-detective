from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .models import InvestigationStatus, VerificationStatus, json_dumps, utc_now
from .storage import InvestigationStore


@dataclass(slots=True)
class GitHubResponse:
    status: int
    body: Any
    headers: dict[str, str]
    requested_url: str
    final_url: str
    from_cache: bool = False

    @property
    def rate_limit_remaining(self) -> int | None:
        value = self.headers.get("x-ratelimit-remaining")
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    @property
    def has_next_page(self) -> bool:
        return 'rel="next"' in self.headers.get("link", "")


class GitHubClient:
    """Small, read-only GitHub REST client with caching and explicit failures."""

    def __init__(self, settings: Settings, store: InvestigationStore):
        self.settings = settings
        self.store = store

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> GitHubResponse:
        if not endpoint.startswith("/") or "://" in endpoint:
            raise ValueError("GitHub endpoints must be relative API paths")

        # Models often send optional filters as "" instead of omitting them; GitHub
        # treats `author=` as a real filter that matches nothing, so drop empties.
        clean_params: dict[str, Any] = {}
        for key, value in (params or {}).items():
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            clean_params[key] = value

        query = urllib.parse.urlencode(clean_params, doseq=True)
        url = f"{self.settings.github_api_url}{endpoint}"
        if query:
            url = f"{url}?{query}"

        cache_key = hashlib.sha256(
            json_dumps({"method": "GET", "url": url}).encode("utf-8")
        ).hexdigest()
        cached = self.store.get_cache(cache_key)
        now = time.time()
        if cached and now - cached["fetched_at_epoch"] <= self.settings.github_cache_ttl_seconds:
            return GitHubResponse(
                status=cached["status"],
                body=cached["body"],
                headers=cached["headers"],
                requested_url=url,
                final_url=cached["final_url"],
                from_cache=True,
            )

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "repo-detective/0.1",
            "X-GitHub-Api-Version": self.settings.github_api_version,
        }
        if self.settings.github_token:
            headers["Authorization"] = f"Bearer {self.settings.github_token}"
        if cached and cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]

        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(
                request, timeout=self.settings.http_timeout_seconds
            ) as response:
                raw = response.read()
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                body = self._decode_body(raw, response_headers)
                result = GitHubResponse(
                    status=response.status,
                    body=body,
                    headers=response_headers,
                    requested_url=url,
                    final_url=response.geturl(),
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and cached:
                return GitHubResponse(
                    status=cached["status"],
                    body=cached["body"],
                    headers=cached["headers"],
                    requested_url=url,
                    final_url=cached["final_url"],
                    from_cache=True,
                )
            raw = exc.read()
            response_headers = {key.lower(): value for key, value in exc.headers.items()}
            result = GitHubResponse(
                status=exc.code,
                body=self._decode_body(raw, response_headers),
                headers=response_headers,
                requested_url=url,
                final_url=exc.geturl(),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return GitHubResponse(
                status=0,
                body={"message": f"Network error: {type(exc).__name__}: {exc}"},
                headers={},
                requested_url=url,
                final_url=url,
            )

        # Only cache answers that are stable facts: success and "does not exist".
        # Rate limits, auth failures, and server errors must be retried next time,
        # otherwise a single 403 would poison the cache for the whole TTL.
        if 200 <= result.status < 300 or result.status == 404:
            self.store.set_cache(
                cache_key,
                status=result.status,
                final_url=result.final_url,
                headers=result.headers,
                body=result.body,
                etag=result.headers.get("etag"),
                fetched_at_epoch=now,
            )
        return result

    @staticmethod
    def _decode_body(raw: bytes, headers: dict[str, str]) -> Any:
        if not raw:
            return None
        content_type = headers.get("content-type", "")
        if "json" in content_type:
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        return {"text": raw.decode("utf-8", errors="replace")[:20_000]}


def parse_repository_input(value: str) -> tuple[str, str]:
    """Accept owner/repo or a github.com URL, never an arbitrary host."""
    raw = value.strip()
    if not raw:
        raise ValueError("Repository URL is required")

    if "://" in raw:
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
            "github.com",
            "www.github.com",
        }:
            raise ValueError("Only public github.com repository URLs are supported")
        parts = [part for part in parsed.path.split("/") if part]
    else:
        parts = [part for part in raw.split("/") if part]

    if len(parts) != 2:
        raise ValueError("Expected a repository in owner/repo form")
    owner, repo = parts
    if repo.endswith(".git"):
        repo = repo[:-4]

    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    if not owner or not repo or any(char not in allowed for char in owner + repo):
        raise ValueError("Repository owner/name contains unsupported characters")
    return owner, repo


def response_verification_status(response: GitHubResponse) -> VerificationStatus:
    if 200 <= response.status < 300:
        return VerificationStatus.VERIFIED
    if response.status == 404:
        return VerificationStatus.NOT_FOUND
    if response.status in {403, 429}:
        message = ""
        if isinstance(response.body, dict):
            message = str(response.body.get("message", "")).lower()
        if response.rate_limit_remaining == 0 or "rate limit" in message:
            return VerificationStatus.RATE_LIMITED
    return VerificationStatus.UNAVAILABLE


def safe_api_message(response: GitHubResponse) -> str:
    if isinstance(response.body, dict):
        message = response.body.get("message") or response.body.get("text")
        if message:
            return str(message)[:1_000]
    return f"GitHub returned HTTP {response.status}"


class IntakeService:
    """Deterministic starting-point collection. This service never calls an LLM."""

    def __init__(self, client: GitHubClient, store: InvestigationStore):
        self.client = client
        self.store = store

    def run(self, investigation_id: str) -> dict[str, Any]:
        investigation = self.store.get_investigation(investigation_id)
        self.store.set_status(investigation_id, InvestigationStatus.INTAKE_RUNNING)
        owner = investigation["owner"]
        repo = investigation["repo"]

        repository_response = self.client.get(f"/repos/{owner}/{repo}")
        repository_status = response_verification_status(repository_response)
        repository_body = repository_response.body if isinstance(repository_response.body, dict) else {}

        if repository_status is not VerificationStatus.VERIFIED:
            evidence_id = self.store.add_evidence(
                investigation_id,
                step_id=None,
                source="intake",
                tool_name="get_repository",
                api_url=repository_response.final_url,
                html_url=None,
                request_parameters={},
                http_status=repository_response.status,
                verification_status=repository_status.value,
                summary=f"Could not fetch repository metadata: {safe_api_message(repository_response)}",
                normalized={},
                rate_limit_remaining=repository_response.rate_limit_remaining,
                raw=repository_response.body,
            )
            message = f"Repository intake failed ({evidence_id}): {safe_api_message(repository_response)}"
            self.store.set_status(
                investigation_id, InvestigationStatus.INTAKE_FAILED, error=message
            )
            raise RuntimeError(message)

        canonical = str(repository_body.get("full_name") or f"{owner}/{repo}")
        canonical_owner, canonical_repo = canonical.split("/", 1)
        html_url = str(repository_body.get("html_url") or f"https://github.com/{canonical}")

        repository_normalized = {
            "github_id": repository_body.get("id"),
            "full_name": canonical,
            "description": repository_body.get("description"),
            "stars": repository_body.get("stargazers_count", 0),
            "forks": repository_body.get("forks_count", 0),
            "open_issues_and_pull_requests": repository_body.get("open_issues_count", 0),
            "archived": bool(repository_body.get("archived")),
            "disabled": bool(repository_body.get("disabled")),
            "is_fork": bool(repository_body.get("fork")),
            "default_branch": repository_body.get("default_branch"),
            "license_name": (repository_body.get("license") or {}).get("spdx_id"),
            "created_at": repository_body.get("created_at"),
            "updated_at": repository_body.get("updated_at"),
            "pushed_at": repository_body.get("pushed_at"),
            "topics": repository_body.get("topics", []),
        }
        repo_evidence = self.store.add_evidence(
            investigation_id,
            step_id=None,
            source="intake",
            tool_name="get_repository",
            api_url=repository_response.final_url,
            html_url=html_url,
            request_parameters={},
            http_status=repository_response.status,
            verification_status=VerificationStatus.VERIFIED.value,
            summary=(
                f"Repository {canonical}: {repository_normalized['stars']} stars, "
                f"archived={repository_normalized['archived']}, "
                f"last push={repository_normalized['pushed_at']}"
            ),
            normalized=repository_normalized,
            rate_limit_remaining=repository_response.rate_limit_remaining,
            raw=repository_body,
        )

        release_response = self.client.get(
            f"/repos/{canonical_owner}/{canonical_repo}/releases/latest"
        )
        release_status = response_verification_status(release_response)
        release_body = release_response.body if isinstance(release_response.body, dict) else {}
        latest_release: dict[str, Any] | None = None
        if release_status is VerificationStatus.VERIFIED:
            latest_release = {
                "name": release_body.get("name"),
                "tag_name": release_body.get("tag_name"),
                "published_at": release_body.get("published_at"),
                "prerelease": bool(release_body.get("prerelease")),
                "draft": bool(release_body.get("draft")),
                "html_url": release_body.get("html_url"),
            }
            release_summary = (
                f"Latest GitHub Release is {latest_release['tag_name']}, "
                f"published {latest_release['published_at']}"
            )
        elif release_status is VerificationStatus.NOT_FOUND:
            release_summary = "No latest GitHub Release was found"
        else:
            release_summary = f"Latest release could not be verified: {safe_api_message(release_response)}"
        release_evidence = self.store.add_evidence(
            investigation_id,
            step_id=None,
            source="intake",
            tool_name="get_latest_release",
            api_url=release_response.final_url,
            html_url=latest_release.get("html_url") if latest_release else None,
            request_parameters={},
            http_status=release_response.status,
            verification_status=release_status.value,
            summary=release_summary,
            normalized=latest_release or {},
            rate_limit_remaining=release_response.rate_limit_remaining,
            raw=release_response.body,
        )

        contributor_response = self.client.get(
            f"/repos/{canonical_owner}/{canonical_repo}/contributors",
            {"per_page": 10, "anon": "true"},
        )
        contributor_status = response_verification_status(contributor_response)
        contributor_items = (
            contributor_response.body if isinstance(contributor_response.body, list) else []
        )
        contributors = [
            {
                "login": item.get("login") or item.get("name") or "anonymous",
                "contributions": int(item.get("contributions", 0)),
                "html_url": item.get("html_url"),
                "type": item.get("type", "Anonymous"),
            }
            for item in contributor_items[:10]
            if isinstance(item, dict)
        ]
        total_sample_contributions = sum(item["contributions"] for item in contributors)
        if contributors and total_sample_contributions:
            top_share = contributors[0]["contributions"] / total_sample_contributions
            contributor_summary = (
                f"Top 10 contributor sample: {contributors[0]['login']} accounts for "
                f"{top_share:.1%} of sampled contributions"
            )
        elif contributor_status is VerificationStatus.VERIFIED:
            top_share = None
            contributor_summary = "No contributors were returned"
        else:
            top_share = None
            contributor_summary = (
                f"Contributors could not be verified: {safe_api_message(contributor_response)}"
            )
        contributor_evidence = self.store.add_evidence(
            investigation_id,
            step_id=None,
            source="intake",
            tool_name="list_top_contributors",
            api_url=contributor_response.final_url,
            html_url=f"{html_url}/graphs/contributors",
            request_parameters={"per_page": 10, "anon": True},
            http_status=contributor_response.status,
            verification_status=contributor_status.value,
            summary=contributor_summary,
            normalized={
                "contributors": contributors,
                "top_share_within_sample": top_share,
                "sample_limit": 10,
            },
            rate_limit_remaining=contributor_response.rate_limit_remaining,
            raw=contributor_response.body,
        )

        gaps: list[str] = []
        if release_status is not VerificationStatus.VERIFIED:
            gaps.append(release_summary)
        if contributor_status is not VerificationStatus.VERIFIED:
            gaps.append(contributor_summary)

        intake = {
            "repository": {
                "input_url": investigation["input_url"],
                "owner": owner,
                "name": repo,
                "github_id": repository_body.get("id"),
                "canonical_full_name": canonical,
                "html_url": html_url,
            },
            "captured_at": utc_now(),
            "canonical_full_name": canonical,
            **repository_normalized,
            "latest_release": latest_release,
            "top_contributors": contributors,
            "evidence_ids": [repo_evidence, release_evidence, contributor_evidence],
            "verification_gaps": gaps,
        }
        self.store.save_intake(
            investigation_id,
            intake,
            canonical_full_name=canonical,
            html_url=html_url,
        )
        return intake
