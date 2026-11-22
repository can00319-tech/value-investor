"""가치투자 스튜디오 v2 테스트 — 로직 단위 테스트 + Streamlit AppTest 스모크 테스트.

실행:  python3 test_app.py

- 숫자/금액 파싱, 가치함정 필터, 가치점수, 3단 시나리오
- 저장소(로컬 JSON, 구글 시트 웹 앱 모킹), AI 키·Secrets
- 종목명 자동 검색, 400종목 유니버스/시장 필터
- 6개월·1년 가격 흐름과 20/60일선, 52주 위치
- 거시 지표판(지수·환율·금리·유가·금·구리) + 시장 수급
- 외국인·기관 수급, 배당, 3~5년 재무 추이
- PER·PBR·DCF 3방식 밸류에이션과 4단계 대응 구간표
- 뉴스·공시 분류(URL·게시시각 고정)와 이슈→종목 영향 해석
- 실제 app.py 렌더링(빈 상태 · 보유 · 스캔 · 종목 검색 · 시트 모드)
네트워크는 전혀 쓰지 않는다(모든 외부 호출을 대역으로 교체).
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import vi_core as core  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"  → {detail}"))
    if not cond:
        failures.append(name)


def snap(**kw) -> core.StockSnapshot:
    base = dict(code="005930", name="테스트전자", market="KR", currency="KRW", sector="반도체",
                price=10_000.0, change_pct=1.2, per=8.0, pbr=1.0, eps=1_250.0, bps=10_000.0,
                roe=12.0, debt_ratio=80.0, op_income=1_000.0, op_margin=8.0,
                revenue=20_000.0, revenue_yoy=10.0, op_yoy=12.0, ocf=5e8,
                net_income=600.0, shares=1_000_000.0,
                market_cap=1e10, high52=12_000.0, low52=8_000.0,
                dividend_yield=2.5, dps=250.0, dividend_year="2025", sources=["테스트"])
    base.update(kw)
    return core.StockSnapshot(**base)


def fake_history(code="005930", n=300, currency="KRW", start=10_000.0, step=10.0) -> core.PriceHistory:
    dates = [(date(2025, 9, 1) + timedelta(days=i)).isoformat() for i in range(n)]
    closes = [start + step * i for i in range(n)]
    hist = core.PriceHistory(code=code, currency=currency, dates=dates, closes=closes,
                             volumes=[1000.0] * n, source="테스트")
    hist.ma20 = core._sma(closes, 20)
    hist.ma60 = core._sma(closes, 60)
    return hist


# ------------------------------------------------------------ [1] 숫자 파싱 ----
def test_parsing() -> None:
    print("[1] 숫자·통화 파싱")
    check("'1,234' → 1234", core.parse_num("1,234") == 1234)
    check("'-3,550' → -3550", core.parse_num("-3,550") == -3550)
    check("'+4,051,865' → 양수", core.parse_num("+4,051,865") == 4051865)
    check("'8.38배' → 8.38", core.parse_num("8.38배") == 8.38)
    check("'N/A' → None", core.parse_num("N/A") is None)
    check("'1조 4,362억' 환산", core.parse_korean_amount("1조 4,362억") == 1_436_200_000_000)
    check("'14,949,185백만' 환산(거래대금)", core.parse_korean_amount("14,949,185백만") == 14_949_185_000_000)
    check("원화 표기", core.money(40250, "KRW") == "40,250원")
    check("달러 표기", core.money(237.4, "USD") == "$237.40")
    check("통화 표기(None)", core.money(None) == "-")
    check("앞자리 0 복원(67310→067310)", core.normalize_code("67310") == "067310")
    check("미국 티커 유지", core.normalize_code("AAPL") == "AAPL")
    check("compact_money 원화 조 단위", core.compact_money(14.9e12) == "14.9조원")
    check("compact_money 달러 B 단위", core.compact_money(3.2e9, "USD") == "$3.20B")


# --------------------------------------------------------- [2] 가치함정 필터 ----
def test_filter() -> None:
    print("[2] 가치함정 필터")
    rules = core.FilterRules()
    good = core.run_filter(snap(), rules)
    check("저PER·저PBR·흑자·저부채·고ROE·OCF+ → 통과", good["verdict"] == "통과", good["reason"])
    trap = core.run_filter(snap(ocf=-1e8), rules)
    check("영업현금흐름 마이너스 → 탈락", trap["verdict"] == "탈락")
    debt = core.run_filter(snap(debt_ratio=320.0), rules)
    check("부채비율 320% → 탈락", debt["verdict"] == "탈락")
    loss = core.run_filter(snap(op_income=-50.0, per=-3.0), rules)
    check("영업이익 적자 → 탈락", loss["verdict"] == "탈락")
    unknown = core.run_filter(snap(debt_ratio=None, ocf=None), rules)
    check("확인 불가 2개 → 판정 보류", unknown["verdict"] == "판정 보류", unknown["verdict"])
    empty = core.run_filter(core.StockSnapshot(code="X", price=None), rules)
    check("데이터 없음 → 판정 불가", empty["verdict"] == "데이터 없음")
    bank = core.run_filter(snap(sector="금융", debt_ratio=None, ocf=None), rules)
    check("금융업 안내 문구", "금융업" in bank["note"])


# ------------------------------------------------------------ [3] 가치점수 ----
def test_score() -> None:
    print("[3] 종합 가치점수")
    cheap = core.value_score(snap(per=4.0, pbr=0.4, roe=18.0, op_margin=20.0,
                                  debt_ratio=20.0, ocf=1e9, revenue_yoy=25.0, op_yoy=40.0))[0]
    pricey = core.value_score(snap(per=40.0, pbr=4.0, roe=1.0, op_margin=0.5,
                                   debt_ratio=350.0, ocf=-1e8, revenue_yoy=-15.0, op_yoy=-30.0))[0]
    check(f"저평가 우량주 점수 높음({cheap})", cheap >= 80)
    check(f"고평가 부실주 점수 낮음({pricey})", pricey <= 20)
    score, parts = core.value_score(snap())
    check("점수 구성 4개 항목", set(parts) == {"저평가", "수익성", "안정성", "성장성"})


# ------------------------------------------------------------ [4] 시나리오 ----
def test_scenarios() -> None:
    print("[4] 3단 시나리오")
    s = snap()
    scn = core.build_scenarios(s, avg_price=12_000.0)
    check("3단 키 존재", {"상승", "기본", "경계"} <= set(scn))
    check("상승 목표 ≥ 현재가 +20%", scn["target"] >= s.price * 1.2 - 1e-6)
    check("경계선 = 평단 -15%", abs(scn["stop"] - 12_000 * 0.85) < 1e-6)
    check("기본 시나리오 근거 문구", "적정가" in scn["기본"]["basis"])
    val = core.valuate(s)
    scn2 = core.build_scenarios(s, avg_price=12_000.0, valuation=val)
    check("밸류에이션 전달 시 기본 적정가 = 3방식 가중값",
          abs(scn2["기본"]["price"] - val.fair) < 1e-6)
    check("3방식 근거 표기", "3방식" in scn2["기본"]["basis"], scn2["기본"]["basis"])
    us = core.build_scenarios(snap(market="US", currency="USD", price=200.0, eps=10.0, bps=80.0))
    check("미국 종목은 달러 표기", us["상승"]["range_text"].startswith("목표 $"))


# ------------------------------------------------------------- [5] 저장소 ----
class FakeResp:
    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_local_storage() -> None:
    print("[5] 저장소 — 로컬 파일 모드")
    tmp = Path(tempfile.mkdtemp(prefix="vi-store-"))
    os.environ.pop("GSHEET_WEBAPP_URL", None)
    os.environ.pop("GSHEET_TOKEN", None)
    os.environ["VI_DATA_DIR"] = str(tmp)
    core.clear_cache()
    check("시트 없으면 로컬 모드", core.storage_mode() == "local")
    rows = [{"code": "034020", "name": "두산에너빌리티", "qty": 3, "avg_price": 100000, "memo": "테스트"}]
    core.save_holdings(rows)
    loaded = core.load_holdings(force=True)
    check("보유 종목 저장·조회", len(loaded) == 1 and loaded[0]["code"] == "034020")
    picks = [{"date": "2026-11-22", "code": "069620", "name": "대웅제약", "entry_price": 98500,
              "rationale": "가치점수 88점", "target": 141576, "stop": 86680, "memo": ""}]
    core.save_picks(picks)
    check("추천 저장·조회", core.load_picks(force=True)[0]["code"] == "069620")
    core.save_holdings([])
    check("빈 목록 저장 가능", core.load_holdings(force=True) == [])


def test_sheet_storage() -> None:
    print("[6] 저장소 — 구글 시트 웹 앱 모킹")
    os.environ["VI_DATA_DIR"] = tempfile.mkdtemp(prefix="vi-store2-")
    os.environ["GSHEET_WEBAPP_URL"] = "https://script.google.com/macros/s/TEST/exec"
    os.environ["GSHEET_TOKEN"] = "test-token"
    core.clear_cache()
    check("URL 설정 시 시트 모드", core.storage_mode() == "gsheet")
    calls: list[dict] = []

    class FakeRequests:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None, allow_redirects=True):
            calls.append({"method": "GET", "params": params})
            if (params or {}).get("sheet") == "holdings":
                return FakeResp({"ok": True, "records": [
                    {"종목코드": 67310, "종목명": "하나마이크론", "수량": 8, "평단가": 25000, "메모": ""},
                    {"종목코드": "", "종목명": "빈행", "수량": 1, "평단가": 1}]})
            return FakeResp({"ok": True, "records": [
                {"등록일": "2026-11-22", "종목코드": "069620", "종목명": "대웅제약", "등록가": 98500,
                 "선정근거": "가치점수 88점", "목표가": 141576, "경계가": 86680, "상태메모": ""}]})

        @staticmethod
        def post(url, json=None, headers=None, timeout=None, allow_redirects=True):
            calls.append({"method": "POST", "json": json})
            return FakeResp({"ok": True})

    original = core.requests
    core.requests = FakeRequests  # type: ignore[assignment]
    try:
        holders = core.load_holdings(force=True)
        check("GET 응답 파싱(숫자 코드 복원)", len(holders) == 1 and holders[0]["code"] == "067310")
        core.save_holdings([{"code": "005930", "name": "삼성전자", "qty": 2, "avg_price": 70000, "memo": ""}])
        post = [c for c in calls if c["method"] == "POST"][-1]
        check("POST 한글 헤더 매핑", post["json"]["records"][0]["종목코드"] == "005930")
        check("추천추적 탭 조회", core.load_picks(force=True)[0]["code"] == "069620")

        class Denied(FakeRequests):
            @staticmethod
            def get(url, params=None, headers=None, timeout=None, allow_redirects=True):
                return FakeResp({"ok": False, "error": "unauthorized"})

        core.requests = Denied  # type: ignore[assignment]
        core.clear_cache()
        raised = ""
        try:
            core.load_holdings(force=True)
        except core.StorageError as exc:
            raised = str(exc)
        check("권한 오류 → StorageError", "unauthorized" in raised, raised)
    finally:
        core.requests = original  # type: ignore[assignment]
        os.environ.pop("GSHEET_WEBAPP_URL", None)
        os.environ.pop("GSHEET_TOKEN", None)
        core.clear_cache()


def test_ai_and_secrets() -> None:
    print("[7] AI 키·Secrets 처리")
    for key in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "AI_PROVIDER"):
        os.environ.pop(key, None)
    check("키 없으면 ai_available False", core.ai_available() is False)
    text, err = core.generate_ai_text("프롬프트")
    check("키 없으면 본문 없이 안내", text is None and "AI 키" in err, err)

    class Secrets(dict):
        pass

    core.apply_secrets(Secrets({"GSHEET_WEBAPP_URL": "https://example.test/exec"}))
    check("Secrets → 환경변수 반영", os.environ.get("GSHEET_WEBAPP_URL") == "https://example.test/exec")
    os.environ.pop("GSHEET_WEBAPP_URL", None)


# --------------------------------------------------------- [8] 종목명 검색 ----
def test_search() -> None:
    print("[8] 종목명·티커 자동 검색")
    core._naver_ac.cache_clear()  # type: ignore[attr-defined]
    original_ac = core._naver_ac
    AC_ITEMS = [
        {"code": "403870", "name": "HPSP", "market": "KR", "board": "KOSDAQ", "reuters": "403870", "sector": ""},
        {"code": "NVDA", "name": "엔비디아", "market": "US", "board": "NASDAQ", "reuters": "NVDA.O", "sector": ""},
        {"code": "900110", "name": "이스트아시아홀딩스", "market": "KR", "board": "KOSDAQ", "reuters": "", "sector": ""},
    ]
    core._naver_ac = lambda q: [i for i in AC_ITEMS if q.lower() in i["name"].lower()
                                or q.lower() in i["code"].lower()]  # type: ignore[assignment]
    try:
        for query, expect in (("HPSP", "403870"), ("삼성전자", "005930"), ("현대차", "005380"),
                              ("엔비디아", "NVDA"), ("NVDA", "NVDA"), ("코미코", "183300"),
                              ("두산에너빌", "034020")):
            found = core.search_stocks(query, limit=3)
            check(f"'{query}' 검색 → {expect}",
                  bool(found) and found[0]["code"] == expect,
                  str([(f['code'], f['name']) for f in found]))
        check("검색 결과에 시장·섹터 포함",
              "sector" in core.search_stocks("삼성전자")[0] and "board" in core.search_stocks("삼성전자")[0])
        check("네이버 자동완성 결과도 반영",
              any(f["code"] == "403870" for f in core.search_stocks("HPSP")))
        check("resolve_query(이름) 동작", core.resolve_query("HPSP")["code"] == "403870")
        check("없는 종목은 빈 결과", core.search_stocks("없는종목이름") == [])
    finally:
        core._naver_ac = original_ac  # type: ignore[assignment]


# --------------------------------------------------------- [9] 유니버스 확장 ----
def test_universe() -> None:
    print("[9] 유니버스 200종목 이상 + 시장 필터")
    check(f"유니버스 {len(core.UNIVERSE)}종목 (200 이상)", len(core.UNIVERSE) >= 200, str(len(core.UNIVERSE)))
    sizes = core.universe_size()
    check("코스피·코스닥·미국 모두 포함",
          sizes.get("KOSPI", 0) > 100 and sizes.get("KOSDAQ", 0) > 50 and sizes.get("US", 0) > 50, str(sizes))
    check("HPSP 포함", core.universe_lookup().get("403870", {}).get("name") == "HPSP")
    check("미국 종목 섹터 지정", core.universe_lookup()["NVDA"]["sector"] == "반도체")
    kospi = core.universe_items(markets=["코스피"])
    check("코스피 필터", kospi and all(i["market"] == "KOSPI" for i in kospi))
    us = core.universe_items(markets=["미국"])
    check("미국 필터", us and all(i["market"] == "US" for i in us))
    semi = core.universe_items(sectors=["반도체"])
    check("섹터 필터", semi and all(i["sector"] == "반도체" for i in semi))
    limited = core.universe_items(markets=["코스피", "코스닥", "미국"], limit=60)
    check("limit 60 적용", len(limited) == 60, str(len(limited)))
    check("시장별 비례 선택", len({i["market"] for i in limited}) >= 2)
    default = core.universe_items()
    check("조건 없으면 기본 세트", len(default) == len(core.DEFAULT_UNIVERSE_CODES))
    check("섹터 순서에 관심 섹터 포함",
          {"반도체", "원전·에너지", "바이오", "건설", "제조·철강"} <= set(core.SECTOR_ORDER))


# --------------------------------------------------------- [10] 가격 흐름 ----
def test_price_history() -> None:
    print("[10] 가격 흐름 · 이동평균 · 52주 위치")
    original = core._get_list
    rows = [{"localDate": f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}" if False else "20260101",
             "closePrice": 100.0 + i, "accumulatedTradingVolume": 1000}
            for i in range(40)]
    # 날짜를 순증하도록 다시 만든다
    base = date(2026, 1, 1)
    rows = [{"localDate": (base + timedelta(days=i)).strftime("%Y%m%d"),
             "closePrice": 100.0 + i, "accumulatedTradingVolume": 1000 + i} for i in range(200)]
    core._get_list = lambda url, params=None: rows  # type: ignore[assignment]
    core.fetch_price_history.cache_clear()  # type: ignore[attr-defined]
    try:
        hist = core.fetch_price_history("005930", months=12)
        check("가격 데이터 파싱", len(hist.closes) == 200 and hist.closes[-1] == 299.0)
        check("20일 이동평균 계산", abs(hist.ma20[-1] - sum(hist.closes[-20:]) / 20) < 1e-6)
        check("60일 이동평균 계산", abs(hist.ma60[-1] - sum(hist.closes[-60:]) / 60) < 1e-6)
        stats = hist.stats()
        check("52주 최고/최저", stats["high52"] == 299.0 and stats["low52"] == 100.0, str(stats))
        check("52주 레인지 위치 100%", abs(stats["position_pct"] - 100.0) < 1e-6)
        check("1년 수익률 계산", stats["return_1y"] is not None)
        w6 = hist.window(6)
        check("6개월 구간 축소", 0 < len(w6.closes) < len(hist.closes), f"{len(w6.closes)}")
        check("구간 축소 후에도 이동평균 유지", w6.ma20[-1] is not None)
        rows_out = w6.to_rows()
        check("차트용 행 구조", {"날짜", "종가", "20일선", "60일선"} <= set(rows_out[0]))
    finally:
        core._get_list = original  # type: ignore[assignment]
        core.fetch_price_history.cache_clear()  # type: ignore[attr-defined]


# --------------------------------------------------------- [11] 거시 지표 ----
def test_macro() -> None:
    print("[11] 거시 지표판 · 시장 수급")
    original = core._get_json

    def router(url, params=None):
        params = params or {}
        if url.endswith("/api/index/KOSPI/basic"):
            return {"closePrice": "2,500.00", "compareToPreviousClosePrice": "-25.00",
                    "fluctuationsRatio": "-1.00", "localTradedAt": "2026-11-20T15:30:00+09:00"}
        if url.endswith("/api/index/KOSPI/integration"):
            return {"totalInfos": [{"code": "accumulatedTradingValue", "value": "10조 5,000억"}]}
        if url.endswith("/api/index/KOSPI/trend"):
            return {"bizdate": "20261120", "foreignValue": "-5,000", "institutionalValue": "+2,000",
                    "personalValue": "+3,000"}
        if url.endswith("/api/index/KOSDAQ/basic"):
            return {"closePrice": "800.00", "compareToPreviousClosePrice": "8.00",
                    "fluctuationsRatio": "1.01", "localTradedAt": "2026-11-20T15:30:00+09:00"}
        if url.endswith("/api/index/KOSDAQ/integration"):
            return {"totalInfos": [{"code": "accumulatedTradingValue", "value": "3조 1,000억"}]}
        if url.endswith("/api/index/KOSDAQ/trend"):
            return {"bizdate": "20261120", "foreignValue": "-1,000", "institutionalValue": "+500",
                    "personalValue": "+500"}
        if "/index/.INX/basic" in url:
            return {"closePrice": "5,000.00", "fluctuationsRatio": "-1.5"}
        if "/index/.IXIC/basic" in url:
            return {"closePrice": "18,000.00", "fluctuationsRatio": "-2.0"}
        if url == core.NAVER_MARKET_INDEX:
            code = params.get("reutersCode")
            table = {"FX_USDKRW": ("1,350.00", "10.00", "0.75"),
                     "US10YT=RR": ("4.20", "0.05", "1.20"),
                     "KR10YT=RR": ("3.10", "-0.02", "-0.64"),
                     "CLcv1": ("80.00", "1.60", "2.04"),
                     "GCcv1": ("2,400.00", "-10.00", "-0.41"),
                     "HGcv1": ("4.20", "0.05", "1.20")}
            if code in table:
                close, chg, ratio = table[code]
                return {"result": {"closePrice": close, "fluctuations": chg,
                                   "fluctuationsRatio": ratio, "localTradedAt": "2026-11-20T06:00:00+09:00"}}
            return {"result": {}}
        if "markets.newyorkfed.org" in url:
            return {"refRates": [{"effectiveDate": "2026-11-19", "percentRate": 3.9}]}
        return None

    core._get_json = router  # type: ignore[assignment]
    core.fetch_macro.cache_clear()  # type: ignore[attr-defined]
    try:
        macro = core.fetch_macro()
        keys = [i.key for i in macro.items]
        check("지표 11종 수집", len(macro.items) == 11, str(keys))
        for key in ("KOSPI", "KOSDAQ", "SP500", "NASDAQ", "USDKRW", "US10Y", "KR10Y", "FED", "WTI", "GOLD", "COPPER"):
            check(f"{key} 값 존재", macro.get(key) is not None and macro.get(key).value is not None)
        check("코스피 등락률·거래대금", macro.get("KOSPI").change_pct == -1.0
              and "10.5조" in macro.get("KOSPI").extra, macro.get("KOSPI").extra)
        check("원/달러 표기", macro.get("USDKRW").value_text() == "1,350.00원")
        check("미 10년물 %p 표기", macro.get("US10Y").change_text() == "+0.050%p")
        check("시장 수급 2개", len(macro.flows) == 2
              and macro.flows[0].foreign == -5000, str([f.text() for f in macro.flows]))
        brief = core.macro_brief(macro)
        check("시장 요약 문장 생성", any("코스피" in line for line in brief) and len(brief) >= 3, str(brief))
    finally:
        core._get_json = original  # type: ignore[assignment]
        core.fetch_macro.cache_clear()  # type: ignore[attr-defined]


# ------------------------------------------------ [12] 수급·재무·밸류에이션 ----
def test_flow_and_trend() -> None:
    print("[12] 수급 · 3~5년 재무 추이 · 밸류에이션")
    original_list, original_json = core._get_list, core._get_json
    core._get_list = lambda url, params=None: [  # type: ignore[assignment]
        {"bizdate": "20261120", "foreignerPureBuyQuant": "-1,000", "organPureBuyQuant": "+2,000",
         "individualPureBuyQuant": "-1,000", "closePrice": "10,000", "foreignerHoldRatio": "45.6%"},
        {"bizdate": "20261119", "foreignerPureBuyQuant": "+3,000", "organPureBuyQuant": "+1,000",
         "individualPureBuyQuant": "-4,000", "closePrice": "10,100", "foreignerHoldRatio": "45.7%"},
    ]
    core.fetch_flow.cache_clear()  # type: ignore[attr-defined]
    try:
        flow = core.fetch_flow("005930")
        check("수급 파싱", flow.available and len(flow.rows) == 2)
        check("외국인/기관 합계", flow.foreign_sum == 2000 and flow.organ_sum == 3000, str(flow.text()))
        check("수급 태그", flow.tag().startswith("🟢"))
        us_flow = core.fetch_flow("NVDA")
        check("미국 종목은 수급 없음 안내", not us_flow.available and "해외" in us_flow.note)
    finally:
        core._get_list = original_list  # type: ignore[assignment]
        core.fetch_flow.cache_clear()  # type: ignore[attr-defined]

    def router(url, params=None):
        if "finance/annual" in url:
            return {"financeInfo": {
                "trTitleList": [{"title": "2023.12.", "key": "202312", "isConsensus": "N"},
                                {"title": "2024.12.", "key": "202412", "isConsensus": "N"},
                                {"title": "2025.12.", "key": "202512", "isConsensus": "N"},
                                {"title": "2026.12.", "key": "202612", "isConsensus": "Y"}],
                "rowList": [
                    {"title": "매출액", "columns": {"202312": {"value": "1,000"},
                                                    "202412": {"value": "1,200"},
                                                    "202512": {"value": "1,500"},
                                                    "202612": {"value": "1,800"}}},
                    {"title": "영업이익", "columns": {"202312": {"value": "100"},
                                                      "202412": {"value": "150"},
                                                      "202512": {"value": "220"},
                                                      "202612": {"value": "260"}}},
                    {"title": "당기순이익", "columns": {"202312": {"value": "80"},
                                                        "202412": {"value": "120"},
                                                        "202512": {"value": "180"},
                                                        "202612": {"value": "210"}}},
                    {"title": "ROE", "columns": {"202512": {"value": "12.5"}}},
                    {"title": "부채비율", "columns": {"202512": {"value": "85.0"}}},
                    {"title": "EPS", "columns": {"202512": {"value": "1,800"}}},
                    {"title": "PER", "columns": {"202512": {"value": "7.5"}}},
                    {"title": "BPS", "columns": {"202512": {"value": "21,000"}}},
                    {"title": "PBR", "columns": {"202512": {"value": "0.9"}}},
                    {"title": "주당배당금", "columns": {"202312": {"value": "200"},
                                                        "202412": {"value": "220"},
                                                        "202512": {"value": "250"}}},
                ]}}
        return None

    core._get_json = router  # type: ignore[assignment]
    core.fetch_financial_history.cache_clear()  # type: ignore[attr-defined]
    core._fin_raw.cache_clear()  # type: ignore[attr-defined]
    try:
        trend = core.fetch_financial_history("005930", years=4)
        check("연도 4개(추정 포함)", trend.years == ["2023", "2024", "2025", "2026"], str(trend.years))
        check("마지막 연도는 추정 표시", trend.estimated[-1] is True and trend.estimated[0] is False)
        check("매출 추이", trend.series["매출액"] == [1000.0, 1200.0, 1500.0, 1800.0])
        check("영업이익·순이익 추이", trend.series["영업이익"][-1] == 260.0 and trend.series["순이익"][-1] == 210.0)
        check("배당(DPS) 이력", trend.series["주당배당금"] == [200.0, 220.0, 250.0, None])
        rows = trend.series_rows()
        check("표 변환(추정 라벨)", rows[-1]["연도"] == "2026(추정)")
        check("단위 표기", trend.unit == "억원", trend.unit)
    finally:
        core._get_json = original_json  # type: ignore[assignment]
        core.fetch_financial_history.cache_clear()  # type: ignore[attr-defined]
        core._fin_raw.cache_clear()  # type: ignore[attr-defined]


def test_valuation() -> None:
    print("[13] PER·PBR·DCF 3방식 + 4단계 대응 구간")
    s = snap(price=10_000.0, eps=1_000.0, bps=10_000.0, per=10.0, pbr=1.0,
             ocf=12_000_000_000.0, net_income=1_000.0, shares=1_000_000.0,
             market_cap=1e10, revenue_yoy=8.0, sector="제조·철강")
    val = core.valuate(s)
    check("방식 3개", len(val.methods) == 3 and [m.name for m in val.methods] ==
          ["PER 방식", "PBR 방식", "DCF 방식"])
    check("PER 방식 값 = EPS × 목표 PER", val.methods[0].value == 1_000.0 * core.TARGET_PER_BY_SECTOR["제조·철강"])
    check("PBR 방식 값 = BPS × 목표 PBR", val.methods[1].value == 10_000.0 * core.TARGET_PBR_BY_SECTOR["제조·철강"])
    check("DCF 방식 계산됨", val.methods[2].value is not None and val.methods[2].value > 0)
    check("가중 평균 적정가", val.fair is not None and val.low < val.fair < val.high)
    check("4단계 구간표", [r["구간"] for r in val.rows] ==
          ["적극 매수 검토", "분할매수 검토", "보유 검토", "신규매수 신중"])
    check("현재가 구간 표시", sum(1 for r in val.rows if r["지금"]) == 1, str(val.rows))
    check("할인율·성장률 가정 공개", any("할인율" in a for a in val.assumptions))

    bank = core.valuate(snap(sector="금융", price=8_000.0, eps=1_500.0, bps=20_000.0,
                             ocf=None, net_income=900.0, market_cap=8e9, shares=1_000_000.0))
    check("금융주는 PBR 가중치 높음", core.valuation_profile(snap(sector="금융")) == "금융"
          and bank.methods[1].weight > bank.methods[0].weight)
    loss = core.valuate(snap(eps=-100.0, per=-3.0, bps=None, pbr=None, ocf=None, net_income=None,
                            market_cap=None, shares=None))
    check("적자·현금흐름 없음 → 적정가 불가 안내", loss.fair is None and "계산하지 못" in loss.note)
    check("적자 종목 DCF 비활성", loss.methods[2].value is None and "부족" in loss.methods[2].note)


# ------------------------------------------------------------ [14] 뉴스 ----
RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>삼성전자 HBM 수주 확대 - 테스트일보</title>
<link>https://news.google.com/rss/articles/AAA</link>
<pubDate>Sat, 22 Nov 2026 01:30:00 GMT</pubDate><source>테스트일보</source></item>
<item><title>삼성전자 3분기 실적 부진 우려 - 테스트경제</title>
<link>https://news.google.com/rss/articles/BBB</link>
<pubDate>Fri, 21 Nov 2026 15:00:00 GMT</pubDate><source>테스트경제</source></item>
<item><title>반도체 업황 관련 정부 규제 논의 - 테스트뉴스</title>
<link>https://news.google.com/rss/articles/CCC</link>
<pubDate>Thu, 20 Nov 2026 05:00:00 GMT</pubDate><source>테스트뉴스</source></item>
</channel></rss>"""


