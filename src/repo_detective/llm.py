from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

from .config import Settings
from .models import ExternalServiceError, LLMResponse, LLMToolCall


class _ToolChoiceRejected(ExternalServiceError):
    """The provider returned HTTP 400 complaining about the tool_choice value."""


class OpenAICompatibleClient:
    """Provider-neutral Chat Completions client using only the standard HTTP contract."""

    def __init__(self, settings: Settings):
        if not settings.openai_api_key or not settings.openai_model:
            raise ValueError("LLM settings are required")
        self.api_key = settings.openai_api_key
        self.base_url = settings.openai_base_url
        self.model = settings.openai_model
        self.timeout = settings.llm_timeout_seconds
        # "required" forces a function call on providers that honor it. Some
        # OpenAI-compatible gateways reject the value with HTTP 400; on that exact
        # signal we downgrade to "auto" for the rest of the process and retry once.
        # A schema rejection is not an inference call, so it is not budgeted.
        self.tool_choice = settings.openai_tool_choice

    def choose_tool(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        try:
            body = self._post(messages=messages, tools=tools)
        except _ToolChoiceRejected as exc:
            if self.tool_choice == "auto":
                raise ExternalServiceError(str(exc)) from exc
            print(
                "warning: provider rejected tool_choice='required'; "
                "retrying with tool_choice='auto' for the rest of this run",
                file=sys.stderr,
            )
            self.tool_choice = "auto"
            body = self._post(messages=messages, tools=tools)

        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExternalServiceError("LLM response is missing choices[0].message") from exc

        tool_calls: list[LLMToolCall] = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments = self._parse_arguments(function.get("arguments"))
            tool_calls.append(
                LLMToolCall(
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                    call_id=call.get("id"),
                )
            )

        # A few OpenAI-compatible providers still expose the legacy single-call field.
        if not tool_calls and message.get("function_call"):
            function = message["function_call"]
            tool_calls.append(
                LLMToolCall(
                    name=str(function.get("name") or ""),
                    arguments=self._parse_arguments(function.get("arguments")),
                )
            )

        usage = body.get("usage") or {}
        return LLMResponse(
            tool_calls=tool_calls,
            content=message.get("content"),
            provider_request_id=body.get("id"),
            input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens"),
            output_tokens=usage.get("completion_tokens") or usage.get("output_tokens"),
            raw=body,
        )

    def _post(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": self.tool_choice,
            "stream": False,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "repo-detective/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")[:2_000]
            detail = f"LLM provider returned HTTP {exc.code}: {raw}"
            if exc.code == 400 and "tool_choice" in raw:
                raise _ToolChoiceRejected(detail) from exc
            raise ExternalServiceError(detail) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ExternalServiceError(
                f"LLM provider network error: {type(exc).__name__}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ExternalServiceError("LLM provider returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise ExternalServiceError("LLM provider returned a non-object JSON body")
        return body

    @staticmethod
    def _parse_arguments(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"_invalid_json": value[:4_000]}
        return parsed if isinstance(parsed, dict) else {}

