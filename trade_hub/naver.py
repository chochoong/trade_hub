"""
NAVER API HUB 이미지 검색 — 대시보드의 '참고 이미지'용.

결과는 화면(대시보드 HTML)에만 쓰고 DB에는 저장하지 않는다.
검색 이미지는 우리 상품 사진이 아니라 "이런 물건" 참고용이기 때문이다.

키: 환경변수 또는 trade_hub/.env 의 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET
     (NAVER API HUB 애플리케이션에 '검색' API 구독 필요. 하루 25,000회)
문서: https://api.ncloud-docs.com/docs/naver-api-hub-search-image
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOST = "https://naverapihub.apigw.ntruss.com"
IMAGE_PATH = "/search/v1/image"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _load_env_file(path=ENV_FILE):
    """trade_hub/.env에서 NAVER_ 키만 읽는다 (이미 설정된 환경변수가 우선)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line.startswith("NAVER_") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value[:1] in ('"', "'") and value[0] in value[1:]:
            value = value[1:value.index(value[0], 1)]
        else:
            value = re.split(r"(?:^|\s+)#", value, maxsplit=1)[0].strip()
        if value:
            os.environ.setdefault(key.strip(), value)


def keys():
    _load_env_file()
    cid, sec = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    return (cid, sec) if cid and sec else None


class NaverError(RuntimeError):
    pass


def image_search(query, k, display=1, timeout=15):
    """[{thumbnail, link, title}] — 실패하면 NaverError (401·403은 키·구독 문제)."""
    url = HOST + IMAGE_PATH + "?" + urllib.parse.urlencode(
        {"query": query, "display": display, "filter": "medium", "sort": "sim"})
    req = urllib.request.Request(url, headers={"X-NCP-APIGW-API-KEY-ID": k[0], "X-NCP-APIGW-API-KEY": k[1]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise NaverError(f"HTTP {e.code}" + (" (키 또는 '검색' API 구독 확인)" if e.code in (401, 403) else "")) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise NaverError(str(e)) from None
    out = []
    for it in data.get("items", []):
        thumb = it.get("thumbnail") or ""
        if thumb.startswith("https://") or thumb.startswith("http://"):
            title = re.sub(r"<[^>]+>", "", it.get("title") or "")
            out.append({"thumbnail": thumb, "link": it.get("link") or thumb, "title": title})
    return out


def reference_images(queries, log=None):
    """{키: 검색어} → {키: 첫 번째 결과 또는 None}. 키가 없거나 실패하면 빈 dict / None (화면에서 '이미지 없음')."""
    k = keys()
    if not k or not queries:
        return {}
    errors = []

    def one(item):
        key, q = item
        try:
            hits = image_search(q, k)
            return key, (hits[0] if hits else None)
        except NaverError as e:
            errors.append(str(e))
            return key, None

    with ThreadPoolExecutor(max_workers=6) as ex:
        result = dict(ex.map(one, queries.items()))
    if errors and log:
        log(f"  · 참고 이미지 일부 실패 ({len(errors)}건): {errors[0]}")
    return result