def test_news() -> None:
    print("[14] 뉴스·공시 분류(URL·시각 고정)")
    check("RFC822 날짜 파싱(GMT→KST)", core._parse_pubdate("Sat, 22 Nov 2026 01:30:00 GMT")
          == datetime(2026, 11, 22, 10, 30, tzinfo=core.KST))
    check("잘못된 날짜는 None", core._parse_pubdate("어제") is None)
    check("호재/악재 판단", core._sentiment("수주 확대") == "🟢 호재"
          and core._sentiment("실적 부진 우려") == "🔴 악재")
    check("카테고리 — 실적", core._categorize("3분기 실적 발표", 5) == "공시·실적")
    check("카테고리 — 수주", core._categorize("대규모 수주 계약 체결", 5) == "수주·계약")
    check("카테고리 — 규제", core._categorize("정부 규제 발표", 5) == "정책·규제")
    check("카테고리 — 리포트", core._categorize("증권사 목표주가 상향", 5) == "증권사 리포트")

    original_requests, original_json = core.requests, core._get_json

    class FakeReq:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            return FakeResp(None, text=RSS_SAMPLE)

    core.requests = FakeReq  # type: ignore[assignment]
    core._get_json = lambda url, params=None: [  # type: ignore[assignment]
        {"items": [{"title": "삼성전자 자사주 매입 공시", "mobileNewsUrl": "https://n.news.naver.com/a/1",
                    "officeName": "테스트연합", "datetime": "202611222130"}]}]
    core._naver_stock_news.cache_clear()  # type: ignore[attr-defined]
    core.fetch_news_digest.cache_clear()  # type: ignore[attr-defined]
    try:
        digest = core.fetch_news_digest("삼성전자", code="005930", limit=10)
        check("RSS + 네이버 뉴스 수집", len(digest.items) == 4, str(len(digest.items)))
        check("모든 항목에 실제 URL", all(i.link.startswith("http") for i in digest.items))
        check("모든 항목에 게시 시각", all(i.published and i.published != "시각 미상" for i in digest.items),
              str([i.published for i in digest.items]))
        cats = {i.category for i in digest.items}
        check("카테고리 분류", {"공시·실적", "수주·계약", "정책·규제"} <= cats, str(cats))
        check("호재/악재 카운트", digest.counts()["🟢 호재"] >= 1 and digest.counts()["🔴 악재"] >= 1,
              str(digest.counts()))
        check("최신순 정렬", digest.items[0].published >= digest.items[-1].published)
        rows = digest.items[0].to_row()
        check("표 행에 링크·게시 포함", {"링크", "게시", "판단"} <= set(rows))
        lines = core.news_impact_lines(digest, "반도체", "삼성전자")
        check("영향 요약 문장", any("호재" in line or "악재" in line for line in lines), str(lines))
    finally:
        core.requests = original_requests  # type: ignore[assignment]
        core._get_json = original_json  # type: ignore[assignment]
        core._naver_stock_news.cache_clear()  # type: ignore[attr-defined]
        core.fetch_news_digest.cache_clear()  # type: ignore[attr-defined]


