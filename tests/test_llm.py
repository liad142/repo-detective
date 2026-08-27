from __future__ import annotations

import io
import json
import unittest
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from repo_detective import llm as llm_module
from repo_detective.config import Settings
from repo_detective.models import ExternalServiceError


def make_settings(tool_choice: str = "required") -> Settings:
    root = Path(".")
    return Settings(
        data_dir=root,
        database_path=root / "unused.db",
        reports_dir=root / "reports",
        openai_api_key="fake",
        openai_base_url="https://example.test/v1",
        openai_model="fake-model",
        github_token=None,
        github_api_url="https://api.github.com",
        github_api_version="2026-03-10",
        http_timeout_seconds=7,
        github_cache_ttl_seconds=60,
        max_tool_items=50,
        max_file_chars=1000,
        llm_timeout_seconds=99,
        openai_tool_choice=tool_choice,
    )


class FakeHTTPResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def tool_call_body(name: str = "fake_tool") -> bytes:
    return json.dumps(
        {
            "id": "resp-1",
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {"name": name, "arguments": json.dumps({"a": 1})},
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    ).encode("utf-8")


def http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.test/v1/chat/completions", code, "Bad Request", {}, io.BytesIO(body.encode())
    )


class ToolChoiceFallbackTests(unittest.TestCase):
    def test_downgrades_to_auto_when_provider_rejects_required(self) -> None:
        payloads: list[dict] = []
        timeouts: list[int] = []

        def fake_urlopen(request, timeout=None):
            payloads.append(json.loads(request.data.decode("utf-8")))
            timeouts.append(timeout)
            if len(payloads) == 1:
                raise http_error(400, '{"error":{"message":"tool_choice value not supported"}}')
            return FakeHTTPResponse(tool_call_body())

        client = llm_module.OpenAICompatibleClient(make_settings())
        with mock.patch.object(llm_module.urllib.request, "urlopen", fake_urlopen), mock.patch(
            "sys.stderr", new=io.StringIO()
        ):
            first = client.choose_tool(messages=[], tools=[])
            second = client.choose_tool(messages=[], tools=[])

        self.assertEqual([p["tool_choice"] for p in payloads], ["required", "auto", "auto"])
        self.assertEqual(first.tool_calls[0].name, "fake_tool")
        self.assertEqual(second.tool_calls[0].name, "fake_tool")
        self.assertEqual(timeouts, [99, 99, 99], "LLM calls use LLM_TIMEOUT_SECONDS, not the GitHub timeout")

    def test_other_400s_are_not_retried(self) -> None:
        calls = 0

        def fake_urlopen(request, timeout=None):
            nonlocal calls
            calls += 1
            raise http_error(400, '{"error":{"message":"context length exceeded"}}')

        client = llm_module.OpenAICompatibleClient(make_settings())
        with mock.patch.object(llm_module.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaisesRegex(ExternalServiceError, "HTTP 400"):
                client.choose_tool(messages=[], tools=[])
        self.assertEqual(calls, 1)

    def test_configured_auto_is_sent_as_is(self) -> None:
        payloads: list[dict] = []

        def fake_urlopen(request, timeout=None):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return FakeHTTPResponse(tool_call_body())

        client = llm_module.OpenAICompatibleClient(make_settings("auto"))
        with mock.patch.object(llm_module.urllib.request, "urlopen", fake_urlopen):
            client.choose_tool(messages=[], tools=[])
        self.assertEqual(payloads[0]["tool_choice"], "auto")


if __name__ == "__main__":
    unittest.main()
