"""가치투자 스튜디오 테스트 — 로직 단위 테스트 + Streamlit AppTest 스모크 테스트.

실행:  python3 test_app.py

- 가치함정 필터 / 가치점수 / 3단 시나리오 로직
- 저장소 계층(로컬 JSON, 구글 시트 웹 앱 모킹)
- 실제 app.py 렌더링(빈 상태 · 보유 종목 · 스캔 · 추천 등록 · 추적 성적표)
네트워크는 전혀 쓰지 않는다(모든 외부 호출을 대역으로 교체).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
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
                market_cap=1e12, high52=12_000.0, low52=8_000.0, sources=["테스트"])
    base.update(kw)
    return core.StockSnapshot(**base)


# ------------------------------------------------------------ [1] 숫자 파싱 ----
def test_parsing() -> None:
    print("[1] 숫자·통화 파싱")
    check("'1,234' → 1234", core.parse_num("1,234") == 1234)
    check("'-3,550' → -3550", core.parse_num("-3,550") == -3550)
    check("'8.38배' → 8.38", core.parse_num("8.38배") == 8.38)
    check("'N/A' → None", core.parse_num("N/A") is None)
    check("'-' → None", core.parse_num("-") is None)
    check("'1조 4,362억' 환산", core.parse_korean_amount("1조 4,362억") == 1_436_200_000_000)
    check("'8,922억' 환산", core.parse_korean_amount("8,922억") == 892_200_000_000,
          str(core.parse_korean_amount("8,922억")))
    check("원화 표기", core.money(40250, "KRW") == "40,250원", core.money(40250, "KRW"))
    check("달러 표기", core.money(237.4, "USD") == "$237.40", core.money(237.4, "USD"))
    check("통화 표기(None)", core.money(None) == "-")


# --------------------------------------------------------- [2] 가치함정 필터 ----
def test_filter() -> None:
    print("[2] 가치함정 필터")
    rules = core.FilterRules()
    good = core.run_filter(snap(), rules)
    check("저PER·저PBR·흑자·저부채·고ROE·OCF+ → 통과", good["verdict"] == "통과", good["reason"])
    check("통과 시 사유 표시", "모든 기준 충족" in good["reason"], good["reason"])

    trap = core.run_filter(snap(code="999999", ocf=-1e8), rules)
    check("영업현금흐름 마이너스 → 탈락", trap["verdict"] == "탈락", trap["verdict"])
    check("탈락 사유에 가치함정 표현", any("가치함정" in r for r in trap["fail_reasons"]), trap["fail_reasons"])

    debt = core.run_filter(snap(debt_ratio=320.0), rules)
    check("부채비율 320% → 탈락", debt["verdict"] == "탈락" and any("부채비율" in r for r in debt["fail_reasons"]))

    loss = core.run_filter(snap(op_income=-50.0, per=-3.0), rules)
    check("영업이익 적자 → 탈락", loss["verdict"] == "탈락", loss["verdict"])
    check("적자 PER 은 '적자'로 표기", any("적자" in c["text"] for c in loss["checks"]),
          [c["text"] for c in loss["checks"]])

    priced = core.run_filter(snap(per=40.0), rules)
    check("PER 40배 → 탈락", priced["verdict"] == "탈락")

    relaxed = core.FilterRules(max_per=50, max_pbr=3.0, min_roe=0.0)
    check("기준 완화 시 통과", core.run_filter(snap(per=40.0), relaxed)["verdict"] == "통과")

    unknown = core.run_filter(snap(debt_ratio=None, ocf=None), rules)
    check("확인 불가 2개 → 판정 보류", unknown["verdict"] == "판정 보류", unknown["verdict"])

    empty = core.run_filter(core.StockSnapshot(code="X", price=None), rules)
    check("데이터 없음 → 판정 불가", empty["verdict"] == "데이터 없음", empty["verdict"])

    bank = core.run_filter(snap(sector="금융", debt_ratio=None, ocf=None), rules)
    check("금융업 안내 문구", "금융업" in bank["note"], bank["note"])


# ------------------------------------------------------------ [3] 가치점수 ----
def test_score() -> None:
    print("[3] 종합 가치점수")
    cheap = core.value_score(snap(per=4.0, pbr=0.4, roe=18.0, op_margin=20.0,
                                  debt_ratio=20.0, ocf=1e9, revenue_yoy=25.0, op_yoy=40.0))[0]
    pricey = core.value_score(snap(per=40.0, pbr=4.0, roe=1.0, op_margin=0.5,
                                   debt_ratio=350.0, ocf=-1e8, revenue_yoy=-15.0, op_yoy=-30.0))[0]
    check(f"저평가 우량주 점수 높음({cheap})", cheap >= 80, str(cheap))
    check(f"고평가 부실주 점수 낮음({pricey})", pricey <= 20, str(pricey))
    check("점수 범위 0~100", 0 <= cheap <= 100 and 0 <= pricey <= 100)
    score, parts = core.value_score(snap())
    check("점수 구성 4개 항목", set(parts) == {"저평가", "수익성", "안정성", "성장성"}, str(parts))
    check("값 없으면 중립 50 처리", core.value_score(core.StockSnapshot(code="Z"))[0] == 50)


# ------------------------------------------------------------ [4] 시나리오 ----
def test_scenarios() -> None:
    print("[4] 3단 시나리오")
    s = snap()
    scn = core.build_scenarios(s, avg_price=12_000.0)
    check("3단 시나리오 키 존재", set(["상승", "기본", "경계"]) <= set(scn))
    check("상승 목표 ≥ 현재가 +20%", scn["target"] >= s.price * 1.2 - 1e-6,
          f"{scn['target']} vs {s.price * 1.2}")
    check("상승 목표 ≤ 현재가 +60%", scn["target"] <= s.price * 1.6 + 1e-6, str(scn["target"]))
    check("기본 적정가 산출", scn["fair"] is not None and scn["fair"] > 0)
    check("경계선 = 평단 -15%", abs(scn["stop"] - 12_000 * 0.85) < 1e-6, str(scn["stop"]))
    check("각 시나리오에 대응 기준 3개 이상", all(len(scn[k]["triggers"]) >= 3 for k in ("상승", "기본", "경계")))
    check("상승 시나리오에 분할 익절 기준", any("분할 익절" in t for t in scn["상승"]["triggers"]))
    check("경계 시나리오에 투자금 보존 언급", any("평단 -15%" in scn["경계"]["range_text"] for _ in [0]))

    us = core.build_scenarios(snap(market="US", currency="USD", price=200.0, eps=10.0, bps=80.0))
    check("미국 종목은 달러 표기", us["상승"]["range_text"].startswith("목표 $"), us["상승"]["range_text"])

    risky = core.build_scenarios(snap(eps=-500.0, bps=None, per=None, pbr=None))
    check("적자·BPS 없음 → 적정가 산출 불가 문구", "산출 불가" in risky["기본"]["range_text"],
          risky["기본"]["range_text"])
    check("적자 종목도 경계선은 산출", risky["stop"] is not None)
    check("단독 종목(평단 없음)은 현재가 기준 경계선", abs(core.build_scenarios(s)["stop"] - s.price * 0.88) < 1e-6)


# ------------------------------------------------------------- [5] 저장소 ----
class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

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
    check("시트 없으면 로컬 모드", core.storage_mode() == "local", core.storage_mode())

    rows = [{"code": "034020", "name": "두산에너빌리티", "qty": 3, "avg_price": 100000, "memo": "테스트"}]
    core.save_holdings(rows)
    loaded = core.load_holdings(force=True)
    check("보유 종목 저장·조회", len(loaded) == 1 and loaded[0]["code"] == "034020", str(loaded))
    check("수량 숫자 유지", loaded[0]["qty"] == 3, str(loaded[0]))

    picks = [{"date": "2026-11-22", "code": "069620", "name": "대웅제약", "entry_price": 98500,
              "rationale": "가치점수 88점", "target": 141576, "stop": 86680, "memo": ""}]
    core.save_picks(picks)
    check("추천 저장·조회", core.load_picks(force=True)[0]["code"] == "069620")
    check("추천 파일 JSON 기록", (tmp / "picks.json").exists())

    core.save_holdings([])
    check("빈 목록 저장 가능", core.load_holdings(force=True) == [])


def test_sheet_storage() -> None:
    print("[6] 저장소 — 구글 시트 웹 앱 모킹")
    os.environ["VI_DATA_DIR"] = tempfile.mkdtemp(prefix="vi-store2-")
    os.environ["GSHEET_WEBAPP_URL"] = "https://script.google.com/macros/s/TEST/exec"
    os.environ["GSHEET_TOKEN"] = "test-token"
    core.clear_cache()
    check("URL 설정 시 시트 모드", core.storage_mode() == "gsheet")
    check("연동 상태 표기", core.storage_label() == "구글 시트 연동 중")

    calls: list[dict] = []

    class FakeRequests:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None, allow_redirects=True):
            calls.append({"method": "GET", "url": url, "params": params})
            sheet = (params or {}).get("sheet")
            if sheet == "holdings":
                return FakeResp({"ok": True, "records": [
                    {"종목코드": "067310", "종목명": "하나마이크론", "수량": 8, "평단가": 25000, "메모": ""},
                    {"종목코드": "", "종목명": "빈행", "수량": 1, "평단가": 1},
                ]})
            return FakeResp({"ok": True, "records": [
                {"등록일": "2026-11-22", "종목코드": "069620", "종목명": "대웅제약", "등록가": 98500,
                 "선정근거": "가치점수 88점", "목표가": 141576, "경계가": 86680, "상태메모": ""},
            ]})

        @staticmethod
        def post(url, json=None, headers=None, timeout=None, allow_redirects=True):
            calls.append({"method": "POST", "url": url, "json": json})
            return FakeResp({"ok": True, "count": len((json or {}).get("records") or [])})

    original = core.requests
    core.requests = FakeRequests  # type: ignore[assignment]
    try:
        holders = core.load_holdings(force=True)
        check("GET 응답 파싱", len(holders) == 1 and holders[0]["name"] == "하나마이크론", str(holders))
        check("GET 요청에 token·sheet 전달",
              calls[0]["params"].get("token") == "test-token" and calls[0]["params"].get("sheet") == "holdings",
              str(calls[0]))

        core.save_holdings([{"code": "005930", "name": "삼성전자", "qty": 2, "avg_price": 70000, "memo": ""}])
        post = [c for c in calls if c["method"] == "POST"][-1]
        check("POST 토큰·시트 지정", post["json"].get("token") == "test-token" and post["json"].get("sheet") == "holdings")
        rec = post["json"]["records"][0]
        check("POST 한글 헤더 매핑", rec.get("종목코드") == "005930" and rec.get("평단가") == 70000, str(rec))

        picks = core.load_picks(force=True)
        check("추천추적 탭 조회", calls[-1]["params"]["sheet"] == "picks" and len(picks) == 1
              and picks[0]["code"] == "069620" and picks[0]["entry_price"] == 98500, str(picks))

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

        class Broken(FakeRequests):
            @staticmethod
            def get(url, params=None, headers=None, timeout=None, allow_redirects=True):
                raise RuntimeError("DNS 실패")

        core.requests = Broken  # type: ignore[assignment]
        core.clear_cache()
        raised = ""
        try:
            core.load_holdings(force=True)
        except core.StorageError as exc:
            raised = str(exc)
        check("네트워크 오류 → StorageError", "구글 시트 연결 실패" in raised, raised)
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
    text, err = core.generate_ai_report(snap(), core.build_scenarios(snap()), core.run_filter(snap(), core.FilterRules()))
    check("키 없으면 본문 없이 안내", text is None and "AI 키" in err, err)

    class Secrets(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    core.apply_secrets(Secrets({"GSHEET_WEBAPP_URL": "https://example.test/exec"}))
    check("Secrets → 환경변수 반영", os.environ.get("GSHEET_WEBAPP_URL") == "https://example.test/exec")
    os.environ.pop("GSHEET_WEBAPP_URL", None)

    os.environ["OPENAI_API_KEY"] = "sk-test"
    check("키 설정 시 제공자 인식", core.ai_config()[0] == "openai", str(core.ai_config()))
    os.environ.pop("OPENAI_API_KEY", None)


# ------------------------------------------------------- [8] 앱 렌더링 --------
def mocked_core(monkeypatch_scan: bool = True) -> None:
    """앱이 네트워크 없이 돌도록 외부 호출을 대역으로 교체."""
    def fake_snapshot(code, sector=None, name_hint=None):
        table = {
            "034020": snap(code="034020", name="두산에너빌리티", sector="원전·에너지", price=40_250,
                           per=118.7, pbr=3.46, eps=339, bps=11_648, roe=1.1, debt_ratio=129.1,
                           op_income=7_627, op_margin=4.5, ocf=3.6e11),
            "047040": snap(code="047040", name="대우건설", sector="건설", price=10_330,
                           per=-1.74, pbr=1.12, eps=-1_362, bps=9_216, roe=-23.9, debt_ratio=284.5,
                           op_income=-800, op_margin=-1.2, ocf=None),
            "067310": snap(code="067310", name="하나마이크론", sector="반도체", price=21_600,
                           per=8.38, pbr=2.6, eps=2_578, bps=8_311, roe=9.9, debt_ratio=209.2,
                           op_income=1_277, op_margin=9.0, ocf=None),
        }
        if code in table:
            return table[code]
        if code == "069620":
            return snap(code="069620", name="대웅제약", sector="바이오", price=98_500, per=6.22, pbr=1.06,
                        eps=15_836, bps=92_925, roe=21.75, debt_ratio=108.2, op_income=1_200,
                        op_margin=12.0, ocf=2e8, revenue_yoy=8.0, op_yoy=15.0)
        # 그 밖의 종목은 전형적인 가치함정(고평가·고부채·현금흐름 마이너스)
        return snap(code=code, name=name_hint or f"종목{code}", price=50_000, per=28.0, pbr=3.2,
                    eps=1_800, bps=15_600, roe=2.0, debt_ratio=260.0, op_income=100.0,
                    op_margin=2.0, ocf=-1e8, revenue_yoy=-5.0, op_yoy=-10.0)

    def fake_snapshots(items, workers=8):
        return [fake_snapshot(it["code"], it.get("sector"), it.get("name")) for it in items]

    def fake_scan(items, rules):
        rows = []
        for item in items:
            s = fake_snapshot(item["code"], item.get("sector"), item.get("name"))
            rows.append({"snap": s, "filter": core.run_filter(s, rules)})
        rows.sort(key=lambda r: (-(r["filter"]["passed"]), -r["filter"]["score"]))
        return rows

    core.fetch_snapshot = fake_snapshot  # type: ignore[assignment]
    core.fetch_snapshots = fake_snapshots  # type: ignore[assignment]
    if monkeypatch_scan:
        core.scan_universe = fake_scan  # type: ignore[assignment]
    core.fetch_news = lambda query, limit=4: [  # type: ignore[assignment]
        {"title": f"{query} 관련 테스트 뉴스", "link": "https://example.test/news",
         "source": "테스트일보", "published": ""}]
    core.fetch_market_news = lambda per_topic=4: {  # type: ignore[assignment]
        "국내 증시": [{"title": "코스피 상승 마감", "link": "https://example.test/1", "source": "테스트", "published": ""}],
        "반도체": [{"title": "HBM 수출 확대", "link": "https://example.test/2", "source": "테스트", "published": ""}],
    }
    core.usd_krw = lambda: 1_350.0  # type: ignore[assignment]


def test_app_render() -> None:
    print("[8] 앱 렌더링(AppTest) — 빈 상태")
    from streamlit.testing.v1 import AppTest

    tmp = Path(tempfile.mkdtemp(prefix="vi-app-"))
    os.environ["VI_DATA_DIR"] = str(tmp)
    os.environ.pop("GSHEET_WEBAPP_URL", None)
    os.environ.pop("GSHEET_TOKEN", None)
    core.clear_cache()
    mocked_core()

    at = AppTest.from_file(str(HERE / "app.py"), default_timeout=120).run()
    check("예외 없이 실행", not list(at.exception), str(list(at.exception)))
    check("4개 탭 존재", len(at.tabs) == 4, str([t.label for t in at.tabs]))
    labels = [t.label for t in at.tabs]
    check("탭 이름", labels[0].startswith("🛡️") and labels[1].startswith("🔍")
          and labels[2].startswith("🌐") and labels[3].startswith("📈"), str(labels))
    check("로컬 저장소 표시", any("로컬 파일" in i.value for i in at.info))
    check("빈 상태 안내", any("보유 종목이 아직 없습니다" in i.value for i in at.info))

    print("[9] 앱 렌더링 — 보유 종목 · 시나리오")
    json.dump([
        {"code": "034020", "name": "두산에너빌리티", "qty": 10, "avg_price": 50000, "memo": ""},
        {"code": "047040", "name": "대우건설", "qty": 20, "avg_price": 12000, "memo": ""},
        {"code": "067310", "name": "하나마이크론", "qty": 8, "avg_price": 25000, "memo": ""},
    ], open(tmp / "holdings.json", "w"), ensure_ascii=False)
    core.clear_cache()
    at = AppTest.from_file(str(HERE / "app.py"), default_timeout=120).run()
    check("보유 종목 렌더 예외 없음", not list(at.exception), str(list(at.exception)))
    metrics = {m.label: m.value for m in at.metric}
    check("총 매수금액 계산", metrics.get("총 매수금액", "") == "940,000원", str(metrics))
    check("보유 종목 수", metrics.get("보유 종목") == "3종목", str(metrics.get("보유 종목")))
    df = at.dataframe[0].value
    check("보유 표 3행", len(df) == 3, str(len(df)))
    check("현재가·손익·밸류에이션 열", {"현재가", "평가손익(원)", "PER", "PBR", "신호"} <= set(df.columns), str(list(df.columns)))
    check("영업적자 신호", any("영업적자" in str(v) for v in df["신호"]), str(df["신호"].tolist()))
    expanders = [e.label for e in at.expander]
    check("종목별 시나리오 expander", any("두산에너빌리티" in e for e in expanders), str(expanders))
    cards = [m.value for m in at.markdown if "vi-card" in m.value]
    check("상승/기본/경계 카드 렌더", len(cards) >= 9, f"{len(cards)} cards")

    print("[10] 앱 렌더링 — 스캔 → 추천 등록 → 성적표")
    at.run()
    scan_btn = [b for b in at.button if b.label.startswith("🔍 스캔")]
    check("스캔 버튼 존재", len(scan_btn) == 1)
    scan_btn[0].click().run()
    check("스캔 예외 없음", not list(at.exception), str(list(at.exception)))
    m = {x.label: x.value for x in at.metric}
    check("스캔 결과 집계", m.get("스캔 종목") == "52개" and "✅ 통과" in m and "❌ 탈락" in m, str(m))
    check("가치함정은 탈락, 우량주는 통과", m.get("✅ 통과") == "1개", str(m.get("✅ 통과")))
    reg = [b for b in at.button if "추천 추적에 등록" in b.label]
    check("통과 종목 등록 버튼", len(reg) >= 1, str([b.label for b in reg]))
    reg[0].click().run()
    picks_file = tmp / "picks.json"
    check("추천 등록 저장", picks_file.exists(), "picks.json 없음")
    if picks_file.exists():
        saved = json.loads(picks_file.read_text(encoding="utf-8"))
        check("저장된 추천에 등록가·목표·경계", bool(saved) and saved[0]["entry_price"] and saved[0]["target"]
              and saved[0]["stop"], str(saved))
    tracker_metric = {x.label: x.value for x in at.metric}
    check("성적표에 등록 종목 반영", tracker_metric.get("등록 종목") == "1개", str(tracker_metric))

    print("[11] 앱 렌더링 — 구글 시트 모드 표시")
    os.environ["GSHEET_WEBAPP_URL"] = "https://script.google.com/macros/s/TEST/exec"
    os.environ["GSHEET_TOKEN"] = "t"
    core.requests = type("R", (), {  # type: ignore[assignment]
        "get": staticmethod(lambda *a, **k: FakeResp({"ok": True, "records": []})),
        "post": staticmethod(lambda *a, **k: FakeResp({"ok": True})),
    })
    core.clear_cache()
    at = AppTest.from_file(str(HERE / "app.py"), default_timeout=120).run()
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
    test_app_render()
    print()
    if failures:
        print(f"❌ 실패 {len(failures)}건: " + " / ".join(failures))
        return 1
    print("✅ 모든 테스트 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
