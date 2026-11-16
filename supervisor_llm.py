"""
슈퍼바이저 검토관의 LLM 호출 (웹 검색 없이, 제출 도구 1개로 구조화된 결과를 받는다).

.env 키:
  ANTHROPIC_API_KEY / ANTHROPIC_EFFORT       Claude
  OPENROUTER_API_KEY / OPENROUTER_MODEL      OpenRouter (DeepSeek 등)
  PROVIDER                                   기본 접속처: anthropic 또는 openrouter
"""

import os
import re
from pathlib import Path

DEFAULT_MODEL = "claude-sonnet-5-5"
OPENROUTER_BASE_URL = "https://openrouter.ai/api"
OPENROUTER_DEFAULT_MODEL = "deepseek/deepseek-v4-pro"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
MAX_TOKENS = 16000


def load_dotenv(path):
    """KEY=VALUE 형식의 .env를 읽어 환경변수로 설정한다. 이미 있는 환경변수·빈 값은 건드리지 않고, 줄 끝 '# 주석'은 무시."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in ('"', "'") and value[0] in value[1:]:
            value = value[1:value.index(value[0], 1)]
        else:
            value = re.split(r"(?:^|\s+)#", value, maxsplit=1)[0].strip()
        if key and value:
            os.environ.setdefault(key, value)


def _anthropic():
    """LLM을 실제로 쓸 때만 불러온다 (status·review --no-llm 등은 anthropic 패키지 없이도 동작)."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic 패키지가 필요합니다:  pip install -r requirements.txt") from None
    return anthropic


def make_backend(provider=None, model=None):
    """(client, model, 추가 요청 옵션, 표시 이름). 키는 각 접속처 것만 넘긴다."""
    anthropic = _anthropic()
    provider = (provider or os.environ.get("PROVIDER") or "anthropic").strip().lower()
    if provider == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY가 없습니다 (.env)")
        client = anthropic.Anthropic(auth_token=key, base_url=OPENROUTER_BASE_URL)
        return client, model or os.environ.get("OPENROUTER_MODEL") or OPENROUTER_DEFAULT_MODEL, {}, "OpenRouter"
    if provider != "anthropic":
        raise RuntimeError(f"PROVIDER={provider!r}는 anthropic 또는 openrouter여야 합니다")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY가 없습니다 (.env)")
    extra = {}
    effort = os.environ.get("ANTHROPIC_EFFORT", "").strip().lower()
    if effort in EFFORT_LEVELS:
        extra["output_config"] = {"effort": effort}
    return anthropic.Anthropic(api_key=key), model or DEFAULT_MODEL, extra, "Anthropic"


_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(value, schema, path="입력"):
    """schema에 맞지 않는 부분을 오류 메시지 목록으로 돌려준다 (빈 목록이면 통과)."""
    types = schema.get("type")
    if types:
        types = types if isinstance(types, list) else [types]
        if not any(_TYPE_CHECKS[t](value) for t in types):
            return [f"{path}: {'/'.join(types)} 타입이어야 합니다 (받은 값: {value!r:.60})"]
    errors = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {schema['enum']} 중 하나여야 합니다 (받은 값: {value!r})")
    if isinstance(value, dict):
        errors += [f"{path}.{k}: 필수 항목이 없습니다" for k in schema.get("required", []) if k not in value]
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                errors += validate(value[k], sub, f"{path}.{k}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors += validate(item, schema["items"], f"{path}[{i}]")
    return errors


def submit(backend, system, prompt, tool, max_rounds=5):
    """모델이 tool로 결과를 제출하게 하고, 스키마 검증을 통과한 입력을 돌려준다."""
    client, model, extra, _ = backend
    messages = [{"role": "user", "content": prompt}]
    nudged = False
    for _ in range(max_rounds):
        resp = client.messages.create(model=model, max_tokens=MAX_TOKENS, system=system,
                                      tools=[tool], messages=messages, **extra)
        if resp.stop_reason == "refusal":
            d = resp.stop_details
            raise RuntimeError("모델이 요청을 거절했습니다" + (f" ({d.category}: {d.explanation})" if d else ""))
        call = next((b for b in resp.content if b.type == "tool_use" and b.name == tool["name"]), None)
        messages.append({"role": "assistant", "content": resp.content})
        if call:
            errors = (["응답이 max_tokens에서 잘렸습니다. 더 짧게 다시 제출하세요."] if resp.stop_reason == "max_tokens"
                      else validate(call.input, tool["input_schema"]))
            if not errors:
                return call.input
            messages.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": call.id, "is_error": True,
                "content": "제출 형식 오류를 고쳐 다시 제출하세요:\n" + "\n".join(errors[:20])}]})
            continue
        if nudged:
            break
        messages.append({"role": "user", "content": f"{tool['name']} 도구로 제출해 주세요."})
        nudged = True
    raise RuntimeError("검토관이 결과를 제출하지 않았습니다")
