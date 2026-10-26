import asyncio
import json
import httpx
from pydantic import ValidationError
from agent.llm.deepseek import DeepSeekProvider
from .contracts import Decision, DraftError
from .prompts import SYSTEM


class AdmissionProvider(DeepSeekProvider):
    def decide(self, observations, timeout):
        if not self.key:
            raise DraftError("DEEPSEEK_NOT_CONFIGURED")
        read = {o['tool_name'] for o in observations}
        remaining = [t for t in ('read_patient_profile','read_interview','read_examinations') if t not in read]
        async def request():
            async with httpx.AsyncClient(transport=self.transport, timeout=timeout, follow_redirects=False) as client:
                response = await client.post(self.base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.key},
                    json={"model": self.model, "messages": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": json.dumps({"schema": Decision.model_json_schema(),
                         "task": "起草当前患者本次住院的入院记录，必须依据以下观察资料。",
                         "required_next_action": "call_tool" if remaining else "submit_draft",
                         "available_tools": remaining,
                         "already_read": sorted(read),
                         "instruction": "只允许读取available_tools中的工具，禁止重复读取。三个工具都读完后必须提交九章节草稿。",
                         "call_example": {"action":"call_tool","tool_name":"read_interview","sections":[]},
                         "observations": observations}, ensure_ascii=False)}],
                        "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"}, "max_tokens": 6000})
                response.raise_for_status()
                try:
                    return response.json()
                except ValueError:
                    raise DraftError('MODEL_HTTP_BODY_NOT_JSON_'+response.headers.get('content-type','unknown').split(';')[0]) from None
        try:
            body = asyncio.run(asyncio.wait_for(request(), timeout))
            choice = body["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise DraftError("MODEL_OUTPUT_INCOMPLETE")
            content = choice["message"]["content"].strip()
            if content.startswith("```json") and content.endswith("```"):
                content = content[7:-3].strip()
            return Decision.model_validate(json.loads(content))
        except DraftError:
            raise
        except httpx.HTTPStatusError as exc:
            raise DraftError("MODEL_HTTP_" + str(exc.response.status_code)) from None
        except httpx.RequestError as exc:
            raise DraftError("MODEL_NETWORK_" + type(exc).__name__) from None
        except (TimeoutError, asyncio.TimeoutError):
            raise DraftError("MODEL_TIMEOUT") from None
        except ValidationError as exc:
            issue = exc.errors(include_input=False)[0]
            location = '.'.join(map(str, issue['loc']))[:35]
            raise DraftError(('MODEL_FORMAT_'+location+'_'+issue['type'])[:100]) from None
        except (ValueError, KeyError, IndexError, AttributeError, TypeError) as exc:
            raise DraftError("MODEL_FORMAT_INVALID_"+type(exc).__name__) from None
        except Exception:
            raise DraftError("MODEL_REQUEST_OR_FORMAT_FAILED") from None


def get_provider():
    return AdmissionProvider()
