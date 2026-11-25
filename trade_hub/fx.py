"""
대시보드 표시용 환율 (USD 기준) — 4번 cost_agent 와 같은 출처·예비값.

  출처: open.er-api.com/v6/latest/USD (4번·5번과 같음, 통화 코드만 보냄 · 키 없음)
  실패하면 4번 config/hub.yaml 의 fx.USD (예비값, 없으면 1400)
  화면을 그릴 때마다 부르지 않게 6시간 캐시 (실패도 30분 캐시) · 응답 대기 최대 4초
  표시용 어림값일 뿐 — 원가·마진 계산은 4번이 함
"""
import json
import re
import time
import urllib.request

from trade_hub import db

FX_URL = "https://open.er-api.com/v6/latest/USD"
TTL, FAIL_TTL, TIMEOUT = 6 * 3600, 30 * 60, 4
ONLINE = True            # 테스트는 끔 (conftest) — 네트워크 없이 예비값
FALLBACK_USD_KRW = 1400.0
_cache = {"at": 0.0, "rates": None, "ok": False}


def _cost_fallback():
    """4번 config/hub.yaml fx.USD (supervisor.json agents.costing.cwd 기준). 못 읽으면 1400."""
    cfg = db.PKG_DIR.parent / "supervisor.json"
    try:
        cwd = json.loads(cfg.read_text(encoding="utf-8"))["agents"]["costing"]["cwd"]
        text = (cfg.parent / cwd / "config" / "hub.yaml").read_text(encoding="utf-8")
        m = re.search(r"^fx:\s*(?:#.*)?\n(?:\s+#.*\n)*\s+USD:\s*([0-9.]+)", text, re.M)
        v = float(m.group(1)) if m else None
        return v if v and 500 < v < 5000 else FALLBACK_USD_KRW
    except (OSError, ValueError, KeyError, TypeError):
        return FALLBACK_USD_KRW


def rates():
    """{통화: 1 USD 당 그 통화} 또는 None (오프라인·실패)."""
    now = time.time()
    if ONLINE and now - _cache["at"] > (TTL if _cache["ok"] else FAIL_TTL):
        _cache["at"] = now
        try:
            with urllib.request.urlopen(FX_URL, timeout=TIMEOUT) as r:
                data = json.loads(r.read().decode())
            rt = data.get("rates") or {}
            krw = float(rt.get("KRW"))
            _cache["rates"], _cache["ok"] = (rt, True) if 500 < krw < 5000 else (None, False)
        except Exception:
            _cache["rates"], _cache["ok"] = None, False
    return _cache["rates"] if ONLINE else None


def usd_krw():
    """(1 USD = ?원, 출처)."""
    rt = rates()
    if rt:
        return float(rt["KRW"]), "open.er-api.com"
    return _cost_fallback(), "4번 예비 환율(config/hub.yaml)"


def per_usd(currency, fx_assumed=None):
    """1 USD = ? currency. USD(또는 없음) → 1. fx_assumed(2번 가정 환율)가 있으면 그것. 모르면 None."""
    cur = (currency or "USD").upper()
    if cur == "USD":
        return 1.0
    if isinstance(fx_assumed, (int, float)) and fx_assumed > 0:
        return float(fx_assumed)
    rt = rates()
    v = (rt or {}).get(cur)
    return float(v) if isinstance(v, (int, float)) and v > 0 else None
