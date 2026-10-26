"""DeepSeek JSON actions. No SDK, credentials, or clinical text in error messages."""
import asyncio
import json
import os
import time
import httpx
from pydantic import ValidationError

from agent.contracts import action_json_schema, parse_action

from .prompts import PROMPT_VERSION, SYSTEM



class ProviderError(RuntimeError):
    pass


def safe_error_code(exc):
    if not isinstance(exc, ProviderError):
        return "MODEL_FAILED"
    value = str(exc)
    if value.startswith(("MODEL_", "DEEPSEEK_NOT_CONFIGURED")) and len(value) <= 2048:
        return value
    return "MODEL_FAILED"


class DeepSeekProvider:
    name = "deepseek"

    def __init__(self, *, transport=None):
        self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
        self.key = os.getenv("DEEPSEEK_API_KEY", "")
        self.base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        self.transport = transport

    def next_action(self, context, history, timeout):
        if not self.key:
            raise ProviderError("DEEPSEEK_NOT_CONFIGURED")
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps({
            "action_schema": action_json_schema(), "context": context, "observations": history,
            "tools": {
                "get_patient_snapshot": "arguments={}；当前授权患者住院摘要",
                "get_loop_state": "arguments={}；当前任务和报告证据编号",
                "get_lab_reports": "arguments 可含 limit/window/report_id/report_version；身份、项目、标本由服务器注入",
                "get_progress_notes": "arguments 仅 limit/window；默认整个本次调查窗口；text 是原文，digest 是版本快照",
                "get_result_acknowledgements": "arguments 仅 limit/window；自动限定当前任务与当前报告版本，无需传 evidence_id",
                "get_evidence": "arguments 可含 evidence_id，省略时读取当前任务证据；只读",
            },
        }, ensure_ascii=False)}]
        # One bounded HTTP request per runtime step. No hidden retries or fallback.
        started = time.monotonic()
        try:
            async def request_json():
                async with httpx.AsyncClient(transport=self.transport, timeout=max(0.01, timeout), follow_redirects=False) as client:
                    response = await client.post(self.base_url + "/chat/completions", headers={"Authorization": "Bearer " + self.key},
                        json={"model": self.model, "messages": messages, "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"}, "max_tokens": 4096})
                    response.raise_for_status()
                    try:
                        return response.json()
                    except ValueError:
                        raise ProviderError("MODEL_HTTP_BODY_NOT_JSON_" + response.headers.get("content-type", "unknown").split(";")[0]) from None
            async def bounded_request():
                return await asyncio.wait_for(request_json(), timeout=timeout)
            body = asyncio.run(bounded_request())
            choice = body["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ProviderError("MODEL_OUTPUT_INCOMPLETE")
            content = choice["message"]["content"]
            if not content or not content.strip():
                raise ProviderError("MODEL_EMPTY_CONTENT")
            content = content.strip()
            if content.startswith("```json") and content.endswith("```"):
                content = content[7:-3].strip()
            elif content.startswith("```") and content.endswith("```"):
                content = content[3:-3].strip()
            try:
                payload = json.loads(content)
            except ValueError:
                raise ProviderError("MODEL_ACTION_NOT_JSON") from None
            action = parse_action(payload)
            if time.monotonic() - started > timeout:
                raise ProviderError("MODEL_TIMEOUT")
            return action, body.get("usage", {})
        except ProviderError:
            raise
        except ValidationError as exc:
            issues = [{"location": e["loc"], "type": e["type"]} for e in exc.errors(include_input=False)]
            raise ProviderError("MODEL_OUTPUT_INVALID_" + json.dumps(issues, ensure_ascii=True)) from None
        except httpx.HTTPStatusError as exc:
            raise ProviderError("MODEL_HTTP_" + str(exc.response.status_code)) from None
        except Exception as exc:
            raise ProviderError("MODEL_REQUEST_FAILED_" + type(exc).__name__) from None


def configured():
    return bool(os.getenv("DEEPSEEK_API_KEY")) and os.getenv("AGENT_ENABLED", "true").lower() == "true"


def get_provider():
    return DeepSeekProvider()
