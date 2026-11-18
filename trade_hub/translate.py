"""
구매대행 직접 검색어: 한글이 들어 있으면 일본 쇼핑몰(라쿠텐)에서 통하는 일본어 검색어로 바꾼다.

  · 한글이 없으면(일본어·모델명만) 그대로 쓴다 — 비용 없음
  · 한글이 있으면 Claude Haiku로 한 번 번역 (검색 1번에 1원 미만). 같은 검색어는 data/translate_cache.json에서 다시 씀
  · 키: trade_hub/.env 의 ANTHROPIC_API_KEY (슈퍼바이저와 같은 키)
"""

import json
import re
import threading

from trade_hub import db

MODEL = "claude-haiku-5-5"
HANGUL = re.compile(r"[가-힣ㄱ-ㆎ]")
_LOCK = threading.Lock()

SYSTEM = ("You convert Korean shopping search queries into the Japanese query a shopper would type on Rakuten Ichiba. "
          "Use the brand and product names as they are written in the Japanese market (katakana or the official name). "
          "Keep model numbers, sizes and Latin letters exactly as given. Do not add words that are not in the query. "
          "Reply with the Japanese query only — one line, no quotes, no explanation.")


def has_korean(text):
    return bool(HANGUL.search(text or ""))


def cache_path():
    return db.default_path().parent / "translate_cache.json"


def _load():
    try:
        return json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def original_of(japanese):
    """번역 결과 → 원래 한글 검색어 (화면에 '← 세이코 시계'로 보여 주기용). 없으면 None."""
    return next((k for k, v in _load().items() if v == japanese), None)


def _call_llm(query):
    import sys
    root = str(db.PKG_DIR.parent)
    if root not in sys.path:                          # 다른 폴더에서 실행해도 저장소 루트 모듈을 찾도록
        sys.path.append(root)
    import supervisor_llm as llm                      # 저장소 루트 모듈 (.env 읽기·키 확인 공용)
    llm.load_dotenv(db.PKG_DIR.parent / ".env")
    client, model, _, _ = llm.make_backend("anthropic", MODEL)
    resp = client.messages.create(model=model, max_tokens=200, system=SYSTEM,
                                  messages=[{"role": "user", "content": query}])
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def to_japanese(query, call=None):
    """한글이 섞인 검색어 → 일본어 검색어. 실패하면 RuntimeError (사람이 일본어로 직접 넣으면 됨)."""
    query = " ".join(query.split())
    if not has_korean(query):
        return query
    with _LOCK:
        cache = _load()
    if query in cache:
        return cache[query]
    try:
        out = (call or _call_llm)(query)
    except RuntimeError:
        raise
    except Exception as e:                            # 네트워크·API 오류
        raise RuntimeError(f"번역 실패 ({type(e).__name__}): 일본어로 직접 입력해 주세요") from e
    out = " ".join((out or "").strip().strip('"「」\'').split())
    if not out or "\n" in out or len(out) > 100 or has_korean(out):
        raise RuntimeError(f"번역 결과가 이상합니다({out[:40]!r}): 일본어로 직접 입력해 주세요")
    with _LOCK:
        cache = _load()
        cache[query] = out
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    return out
