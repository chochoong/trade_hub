"""
구매대행 직접 검색창의 [영어]·[일본어]·[중국어] 버튼: 한글 검색어를 그 나라 쇼핑몰에서 통하는 검색어로 바꾼다.

  · 한글이 없으면 그대로 돌려줌 (비용 없음)
  · 한글이 있으면 Claude Haiku로 한 번 번역 (1원 미만). 같은 검색어·언어는 data/translate_cache.json에서 다시 씀
  · 키: trade_hub/.env 의 ANTHROPIC_API_KEY
  · 검색 버튼만 누르면(언어 버튼 없이) 번역은 buyagent(main.py --search)가 한다 — 이건 미리 보고 고치기용
"""

import json
import re
import threading

from trade_hub import db

MODEL = "claude-haiku-5-5"
HANGUL = re.compile(r"[가-힣ㄱ-ㆎ]")
_LOCK = threading.Lock()

LANGS = {   # 언어 → (화면 이름, 검색할 쇼핑몰 설명 — 번역 지시에 씀)
    "ja": ("일본어", "Japanese", "Rakuten Ichiba (Japan)"),
    "en": ("영어", "English", "AliExpress"),
    "zh": ("중국어", "Simplified Chinese", "Taobao / 1688 (China)"),
}


def system_prompt(lang):
    _, language, shop = LANGS[lang]
    return (f"You convert Korean shopping search queries into the {language} query a shopper would type on {shop}. "
            f"Use the brand and product names as they are written in that market. "
            "Keep model numbers, sizes and Latin letters exactly as given. Do not add words that are not in the query. "
            f"Reply with the {language} query only — one line, no quotes, no explanation.")


def has_korean(text):
    return bool(HANGUL.search(text or ""))


def cache_path():
    return db.default_path().parent / "translate_cache.json"


def _load():
    try:
        return json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _call_llm(query, lang):
    import sys
    root = str(db.PKG_DIR.parent)
    if root not in sys.path:                          # 다른 폴더에서 실행해도 저장소 루트 모듈을 찾도록
        sys.path.append(root)
    import supervisor_llm as llm                      # .env 읽기·키 확인 공용
    llm.load_dotenv(db.PKG_DIR.parent / ".env")
    client, model, _, _ = llm.make_backend("anthropic", MODEL)
    resp = client.messages.create(model=model, max_tokens=200, system=system_prompt(lang),
                                  messages=[{"role": "user", "content": query}])
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def to_lang(query, lang, call=None):
    """한글이 섞인 검색어 → lang 검색어. 실패하면 RuntimeError (사람이 직접 입력하면 됨)."""
    if lang not in LANGS:
        raise ValueError(f"언어: {lang!r}")
    query = " ".join(str(query).split())
    if not has_korean(query):
        return query
    key = f"{lang}:{query}"
    with _LOCK:
        cached = _load().get(key)
    if cached:
        return cached
    name = LANGS[lang][0]
    try:
        out = (call or _call_llm)(query, lang)
    except RuntimeError as e:                         # 키 없음 등 (메시지 그대로)
        raise RuntimeError(f"번역 실패: {e} → {name}로 직접 입력해 주세요") from e
    except Exception as e:                            # 네트워크·API 오류
        raise RuntimeError(f"번역 실패 ({type(e).__name__}): {name}로 직접 입력해 주세요") from e
    out = (out or "").strip()
    if "\n" in out:
        raise RuntimeError(f"번역 결과가 여러 줄입니다: {name}로 직접 입력해 주세요")
    out = " ".join(out.strip('"「」『』\'').split())
    if not out or len(out) > 100 or has_korean(out):
        raise RuntimeError(f"번역 결과가 이상합니다({out[:40]!r}): {name}로 직접 입력해 주세요")
    with _LOCK:
        cache = _load()
        cache[key] = out
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    return out