# ------------------------------------------------- [15] 이슈→종목 영향 해석 ----
def test_impact() -> None:
    print("[15] 이슈 → 내 종목 영향 해석")
    macro = core.MacroBoard(items=[
        core.MacroItem("KOSPI", "코스피", 2500, -25, -1.0),
        core.MacroItem("KOSDAQ", "코스닥", 800, -10, -1.2),
        core.MacroItem("NASDAQ", "나스닥", 18000, -360, -2.0),
        core.MacroItem("SP500", "S&P500", 5000, -50, -1.0),
        core.MacroItem("USDKRW", "원/달러", 1350, 10, 0.75, unit="원"),
        core.MacroItem("US10Y", "미 10년물", 4.2, 0.05, 1.2, unit="%"),
        core.MacroItem("WTI", "WTI 유가", 80, 2.0, 2.6, unit="달러"),
        core.MacroItem("COPPER", "구리", 4.2, 0.1, 2.4, unit="달러"),
    ])
    news = {"반도체": [{"title": "HBM 수주 확대", "link": "https://x/1", "source": "t"}]}
    report = core.analyze_impact(macro, news, holdings=[
        {"code": "005930", "name": "삼성전자", "sector": "반도체"},
        {"code": "034020", "name": "두산에너빌리티", "sector": "원전·에너지"},
        {"code": "105560", "name": "KB금융", "sector": "금융"},
    ])
    check("종목별 해석 3개", len(report.rows) == 3, str(len(report.rows)))
    by_name = {r.name: r for r in report.rows}
    check("반도체 — 나스닥 급락은 '주의'", by_name["삼성전자"].tag == "🔴 주의", by_name["삼성전자"].tag)

    fx_only = core.MacroBoard(items=[core.MacroItem("USDKRW", "원/달러", 1350, 10, 0.75, unit="원")])
    fx_impact = core.analyze_impact(fx_only, {}, holdings=[{"code": "034020", "name": "두산에너빌리티",
                                                           "sector": "원전·에너지"}])
    check("원전 — 원화 약세만 있으면 '호재'", fx_impact.rows[0].tag == "🟢 호재", fx_impact.rows[0].tag)

    rate_only = core.MacroBoard(items=[core.MacroItem("US10Y", "미 10년물", 4.2, 0.05, 1.2, unit="%")])
    rate_impact = core.analyze_impact(rate_only, {}, holdings=[{"code": "105560", "name": "KB금융", "sector": "금융"}])
    check("금융 — 금리 상승만 있으면 '호재'", rate_impact.rows[0].tag == "🟢 호재", rate_impact.rows[0].tag)
    check("근거 문장에 수치 포함", any("%" in reason for reason in by_name["삼성전자"].reasons))
    check("섹터 요약 생성", len(report.sectors) == 3)
    check("거시 요약 포함", any("코스피" in line for line in report.brief))
    check("태그 카운트", sum(report.tag_counts().values()) == 3)
    check("보유 종목 없으면 관심 섹터 대표로 채움",
          len(core.analyze_impact(macro, {}).rows) >= 5)


