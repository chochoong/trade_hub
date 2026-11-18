import pytest

from trade_hub import translate


def test_japanese_or_model_number_passes_through_without_llm(conn):
    boom = lambda q: pytest.fail("한글이 없으면 번역하지 않아야 함")
    assert translate.to_japanese("セイコー  SBTQ041", call=boom) == "セイコー SBTQ041"
    assert translate.to_japanese("SBTQ041", call=boom) == "SBTQ041"


def test_korean_translated_once_then_cached(conn):
    calls = []
    fake = lambda q: (calls.append(q), "「セイコー 腕時計 SBTQ041」\n")[1]
    assert translate.to_japanese("세이코 손목시계 SBTQ041", call=fake) == "セイコー 腕時計 SBTQ041"
    assert translate.to_japanese("세이코  손목시계 SBTQ041", call=fake) == "セイコー 腕時計 SBTQ041"   # 띄어쓰기 달라도 같은 검색어
    assert len(calls) == 1                                              # 두 번째는 캐시
    assert translate.original_of("セイコー 腕時計 SBTQ041") == "세이코 손목시계 SBTQ041"


@pytest.mark.parametrize("bad", ["", "세이코 시계", "x" * 101])
def test_bad_translation_rejected(conn, bad):
    with pytest.raises(RuntimeError, match="일본어로 직접"):
        translate.to_japanese("세이코 시계", call=lambda q: bad)


def test_api_error_becomes_friendly_message(conn):
    def fail(q):
        raise ConnectionError("offline")
    with pytest.raises(RuntimeError, match="번역 실패"):
        translate.to_japanese("세이코 시계", call=fail)
