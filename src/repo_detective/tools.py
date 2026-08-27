from __future__ import annotations

import base64
import binascii
import urllib.parse
from typing import Any, Callable

from .config import Settings
from .github import (
    GitHubClient,
    GitHubResponse,
    response_verification_status,
    safe_api_message,
)
from .models import ToolResult, ToolResultStatus, VerificationStatus
from .storage import InvestigationStore


COMMON_PROPERTIES: dict[str, Any] = {
    "rationale": {
        "type": "string",
        "description": "Why this is the highest-value next step given existing evidence.",
    },
    "question_to_answer": {
        "type": "string",
        "description": "The concrete uncertainty this tool call should resolve.",
    },
    "based_on_evidence_ids": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Evidence IDs that caused this investigative direction.",
    },
}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    merged = {**COMMON_PROPERTIES, **properties}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": merged,
                "required": [
                    "rationale",
                    "question_to_answer",
                    "based_on_evidence_ids",
                    *required,
                ],
                "additionalProperties": False,
            },
        },
    }


INVESTIGATION_TOOL_DEFINITIONS = [
    _tool(
        "get_repository",
        "Fetch metadata for the primary repository or a related fork.",
        {"repository": {"type": "string", "description": "owner/repo"}},
        ["repository"],
    ),
    _tool(
        "read_repository_file",
        "Read a bounded text file such as README, SECURITY.md, or a package manifest.",
        {
            "repository": {"type": "string", "description": "owner/repo; omit for primary"},
            "path": {"type": "string"},
            "ref": {"type": "string", "description": "Branch, tag, or commit SHA"},
        },
        ["path"],
    ),
    _tool(
        "list_commits",
        "List a bounded page of commits, optionally by author or date window.",
        {
            "repository": {"type": "string", "description": "owner/repo; omit for primary"},
            "author": {"type": "string"},
            "since": {"type": "string", "description": "ISO-8601 timestamp"},
            "until": {"type": "string", "description": "ISO-8601 timestamp"},
            "page": {"type": "integer", "minimum": 1, "maximum": 10},
            "per_page": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        [],
    ),
    _tool(
        "get_commit",
        "Inspect a commit, its author, signature verification, changed files, and bounded patches.",
        {
            "repository": {"type": "string", "description": "owner/repo; omit for primary"},
            "sha": {"type": "string"},
        },
        ["sha"],
    ),
    _tool(
        "compare_refs",
        "Compare two refs to inspect changes between releases, tags, or branches.",
        {
            "repository": {"type": "string", "description": "owner/repo; omit for primary"},
            "base": {"type": "string"},
            "head": {"type": "string"},
        },
        ["base", "head"],
    ),
    _tool(
        "list_contributors",
        "List a bounded page of contributors and contribution concentration.",
        {
            "repository": {"type": "string", "description": "owner/repo; omit for primary"},
            "page": {"type": "integer", "minimum": 1, "maximum": 10},
            "per_page": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        [],
    ),
    _tool(
        "list_issues",
        "List a bounded page of issues. Pull requests are excluded unless explicitly requested.",
        {
            "repository": {"type": "string", "description": "owner/repo; omit for primary"},
            "state": {"type": "string", "enum": ["open", "closed", "all"]},
            "sort": {"type": "string", "enum": ["created", "updated", "comments"]},
            "direction": {"type": "string", "enum": ["asc", "desc"]},
            "since": {"type": "string", "description": "ISO-8601 timestamp"},
            "include_pull_requests": {"type": "boolean"},
            "page": {"type": "integer", "minimum": 1, "maximum": 10},
            "per_page": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        [],
    ),
    _tool(
        "inspect_issue",
        "Inspect one issue and a bounded page of its comments for maintainer response evidence.",
        {
            "repository": {"type": "string", "description": "owner/repo; omit for primary"},
            "number": {"type": "integer", "minimum": 1},
            "comments_page": {"type": "integer", "minimum": 1, "maximum": 10},
            "comments_per_page": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        ["number"],
    ),
    _tool(
        "list_pull_requests",
        "List a bounded page of pull requests to examine community and maintainer activity.",
        {
            "repository": {"type": "string", "description": "owner/repo; omit for primary"},
            "state": {"type": "string", "enum": ["open", "closed", "all"]},
            "sort": {"type": "string", "enum": ["created", "updated", "popularity", "long-running"]},
            "direction": {"type": "string", "enum": ["asc", "desc"]},
            "page": {"type": "integer", "minimum": 1, "maximum": 10},
            "per_page": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        [],
    ),
    _tool(
        "list_releases",
        "List releases and bounded release notes to inspect cadence and unusual releases.",
        {
            "repository": {"type": "string", "description": "owner/repo; omit for primary"},
            "page": {"type": "integer", "minimum": 1, "maximum": 10},
            "per_page": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        [],
    ),
    _tool(
        "list_tags",
        "List tags when GitHub Releases are absent or incomplete.",
        {
            "repository": {"type": "string", "description": "owner/repo; omit for primary"},
            "page": {"type": "integer", "minimum": 1, "maximum": 10},
            "per_page": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        [],
    ),
    _tool(
        "list_forks",
        "List forks sorted by stars or recency to test whether activity moved elsewhere.",
        {
            "repository": {"type": "string", "description": "owner/repo; omit for primary"},
            "sort": {"type": "string", "enum": ["newest", "oldest", "stargazers", "watchers"]},
            "page": {"type": "integer", "minimum": 1, "maximum": 10},
            "per_page": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        [],
    ),
    _tool(
        "list_repository_advisories",
        "List published GitHub security advisories attached to a repository.",
        {
            "repository": {"type": "string", "description": "owner/repo; omit for primary"},
            "per_page": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        [],
    ),
    _tool(
        "search_global_advisories",
        "Search GitHub's global advisory database by ecosystem and package, including malware when requested.",
        {
            "ecosystem": {
                "type": "string",
                "enum": ["rubygems", "npm", "pip", "maven", "nuget", "composer", "go", "rust", "erlang", "actions", "pub", "other", "swift"],
            },
            "package": {"type": "string"},
            "version": {"type": "string"},
            "advisory_type": {"type": "string", "enum": ["reviewed", "malware", "unreviewed"]},
            "per_page": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        ["ecosystem", "package", "advisory_type"],
    ),
]


class GitHubToolRegistry:
    def __init__(self, client: GitHubClient, store: InvestigationStore, settings: Settings):
        self.client = client
        self.store = store
        self.settings = settings
        self._handlers: dict[str, Callable[..., ToolResult]] = {
            "get_repository": self.get_repository,
            "read_repository_file": self.read_repository_file,
            "list_commits": self.list_commits,
            "get_commit": self.get_commit,
            "compare_refs": self.compare_refs,
            "list_contributors": self.list_contributors,
            "list_issues": self.list_issues,
            "inspect_issue": self.inspect_issue,
            "list_pull_requests": self.list_pull_requests,
            "list_releases": self.list_releases,
            "list_tags": self.list_tags,
            "list_forks": self.list_forks,
            "list_repository_advisories": self.list_repository_advisories,
            "search_global_advisories": self.search_global_advisories,
        }

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return INVESTIGATION_TOOL_DEFINITIONS

    @property
    def names(self) -> set[str]:
        return set(self._handlers)

    def execute(
        self,
        name: str,
        *,
        investigation: dict[str, Any],
        step_id: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(
                status=ToolResultStatus.ERROR,
                summary=f"Unknown tool: {name}",
                limitations=["No GitHub request was made"],
            )
        clean = {
            key: value
            for key, value in arguments.items()
            if key not in {"rationale", "question_to_answer", "based_on_evidence_ids"}
        }
        try:
            return handler(investigation, step_id, clean)
        except (ValueError, TypeError) as exc:
            return ToolResult(
                status=ToolResultStatus.ERROR,
                summary=f"Tool arguments were invalid: {exc}",
                limitations=["No conclusion should be drawn from this failed tool call"],
            )
        except Exception as exc:  # defensive boundary: expected API errors are normalized earlier
            return ToolResult(
                status=ToolResultStatus.ERROR,
                summary=f"Tool execution failed safely: {type(exc).__name__}: {exc}",
                limitations=["The requested fact could not be verified"],
            )

    def _repository(self, investigation: dict[str, Any], arguments: dict[str, Any]) -> tuple[str, str]:
        value = arguments.get("repository") or investigation.get("canonical_full_name")
        if not value:
            value = f"{investigation['owner']}/{investigation['repo']}"
        if not isinstance(value, str) or value.count("/") != 1:
            raise ValueError("repository must use owner/repo form")
        owner, repo = value.split("/", 1)
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
        if not owner or not repo or any(char not in allowed for char in owner + repo):
            raise ValueError("repository contains unsupported characters")
        return owner, repo

    def _page_params(
        self, arguments: dict[str, Any], *, default_per_page: int = 30, max_per_page: int = 50
    ) -> tuple[int, int]:
        page = int(arguments.get("page", 1))
        per_page = int(arguments.get("per_page", default_per_page))
        if page < 1 or page > 10:
            raise ValueError("page must be between 1 and 10")
        per_page = min(per_page, max_per_page, self.settings.max_tool_items)
        if per_page < 1:
            raise ValueError("per_page must be greater than zero")
        return page, per_page

    @staticmethod
    def _truncate(value: Any, limit: int = 4_000) -> str | None:
        if value is None:
            return None
        text = str(value)
        return text if len(text) <= limit else text[:limit] + "\n...[truncated]"

    def _record(
        self,
        investigation_id: str,
        step_id: str,
        *,
        tool_name: str,
        response: GitHubResponse,
        params: dict[str, Any],
        summary: str,
        normalized: Any,
        html_url: str | None = None,
    ) -> str:
        verification = response_verification_status(response)
        return self.store.add_evidence(
            investigation_id,
            step_id=step_id,
            source="github_rest",
            tool_name=tool_name,
            api_url=response.final_url,
            html_url=html_url,
            request_parameters=params,
            http_status=response.status,
            verification_status=verification.value,
            summary=summary,
            normalized=normalized,
            rate_limit_remaining=response.rate_limit_remaining,
            raw=response.body,
        )

    def _failed_result(self, response: GitHubResponse, evidence_id: str, subject: str) -> ToolResult:
        verification = response_verification_status(response)
        status_map = {
            VerificationStatus.NOT_FOUND: ToolResultStatus.NOT_FOUND,
            VerificationStatus.RATE_LIMITED: ToolResultStatus.RATE_LIMITED,
            VerificationStatus.UNAVAILABLE: ToolResultStatus.UNAVAILABLE,
        }
        return ToolResult(
            status=status_map.get(verification, ToolResultStatus.ERROR),
            summary=f"{subject} could not be verified: {safe_api_message(response)}",
            evidence_ids=[evidence_id],
            limitations=["Do not interpret an unavailable result as evidence of absence"],
        )

    def get_repository(self, investigation: dict[str, Any], step_id: str, args: dict[str, Any]) -> ToolResult:
        owner, repo = self._repository(investigation, args)
        response = self.client.get(f"/repos/{owner}/{repo}")
        body = response.body if isinstance(response.body, dict) else {}
        normalized = {
            "full_name": body.get("full_name"),
            "description": body.get("description"),
            "stars": body.get("stargazers_count"),
            "forks": body.get("forks_count"),
            "open_issues_and_pull_requests": body.get("open_issues_count"),
            "archived": body.get("archived"),
            "fork": body.get("fork"),
            "default_branch": body.get("default_branch"),
            "pushed_at": body.get("pushed_at"),
            "updated_at": body.get("updated_at"),
            "parent": (body.get("parent") or {}).get("full_name"),
            "source": (body.get("source") or {}).get("full_name"),
        }
        if 200 <= response.status < 300:
            summary = (
                f"{normalized['full_name']}: {normalized['stars']} stars, "
                f"archived={normalized['archived']}, pushed_at={normalized['pushed_at']}"
            )
        else:
            summary = f"Repository {owner}/{repo} lookup failed: {safe_api_message(response)}"
        evidence = self._record(
            investigation["id"], step_id, tool_name="get_repository", response=response,
            params={"repository": f"{owner}/{repo}"}, summary=summary,
            normalized=normalized, html_url=body.get("html_url")
        )
        if not 200 <= response.status < 300:
            return self._failed_result(response, evidence, f"Repository {owner}/{repo}")
        return ToolResult(ToolResultStatus.SUCCESS, summary, normalized, [evidence])

    def read_repository_file(self, investigation: dict[str, Any], step_id: str, args: dict[str, Any]) -> ToolResult:
        owner, repo = self._repository(investigation, args)
        path = str(args.get("path", "")).strip("/")
        if not path or any(part in {".", ".."} for part in path.split("/")) or "\x00" in path:
            raise ValueError("path must be a safe repository-relative file path")
        ref = args.get("ref")
        params = {"ref": ref} if ref else {}
        encoded_path = "/".join(urllib.parse.quote(part, safe="") for part in path.split("/"))
        response = self.client.get(f"/repos/{owner}/{repo}/contents/{encoded_path}", params)
        body = response.body if isinstance(response.body, dict) else {}
        content = ""
        truncated = False
        if response.status == 200 and body.get("type") == "file":
            try:
                content = base64.b64decode(body.get("content", ""), validate=False).decode(
                    "utf-8", errors="replace"
                )
            except (ValueError, binascii.Error):
                content = ""
            if len(content) > self.settings.max_file_chars:
                content = content[: self.settings.max_file_chars]
                truncated = True
        normalized = {
            "repository": f"{owner}/{repo}", "path": path, "ref": ref,
            "sha": body.get("sha"), "size": body.get("size"), "content": content,
            "truncated": truncated,
        }
        summary = (
            f"Read {path} from {owner}/{repo} ({len(content)} characters"
            + (", truncated)" if truncated else ")")
            if response.status == 200 and content
            else f"File {path} could not be read: {safe_api_message(response)}"
        )
        evidence = self._record(
            investigation["id"], step_id, tool_name="read_repository_file", response=response,
            params={"repository": f"{owner}/{repo}", "path": path, "ref": ref},
            summary=summary, normalized=normalized, html_url=body.get("html_url")
        )
        if response.status != 200 or not content:
            return self._failed_result(response, evidence, f"File {path}")
        limitations = ["File content was truncated"] if truncated else []
        return ToolResult(ToolResultStatus.PARTIAL if truncated else ToolResultStatus.SUCCESS, summary, normalized, [evidence], limitations)

    def list_commits(self, investigation: dict[str, Any], step_id: str, args: dict[str, Any]) -> ToolResult:
        owner, repo = self._repository(investigation, args)
        page, per_page = self._page_params(args)
        params = {
            "author": args.get("author"), "since": args.get("since"), "until": args.get("until"),
            "page": page, "per_page": per_page,
        }
        response = self.client.get(f"/repos/{owner}/{repo}/commits", params)
        items = response.body if isinstance(response.body, list) else []
        commits = []
        authors: dict[str, int] = {}
        for item in items:
            commit = item.get("commit") or {}
            author = (item.get("author") or {}).get("login") or (commit.get("author") or {}).get("name") or "unknown"
            authors[author] = authors.get(author, 0) + 1
            commits.append({
                "sha": item.get("sha"), "author": author,
                "date": (commit.get("author") or {}).get("date"),
                "message": self._truncate(commit.get("message"), 500),
                "html_url": item.get("html_url"),
                "verified": (commit.get("verification") or {}).get("verified"),
            })
        top_author = max(authors, key=authors.get) if authors else None
        normalized = {
            "repository": f"{owner}/{repo}", "commits": commits, "sample_size": len(commits),
            "unique_authors": len(authors), "top_author": top_author,
            "top_author_share": (authors[top_author] / len(commits) if top_author and commits else None),
        }
        summary = (
            f"Found {len(commits)} commits in the requested page; {len(authors)} authors"
            + (f"; top author {top_author} contributed {normalized['top_author_share']:.1%}" if top_author else "")
            if response.status == 200 else f"Commits could not be verified: {safe_api_message(response)}"
        )
        evidence = self._record(
            investigation["id"], step_id, tool_name="list_commits", response=response,
            params=params, summary=summary, normalized=normalized,
            html_url=f"https://github.com/{owner}/{repo}/commits"
        )
        if response.status != 200:
            return self._failed_result(response, evidence, "Commits")
        limitations = [f"Only page {page} with at most {per_page} commits was inspected"]
        return ToolResult(ToolResultStatus.SUCCESS, summary, normalized, [evidence], limitations, response.has_next_page, page + 1 if response.has_next_page else None)

    def get_commit(self, investigation: dict[str, Any], step_id: str, args: dict[str, Any]) -> ToolResult:
        owner, repo = self._repository(investigation, args)
        sha = str(args.get("sha", "")).strip()
        if not sha or len(sha) > 100:
            raise ValueError("sha is required")
        response = self.client.get(f"/repos/{owner}/{repo}/commits/{urllib.parse.quote(sha, safe='')}")
        body = response.body if isinstance(response.body, dict) else {}
        commit = body.get("commit") or {}
        files = []
        for item in (body.get("files") or [])[:20]:
            files.append({
                "filename": item.get("filename"), "status": item.get("status"),
                "additions": item.get("additions"), "deletions": item.get("deletions"),
                "patch": self._truncate(item.get("patch"), 2_000),
            })
        normalized = {
            "sha": body.get("sha"),
            "author": (body.get("author") or {}).get("login") or (commit.get("author") or {}).get("name"),
            "date": (commit.get("author") or {}).get("date"),
            "message": self._truncate(commit.get("message"), 2_000),
            "verification": commit.get("verification"), "stats": body.get("stats"),
            "files": files, "files_truncated": len(body.get("files") or []) > 20,
        }
        summary = (
            f"Commit {str(body.get('sha') or sha)[:12]} changed {len(body.get('files') or [])} files; author={normalized['author']}"
            if response.status == 200 else f"Commit {sha} could not be verified: {safe_api_message(response)}"
        )
        evidence = self._record(
            investigation["id"], step_id, tool_name="get_commit", response=response,
            params={"repository": f"{owner}/{repo}", "sha": sha}, summary=summary,
            normalized=normalized, html_url=body.get("html_url")
        )
        if response.status != 200:
            return self._failed_result(response, evidence, f"Commit {sha}")
        limitations = ["Only the first 20 changed files and bounded patches are shown"] if normalized["files_truncated"] else []
        return ToolResult(ToolResultStatus.PARTIAL if limitations else ToolResultStatus.SUCCESS, summary, normalized, [evidence], limitations)

    def compare_refs(self, investigation: dict[str, Any], step_id: str, args: dict[str, Any]) -> ToolResult:
        owner, repo = self._repository(investigation, args)
        base = str(args.get("base", "")).strip()
        head = str(args.get("head", "")).strip()
        if not base or not head or len(base) > 200 or len(head) > 200:
            raise ValueError("base and head are required")
        endpoint = f"/repos/{owner}/{repo}/compare/{urllib.parse.quote(base, safe='')}...{urllib.parse.quote(head, safe='')}"
        response = self.client.get(endpoint)
        body = response.body if isinstance(response.body, dict) else {}
        files = [{
            "filename": item.get("filename"), "status": item.get("status"),
            "additions": item.get("additions"), "deletions": item.get("deletions"),
            "patch": self._truncate(item.get("patch"), 1_500),
        } for item in (body.get("files") or [])[:20]]
        normalized = {
            "status": body.get("status"), "ahead_by": body.get("ahead_by"),
            "behind_by": body.get("behind_by"), "total_commits": body.get("total_commits"),
            "files": files, "files_truncated": len(body.get("files") or []) > 20,
        }
        summary = (
            f"Compared {base}...{head}: {normalized['total_commits']} commits and {len(body.get('files') or [])} files"
            if response.status == 200 else f"Comparison could not be verified: {safe_api_message(response)}"
        )
        evidence = self._record(
            investigation["id"], step_id, tool_name="compare_refs", response=response,
            params={"repository": f"{owner}/{repo}", "base": base, "head": head},
            summary=summary, normalized=normalized, html_url=body.get("html_url")
        )
        if response.status != 200:
            return self._failed_result(response, evidence, "Ref comparison")
        limitations = ["Changed files were truncated to 20"] if normalized["files_truncated"] else []
        return ToolResult(ToolResultStatus.PARTIAL if limitations else ToolResultStatus.SUCCESS, summary, normalized, [evidence], limitations)

    def list_contributors(self, investigation: dict[str, Any], step_id: str, args: dict[str, Any]) -> ToolResult:
        owner, repo = self._repository(investigation, args)
        page, per_page = self._page_params(args)
        params = {"page": page, "per_page": per_page, "anon": "true"}
        response = self.client.get(f"/repos/{owner}/{repo}/contributors", params)
        items = response.body if isinstance(response.body, list) else []
        contributors = [{
            "login": item.get("login") or item.get("name") or "anonymous",
            "contributions": int(item.get("contributions", 0)), "html_url": item.get("html_url"),
        } for item in items]
        total = sum(item["contributions"] for item in contributors)
        normalized = {
            "contributors": contributors, "sample_contributions": total,
            "top_share_within_page": (contributors[0]["contributions"] / total if contributors and total else None),
            "page": page,
        }
        summary = (
            f"Found {len(contributors)} contributors on page {page}"
            + (f"; top contributor share within page is {normalized['top_share_within_page']:.1%}" if normalized["top_share_within_page"] is not None else "")
            if response.status == 200 else f"Contributors could not be verified: {safe_api_message(response)}"
        )
        evidence = self._record(
            investigation["id"], step_id, tool_name="list_contributors", response=response,
            params=params, summary=summary, normalized=normalized,
            html_url=f"https://github.com/{owner}/{repo}/graphs/contributors"
        )
        if response.status != 200:
            return self._failed_result(response, evidence, "Contributors")
        limitations = ["Contribution share is calculated only within the returned page"]
        return ToolResult(ToolResultStatus.SUCCESS, summary, normalized, [evidence], limitations, response.has_next_page, page + 1 if response.has_next_page else None)

    def list_issues(self, investigation: dict[str, Any], step_id: str, args: dict[str, Any]) -> ToolResult:
        owner, repo = self._repository(investigation, args)
        page, per_page = self._page_params(args)
        include_prs = bool(args.get("include_pull_requests", False))
        params = {
            "state": args.get("state", "open"), "sort": args.get("sort", "created"),
            "direction": args.get("direction", "desc"), "since": args.get("since"),
            "page": page, "per_page": per_page,
        }
        response = self.client.get(f"/repos/{owner}/{repo}/issues", params)
        raw_items = response.body if isinstance(response.body, list) else []
        items = raw_items if include_prs else [item for item in raw_items if "pull_request" not in item]
        issues = [{
            "number": item.get("number"), "title": self._truncate(item.get("title"), 500),
            "state": item.get("state"), "author": (item.get("user") or {}).get("login"),
            "comments": item.get("comments"), "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"), "closed_at": item.get("closed_at"),
            "html_url": item.get("html_url"),
        } for item in items]
        zero_comment = sum(1 for item in issues if item.get("comments") == 0)
        normalized = {
            "issues": issues, "returned_api_items": len(raw_items),
            "issues_after_filter": len(issues), "zero_comment_issues": zero_comment,
            "page": page,
        }
        summary = (
            f"Found {len(issues)} issues after filtering; {zero_comment} have zero comments"
            if response.status == 200 else f"Issues could not be verified: {safe_api_message(response)}"
        )
        evidence = self._record(
            investigation["id"], step_id, tool_name="list_issues", response=response,
            params={**params, "include_pull_requests": include_prs}, summary=summary,
            normalized=normalized, html_url=f"https://github.com/{owner}/{repo}/issues"
        )
        if response.status != 200:
            return self._failed_result(response, evidence, "Issues")
        limitations = ["Comment count does not identify whether a maintainer responded", f"Only page {page} was inspected"]
        return ToolResult(ToolResultStatus.SUCCESS, summary, normalized, [evidence], limitations, response.has_next_page, page + 1 if response.has_next_page else None)

    def inspect_issue(self, investigation: dict[str, Any], step_id: str, args: dict[str, Any]) -> ToolResult:
        owner, repo = self._repository(investigation, args)
        number = int(args.get("number", 0))
        if number < 1:
            raise ValueError("number must be positive")
        issue_response = self.client.get(f"/repos/{owner}/{repo}/issues/{number}")
        issue = issue_response.body if isinstance(issue_response.body, dict) else {}
        issue_normalized = {
            "number": issue.get("number"), "title": self._truncate(issue.get("title"), 500),
            "body": self._truncate(issue.get("body"), 8_000), "state": issue.get("state"),
            "author": (issue.get("user") or {}).get("login"), "association": issue.get("author_association"),
            "comments": issue.get("comments"), "created_at": issue.get("created_at"),
            "updated_at": issue.get("updated_at"), "closed_at": issue.get("closed_at"),
        }
        issue_summary = (
            f"Issue #{number}: {issue_normalized['title']} ({issue_normalized['state']}, {issue_normalized['comments']} comments)"
            if issue_response.status == 200 else f"Issue #{number} could not be verified: {safe_api_message(issue_response)}"
        )
        issue_ev = self._record(
            investigation["id"], step_id, tool_name="inspect_issue", response=issue_response,
            params={"repository": f"{owner}/{repo}", "number": number}, summary=issue_summary,
            normalized=issue_normalized, html_url=issue.get("html_url")
        )
        if issue_response.status != 200:
            return self._failed_result(issue_response, issue_ev, f"Issue #{number}")

        comments_page = int(args.get("comments_page", 1))
        comments_per_page = min(int(args.get("comments_per_page", 20)), 30)
        comments_response = self.client.get(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            {"page": comments_page, "per_page": comments_per_page},
        )
        raw_comments = comments_response.body if isinstance(comments_response.body, list) else []
        comments = [{
            "author": (item.get("user") or {}).get("login"),
            "association": item.get("author_association"),
            "created_at": item.get("created_at"), "updated_at": item.get("updated_at"),
            "body": self._truncate(item.get("body"), 2_000), "html_url": item.get("html_url"),
        } for item in raw_comments]
        maintainer_associations = {"OWNER", "MEMBER", "COLLABORATOR"}
        maintainer_comments = [item for item in comments if item.get("association") in maintainer_associations]
        comments_normalized = {
            "comments": comments, "maintainer_comment_count": len(maintainer_comments),
            "first_maintainer_response_at": maintainer_comments[0]["created_at"] if maintainer_comments else None,
            "page": comments_page,
        }
        comments_summary = (
            f"Inspected {len(comments)} comments; {len(maintainer_comments)} have maintainer associations"
            if comments_response.status == 200 else f"Issue comments could not be verified: {safe_api_message(comments_response)}"
        )
        comments_ev = self._record(
            investigation["id"], step_id, tool_name="inspect_issue_comments", response=comments_response,
            params={"repository": f"{owner}/{repo}", "number": number, "page": comments_page, "per_page": comments_per_page},
            summary=comments_summary, normalized=comments_normalized, html_url=issue.get("html_url")
        )
        combined = {"issue": issue_normalized, **comments_normalized}
        if comments_response.status != 200:
            return ToolResult(ToolResultStatus.PARTIAL, f"{issue_summary}. {comments_summary}", combined, [issue_ev, comments_ev], ["Issue metadata is verified, comments are unavailable"])
        return ToolResult(ToolResultStatus.SUCCESS, f"{issue_summary}. {comments_summary}", combined, [issue_ev, comments_ev], ["Maintainer identity is inferred from GitHub author_association"], comments_response.has_next_page, comments_page + 1 if comments_response.has_next_page else None)

    def list_pull_requests(self, investigation: dict[str, Any], step_id: str, args: dict[str, Any]) -> ToolResult:
        owner, repo = self._repository(investigation, args)
        page, per_page = self._page_params(args)
        params = {
            "state": args.get("state", "open"), "sort": args.get("sort", "created"),
            "direction": args.get("direction", "desc"), "page": page, "per_page": per_page,
        }
        response = self.client.get(f"/repos/{owner}/{repo}/pulls", params)
        raw = response.body if isinstance(response.body, list) else []
        pulls = [{
            "number": item.get("number"), "title": self._truncate(item.get("title"), 500),
            "state": item.get("state"), "draft": item.get("draft"),
            "author": (item.get("user") or {}).get("login"),
            "created_at": item.get("created_at"), "updated_at": item.get("updated_at"),
            "closed_at": item.get("closed_at"), "merged_at": item.get("merged_at"),
            "html_url": item.get("html_url"),
        } for item in raw]
        normalized = {"pull_requests": pulls, "page": page}
        summary = (
            f"Found {len(pulls)} pull requests on page {page}"
            if response.status == 200 else f"Pull requests could not be verified: {safe_api_message(response)}"
        )
        evidence = self._record(
            investigation["id"], step_id, tool_name="list_pull_requests", response=response,
            params=params, summary=summary, normalized=normalized,
            html_url=f"https://github.com/{owner}/{repo}/pulls"
        )
        if response.status != 200:
            return self._failed_result(response, evidence, "Pull requests")
        return ToolResult(ToolResultStatus.SUCCESS, summary, normalized, [evidence], [f"Only page {page} was inspected"], response.has_next_page, page + 1 if response.has_next_page else None)

    def list_releases(self, investigation: dict[str, Any], step_id: str, args: dict[str, Any]) -> ToolResult:
        owner, repo = self._repository(investigation, args)
        page, per_page = self._page_params(args, default_per_page=10, max_per_page=30)
        params = {"page": page, "per_page": per_page}
        response = self.client.get(f"/repos/{owner}/{repo}/releases", params)
        raw = response.body if isinstance(response.body, list) else []
        releases = [{
            "id": item.get("id"), "tag_name": item.get("tag_name"), "name": item.get("name"),
            "draft": item.get("draft"), "prerelease": item.get("prerelease"),
            "created_at": item.get("created_at"), "published_at": item.get("published_at"),
            "author": (item.get("author") or {}).get("login"),
            "body": self._truncate(item.get("body"), 2_000), "html_url": item.get("html_url"),
        } for item in raw]
        normalized = {"releases": releases, "page": page}
        summary = (
            f"Found {len(releases)} releases on page {page}"
            if response.status == 200 else f"Releases could not be verified: {safe_api_message(response)}"
        )
        evidence = self._record(
            investigation["id"], step_id, tool_name="list_releases", response=response,
            params=params, summary=summary, normalized=normalized,
            html_url=f"https://github.com/{owner}/{repo}/releases"
        )
        if response.status != 200:
            return self._failed_result(response, evidence, "Releases")
        return ToolResult(ToolResultStatus.SUCCESS, summary, normalized, [evidence], [f"Only page {page} and bounded release notes were inspected"], response.has_next_page, page + 1 if response.has_next_page else None)

    def list_tags(self, investigation: dict[str, Any], step_id: str, args: dict[str, Any]) -> ToolResult:
        owner, repo = self._repository(investigation, args)
        page, per_page = self._page_params(args)
        params = {"page": page, "per_page": per_page}
        response = self.client.get(f"/repos/{owner}/{repo}/tags", params)
        raw = response.body if isinstance(response.body, list) else []
        tags = [{"name": item.get("name"), "sha": (item.get("commit") or {}).get("sha"), "zipball_url": item.get("zipball_url")} for item in raw]
        normalized = {"tags": tags, "page": page}
        summary = f"Found {len(tags)} tags on page {page}" if response.status == 200 else f"Tags could not be verified: {safe_api_message(response)}"
        evidence = self._record(
            investigation["id"], step_id, tool_name="list_tags", response=response,
            params=params, summary=summary, normalized=normalized,
            html_url=f"https://github.com/{owner}/{repo}/tags"
        )
        if response.status != 200:
            return self._failed_result(response, evidence, "Tags")
        return ToolResult(ToolResultStatus.SUCCESS, summary, normalized, [evidence], [f"Only page {page} was inspected"], response.has_next_page, page + 1 if response.has_next_page else None)

    def list_forks(self, investigation: dict[str, Any], step_id: str, args: dict[str, Any]) -> ToolResult:
        owner, repo = self._repository(investigation, args)
        page, per_page = self._page_params(args)
        params = {"sort": args.get("sort", "stargazers"), "page": page, "per_page": per_page}
        response = self.client.get(f"/repos/{owner}/{repo}/forks", params)
        raw = response.body if isinstance(response.body, list) else []
        forks = [{
            "full_name": item.get("full_name"), "stars": item.get("stargazers_count"),
            "forks": item.get("forks_count"), "open_issues_and_pull_requests": item.get("open_issues_count"),
            "archived": item.get("archived"), "pushed_at": item.get("pushed_at"),
            "updated_at": item.get("updated_at"), "default_branch": item.get("default_branch"),
            "html_url": item.get("html_url"),
        } for item in raw]
        normalized = {"forks": forks, "sort": params["sort"], "page": page}
        summary = f"Found {len(forks)} forks sorted by {params['sort']}" if response.status == 200 else f"Forks could not be verified: {safe_api_message(response)}"
        evidence = self._record(
            investigation["id"], step_id, tool_name="list_forks", response=response,
            params=params, summary=summary, normalized=normalized,
            html_url=f"https://github.com/{owner}/{repo}/forks"
        )
        if response.status != 200:
            return self._failed_result(response, evidence, "Forks")
        return ToolResult(ToolResultStatus.SUCCESS, summary, normalized, [evidence], ["A popular fork is not automatically an endorsed successor"], response.has_next_page, page + 1 if response.has_next_page else None)

    def list_repository_advisories(self, investigation: dict[str, Any], step_id: str, args: dict[str, Any]) -> ToolResult:
        owner, repo = self._repository(investigation, args)
        per_page = min(int(args.get("per_page", 20)), 30, self.settings.max_tool_items)
        if per_page < 1:
            raise ValueError("per_page must be greater than zero")
        params = {"per_page": per_page, "direction": "desc", "sort": "published"}
        response = self.client.get(f"/repos/{owner}/{repo}/security-advisories", params)
        raw = response.body if isinstance(response.body, list) else []
        advisories = [self._normalize_advisory(item) for item in raw]
        normalized = {"advisories": advisories}
        summary = f"Found {len(advisories)} published repository advisories" if response.status == 200 else f"Repository advisories could not be verified: {safe_api_message(response)}"
        evidence = self._record(
            investigation["id"], step_id, tool_name="list_repository_advisories", response=response,
            params=params, summary=summary, normalized=normalized,
            html_url=f"https://github.com/{owner}/{repo}/security/advisories"
        )
        if response.status != 200:
            return self._failed_result(response, evidence, "Repository security advisories")
        return ToolResult(ToolResultStatus.SUCCESS, summary, normalized, [evidence], ["An empty result does not cover advisories published only in external databases", "Only the first cursor page is inspected"], response.has_next_page, None)

    def search_global_advisories(self, investigation: dict[str, Any], step_id: str, args: dict[str, Any]) -> ToolResult:
        ecosystem = str(args.get("ecosystem", "")).strip().lower()
        package = str(args.get("package", "")).strip()
        advisory_type = str(args.get("advisory_type", "reviewed"))
        if not ecosystem or not package or len(package) > 300:
            raise ValueError("ecosystem and package are required")
        per_page = min(int(args.get("per_page", 20)), 30, self.settings.max_tool_items)
        if per_page < 1:
            raise ValueError("per_page must be greater than zero")
        affects = package + (f"@{args['version']}" if args.get("version") else "")
        params = {
            "ecosystem": ecosystem, "affects": affects, "type": advisory_type,
            "per_page": per_page,
        }
        response = self.client.get("/advisories", params)
        raw = response.body if isinstance(response.body, list) else []
        advisories = [self._normalize_advisory(item) for item in raw]
        normalized = {"ecosystem": ecosystem, "affects": affects, "advisory_type": advisory_type, "advisories": advisories}
        summary = f"Global advisory search found {len(advisories)} {advisory_type} advisories affecting {affects}" if response.status == 200 else f"Global advisories could not be verified: {safe_api_message(response)}"
        evidence = self._record(
            investigation["id"], step_id, tool_name="search_global_advisories", response=response,
            params=params, summary=summary, normalized=normalized,
            html_url="https://github.com/advisories"
        )
        if response.status != 200:
            return self._failed_result(response, evidence, "Global security advisories")
        limitations = [f"Only advisory type '{advisory_type}' was queried", "Package identity comes from the investigation and must be verified against a manifest", "Only the first cursor page is inspected"]
        return ToolResult(ToolResultStatus.SUCCESS, summary, normalized, [evidence], limitations, response.has_next_page, None)

    def _normalize_advisory(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "ghsa_id": item.get("ghsa_id"), "cve_id": item.get("cve_id"),
            "type": item.get("type"), "severity": item.get("severity"),
            "summary": self._truncate(item.get("summary"), 1_000),
            "description": self._truncate(item.get("description"), 3_000),
            "published_at": item.get("published_at"), "withdrawn_at": item.get("withdrawn_at"),
            "html_url": item.get("html_url"), "vulnerabilities": item.get("vulnerabilities", []),
            "cvss": item.get("cvss"), "cwes": item.get("cwes", []),
        }