# ------------------------------------------------------- [16] 앱 렌더링 ----
def mocked_core() -> None:
    """앱이 네트워크 없이 돌도록 외부 호출을 대역으로 교체."""
    def fake_snapshot(code, sector=None, name_hint=None, deep=False):
        table = {
            "034020": snap(code="034020", name="두산에너빌리티", sector="원전·에너지", price=40_250,
                           per=118.7, pbr=3.46, eps=339, bps=11_648, roe=1.1, debt_ratio=129.1,
                           op_income=7_627, op_margin=4.5, ocf=3.6e11, dividend_yield=0.6, dps=250.0),
            "047040": snap(code="047040", name="대우건설", sector="건설", price=10_330,
                           per=-1.74, pbr=1.12, eps=-1_362, bps=9_216, roe=-23.9, debt_ratio=284.5,
                           op_income=-800, op_margin=-1.2, ocf=None),
            "067310": snap(code="067310", name="하나마이크론", sector="반도체", price=21_600,
                           per=8.38, pbr=2.6, eps=2_578, bps=8_311, roe=9.9, debt_ratio=209.2,
                           op_income=1_277, op_margin=9.0, ocf=None),
        }
        if code in table:
            return table[code]
        if code in ("005930", "069620"):
            return snap(code=code, name="삼성전자" if code == "005930" else "대웅제약",
                        sector="반도체" if code == "005930" else "바이오",
                        price=70_000, per=6.22, pbr=1.06, eps=15_836, bps=92_925, roe=21.75,
                        debt_ratio=108.2, op_income=1_200, op_margin=12.0, ocf=2e8,
                        revenue_yoy=8.0, op_yoy=15.0, dividend_yield=1.8, dps=1_668.0)
        return snap(code=code, name=name_hint or f"종목{code}", price=50_000, per=28.0, pbr=3.2,
                    eps=1_800, bps=15_600, roe=2.0, debt_ratio=260.0, op_income=100.0,
                    op_margin=2.0, ocf=-1e8, revenue_yoy=-5.0, op_yoy=-10.0)

    def fake_snapshots(items, workers=8, deep=False, progress=None):
        out = []
        for i, it in enumerate(items, start=1):
            out.append(fake_snapshot(it["code"], it.get("sector"), it.get("name")))
            if progress:
                progress(i, len(items))
        return out

    def fake_scan(items, rules, workers=10, progress=None, deep=False):
        rows = []
        for i, item in enumerate(items, start=1):
            s = fake_snapshot(item["code"], item.get("sector"), item.get("name"))
            rows.append({"snap": s, "filter": core.run_filter(s, rules)})
            if progress:
                progress(i, len(items))
        rows.sort(key=lambda r: (-(r["filter"]["passed"]), -r["filter"]["score"]))
        return rows

    def fake_trend(code, years=4):
        hist = core.FinancialHistory(code=code, currency="KRW", unit="억원",
                                     years=["2023", "2024", "2025", "2026"],
                                     estimated=[False, False, False, True],
                                     source="테스트 재무")
        hist.series = {
            "매출액": [1000.0, 1200.0, 1500.0, 1800.0],
            "영업이익": [100.0, 150.0, 220.0, 260.0],
            "순이익": [80.0, 120.0, 180.0, 210.0],
            "영업이익률": [10.0, 12.5, 14.6, 14.4],
            "ROE": [5.0, 7.0, 9.0, 10.0],
            "부채비율": [90.0, 88.0, 85.0, 82.0],
            "EPS": [800.0, 1200.0, 1800.0, 2100.0],
            "PER": [12.0, 10.0, 7.5, 6.0],
            "BPS": [16000.0, 18000.0, 21000.0, 23000.0],
            "PBR": [1.2, 1.05, 0.9, 0.8],
            "주당배당금": [200.0, 220.0, 250.0, None],
            "배당수익률": [None, None, None, None],
            "현금흐름": [None, None, None, None],
        }
        return hist

    def fake_digest(query, code="", limit=12):
        return core.NewsDigest(generated_at="2026-11-22 10:00", items=[
            core.NewsItem(title=f"{query} HBM 수주 확대", link="https://example.test/1",
                          published="2026-11-22 09:00", source="테스트일보",
                          category="수주·계약", sentiment="🟢 호재"),
            core.NewsItem(title=f"{query} 3분기 실적 부진 우려", link="https://example.test/2",
                          published="2026-11-21 18:00", source="테스트경제",
                          category="공시·실적", sentiment="🔴 악재"),
        ])

    def fake_pulse():
        return [core.SectorPulse(key="반도체·AI", label="반도체·AI", change_pct=-1.2,
                                 members=[{"name": "삼성전자", "code": "005930", "change_pct": -1.5, "market": "국내"}],
                                 note="대표 1종목 평균")]

    core.fetch_snapshot = fake_snapshot  # type: ignore[assignment]
    core.fetch_snapshots = fake_snapshots  # type: ignore[assignment]
    core.scan_universe = fake_scan  # type: ignore[assignment]
    core.fetch_price_history = lambda code, months=12: fake_history(code)  # type: ignore[assignment]
    core.fetch_financial_history = fake_trend  # type: ignore[assignment]
    core.fetch_flow = lambda code, days=5: core.FlowSummary(  # type: ignore[assignment]
        code=code, rows=[{"날짜": "2026-11-20", "외국인": -1000, "기관": 2000, "개인": -1000, "종가": 70_000}],
        foreign_sum=-1000, organ_sum=2000, individual_sum=-1000, foreign_hold_ratio=45.6,
        available=True)
    core.fetch_news_digest = fake_digest  # type: ignore[assignment]
    core.fetch_sector_pulse = fake_pulse  # type: ignore[assignment]
    core.fetch_macro = lambda: core.MacroBoard(items=[  # type: ignore[assignment]
        core.MacroItem("KOSPI", "코스피", 2500, -25, -1.0, source="테스트", as_of="2026-11-20T15:30:00+09:00"),
        core.MacroItem("KOSDAQ", "코스닥", 800, 8, 1.0, source="테스트"),
        core.MacroItem("SP500", "S&P500", 5000, -50, -1.0, source="테스트"),
        core.MacroItem("NASDAQ", "나스닥", 18000, -360, -2.0, source="테스트"),
        core.MacroItem("USDKRW", "원/달러", 1350, 10, 0.75, unit="원", source="테스트"),
        core.MacroItem("US10Y", "미 10년물", 4.2, 0.05, 1.2, unit="%", source="테스트"),
        core.MacroItem("KR10Y", "한국 10년물", 3.1, -0.02, -0.6, unit="%", source="테스트"),
        core.MacroItem("FED", "미국 기준금리(EFFR)", 3.9, unit="%", source="테스트"),
        core.MacroItem("WTI", "WTI 유가", 80, 2, 2.6, unit="달러", source="테스트"),
        core.MacroItem("GOLD", "국제 금", 2400, -10, -0.4, unit="달러", source="테스트"),
        core.MacroItem("COPPER", "구리", 4.2, 0.1, 2.4, unit="달러", source="테스트"),
    ], flows=[core.MarketFlow(board="KOSPI", label="코스피", date="2026-11-20", foreign=-5000,
                              institution=2000, personal=3000, trading_value=10e12)])
    core.fetch_market_news = lambda per_topic=4: {  # type: ignore[assignment]
        "국내 증시": [{"title": "코스피 하락 마감", "link": "https://example.test/m1", "source": "테스트", "published": ""}],
        "반도체": [{"title": "HBM 수출 확대", "link": "https://example.test/m2", "source": "테스트", "published": ""}],
    }
    core.fetch_news = lambda query, limit=4: [  # type: ignore[assignment]
        {"title": f"{query} 관련 테스트 뉴스", "link": "https://example.test/news",
         "source": "테스트일보", "published": ""}]
    core.usd_krw = lambda: 1_350.0  # type: ignore[assignment]


