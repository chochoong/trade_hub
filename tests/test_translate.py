import pytest

from trade_hub import translate


def test_no_korean_passes_through_without_llm(conn):
    boom = lambda q, lang: pytest.fail("한글이 없으면 번역하지 않아야 함")
    assert translate.to_lang("セイコー  SBTQ041", "ja", call=boom) == "セイコー SBTQ041"
    assert translate.to_lang("SBTQ041", "zh", call=boom) == "SBTQ041"


def test_each_language_cached_separately(conn):
    calls = []
    out = {"ja": "「セイコー 腕時計」", "zh": "精工 手表", "en": "Seiko watch"}
    fake = lambda q, lang: (calls.append(lang), out[lang])[1]
    assert translate.to_lang("세이코 시계", "ja", call=fake) == "セイコー 腕時計"
    assert translate.to_lang("세이코  시계", "zh", call=fake) == "精工 手表"
    assert translate.to_lang("세이코 시계", "en", call=fake) == "Seiko watch"
    assert translate.to_lang("세이코 시계", "zh", call=fake) == "精工 手表"      # 캐시
    assert calls == ["ja", "zh", "en"]
    assert "English" in translate.system_prompt("en") and "Chinese" in translate.system_prompt("zh")


@pytest.mark.parametrize("bad", ["", "세이코 시계", "x" * 101, "a\nb"])
def test_bad_translation_rejected(conn, bad):
    with pytest.raises(RuntimeError, match="직접 입력"):
        translate.to_lang("세이코 시계", "zh", call=lambda q, lang: bad)


def test_errors_become_friendly_message(conn):
    def offline(q, lang):
        raise ConnectionError("offline")
    def nokey(q, lang):
        raise RuntimeError("ANTHROPIC_API_KEY가 없습니다 (.env)")
    with pytest.raises(RuntimeError, match="번역 실패.*일본어로 직접"):
        translate.to_lang("세이코", "ja", call=offline)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY.*중국어로 직접"):
        translate.to_lang("세이코", "zh", call=nokey)
    with pytest.raises(ValueError):
        translate.to_lang("세이코", "fr")