def test_app_render() -> None:
    print("[16] 앱 렌더링(AppTest) — 빈 상태")
    from streamlit.testing.v1 import AppTest

    tmp = Path(tempfile.mkdtemp(prefix="vi-app-"))
    os.environ["VI_DATA_DIR"] = str(tmp)
    os.environ.pop("GSHEET_WEBAPP_URL", None)
    os.environ.pop("GSHEET_TOKEN", None)
    core.clear_cache()
    mocked_core()

    at = AppTest.from_file(str(HERE / "app.py"), default_timeout=180).run()
    check("예외 없이 실행", not list(at.exception), str([e.value for e in at.exception]))
    labels = [t.label for t in at.tabs]
    check("4개 탭 존재", len(labels) == 4, str(labels))
    check("탭 이름", labels[0].startswith("🛡️") and labels[1].startswith("🔍")
          and labels[2].startswith("🌐") and labels[3].startswith("📈"), str(labels))
    check("로컬 저장소 표시", any("로컬 파일" in i.value for i in at.info))
    check("빈 상태 안내", any("보유 종목이 아직 없습니다" in i.value for i in at.info))
    check("거시 지표 11개 렌더", {"코스피", "나스닥", "원/달러", "미국 기준금리(EFFR)", "구리"} <=
          {m.label for m in at.metric}, str([m.label for m in at.metric][:20]))

    print("[17] 앱 렌더링 — 보유 종목 + 상세(차트·밸류에이션·뉴스)")
    json.dump([
        {"code": "034020", "name": "두산에너빌리티", "qty": 10, "avg_price": 50000, "memo": ""},
        {"code": "047040", "name": "대우건설", "qty": 20, "avg_price": 12000, "memo": ""},
        {"code": "067310", "name": "하나마이크론", "qty": 8, "avg_price": 25000, "memo": ""},
    ], open(tmp / "holdings.json", "w"), ensure_ascii=False)
    core.clear_cache()
    at = AppTest.from_file(str(HERE / "app.py"), default_timeout=180).run()
    check("보유 종목 렌더 예외 없음", not list(at.exception), str([e.value for e in at.exception]))
    metrics = {m.label: m.value for m in at.metric}
    check("총 매수금액 계산", metrics.get("총 매수금액") == "940,000원", str(metrics))
    check("보유 종목 수", metrics.get("보유 종목") == "3종목")
    df = at.dataframe[0].value
    check("보유 표 3행", len(df) == 3, str(len(df)))
    check("배당 열 포함", {"배당수익률(%)", "DPS"} <= set(df.columns), str(list(df.columns)))
    check("52주 위치·이동평균 렌더", {"20일선", "60일선"} <= set(metrics), str(list(metrics)))
    tables = [m.value for m in at.markdown if "vi-tier" in m.value]
    check("4단계 대응 구간표 렌더", len(tables) >= 1, str(len(tables)))
    check("구간 이름 표기", any("적극 매수 검토" in t and "신규매수 신중" in t for t in tables))
    charts = list(at.get("vega_lite_chart")) if hasattr(at, "get") else []
    check("차트 렌더(가격+재무)", len(charts) >= 5, str(len(charts)))
    check("뉴스 카테고리 표기", any("수주·계약" in m.value for m in at.markdown))

    print("[18] 앱 렌더링 — 스캔 → 후보 상세")
    scan_btn = [b for b in at.button if b.label.startswith("🔍 스캔")]
    check("스캔 버튼 존재", len(scan_btn) == 1)
    scan_btn[0].click().run()
    check("스캔 예외 없음", not list(at.exception), str([e.value for e in at.exception]))
    m = {x.label: x.value for x in at.metric}
    check("스캔 결과 집계", m.get("스캔 종목", "").endswith("개") and "✅ 통과" in m, str(m))
    check("가치함정은 탈락, 우량주는 통과", m.get("✅ 통과") == "1개", str(m.get("✅ 통과")))
    check("후보 상세 선택 상자", any("상세 볼 종목" in s.label for s in at.selectbox),
          str([s.label for s in at.selectbox]))
    reg = [b for b in at.button if "추천 추적에 등록" in b.label]
    check("추천 등록 버튼", len(reg) >= 1, str([b.label for b in reg]))
    reg[0].click().run()
    picks_file = tmp / "picks.json"
    check("추천 등록 저장", picks_file.exists(), "picks.json 없음")
    if picks_file.exists():
        saved = json.loads(picks_file.read_text(encoding="utf-8"))
        check("저장된 추천에 등록가·목표·경계", bool(saved) and saved[0]["entry_price"]
              and saved[0]["target"] and saved[0]["stop"], str(saved))
    tracker = {x.label: x.value for x in at.metric}
    check("성적표에 등록 종목 반영", tracker.get("등록 종목") == "1개", str(tracker))

    print("[19] 앱 렌더링 — 종목명 검색 분석")
    at.text_input(key="an_query").set_value("삼성전자")
    submit = [b for b in at.button if b.label.startswith("검색")]
    check("검색 버튼 존재", len(submit) == 1, str([b.label for b in at.button]))
    submit[0].click().run()
    check("검색 분석 예외 없음", not list(at.exception), str([e.value for e in at.exception]))
    check("분석 결과 렌더", any("삼성전자" in m.value for m in at.markdown))

    print("[20] 앱 렌더링 — 구글 시트 모드 표시")
    os.environ["GSHEET_WEBAPP_URL"] = "https://script.google.com/macros/s/TEST/exec"
    os.environ["GSHEET_TOKEN"] = "t"
    core.requests = type("R", (), {  # type: ignore[assignment]
        "get": staticmethod(lambda *a, **k: FakeResp({"ok": True, "records": []})),
        "post": staticmethod(lambda *a, **k: FakeResp({"ok": True})),
    })
    core.clear_cache()
    mocked_core()
    at = AppTest.from_file(str(HERE / "app.py"), default_timeout=180).run()
    check("구글 시트 연동 표시", any("구글 시트 연동 중" in s.value for s in at.success),
          str([s.value for s in at.success]))
    os.environ.pop("GSHEET_WEBAPP_URL", None)
    os.environ.pop("GSHEET_TOKEN", None)


# ------------------------------------------------------------------- 실행 ----
def main() -> int:
    test_parsing()
    test_filter()
    test_score()
    test_scenarios()
    test_local_storage()
    test_sheet_storage()
    test_ai_and_secrets()
    test_search()
    test_universe()
    test_price_history()
    test_macro()
    test_flow_and_trend()
    test_valuation()
    test_news()
    test_impact()
    test_app_render()
    print()
    if failures:
        print(f"❌ 실패 {len(failures)}건: " + " / ".join(failures))
        return 1
    print("✅ 모든 테스트 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
