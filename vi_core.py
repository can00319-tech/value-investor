"""가치투자 웹앱 핵심 로직.

Streamlit UI(app.py)와 분리된 순수 파이썬 계층:
  - 시세·재무 데이터 수집 (네이버 금융 → yfinance → 야후 차트 폴백)
  - 가치 함정 필터 / 종합 가치점수
  - 상승·기본·경계 3단 시나리오
  - 뉴스(RSS) 수집
  - 저장소 계층 (구글 시트 웹 앱 ←→ 로컬 JSON)

Streamlit 을 import 하지 않으므로 단위 테스트에서 그대로 쓸 수 있다.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ------------------------------------------------------------------ 상수 ----
KST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("VI_DATA_DIR") or (BASE_DIR / "data"))

NAVER_BASE = "https://m.stock.naver.com/api/stock"
NAVER_API = "https://api.stock.naver.com"
NAVER_AC = "https://ac.stock.naver.com/ac"
NAVER_INDEX = "https://m.stock.naver.com/api/index"
NAVER_MARKET_INDEX = "https://m.stock.naver.com/front-api/marketIndex/productDetail"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
GOOGLE_NEWS = "https://news.google.com/rss/search"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}
TIMEOUT = (4.0, 12.0)

SECTOR_ORDER = ["원전·에너지", "건설", "반도체", "바이오", "금융", "제조·철강",
                "자동차·조선", "2차전지", "인터넷·SW", "유통·식품", "통신·운송", "기타"]

# 스캔 화면의 시장 필터 라벨 → 유니버스 market 값
MARKET_LABELS = ["전체", "코스피", "코스닥", "미국"]
MARKET_BOARD = {"코스피": "KOSPI", "코스닥": "KOSDAQ", "미국": "US",
                "KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ", "US": "US"}
BOARD_LABEL = {"KOSPI": "코스피", "KOSDAQ": "코스닥", "US": "미국"}


def now_kst() -> datetime:
    return datetime.now(KST)


def today_str() -> str:
    return now_kst().strftime("%Y-%m-%d")


# ------------------------------------------------------------- 숫자 파싱 ----
_NUM_RE = re.compile(r"[-+]?\d[\d,\.]*")


def parse_num(value) -> float | None:
    """'1,234', '-3,550', '8.38배', 'N/A', '-', '', None → float | None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    text = str(value).strip()
    if text in {"", "-", "N/A", "n/a", "None", "nan", "--"}:
        return None
    m = _NUM_RE.search(text.replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


_UNIT = {"조": 1_000_000_000_000, "억": 100_000_000, "백만": 1_000_000, "만": 10_000, "천": 1_000}


def parse_korean_amount(text) -> float | None:
    """'1조 4,362억' → 1,436,200,000,000 / '8,922억' → 89,220,000,000."""
    if text is None:
        return None
    s = str(text).replace(",", "").replace(" ", "")
    if s in {"", "-", "N/A"}:
        return None
    total = 0.0
    found = False
    for m in re.finditer(r"([\d.]+)(조|억|백만|만|천)?", s):
        num = parse_num(m.group(1))
        if num is None:
            continue
        total += num * _UNIT.get(m.group(2) or "", 1)
        found = True
    return total if found else parse_num(s)


# ---------------------------------------------------------- 종목 스냅샷 ----
@dataclass
class StockSnapshot:
    """한 종목의 시세 + 재무 요약. 값이 없으면 None 으로 두고 note 에 사유를 남긴다."""

    code: str
    name: str = ""
    market: str = "KR"                 # KR | US
    currency: str = "KRW"
    sector: str = "기타"
    price: float | None = None
    change_pct: float | None = None
    per: float | None = None
    pbr: float | None = None
    eps: float | None = None
    bps: float | None = None
    roe: float | None = None           # %
    debt_ratio: float | None = None    # %
    op_income: float | None = None     # KR: 억원, US: 원화 환산 없이 USD
    op_margin: float | None = None     # %
    revenue: float | None = None
    revenue_yoy: float | None = None   # %
    op_yoy: float | None = None        # %
    ocf: float | None = None           # KR: 원, US: USD
    net_income: float | None = None    # KR: 억원, US: USD(백만)
    shares: float | None = None        # 발행주식 수(DCF용)
    market_cap: float | None = None
    high52: float | None = None
    low52: float | None = None
    dividend_yield: float | None = None   # %
    dps: float | None = None              # 주당배당금(통화 동일)
    dividend_year: str = ""
    fiscal_year: str = ""
    as_of: str = ""
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.name}({self.code})" if self.name else self.code

    def to_dict(self) -> dict:
        return {
            k: v for k, v in self.__dict__.items()
        }


def memoize(ttl: float):
    """가벼운 TTL 메모(테스트·서버 공용)."""

    def deco(fn):
        store: dict = {}

        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            hit = store.get(key)
            now = time.time()
            if hit and now - hit[0] < ttl:
                return hit[1]
            value = fn(*args, **kwargs)
            store[key] = (now, value)
            return value

        wrapper.cache_clear = store.clear  # type: ignore[attr-defined]
        return wrapper

    return deco


@memoize(ttl=3600)
def usd_krw() -> float:
    """원/달러 환율(해외 종목 평가금액 환산용). 실패하면 1,400원 가정."""
    data = _get_json(YAHOO_CHART.format(symbol="KRW=X"), {"range": "5d", "interval": "1d"})
    try:
        meta = ((data or {}).get("chart", {}).get("result") or [{}])[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price:
            return float(price)
    except Exception:
        pass
    return 1400.0


@memoize(ttl=3600)
def resolve_query(text: str) -> dict | None:
    """종목명·종목코드·티커 입력 → {code, name, market, board, sector}. 찾지 못하면 None.

    '삼성전자', 'HPSP', '현대차', '엔비디아', 'NVDA' 처럼 이름·티커 어느 쪽으로도 찾는다.
    """
    found = search_stocks(text, limit=1)
    return found[0] if found else None


def detect_market(code_or_name: str) -> str:
    s = str(code_or_name).strip().upper()
    if re.fullmatch(r"\d{6}", s):
        return "KR"
    if re.fullmatch(r"\d{6}\.(KS|KQ)", s):
        return "KR"
    return "US"


def clean_code(code: str) -> str:
    s = str(code).strip().upper()
    s = re.sub(r"\.(KS|KQ)$", "", s)
    return s


_SESSION = requests.Session()
_SESSION.headers.update(HEADERS)


def _get_json(url: str, params: dict | None = None) -> dict | None:
    """GET → JSON. 실패하면 None(앱은 계속 동작). 연결은 세션으로 재사용한다."""
    try:
        resp = _SESSION.get(url, params=params, timeout=TIMEOUT)
        if resp.status_code != 200:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code != 200:
                return None
        return resp.json()
    except Exception:
        return None


def _get_list(url: str, params: dict | None = None) -> list | None:
    """GET → JSON 배열 응답(네이버 차트·수급 API)."""
    data = _get_json(url, params)
    return data if isinstance(data, list) else None


# ---------------------------------------------------------- 네이버 금융 ----
def _naver_quote(code: str) -> dict:
    """네이버 기본 시세: 현재가·등락률·종목명·시장."""
    out: dict = {}
    data = _get_json(f"{NAVER_BASE}/{code}/basic")
    if data:
        out["name"] = data.get("stockName") or ""
        out["price"] = parse_num(data.get("closePrice"))
        out["change_pct"] = parse_num(data.get("fluctuationsRatio"))
        exchange = str(data.get("stockExchangeName") or "")
        out["exchange"] = exchange
        out["as_of"] = str(data.get("localTradedAt") or "")
    return out


def _nav_infos(items: list | None) -> dict:
    """네이버 totalInfos 배열 → {code: (value, valueDesc)}."""
    out: dict = {}
    for t in items or []:
        if not isinstance(t, dict):
            continue
        key = t.get("code")
        if key:
            out[key] = (t.get("value"), t.get("valueDesc") or t.get("keyDesc") or "")
    return out


def _naver_integration(code: str) -> dict:
    """네이버 종합 정보: PER·PBR·EPS·BPS·시총·52주 최고/최저·배당수익률·DPS."""
    out: dict = {}
    data = _get_json(f"{NAVER_BASE}/{code}/integration")
    if not data:
        return out
    if not out.get("name"):
        out["name"] = data.get("stockName") or ""
    raw = _nav_infos(data.get("totalInfos"))
    infos = {k: v[0] for k, v in raw.items()}
    out["per"] = parse_num(infos.get("per"))
    out["pbr"] = parse_num(infos.get("pbr"))
    out["eps"] = parse_num(infos.get("eps"))
    out["bps"] = parse_num(infos.get("bps"))
    out["market_cap"] = parse_korean_amount(infos.get("marketValue"))
    out["high52"] = parse_num(infos.get("highPriceOf52Weeks"))
    out["low52"] = parse_num(infos.get("lowPriceOf52Weeks"))
    out["dividend_yield"] = parse_num(infos.get("dividendYieldRatio"))
    out["dps"] = parse_num(infos.get("dividend"))
    out["dividend_year"] = _year_hint(raw.get("dividendYieldRatio") or raw.get("dividend"))
    if out.get("price") is None:
        out["price"] = parse_num(infos.get("lastClosePrice"))
    return out


def _year_hint(pair: tuple | None) -> str:
    """네이버 valueDesc('2025.12.') → '2025'."""
    if not pair:
        return ""
    m = re.search(r"(20\d{2})", str(pair[1] or ""))
    return m.group(1) if m else ""


@memoize(ttl=3600)
def _fin_raw(url: str) -> dict:
    return _get_json(url) or {}


def _naver_annual(code: str, period: str = "annual") -> dict:
    """네이버 연간 재무: 영업이익·매출·ROE·부채비율·영업이익률(+전년 대비 성장률).

    추정치(isConsensus='Y')가 아닌 가장 최근 확정 실적 열을 쓴다.
    """
    out: dict = {}
    data = _fin_raw(f"{NAVER_BASE}/{code}/finance/{period}")
    if not data:
        return out
    info = data.get("financeInfo") or {}
    titles = info.get("trTitleList") or []
    actual = [t for t in titles if str(t.get("isConsensus", "N")).upper() != "Y"]
    if not actual:
        return out
    rows = {r.get("title"): r.get("columns") or {} for r in info.get("rowList") or []}

    def value_at(title: str, key: str):
        col = rows.get(title) or {}
        cell = col.get(key) or {}
        return parse_num(cell.get("value") if isinstance(cell, dict) else cell)

    cur = actual[-1].get("key")
    prev = actual[-2].get("key") if len(actual) > 1 else None
    out["fiscal_year"] = str(actual[-1].get("title") or "")
    out["revenue"] = value_at("매출액", cur)
    out["op_income"] = value_at("영업이익", cur)
    out["net_income"] = value_at("당기순이익", cur)
    out["op_margin"] = value_at("영업이익률", cur)
    out["roe"] = value_at("ROE", cur)
    out["debt_ratio"] = value_at("부채비율", cur)
    out["per_annual"] = value_at("PER", cur)
    out["pbr_annual"] = value_at("PBR", cur)
    out["dps"] = value_at("주당배당금", cur)
    if prev:
        rev_prev = value_at("매출액", prev)
        op_prev = value_at("영업이익", prev)
        rev_cur = out.get("revenue")
        if rev_cur is not None and rev_prev not in (None, 0):
            out["revenue_yoy"] = (rev_cur - rev_prev) / abs(rev_prev) * 100
        if out.get("op_income") is not None and op_prev not in (None, 0):
            out["op_yoy"] = (out["op_income"] - op_prev) / abs(op_prev) * 100
    return out


def _yahoo_quote(symbol: str) -> dict:
    """야후 차트 API 폴백(가격·등락률)."""
    out: dict = {}
    data = _get_json(YAHOO_CHART.format(symbol=symbol),
                     {"range": "1mo", "interval": "1d"})
    try:
        result = (data or {}).get("chart", {}).get("result") or []
        if not result:
            return out
        meta = result[0].get("meta") or {}
        closes = [c for c in ((result[0].get("indicators") or {})
                              .get("quote") or [{}])[0].get("close", []) if c is not None]
        price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None:
            return out
        out["price"] = float(price)
        if prev:
            out["change_pct"] = (float(price) - float(prev)) / float(prev) * 100
        out["name"] = meta.get("longName") or meta.get("shortName") or ""
        out["currency"] = meta.get("currency") or ""
        out["high52"] = meta.get("fiftyTwoWeekHigh")
        out["low52"] = meta.get("fiftyTwoWeekLow")
    except Exception:
        return out
    return out


def _yfinance_info(symbols: list[str]) -> dict:
    """yfinance 로 재무 지표 보완(영업현금흐름·ROE·부채비율 등). 실패하면 빈 dict."""
    try:
        import yfinance as yf  # 지연 import: 없어도 앱이 동작한다
    except Exception:
        return {}
    for sym in symbols:
        try:
            info = yf.Ticker(sym).info or {}
            if info.get("currentPrice") or info.get("regularMarketPrice") or info.get("trailingPE"):
                return info
        except Exception:
            continue
    return {}


def _from_yf(info: dict, snapshot: StockSnapshot) -> None:
    """yfinance info → 스냅샷 빈 칸 채우기."""
    if not info:
        return

    def take(attr: str, keys: tuple[str, ...], scale: float = 1.0):
        if getattr(snapshot, attr) is not None:
            return
        for k in keys:
            v = info.get(k)
            if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)):
                setattr(snapshot, attr, v * scale)
                return

    take("price", ("currentPrice", "regularMarketPrice"))
    take("per", ("trailingPE",))
    take("pbr", ("priceToBook",))
    take("eps", ("trailingEps",))
    take("bps", ("bookValue",))
    take("roe", ("returnOnEquity",), 100.0)
    take("debt_ratio", ("debtToEquity",))
    take("op_income", ("operatingIncome",))
    take("op_margin", ("operatingMargins",), 100.0)
    take("revenue", ("totalRevenue",))
    take("revenue_yoy", ("revenueGrowth",), 100.0)
    take("ocf", ("operatingCashflow",))
    take("market_cap", ("marketCap",))
    take("high52", ("fiftyTwoWeekHigh",))
    take("low52", ("fiftyTwoWeekLow"))
    if info.get("sector") and snapshot.sector == "기타":
        snapshot.sector = str(info.get("sector"))
    if info.get("longName") and not snapshot.name:
        snapshot.name = str(info.get("longName"))


def _resolve_sector(code: str, name: str, given: str | None) -> str:
    if given:
        return given
    for item in UNIVERSE:
        if clean_code(item["code"]) == clean_code(code):
            return item["sector"]
    text = f"{name}"
    keyword = {
        "원전·에너지": ["에너빌", "원전", "한전", "전력", "파워", "일렉트릭", "에너지", "일렉"],
        "건설": ["건설", "E&A", "이앤에이", "건축", "시멘트"],
        "반도체": ["반도체", "마이크론", "코미코", "하이닉스", "전자", "원익", "SEMI", "디스플레이"],
        "바이오": ["바이오", "약품", "제약", "클래시스", "메디", "헬스", "셀트리온"],
        "금융": ["금융", "은행", "지주", "증권", "보험", "화재", "캐피탈"],
        "자동차·조선": ["자동차", "모비스", "조선", "중공업", "기아", "타이어"],
        "2차전지": ["배터리", "이차전지", "에코프로", "솔루션", "SDI"],
        "인터넷·SW": ["인터넷", "게임", "소프트", "네이버", "카카오", "엔씨"],
        "유통·식품": ["식품", "유통", "제과", "음료", "백화점", "마트"],
        "통신·운송": ["통신", "항공", "해운", "물류", "택배"],
        "제조·철강": ["제철", "포스코", "철강", "화학", "기계"],
    }
    for sector, words in keyword.items():
        if any(w in text for w in words):
            return sector
    return "기타"


@memoize(ttl=86400)
def _reuters_for(code: str, hint: str = "") -> str:
    """미국 티커 → 네이버 해외 종목 코드(NVDA.O / CEG.N). 실패하면 티커 그대로."""
    code = clean_code(code)
    if hint:
        return hint
    if re.fullmatch(r"\d{6}", code):
        return code
    for suffix in (".O", ".N", ".A"):
        data = _get_json(f"{NAVER_API}/stock/{code}{suffix}/basic")
        if data and data.get("closePrice"):
            return f"{code}{suffix}"
    return code


@memoize(ttl=300)
def _naver_us_basic(code: str) -> dict:
    """네이버 해외 종목 기본: 가격·등락률·PER·PBR·52주·배당."""
    out: dict = {}
    data = _get_json(f"{NAVER_API}/stock/{_reuters_for(code)}/basic")
    if not data:
        return out
    raw = _nav_infos(data.get("stockItemTotalInfos"))
    infos = {k: v[0] for k, v in raw.items()}
    out["name"] = data.get("stockName") or ""
    out["name_eng"] = data.get("stockNameEng") or ""
    out["price"] = parse_num(data.get("closePrice"))
    out["change_pct"] = parse_num(data.get("fluctuationsRatio"))
    out["as_of"] = str(data.get("localTradedAt") or "")
    out["exchange"] = str(data.get("stockExchangeName") or "")
    out["per"] = parse_num(infos.get("per"))
    out["pbr"] = parse_num(infos.get("pbr"))
    out["eps"] = parse_num(infos.get("eps"))
    out["bps"] = parse_num(infos.get("bps"))
    out["high52"] = parse_num(infos.get("highPriceOf52Weeks"))
    out["low52"] = parse_num(infos.get("lowPriceOf52Weeks"))
    out["dividend_yield"] = parse_num(infos.get("dividendYieldRatio"))
    out["dps"] = parse_num(infos.get("dividend"))
    out["dividend_year"] = _year_hint(raw.get("dividendYieldRatio") or raw.get("dividend"))
    out["sector_hint"] = str(infos.get("industryGroupKor") or "")
    cap = data.get("marketValueKrwRaw")
    out["market_cap"] = parse_num(cap) if cap else None
    out["shares"] = parse_num(data.get("countOfListedStock"))
    return out


@memoize(ttl=3600)
def _naver_us_annual(code: str) -> dict:
    """네이버 해외 종목 연간 실적(USD 백만 단위 → USD로 환산해 반환)."""
    out: dict = {}
    data = _get_json(f"{NAVER_API}/stock/{_reuters_for(code)}/finance/annual")
    if not data:
        return out
    actual = [t for t in data.get("trTitleList") or []
              if str(t.get("isConsensus", "N")).upper() != "Y"]
    if not actual:
        return out
    rows = {r.get("title"): r.get("columns") or {} for r in data.get("rowList") or []}

    def value_at(title: str, key: str, scale: float = 1.0):
        cell = (rows.get(title) or {}).get(key) or {}
        v = parse_num(cell.get("value") if isinstance(cell, dict) else cell)
        return None if v is None else v * scale

    cur = actual[-1].get("key")
    out["fiscal_year"] = str(actual[-1].get("title") or "")
    out["revenue"] = value_at("매출액", cur, 1e6)
    out["op_income"] = value_at("EBIT", cur, 1e6)
    out["net_income"] = value_at("당기순이익", cur, 1e6)
    out["dividend_yield"] = value_at("배당수익률", cur)
    out["roe"] = value_at("ROA", cur)
    if out.get("revenue") and out.get("op_income") is not None:
        out["op_margin"] = out["op_income"] / out["revenue"] * 100
    prev = actual[-2].get("key") if len(actual) > 1 else None
    if prev:
        rev_prev, op_prev = value_at("매출액", prev, 1e6), value_at("EBIT", prev, 1e6)
        if out.get("revenue") and rev_prev:
            out["revenue_yoy"] = (out["revenue"] - rev_prev) / abs(rev_prev) * 100
        if out.get("op_income") is not None and op_prev:
            out["op_yoy"] = (out["op_income"] - op_prev) / abs(op_prev) * 100
    return out


@memoize(ttl=300)
def fetch_snapshot(code: str, sector: str | None = None, name_hint: str | None = None,
                   deep: bool = False) -> StockSnapshot:
    """한 종목의 시세·재무 요약. 네이버 → yfinance → 야후 차트 순으로 보완한다.

    deep=True 이면 yfinance까지 불러 영업현금흐름 등을 보강한다(개별 분석용).
    deep=False(기본)는 네이버만 써서 스캔이 빠르고 막히지 않게 한다.
    실패해도 예외를 던지지 않고 notes 에 사유를 남긴다(앱은 계속 동작).
    """
    code = clean_code(code)
    market = detect_market(code)
    snap = StockSnapshot(code=code, market=market)
    snap.name = name_hint or ""

    if market == "KR":
        quote = _naver_quote(code)
        snap.name = snap.name or quote.get("name", "")
        snap.price = quote.get("price")
        snap.change_pct = quote.get("change_pct")
        snap.as_of = quote.get("as_of", "")
        if quote:
            snap.sources.append("네이버 금융 시세")

        integ = _naver_integration(code)
        snap.name = snap.name or integ.get("name", "")
        for attr in ("per", "pbr", "eps", "bps", "market_cap", "high52", "low52",
                     "dividend_yield", "dps"):
            if getattr(snap, attr) is None and integ.get(attr) is not None:
                setattr(snap, attr, integ[attr])
        if not snap.dividend_year and integ.get("dividend_year"):
            snap.dividend_year = str(integ["dividend_year"])
        if integ:
            snap.sources.append("네이버 금융 투자지표")

        annual = _naver_annual(code)
        for attr in ("roe", "debt_ratio", "op_income", "op_margin", "revenue", "net_income",
                     "revenue_yoy", "op_yoy", "fiscal_year"):
            if annual.get(attr) is not None:
                setattr(snap, attr, annual[attr])
        if snap.per is None and annual.get("per_annual") is not None:
            snap.per = annual["per_annual"]
        if snap.pbr is None and annual.get("pbr_annual") is not None:
            snap.pbr = annual["pbr_annual"]
        if snap.dps is None and annual.get("dps") is not None:
            snap.dps = annual["dps"]
        if annual:
            snap.sources.append("네이버 금융 재무제표")

        suffix = "KQ" if "코스닥" in str(quote.get("exchange", "")) else "KS"
        if deep:
            info = _yfinance_info([f"{code}.{suffix}", f"{code}.{'KS' if suffix == 'KQ' else 'KQ'}"])
            if info:
                _from_yf(info, snap)
                snap.sources.append("yfinance 재무")
        if snap.price is None:
            fallback = _yahoo_quote(f"{code}.{suffix}")
            if fallback.get("price"):
                snap.price = fallback["price"]
                snap.change_pct = snap.change_pct if snap.change_pct is not None else fallback.get("change_pct")
                snap.sources.append("야후 차트 폴백")
        snap.currency = "KRW"
        if not snap.name:
            snap.name = code
    else:
        basic = _naver_us_basic(code)
        if basic:
            snap.name = snap.name or basic.get("name", "")
            snap.price = basic.get("price")
            snap.change_pct = basic.get("change_pct")
            snap.as_of = basic.get("as_of", "")
            for attr in ("per", "pbr", "eps", "bps", "high52", "low52", "dividend_yield",
                         "dps", "dividend_year", "market_cap", "shares"):
                if getattr(snap, attr) is None and basic.get(attr) is not None:
                    setattr(snap, attr, basic[attr])
            snap.sources.append("네이버 해외 종목")

        annual = _naver_us_annual(code)
        for attr in ("op_income", "op_margin", "revenue", "net_income", "revenue_yoy",
                     "op_yoy", "fiscal_year", "roe"):
            if getattr(snap, attr) is None and annual.get(attr) is not None:
                setattr(snap, attr, annual[attr])
        if snap.dividend_yield is None and annual.get("dividend_yield") is not None:
            snap.dividend_yield = annual["dividend_yield"]
        if annual:
            snap.sources.append("네이버 해외 재무")

        if deep:
            info = _yfinance_info([code])
            if info:
                _from_yf(info, snap)
                snap.sources.append("yfinance")
        if snap.price is None:
            fallback = _yahoo_quote(code)
            if fallback.get("price"):
                snap.price = fallback["price"]
                snap.change_pct = snap.change_pct if snap.change_pct is not None else fallback.get("change_pct")
                snap.name = snap.name or fallback.get("name", "") or code
                snap.high52 = snap.high52 or parse_num(fallback.get("high52"))
                snap.low52 = snap.low52 or parse_num(fallback.get("low52"))
                snap.sources.append("야후 차트")
        snap.currency = "USD"
        if not snap.name:
            snap.name = code

    snap.sector = _resolve_sector(code, snap.name, sector)
    if snap.price is None:
        snap.notes.append("현재가를 가져오지 못했습니다(네트워크·종목코드 확인).")
    for attr, label in (("per", "PER"), ("pbr", "PBR"), ("debt_ratio", "부채비율"),
                        ("roe", "ROE"), ("ocf", "영업현금흐름")):
        if getattr(snap, attr) is None:
            snap.notes.append(f"{label} 미확인")
    return snap


def fetch_snapshots(items: list[dict], workers: int = 8, deep: bool = False,
                    progress=None) -> list[StockSnapshot]:
    """여러 종목 병렬 조회. items: [{code, sector, name}]

    progress: 완료 종목 수를 받는 콜백(앱의 진행 표시줄용).
    """
    results: dict[str, StockSnapshot] = {}
    if not items:
        return []
    total = len(items)
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, min(workers, total))) as pool:
        futures = {
            pool.submit(fetch_snapshot, it["code"], it.get("sector"), it.get("name"), deep): it["code"]
            for it in items
        }
        for fut in as_completed(futures):
            code = futures[fut]
            done += 1
            try:
                results[code] = fut.result()
            except Exception as exc:  # 방어적: 한 종목 실패가 전체를 막지 않는다
                snap = StockSnapshot(code=clean_code(code))
                snap.notes.append(f"조회 실패: {exc}")
                results[code] = snap
            if progress:
                try:
                    progress(done, total)
                except Exception:
                    pass
    order = [clean_code(it["code"]) for it in items]
    return [results[c] for c in order if c in results]


# --------------------------------------------------------------- 뉴스 ----
def _parse_rss(xml_text: str, limit: int) -> list[dict]:
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for node in root.iter("item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        pub = (node.findtext("pubDate") or "").strip()
        source = ""
        src_node = node.find("source")
        if src_node is not None:
            source = (src_node.text or "").strip()
        if not title:
            continue
        suffix = f" - {source}" if source else ""
        if suffix and title.endswith(suffix):
            title = title[: -len(suffix)].strip()
        items.append({"title": title, "link": link, "source": source, "published": pub})
        if len(items) >= limit:
            break
    return items


@memoize(ttl=900)
def fetch_news(query: str, limit: int = 4) -> list[dict]:
    """구글 뉴스 RSS 검색. 실패하면 빈 목록(앱은 계속 동작)."""
    try:
        resp = requests.get(GOOGLE_NEWS,
                            params={"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"},
                            headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return []
        return _parse_rss(resp.text, limit)
    except Exception:
        return []


NEWS_TOPICS = [
    ("국내 증시", "코스피 외국인 수급"),
    ("공시·실적", "실적 공시 잠정실적 발표"),
    ("수주·계약", "수주 계약 체결 공급"),
    ("정책·규제", "정부 규제 정책 발표"),
    ("증권사 리포트", "증권사 리포트 목표주가"),
    ("반도체", "반도체 HBM 수출"),
    ("원전·에너지", "원전 수주 체코"),
    ("건설", "건설 수주 부동산"),
    ("바이오", "바이오 제약 임상"),
    ("글로벌 증시", "뉴욕증시 연준 금리"),
]

SECTOR_KEYWORDS = {
    "원전·에너지": ["원전", "체코", "한전", "전력", "변압기", "SMR", "에너지", "두산에너빌"],
    "건설": ["건설", "재건축", "분양", "토목", "부동산", "수주잔고"],
    "반도체": ["반도체", "HBM", "D램", "낸드", "파운드리", "삼성전자", "하이닉스"],
    "바이오": ["바이오", "제약", "임상", "FDA", "신약", "의료기기"],
    "금융": ["금리", "은행", "보험", "증권", "배당", "밸류업"],
    "제조·철강": ["철강", "자동차", "조선", "화학", "제조"],
}


@memoize(ttl=900)
def fetch_market_news(per_topic: int = 4) -> dict[str, list[dict]]:
    """국내외 주요 이슈를 주제별로 모은다."""
    out: dict[str, list[dict]] = {}
    for label, query in NEWS_TOPICS:
        out[label] = fetch_news(query, per_topic)
    return out


def sector_impact(news_map: dict[str, list[dict]]) -> dict[str, list[str]]:
    """헤드라인 키워드로 관심 섹터별 영향 포인트를 만든다(규칙 기반 요약)."""
    impacts: dict[str, list[str]] = {s: [] for s in SECTOR_ORDER}
    for topic, items in (news_map or {}).items():
        for item in items:
            title = item.get("title", "")
            for sector, words in SECTOR_KEYWORDS.items():
                if any(w in title for w in words):
                    if len(impacts[sector]) < 3:
                        impacts[sector].append(f"[{topic}] {title}")
    return {k: v for k, v in impacts.items() if v}


# ------------------------------------------------------- 가치함정 필터 ----
@dataclass
class FilterRules:
    max_per: float = 15.0
    max_pbr: float = 1.5
    max_debt: float = 200.0
    min_roe: float = 5.0
    require_op_profit: bool = True
    require_ocf: bool = True

    def summary(self) -> str:
        return (f"PER ≤ {self.max_per:g} · PBR ≤ {self.max_pbr:g} · 부채비율 ≤ {self.max_debt:g}% · "
                f"ROE ≥ {self.min_roe:g}% · 영업이익 흑자 · 영업현금흐름 양수")


def _check(name: str, value, ok: bool | None, text: str) -> dict:
    status = "확인불가" if ok is None else ("통과" if ok else "탈락")
    return {"name": name, "value": value, "status": status, "text": text}


def run_filter(snap: StockSnapshot, rules: FilterRules) -> dict:
    """가치 함정 필터 + 종합 가치점수(0~100)."""
    checks: list[dict] = []

    per = snap.per
    checks.append(_check(
        "저PER", per,
        None if per is None or per <= 0 else per <= rules.max_per,
        "PER 미확인" if per is None else (
            "적자(PER 없음)" if per <= 0 else f"PER {per:.2f}배 ≤ {rules.max_per:g}배"
            if per <= rules.max_per else f"PER {per:.2f}배 > {rules.max_per:g}배")))

    pbr = snap.pbr
    checks.append(_check(
        "저PBR", pbr,
        None if pbr is None else pbr <= rules.max_pbr,
        "PBR 미확인" if pbr is None else (f"PBR {pbr:.2f}배 ≤ {rules.max_pbr:g}배"
                                          if pbr <= rules.max_pbr else f"PBR {pbr:.2f}배 > {rules.max_pbr:g}배")))

    op = snap.op_income
    if rules.require_op_profit:
        checks.append(_check(
            "영업이익 흑자", op,
            None if op is None else op > 0,
            "영업이익 미확인" if op is None else (f"영업이익 {op:,.0f}" if op > 0 else f"영업이익 적자({op:,.0f})")))

    debt = snap.debt_ratio
    checks.append(_check(
        "부채비율", debt,
        None if debt is None else debt <= rules.max_debt,
        "부채비율 미확인" if debt is None else (
            f"부채비율 {debt:.1f}% ≤ {rules.max_debt:g}%" if debt <= rules.max_debt
            else f"부채비율 {debt:.1f}% > {rules.max_debt:g}%")))

    roe = snap.roe
    checks.append(_check(
        "ROE", roe,
        None if roe is None else roe >= rules.min_roe,
        "ROE 미확인" if roe is None else (
            f"ROE {roe:.1f}% ≥ {rules.min_roe:g}%" if roe >= rules.min_roe
            else f"ROE {roe:.1f}% < {rules.min_roe:g}%")))

    ocf = snap.ocf
    if rules.require_ocf:
        checks.append(_check(
            "영업현금흐름", ocf,
            None if ocf is None else ocf > 0,
            "영업현금흐름 미확인" if ocf is None else (
                "영업현금흐름 양수" if ocf > 0 else "영업현금흐름 마이너스(가치함정 위험)")))

    fails = [c for c in checks if c["status"] == "탈락"]
    unknowns = [c for c in checks if c["status"] == "확인불가"]
    score, parts = value_score(snap)
    passed = not fails
    if snap.price is None and all(v is None for v in (snap.per, snap.pbr, snap.roe, snap.op_income)):
        verdict, passed = "데이터 없음", False
    elif passed and len(unknowns) >= 2:
        verdict = "판정 보류"
    elif passed:
        verdict = "통과"
    else:
        verdict = "탈락"
    reasons = [c["text"] for c in fails] or [c["text"] for c in unknowns] or ["모든 기준 충족"]
    note = ""
    if snap.sector == "금융":
        note = ("금융업은 예수부채 때문에 부채비율·영업현금흐름이 제조업과 비교되지 않습니다. "
                "PBR·ROE·배당 중심으로 보세요.")
    elif snap.sector == "바이오" and (snap.per is None or (snap.per or 0) <= 0):
        note = "적자 바이오는 PER 기준이 맞지 않습니다. 현금 보유·파이프라인·PBR 위주로 판단하세요."
    return {
        "checks": checks,
        "passed": passed,
        "verdict": verdict,
        "fail_reasons": [c["text"] for c in fails],
        "unknowns": [c["text"] for c in unknowns],
        "score": score,
        "score_parts": parts,
        "reason": " · ".join(reasons),
        "note": note,
    }


def _scale(value: float | None, best: float, worst: float) -> float | None:
    """best→100, worst→0 선형 점수(best<worst 가정)."""
    if value is None:
        return None
    if best == worst:
        return 50.0
    ratio = (value - worst) / (best - worst)
    return max(0.0, min(100.0, ratio * 100.0))


def value_score(snap: StockSnapshot) -> tuple[int, dict[str, float | None]]:
    """종합 가치점수 0~100 — 저평가 35 · 수익성 25 · 안정성 25 · 성장 15."""
    per_score = _scale(snap.per, 5.0, 25.0) if (snap.per or 0) > 0 else None
    pbr_score = _scale(snap.pbr, 0.5, 3.0) if snap.pbr is not None else None
    cheap = [s for s in (per_score, pbr_score) if s is not None]

    roe_score = _scale(snap.roe, 15.0, 0.0)
    margin_score = _scale(snap.op_margin, 15.0, 0.0)
    profit = [s for s in (roe_score, margin_score) if s is not None]

    debt_score = _scale(snap.debt_ratio, 50.0, 300.0)
    ocf_raw = None if snap.ocf is None else (100.0 if snap.ocf > 0 else 0.0)
    stable = [s for s in (debt_score, ocf_raw) if s is not None]

    growth = []
    rev_g = _scale(snap.revenue_yoy, 20.0, -10.0)
    op_g = _scale(snap.op_yoy, 30.0, -20.0)
    growth = [s for s in (rev_g, op_g) if s is not None]

    def avg(values: list[float]) -> float:
        return sum(values) / len(values) if values else 50.0

    parts = {
        "저평가": round(avg(cheap), 1),
        "수익성": round(avg(profit), 1),
        "안정성": round(avg(stable), 1),
        "성장성": round(avg(growth), 1),
    }
    total = parts["저평가"] * 0.35 + parts["수익성"] * 0.25 + parts["안정성"] * 0.25 + parts["성장성"] * 0.15
    return int(round(total)), parts


# ------------------------------------------------------------- 시나리오 ----
TARGET_PER = 12.0      # 기본 적정 PER
UPSIDE_PER = 15.0      # 상승 시나리오 PER
BOOK_PBR = 1.0         # 장부가(PBR 1배) 기준
STOP_LOSS_PCT = 15.0    # 투자금 보존 원칙: 평단 -15%
MIN_UPSIDE = 20.0      # 상승 목표 최소 여력(+20%)
MAX_UPSIDE = 60.0      # 상승 목표 최대 여력(+60%)


def money(value: float | None, currency: str = "KRW", decimals: int = 0) -> str:
    """통화별 가격 표기."""
    if value is None:
        return "-"
    if currency == "USD":
        return f"${value:,.{max(decimals, 2)}f}"
    return f"{value:,.{decimals}f}원"


def fair_values(snap: StockSnapshot) -> dict[str, float | None]:
    """EPS·BPS 기반 적정가 밴드. 근거가 없으면 None."""
    eps = snap.eps if (snap.eps or 0) > 0 else None
    bps = snap.bps if (snap.bps or 0) > 0 else None
    base_candidates = []
    if bps:
        base_candidates.append(bps * BOOK_PBR)
    if eps:
        base_candidates.append(eps * TARGET_PER)
    base = sum(base_candidates) / len(base_candidates) if base_candidates else None
    aggressive = None
    if eps:
        aggressive = eps * UPSIDE_PER
    elif bps:
        aggressive = bps * 1.3
    return {"fair": base, "target": aggressive, "book": bps * BOOK_PBR if bps else None,
            "eps_based": eps * TARGET_PER if eps else None}


def build_scenarios(snap: StockSnapshot, avg_price: float | None = None,
                    valuation: "Valuation | None" = None) -> dict:
    """상승 / 기본 / 경계 3단 시나리오와 각 단계의 대응 기준을 만든다.

    - 상승: 목표가(현재가 +20%와 적정가 중 높은 값, 최대 +60%) 도달 → 분할 익절
    - 기본: 적정가 밴드(EPS×12·BPS×1) 내 → 보유하며 실적 확인
    - 경계: 평단 -15% 이탈 · 영업이익 적자 전환 · 부채비율 200% 초과 → 비중 축소
    """
    price = snap.price
    cur = snap.currency or "KRW"
    band = fair_values(snap)
    fair = band["fair"]
    fair_basis = "적정가 = (BPS×1배 + EPS×12배) 평균"
    if valuation is not None and getattr(valuation, "fair", None):
        fair = valuation.fair
        if getattr(valuation, "excluded", None):
            fair_basis = (f"적정가 = {valuation.weight_summary()} 가중"
                          f" (제외: {', '.join(e['name'].replace(' 방식', '') for e in valuation.excluded)})")
        else:
            fair_basis = "적정가 = PER·PBR·DCF 3방식 가중 평균"
        if getattr(valuation, "profile", ""):
            fair_basis += f" · {valuation.profile} 유형"
    anchor_max = band["target"]

    def pct(base, value):
        if not base or value is None:
            return None
        return (value / base - 1) * 100

    # 상승 목표: 현재가 +20% 를 기본으로, 적정가가 더 높으면 적정가, 최대 +60%.
    target = None
    target_basis = ""
    if price:
        floor_target = price * (1 + MIN_UPSIDE / 100)
        candidates = [floor_target]
        if fair and fair > price:
            candidates.append(fair)
            target_basis = f"현재가 +{MIN_UPSIDE:.0f}% / 적정가 {money(fair, cur)} 중 높은 값"
        else:
            target_basis = f"현재가 +{MIN_UPSIDE:.0f}%(적정가가 현재가보다 낮아 밸류에이션 부담)"
        target = max(candidates)
        target = min(target, price * (1 + MAX_UPSIDE / 100))
    elif anchor_max:
        target = anchor_max
        target_basis = "EPS·BPS 기반 목표가(EPS×15 또는 BPS×1.3)"

    stop = None
    if avg_price:
        stop = avg_price * (1 - STOP_LOSS_PCT / 100)
    elif price:
        stop = price * 0.88

    premium = round(pct(fair, price), 1) if (fair and price) else None

    upside = []
    if target:
        upside.append(f"목표가 {money(target, cur)} 도달 시: 보유분의 30~50% 분할 익절")
    upside.append("PBR 2.5배 초과 또는 목표가 +20% 초과 시: 잔량 정리 검토(과열 구간)")
    if snap.high52:
        upside.append(f"52주 최고 {money(snap.high52, cur)} 돌파 후 거래량 없이 못 넘으면: 일부 차익 실현")
    upside.append("영업이익·영업현금흐름이 2분기 연속 개선되면: 목표가를 실적에 맞춰 상향")
    if premium is not None and premium >= 50:
        upside.append(f"적정가 대비 {premium:.0f}% 고평가 구간: 목표 도달 시 미련 없이 익절")

    base = ["현재가가 적정가 밴드 근처면: 보유 유지, 다음 분기 실적에서 영업이익·현금흐름 확인"]
    if premium is not None:
        if premium <= -20:
            base.append(f"적정가 대비 {abs(premium):.0f}% 저평가: 여유 자금 범위에서 분할 매수 검토")
        elif premium >= 20:
            base.append(f"적정가 대비 {premium:.0f}% 고평가: 신규 매수는 보류, 보유만 유지")
    if snap.per and snap.per > 30:
        base.append(f"PER {snap.per:.0f}배로 이익 대비 부담: 실적이 따라오는지 분기마다 점검")
    base.append("배당·자사주 등 주주환원 정책 변화는 보유 판단의 보조 지표로만 활용")
    base.append("분기 실적 발표 전후 급등락은 추격하지 않고, 발표 후 숫자로 판단")

    warn = []
    if stop:
        label = f"평단 -{STOP_LOSS_PCT:.0f}%인 " if avg_price else ""
        warn.append(f"{label}{money(stop, cur)} 이탈 시: 손실 확대 전 비중 축소")
    warn.append("영업이익이 적자로 전환하거나 2분기 연속 감소하면: 보유 근거 재점검")
    warn.append("부채비율 200% 초과 또는 영업현금흐름 마이너스 전환 시: 가치함정 의심, 축소")
    if snap.debt_ratio and snap.debt_ratio > 150:
        warn.append(f"현재 부채비율 {snap.debt_ratio:.0f}%로 높음: 금리 상승 시 이자 부담 확대 주의")
    if snap.op_income is not None and snap.op_income <= 0:
        warn.append(f"현재 영업이익 적자({snap.op_income:,.0f}): 이미 경계 상태, 보유 이유를 다시 확인")

    # v3: 판단근거·매수/목표/손절 가격·투자논리 훼손 조건까지 한 번에 만든다.
    margin = safety_margin(fair, price)
    health = financial_health(snap)
    judgment = investment_judgment(snap, valuation=valuation, fair=fair)
    first_entry = add_entry = None
    if fair:
        first_entry = fair * 0.90
        if price is not None and price < first_entry:
            first_entry = price
        add_entry = first_entry * 0.92
    elif price:
        first_entry = price * 0.95
        add_entry = first_entry * 0.92

    basis_lines: list[str] = []
    if fair:
        basis_lines.append(f"적정가 {money(fair, cur)} — {fair_basis}")
    if margin is not None:
        basis_lines.append(f"{judgment['badge']} — 현재가 {money(price, cur)} vs 적정가, 안전마진 {margin:+.1f}%")
    basis_lines.append(f"재무상태 {health['badge']}"
                       + (f" — {health['reasons'][0]}" if health.get("reasons") else ""))
    if first_entry:
        gap = pct(price, first_entry)
        basis_lines.append(
            f"1차 매수 기준 {money(first_entry, cur)}" + (f" (현재가 대비 {gap:+.1f}%)" if gap is not None else ""))

    invalidation = invalidation_checks(snap)
    invalidation_lines = [f"{item['label']} — {item['detail']}" for item in invalidation]

    return {
        "currency": cur,
        "상승": {
            "title": "🟢 상승 시나리오",
            "price": target,
            "range_text": f"목표 {money(target, cur)}" if target else "목표가 산출 불가",
            "basis": target_basis,
            "expected": pct(price, target),
            "triggers": upside,
            "stance": "목표 도달 시 분할 익절 · 추격 매수 금지",
        },
        "기본": {
            "title": "🟡 기본 시나리오",
            "price": fair,
            "range_text": f"적정가 {money(fair, cur)}" if fair else "적정가 산출 불가(EPS·BPS 미확인)",
            "basis": fair_basis if fair else "EPS·BPS·현금흐름 확인 불가",
            "expected": pct(price, fair),
            "triggers": base,
            "stance": "보유 유지 · 분기 실적·현금흐름 확인",
        },
        "경계": {
            "title": "🔴 경계 시나리오",
            "price": stop,
            "range_text": (f"경계선 {money(stop, cur)} (평단 -{STOP_LOSS_PCT:.0f}%)" if avg_price and stop
                           else (f"경계선 {money(stop, cur)}" if stop else "경계선 산출 불가")),
            "basis": "투자금 보존 원칙: 평단 대비 -15%" if avg_price else "현재가 대비 -12% 참고선",
            "expected": pct(price, stop),
            "triggers": warn,
            "stance": "원칙 이탈 시 비중 축소 · 근거 재점검",
        },
        "fair": fair,
        "target": target,
        "stop": stop,
        "premium_pct": premium,
        # --- v3 ---
        "safety_margin": margin,
        "judgement_basis": basis_lines,
        "entry": {"first": first_entry, "add": add_entry},
        "buy1": first_entry,
        "buy2": add_entry,
        "hold_price": fair,
        "invalidations": invalidation_lines,
        "invalidation_checks": invalidation,
        "health": health,
        "judgment": judgment,
    }


def health_comment(snap: StockSnapshot) -> list[tuple[str, str]]:
    """재무 건전성 요약 (지표, 해석)."""
    out: list[tuple[str, str]] = []

    def grade(value, good, bad, higher_better=False, unit="%"):
        if value is None:
            return "확인 불가", "⚪"
        if higher_better:
            if value >= good:
                return f"{value:,.1f}{unit} — 우수", "🟢"
            if value >= bad:
                return f"{value:,.1f}{unit} — 보통", "🟡"
            return f"{value:,.1f}{unit} — 부진", "🔴"
        if value <= good:
            return f"{value:,.1f}{unit} — 양호", "🟢"
        if value <= bad:
            return f"{value:,.1f}{unit} — 주의", "🟡"
        return f"{value:,.1f}{unit} — 위험", "🔴"

    out.append(("ROE", *grade(snap.roe, 10, 3, higher_better=True)))
    out.append(("부채비율", *grade(snap.debt_ratio, 100, 200)))
    out.append(("영업이익률", *grade(snap.op_margin, 10, 0, higher_better=True)))
    if snap.ocf is None:
        out.append(("영업현금흐름", "확인 불가", "⚪"))
    else:
        out.append(("영업현금흐름", "양수 — 이익의 현금 전환 양호" if snap.ocf > 0 else "마이너스 — 이익 질 의심", "🟢" if snap.ocf > 0 else "🔴"))
    def ratio_grade(value, good: float, mid: float):
        """PER·PBR 처럼 '배' 단위로 표시하는 지표."""
        if value is None or value <= 0:
            return "적자 또는 미확인", "⚪"
        if value <= good:
            return f"{value:.2f}배 — 양호", "🟢"
        if value <= mid:
            return f"{value:.2f}배 — 보통", "🟡"
        return f"{value:.2f}배 — 부담", "🔴"

    out.append(("PER", *ratio_grade(snap.per, 10, 20)))
    out.append(("PBR", *ratio_grade(snap.pbr, 1.0, 2.0)))
    return out


# --------------------------------------------------------------- AI ----
def ai_config() -> tuple[str, str]:
    """(provider, key). 설정된 AI 키가 없으면 ('', '')."""
    prefer = os.environ.get("AI_PROVIDER", "").strip().lower()
    options = [
        ("openai", "OPENAI_API_KEY"),
        ("gemini", "GEMINI_API_KEY"),
        ("gemini", "GOOGLE_API_KEY"),
        ("openrouter", "OPENROUTER_API_KEY"),
    ]
    if prefer:
        options.sort(key=lambda o: 0 if o[0] == prefer else 1)
    for provider, env in options:
        key = os.environ.get(env, "").strip()
        if key:
            return provider, key
    return "", ""


def ai_available() -> bool:
    return bool(ai_config()[0])


def build_ai_prompt(snap: StockSnapshot, scenarios: dict, filt: dict) -> str:
    """AI 심층 리포트용 프롬프트(정량 분석 결과를 근거로 전달)."""
    cur = snap.currency or "KRW"

    def m(value):
        return money(value, cur)

    def g(value, unit=""):
        return "미확인" if value is None else f"{value}{unit}"

    facts = [
        f"종목: {snap.name}({snap.code}) / 시장: {snap.market} / 섹터: {snap.sector}",
        f"현재가: {m(snap.price)} · PER {g(snap.per)} · PBR {g(snap.pbr)}",
        f"ROE {g(snap.roe, '%')} · 부채비율 {g(snap.debt_ratio, '%')} · 영업이익률 {g(snap.op_margin, '%')}",
        f"영업이익 {g(snap.op_income)} · 영업현금흐름 {g(snap.ocf)}",
        f"매출증가율 {g(snap.revenue_yoy, '%')} · 영업이익증가율 {g(snap.op_yoy, '%')}",
        f"적정가 {m(scenarios.get('fair'))} · 상승 목표 {m(scenarios.get('target'))} · 경계선 {m(scenarios.get('stop'))}",
        f"가치점수 {filt.get('score')}점 (판정 {filt.get('verdict')}) — 사유: {filt.get('reason')}",
    ]
    return (
        "당신은 '저가 우량주를 찾고, 투자금을 잃지 않는 것'을 최우선으로 하는 한국 가치투자 보조 AI입니다. "
        "아래 정량 데이터를 근거로 6~8문장의 한국어 리포트를 작성하세요. "
        "구성: (1) 사업 이해 (2) 밸류에이션 해석 (3) 재무 건전성 (4) 가치함정 위험 "
        "(5) 상승/기본/하락 대응 기준 요약 (6) 지금 확인할 것. "
        "데이터에 없는 사실은 단정하지 말고 '확인 필요'라고 쓰세요. 매수·매도 단정 대신 판단 근거를 제시하세요.\n\n"
        + "\n".join(facts)
    )


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    resp = requests.post(url, json=payload, headers=headers, timeout=(5.0, 60.0))
    resp.raise_for_status()
    return resp.json()


def generate_ai_report(snap: StockSnapshot, scenarios: dict, filt: dict,
                       provider: str | None = None) -> tuple[str | None, str]:
    """AI 심층 리포트. (본문, 오류메시지) — 키가 없거나 실패하면 본문 None."""
    conf_provider, key = ai_config()
    provider = (provider or conf_provider or "").lower()
    if not provider or not key:
        return None, "AI 키가 설정되지 않았습니다."
    prompt = build_ai_prompt(snap, scenarios, filt)
    try:
        if provider == "openai":
            data = _post_json("https://api.openai.com/v1/chat/completions",
                              {"model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                               "messages": [{"role": "user", "content": prompt}],
                               "temperature": 0.3},
                              {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            return data["choices"][0]["message"]["content"].strip(), ""
        if provider == "gemini":
            model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
            data = _post_json(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                {"contents": [{"parts": [{"text": prompt}]}]},
                {"Content-Type": "application/json"})
            return data["candidates"][0]["content"]["parts"][0]["text"].strip(), ""
        if provider == "openrouter":
            data = _post_json("https://openrouter.ai/api/v1/chat/completions",
                              {"model": os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
                               "messages": [{"role": "user", "content": prompt}]},
                              {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            return data["choices"][0]["message"]["content"].strip(), ""
        return None, f"지원하지 않는 AI 제공자: {provider}"
    except Exception as exc:
        return None, f"AI 리포트 생성 실패({exc})"


# ------------------------------------------------------------- 저장소 ----
HOLDINGS_COLUMNS = ["종목코드", "종목명", "수량", "평단가", "메모"]
PICKS_COLUMNS = ["등록일", "종목코드", "종목명", "등록가", "선정근거", "목표가", "경계가", "상태메모"]

SHEET_KEYS = {"holdings": "보유종목", "picks": "추천추적"}
LOCAL_FILES = {"holdings": "holdings.json", "picks": "picks.json"}

HOLDINGS_CACHE_TTL = 20.0
_STORE_CACHE: dict[str, tuple[float, list[dict]]] = {}


class StorageError(RuntimeError):
    """저장소 접근 실패(앱은 이 예외를 잡아 안내만 띄운다)."""


def apply_secrets(secrets) -> None:
    """Streamlit Secrets → 환경변수(이미 있으면 그대로). 코어는 env 만 읽는다."""
    keys = ("GSHEET_WEBAPP_URL", "GSHEET_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY",
            "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "AI_PROVIDER", "VI_DATA_DIR")
    for key in keys:
        if os.environ.get(key):
            continue
        try:
            value = secrets.get(key)
        except Exception:
            value = None
        if value:
            os.environ[key] = str(value).strip()


def gsheet_config() -> tuple[str, str]:
    return (os.environ.get("GSHEET_WEBAPP_URL", "").strip(),
            os.environ.get("GSHEET_TOKEN", "").strip())


def storage_mode() -> str:
    return "gsheet" if gsheet_config()[0] else "local"


def storage_label() -> str:
    return "구글 시트 연동 중" if storage_mode() == "gsheet" else "로컬 파일"


def storage_target() -> str:
    return "'가치투자 포트폴리오 & 추천추적' 시트" if storage_mode() == "gsheet" else str(DATA_DIR)


def _local_path(kind: str) -> Path:
    return Path(os.environ.get("VI_DATA_DIR") or (BASE_DIR / "data")) / LOCAL_FILES[kind]


def _gsheet_call(method: str, kind: str, records: list[dict] | None = None) -> dict:
    url, token = gsheet_config()
    if not url:
        raise StorageError("GSHEET_WEBAPP_URL 이 설정되지 않았습니다.")
    try:
        if method.upper() == "GET":
            resp = requests.get(url, params={"token": token, "sheet": kind},
                                headers=HEADERS, timeout=(4.0, 25.0), allow_redirects=True)
        else:
            resp = requests.post(url, json={"token": token, "sheet": kind,
                                            "records": records or []},
                                 headers=HEADERS, timeout=(4.0, 45.0), allow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise StorageError(f"구글 시트 연결 실패({exc})") from exc
    if not isinstance(data, dict) or not data.get("ok"):
        reason = data.get("error") if isinstance(data, dict) else "응답 형식 오류"
        raise StorageError(f"구글 시트 응답 오류({reason})")
    return data


def normalize_code(code: str) -> str:
    """시트가 숫자로 읽어 앞자리 0을 잃은 한국 종목코드를 6자리로 되돌린다."""
    s = clean_code(code)
    if s.isdigit() and len(s) < 6:
        return s.zfill(6)
    return s


def _normalize_holding(row: dict) -> dict | None:
    code = str(row.get("종목코드") or row.get("code") or "").strip()
    if not code:
        return None
    qty = parse_num(row.get("수량") if "수량" in row else row.get("qty"))
    avg = parse_num(row.get("평단가") if "평단가" in row else row.get("avg_price"))
    return {
        "code": normalize_code(code),
        "name": str(row.get("종목명") or row.get("name") or "").strip(),
        "qty": qty or 0.0,
        "avg_price": avg or 0.0,
        "memo": str(row.get("메모") or row.get("memo") or "").strip(),
    }


def _normalize_pick(row: dict) -> dict | None:
    code = str(row.get("종목코드") or row.get("code") or "").strip()
    if not code:
        return None
    return {
        "date": str(row.get("등록일") or row.get("date") or "").strip() or today_str(),
        "code": normalize_code(code),
        "name": str(row.get("종목명") or row.get("name") or "").strip(),
        "entry_price": parse_num(row.get("등록가") if "등록가" in row else row.get("entry_price")),
        "rationale": str(row.get("선정근거") or row.get("rationale") or "").strip(),
        "target": parse_num(row.get("목표가") if "목표가" in row else row.get("target")),
        "stop": parse_num(row.get("경계가") if "경계가" in row else row.get("stop")),
        "memo": str(row.get("상태메모") or row.get("memo") or "").strip(),
    }


def load_rows(kind: str, force: bool = False) -> list[dict]:
    """저장된 행 목록. kind: holdings | picks."""
    cached = _STORE_CACHE.get(kind)
    if cached and not force and time.time() - cached[0] < HOLDINGS_CACHE_TTL:
        return [dict(r) for r in cached[1]]
    if storage_mode() == "gsheet":
        data = _gsheet_call("GET", kind)
        rows = data.get("records") or []
        normalizer = _normalize_holding if kind == "holdings" else _normalize_pick
        out = [r for r in (normalizer(x) for x in rows if isinstance(x, dict)) if r]
    else:
        path = _local_path(kind)
        rows: list = []
        if path.exists():
            try:
                rows = json.loads(path.read_text(encoding="utf-8")) or []
            except Exception as exc:
                raise StorageError(f"로컬 파일을 읽지 못했습니다({exc})") from exc
        normalizer = _normalize_holding if kind == "holdings" else _normalize_pick
        out = [r for r in (normalizer(x) for x in rows if isinstance(x, dict)) if r]
    _STORE_CACHE[kind] = (time.time(), [dict(r) for r in out])
    return out


def load_holdings(force: bool = False) -> list[dict]:
    return load_rows("holdings", force)


def load_picks(force: bool = False) -> list[dict]:
    return load_rows("picks", force)


def _sheet_record(kind: str, row: dict) -> dict:
    if kind == "holdings":
        return {"종목코드": row["code"], "종목명": row.get("name", ""),
                "수량": row.get("qty"), "평단가": row.get("avg_price"), "메모": row.get("memo", "")}
    return {"등록일": row.get("date", ""), "종목코드": row["code"], "종목명": row.get("name", ""),
            "등록가": row.get("entry_price"), "선정근거": row.get("rationale", ""),
            "목표가": row.get("target"), "경계가": row.get("stop"), "상태메모": row.get("memo", "")}


def save_rows(kind: str, rows: list[dict]) -> list[dict]:
    """전체 목록을 저장한다(시트 모드는 덮어쓰기, 로컬은 JSON)."""
    if storage_mode() == "gsheet":
        _gsheet_call("POST", kind, [_sheet_record(kind, r) for r in rows])
    else:
        path = _local_path(kind)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            raise StorageError(f"로컬 파일에 저장하지 못했습니다({exc})") from exc
    _STORE_CACHE[kind] = (time.time(), [dict(r) for r in rows])
    return rows


def save_holdings(rows: list[dict]) -> list[dict]:
    return save_rows("holdings", rows)


def save_picks(rows: list[dict]) -> list[dict]:
    return save_rows("picks", rows)


def clear_cache() -> None:
    _STORE_CACHE.clear()


# ------------------------------------------------------------- 유니버스 ----
UNIVERSE_FILE = BASE_DIR / "data" / "universe.json"

# universe.json 을 읽지 못할 때 쓰는 최소 사전(앱이 죽지 않도록).
_FALLBACK_UNIVERSE = [
    ("034020", "두산에너빌리티", "원전·에너지", "KOSPI"),
    ("052690", "한전기술", "원전·에너지", "KOSPI"),
    ("051600", "한전KPS", "원전·에너지", "KOSPI"),
    ("267260", "HD현대일렉트릭", "원전·에너지", "KOSPI"),
    ("010120", "LS ELECTRIC", "원전·에너지", "KOSPI"),
    ("006910", "보성파워텍", "원전·에너지", "KOSDAQ"),
    ("000720", "현대건설", "건설", "KOSPI"),
    ("047040", "대우건설", "건설", "KOSPI"),
    ("006360", "GS건설", "건설", "KOSPI"),
    ("028050", "삼성E&A", "건설", "KOSPI"),
    ("005930", "삼성전자", "반도체", "KOSPI"),
    ("000660", "SK하이닉스", "반도체", "KOSPI"),
    ("042700", "한미반도체", "반도체", "KOSPI"),
    ("240810", "원익IPS", "반도체", "KOSDAQ"),
    ("067310", "하나마이크론", "반도체", "KOSDAQ"),
    ("183300", "코미코", "반도체", "KOSDAQ"),
    ("403870", "HPSP", "반도체", "KOSDAQ"),
    ("009150", "삼성전기", "반도체", "KOSPI"),
    ("207940", "삼성바이오로직스", "바이오", "KOSPI"),
    ("000100", "유한양행", "바이오", "KOSPI"),
    ("069620", "대웅제약", "바이오", "KOSPI"),
    ("302440", "SK바이오사이언스", "바이오", "KOSPI"),
    ("214150", "클래시스", "바이오", "KOSDAQ"),
    ("004310", "현대약품", "바이오", "KOSDAQ"),
    ("105560", "KB금융", "금융", "KOSPI"),
    ("055550", "신한지주", "금융", "KOSPI"),
    ("316140", "우리금융지주", "금융", "KOSPI"),
    ("138040", "메리츠금융지주", "금융", "KOSPI"),
    ("005830", "DB손해보험", "금융", "KOSPI"),
    ("005490", "POSCO홀딩스", "제조·철강", "KOSPI"),
    ("004020", "현대제철", "제조·철강", "KOSPI"),
    ("005380", "현대차", "자동차·조선", "KOSPI"),
    ("000270", "기아", "자동차·조선", "KOSPI"),
    ("NVDA", "엔비디아", "반도체", "US"),
    ("AMD", "AMD", "반도체", "US"),
    ("MU", "마이크론", "반도체", "US"),
    ("INTC", "인텔", "반도체", "US"),
    ("AVGO", "브로드컴", "반도체", "US"),
    ("CEG", "컨스텔레이션에너지", "원전·에너지", "US"),
    ("VST", "비스트라", "원전·에너지", "US"),
    ("CCJ", "카메코", "원전·에너지", "US"),
    ("PFE", "화이자", "바이오", "US"),
    ("JNJ", "존슨앤존슨", "바이오", "US"),
    ("MRK", "머크", "바이오", "US"),
    ("AMGN", "암젠", "바이오", "US"),
    ("CAT", "캐터필러", "건설", "US"),
    ("VMC", "벌컨머티리얼즈", "건설", "US"),
    ("JPM", "JP모건", "금융", "US"),
    ("BAC", "뱅크오브아메리카", "금융", "US"),
    ("MET", "메트라이프", "금융", "US"),
    ("NUE", "뉴코", "제조·철강", "US"),
    ("X", "US스틸", "제조·철강", "US"),
    ("F", "포드", "자동차·조선", "US"),
]


def load_universe(path: Path | None = None) -> list[dict]:
    """data/universe.json → [{code,name,sector,market}] (시가총액 순서 유지)."""
    target = path or UNIVERSE_FILE
    try:
        payload = json.loads(Path(target).read_text(encoding="utf-8"))
        rows = payload.get("stocks") if isinstance(payload, dict) else payload
        out: list[dict] = []
        seen: set[str] = set()
        for row in rows or []:
            code = clean_code(str(row.get("code") or ""))
            if not code or code in seen:
                continue
            seen.add(code)
            out.append({
                "code": code,
                "name": str(row.get("name") or "").strip(),
                "sector": str(row.get("sector") or "기타"),
                "market": str(row.get("market") or ("US" if len(code) != 6 else "KOSPI")),
            })
        if out:
            return out
    except Exception:
        pass
    return [{"code": c, "name": n, "sector": s, "market": m} for c, n, s, m in _FALLBACK_UNIVERSE]


UNIVERSE: list[dict] = load_universe()

DEFAULT_UNIVERSE_CODES = [
    "034020", "052690", "051600", "010120",      # 원전
    "000720", "047040", "028050",                # 건설
    "005930", "042700", "183300",                # 반도체
    "207940", "000100", "214150",                # 바이오
    "105560", "055550",                          # 금융
    "005490", "004020",                          # 제조
    "CEG", "JPM", "PFE",                         # 미국 대표
]


def universe_lookup() -> dict[str, dict]:
    return {item["code"]: item for item in UNIVERSE}


def universe_size() -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in UNIVERSE:
        key = item.get("market") or "기타"
        counts[key] = counts.get(key, 0) + 1
    return counts


def universe_sector_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in UNIVERSE:
        counts[item["sector"]] = counts.get(item["sector"], 0) + 1
    return counts


def _take_per_market(items: list[dict], limit: int) -> list[dict]:
    """시장별로 비례해 앞에서부터 limit 개를 고른다(빠른 스캔용)."""
    if limit is None or limit >= len(items):
        return items
    order: list[str] = []
    buckets: dict[str, list[dict]] = {}
    for item in items:
        m = item.get("market") or "기타"
        if m not in buckets:
            buckets[m] = []
            order.append(m)
        buckets[m].append(item)
    total = len(items)
    picked: list[dict] = []
    for m in order:
        quota = max(1, round(limit * len(buckets[m]) / total))
        picked.extend(buckets[m][:quota])
    return picked[:limit]


def universe_items(codes: list[str] | None = None, sectors: list[str] | None = None,
                   markets: list[str] | None = None, limit: int | None = None) -> list[dict]:
    """코드·섹터·시장으로 조회 대상 종목을 고른다.

    markets: ['코스피','코스닥','미국'] 같은 화면 라벨(또는 KOSPI/KOSDAQ/US).
    아무 조건도 없으면 기존 기본 세트(=DEFAULT_UNIVERSE_CODES)를 돌려준다.
    """
    by_code = universe_lookup()
    if codes:
        out = []
        for code in codes:
            c = clean_code(code)
            item = by_code.get(c) or {"code": c, "name": "", "sector": "기타",
                                      "market": "US" if len(c) != 6 else "KOSPI"}
            out.append(dict(item))
        return out
    items = [dict(i) for i in UNIVERSE]
    if sectors:
        items = [i for i in items if i["sector"] in sectors]
    if markets:
        boards = {MARKET_BOARD.get(m, m) for m in markets}
        items = [i for i in items if i.get("market") in boards]
    if not sectors and not markets:
        want = set(DEFAULT_UNIVERSE_CODES)
        items = [i for i in items if i["code"] in want]
    return _take_per_market(items, limit) if limit else items


def scan_universe(items: list[dict], rules: FilterRules, workers: int = 10,
                  progress=None, deep: bool = False) -> list[dict]:
    """유니버스 스캔 → 필터 결과가 붙은 행 목록(통과·점수 내림차순)."""
    snaps = fetch_snapshots(items, workers=workers, deep=deep, progress=progress)
    rows = []
    for snap in snaps:
        verdict = run_filter(snap, rules)
        rows.append({"snap": snap, "filter": verdict})
    rows.sort(key=lambda r: (-(r["filter"]["passed"]), -r["filter"]["score"], r["snap"].name))
    return rows


# ------------------------------------------------------- 종목명 자동 검색 ----
_ETF_LIKE = re.compile(r"(스팩|SPAC|리츠|KODEX|TIGER|ARIRANG|KBSTAR|HANARO|\bSOL\b|\bACE\b|ETN|레버리지|인버스)")


@memoize(ttl=86400)
def _naver_ac(query: str) -> list[dict]:
    """네이버 자동완성(ac.stock.naver.com) — 종목명·티커·종목코드 모두 검색된다."""
    data = _get_json(NAVER_AC, {"q": query, "target": "stock"})
    out: list[dict] = []
    for item in (data or {}).get("items") or []:
        code = clean_code(str(item.get("code") or ""))
        name = str(item.get("name") or "").strip()
        if not code or not name or _ETF_LIKE.search(name):
            continue
        usa = str(item.get("nationCode") or "").upper() == "USA"
        board = str(item.get("typeCode") or "")
        if not usa and not re.fullmatch(r"\d{6}", code):
            continue
        out.append({"code": code, "name": name, "market": "US" if usa else "KR",
                    "board": board, "reuters": str(item.get("reutersCode") or ""),
                    "sector": ""})
    return out


def search_stocks(query: str, limit: int = 8) -> list[dict]:
    """종목명·종목코드·티커로 후보를 찾는다. 정확한 이름 → 앞부분 일치 → 포함 순.

    로컬 사전(data/universe.json)과 네이버 자동완성을 함께 쓴다.
    반환: [{code, name, market(KR|US), board, sector, reuters}]
    """
    q = str(query or "").strip()
    if not q:
        return []
    qn = q.replace(" ", "").lower()
    by_code = universe_lookup()
    scored: dict[str, tuple[float, dict]] = {}

    def add(item: dict, rank: float) -> None:
        code = clean_code(item.get("code") or "")
        if not code:
            return
        base = by_code.get(code) or {}
        merged = {
            "code": code,
            "name": item.get("name") or base.get("name", ""),
            "market": item.get("market") or ("US" if len(code) != 6 else "KR"),
            "board": item.get("board") or base.get("market", ""),
            "sector": item.get("sector") or base.get("sector", ""),
            "reuters": item.get("reuters", ""),
        }
        if code not in scored or rank < scored[code][0]:
            scored[code] = (rank, merged)

    if re.fullmatch(r"\d{6}(\.[A-Za-z]{2})?", q):
        add({"code": q, "market": "KR"}, 0.0)
    if re.fullmatch(r"[A-Za-z][A-Za-z.\-]{0,7}", q) and q.upper() in by_code:
        add({"code": q.upper(), "market": "US"}, 0.0)

    for item in UNIVERSE:
        nm = item["name"].replace(" ", "").lower()
        if nm == qn:
            add(item, 0.0)
        elif nm.startswith(qn):
            add(item, 1.0)
        elif qn in nm:
            add(item, 3.0)
        elif len(qn) >= 2 and item["code"].lower().startswith(qn):
            add(item, 2.0)

    for item in _naver_ac(q):
        nm = item["name"].replace(" ", "").lower()
        rank = 0.0 if nm == qn else (1.5 if nm.startswith(qn) else 3.5)
        add(item, rank)

    if re.fullmatch(r"[A-Za-z][A-Za-z.\-]{0,7}", q) and not scored:
        add({"code": q.upper(), "market": "US"}, 6.0)

    ordered = sorted(scored.values(), key=lambda pair: (pair[0], len(pair[1]["name"])))
    return [item for _, item in ordered][:limit]


def search_sector(code: str) -> str:
    item = universe_lookup().get(clean_code(code))
    return item["sector"] if item else ""


# ------------------------------------------------------------ 가격 흐름 ----
@dataclass
class PriceHistory:
    """일별 종가 + 20일·60일 이동평균 + 52주 레인지."""

    code: str = ""
    currency: str = "KRW"
    dates: list[str] = field(default_factory=list)        # YYYY-MM-DD
    closes: list[float] = field(default_factory=list)
    volumes: list[float] = field(default_factory=list)
    ma20: list[float | None] = field(default_factory=list)
    ma60: list[float | None] = field(default_factory=list)
    source: str = ""
    note: str = ""

    def window(self, months: int = 12) -> "PriceHistory":
        """최근 N개월만 남긴 사본(이동평균 값은 계산된 그대로 유지)."""
        if not self.dates:
            return self
        cutoff = (now_kst().date() - timedelta(days=int(months * 30.5))).isoformat()
        idx = next((i for i, d in enumerate(self.dates) if d >= cutoff), 0)
        return PriceHistory(
            code=self.code, currency=self.currency,
            dates=self.dates[idx:], closes=self.closes[idx:], volumes=self.volumes[idx:],
            ma20=self.ma20[idx:], ma60=self.ma60[idx:],
            source=self.source, note=self.note,
        )

    @property
    def price(self) -> float | None:
        return self.closes[-1] if self.closes else None

    def stats(self) -> dict:
        """52주 최고/최저·현재 위치·1·3·6개월/1년 수익률."""
        if not self.closes:
            return {}
        n = min(len(self.closes), 250)
        recent = self.closes[-n:]
        high, low = max(recent), min(recent)
        last = self.closes[-1]
        pos = None
        if high > low:
            pos = (last - low) / (high - low) * 100

        def ret(days: int):
            if len(self.closes) <= days:
                return None
            base = self.closes[-days - 1]
            return (last / base - 1) * 100 if base else None

        return {
            "last": last,
            "high52": high,
            "low52": low,
            "position_pct": pos,
            "return_1m": ret(21),
            "return_3m": ret(63),
            "return_6m": ret(126),
            "return_1y": ret(min(252, len(self.closes) - 1)),
            "ma20": self.ma20[-1] if self.ma20 else None,
            "ma60": self.ma60[-1] if self.ma60 else None,
        }

    def to_rows(self) -> list[dict]:
        """차트·표용 행 목록."""
        return [
            {"날짜": d, "종가": c, "20일선": self.ma20[i] if i < len(self.ma20) else None,
             "60일선": self.ma60[i] if i < len(self.ma60) else None,
             "거래량": self.volumes[i] if i < len(self.volumes) else None}
            for i, (d, c) in enumerate(zip(self.dates, self.closes))
        ]


def _sma(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    acc = 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= window:
            acc -= values[i - window]
        out.append(acc / window if i + 1 >= window else None)
    return out


def _yahoo_history(symbol: str, months: int) -> PriceHistory:
    data = _get_json(YAHOO_CHART.format(symbol=symbol),
                     {"range": "2y" if months > 12 else "1y", "interval": "1d"})
    hist = PriceHistory(code=clean_code(symbol.split(".")[0]), source="야후 파이낸스")
    try:
        result = (data or {}).get("chart", {}).get("result") or []
        if not result:
            return hist
        stamps = result[0].get("timestamp") or []
        quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        for ts, close in zip(stamps, closes):
            if close is None:
                continue
            hist.dates.append(datetime.fromtimestamp(int(ts), KST).strftime("%Y-%m-%d"))
            hist.closes.append(float(close))
            hist.volumes.append(0.0)
    except Exception:
        return hist
    hist.ma20 = _sma(hist.closes, 20)
    hist.ma60 = _sma(hist.closes, 60)
    return hist


@memoize(ttl=600)
def fetch_price_history(code: str, months: int = 12) -> PriceHistory:
    """일별 종가 흐름(기본 1년). 네이버 차트 → 야후 폴백. 실패해도 예외 없음."""
    code = clean_code(code)
    market = detect_market(code)
    end = now_kst().date()
    start = end - timedelta(days=int(max(months, 12) * 31) + 120)
    params = {"startDateTime": start.strftime("%Y%m%d"), "endDateTime": end.strftime("%Y%m%d")}
    source = "네이버 금융 차트"
    if market == "KR":
        data = _get_list(f"{NAVER_API}/chart/domestic/item/{code}/day", params)
    else:
        data = _get_list(f"{NAVER_API}/chart/foreign/item/{_reuters_for(code)}/day", params)
        source = "네이버 해외 차트"

    hist = PriceHistory(code=code, currency="KRW" if market == "KR" else "USD", source=source)
    for row in data or []:
        try:
            raw = str(row.get("localDate") or "")
            close = row.get("closePrice")
            if len(raw) != 8 or close is None:
                continue
            hist.dates.append(f"{raw[:4]}-{raw[4:6]}-{raw[6:]}")
            hist.closes.append(float(close))
            hist.volumes.append(float(row.get("accumulatedTradingVolume") or 0))
        except Exception:
            continue
    if not hist.closes:
        fallback = _yahoo_history(f"{code}.KS" if market == "KR" else code, months)
        if fallback.closes:
            fallback.currency = hist.currency
            return fallback
        hist.note = "가격 흐름을 가져오지 못했습니다(네트워크·종목코드 확인)."
        return hist
    hist.ma20 = _sma(hist.closes, 20)
    hist.ma60 = _sma(hist.closes, 60)
    return hist


# ------------------------------------------------------ 거시경제 지표판 ----
@dataclass
class MacroItem:
    key: str
    label: str
    value: float | None = None
    change: float | None = None
    change_pct: float | None = None
    unit: str = ""
    as_of: str = ""
    source: str = ""
    ok: bool = True
    extra: str = ""          # 거래대금·수급 등 보조 문구

    def value_text(self) -> str:
        if self.value is None:
            return "확인 불가"
        if self.unit == "%":
            return f"{self.value:.2f}%"
        if self.unit == "원":
            return f"{self.value:,.2f}원"
        if self.unit == "달러":
            return f"${self.value:,.2f}"
        return f"{self.value:,.2f}"

    def change_text(self) -> str:
        if self.key in ("US10Y", "KR10Y", "FED") and self.change is not None:
            return f"{self.change:+.3f}%p"
        if self.change_pct is not None:
            return f"{self.change_pct:+.2f}%"
        if self.change is not None:
            return f"{self.change:+,.2f}"
        return "-"

    def is_up(self) -> bool:
        ref = self.change if self.key in ("US10Y", "KR10Y", "FED") else self.change_pct
        return bool(ref and ref > 0)


@dataclass
class MarketFlow:
    """시장 전체(코스피·코스닥) 투자자별 순매수 — 단위 억원."""

    board: str = ""
    label: str = ""
    date: str = ""
    foreign: float | None = None
    institution: float | None = None
    personal: float | None = None
    trading_value: float | None = None      # 원
    note: str = ""

    def text(self) -> str:
        if self.foreign is None and self.institution is None:
            return self.note or "확인 불가"
        def eok(v):
            return "-" if v is None else f"{v:+,.0f}억"
        return f"외국인 {eok(self.foreign)} · 기관 {eok(self.institution)} · 개인 {eok(self.personal)}"


@dataclass
class SectorPulse:
    """산업 동향 — 대표 종목 등락률 평균(ETF 대용) + 근거."""

    key: str = ""
    label: str = ""
    change_pct: float | None = None
    members: list[dict] = field(default_factory=list)   # [{name, code, change_pct, market}]
    note: str = ""

    def text(self) -> str:
        if self.change_pct is None:
            return "확인 불가"
        return f"{self.change_pct:+.2f}%"


@dataclass
class MacroBoard:
    items: list[MacroItem] = field(default_factory=list)
    flows: list[MarketFlow] = field(default_factory=list)
    as_of: str = ""

    def get(self, key: str) -> MacroItem | None:
        for item in self.items:
            if item.key == key:
                return item
        return None

    def ok_count(self) -> int:
        return sum(1 for i in self.items if i.ok and i.value is not None)


def _naver_index_item(key: str, label: str, symbol: str) -> tuple[MacroItem, MarketFlow]:
    """국내외 지수: 네이버 지수 API → 야후 폴백. (지표, 시장 수급) 반환."""
    item = MacroItem(key=key, label=label, source="네이버 금융")
    flow = MarketFlow(board=symbol, label=label)
    url = f"{NAVER_API}/index/{symbol}/basic" if symbol.startswith(".") else f"{NAVER_INDEX}/{symbol}/basic"
    data = _get_json(url)
    if data:
        item.value = parse_num(data.get("closePrice"))
        item.change = parse_num(data.get("compareToPreviousClosePrice"))
        item.change_pct = parse_num(data.get("fluctuationsRatio"))
        item.as_of = str(data.get("localTradedAt") or "")
    if item.value is None:
        yahoo_symbol = {"KOSPI": "^KS11", "KOSDAQ": "^KQ11", "SP500": "^GSPC", "NASDAQ": "^IXIC"}.get(key)
        fallback = _yahoo_quote(yahoo_symbol) if yahoo_symbol else {}
        if fallback.get("price"):
            item.value = fallback["price"]
            item.change_pct = fallback.get("change_pct")
            item.source = "야후 파이낸스"
    item.ok = item.value is not None
    if symbol in ("KOSPI", "KOSDAQ"):
        detail = _get_json(f"{NAVER_INDEX}/{symbol}/integration") or {}
        infos = {k: v[0] for k, v in _nav_infos(detail.get("totalInfos")).items()}
        flow.trading_value = parse_korean_amount(infos.get("accumulatedTradingValue"))
        if flow.trading_value:
            item.extra = f"거래대금 {compact_money(flow.trading_value)}"
        trend = _get_json(f"{NAVER_INDEX}/{symbol}/trend") or {}
        raw_date = str(trend.get("bizdate") or "")
        if raw_date:
            flow.date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
        flow.foreign = parse_num(trend.get("foreignValue"))
        flow.institution = parse_num(trend.get("institutionalValue"))
        flow.personal = parse_num(trend.get("personalValue"))
        if flow.foreign is None:
            flow.note = "수급 확인 불가"
    return item, flow


def _naver_market_index(key: str, label: str, category: str, reuters: str,
                        unit: str = "") -> MacroItem:
    """환율·금리·원자재: 네이버 시장지표 API → 야후 폴백."""
    item = MacroItem(key=key, label=label, unit=unit, source="네이버 금융")
    data = (_get_json(NAVER_MARKET_INDEX, {"category": category, "reutersCode": reuters}) or {})
    result = data.get("result") or {}
    if result:
        item.value = parse_num(result.get("closePrice"))
        item.change = parse_num(result.get("fluctuations"))
        item.change_pct = parse_num(result.get("fluctuationsRatio"))
        item.as_of = str(result.get("localTradedAt") or "")
    if item.value is None:
        yahoo_symbol = {"USDKRW": "KRW=X", "US10Y": "^TNX", "KR10Y": None,
                        "WTI": "CL=F", "GOLD": "GC=F", "COPPER": "HG=F"}.get(key)
        fallback = _yahoo_quote(yahoo_symbol) if yahoo_symbol else {}
        if fallback.get("price"):
            item.value = fallback["price"]
            item.change_pct = fallback.get("change_pct")
            item.source = "야후 파이낸스"
    item.ok = item.value is not None
    return item


@memoize(ttl=3600)
def _us_policy_rate() -> MacroItem:
    """미국 실효 연방기금금리(EFFR) — 뉴욕 연준 공식 API."""
    item = MacroItem(key="FED", label="미국 기준금리(EFFR)", unit="%", source="뉴욕 연준")
    data = _get_json("https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json")
    try:
        rate = ((data or {}).get("refRates") or [{}])[0]
        item.value = parse_num(rate.get("percentRate"))
        item.as_of = str(rate.get("effectiveDate") or "")
    except Exception:
        pass
    item.ok = item.value is not None
    if not item.ok:
        item.extra = "확인 불가"
    return item


@memoize(ttl=300)
def fetch_macro() -> MacroBoard:
    """코스피·코스닥·S&P500·나스닥·원/달러·미/한 10년물·기준금리·유가·금·구리."""
    items: list[MacroItem] = []
    flows: list[MarketFlow] = []
    for key, label, symbol in (("KOSPI", "코스피", "KOSPI"), ("KOSDAQ", "코스닥", "KOSDAQ"),
                               ("SP500", "S&P500", ".INX"), ("NASDAQ", "나스닥", ".IXIC")):
        item, flow = _naver_index_item(key, label, symbol)
        items.append(item)
        if symbol in ("KOSPI", "KOSDAQ"):
            flows.append(flow)
    items.append(_naver_market_index("USDKRW", "원/달러", "exchange", "FX_USDKRW", unit="원"))
    items.append(_naver_market_index("US10Y", "미 10년물", "bond", "US10YT=RR", unit="%"))
    items.append(_naver_market_index("KR10Y", "한국 10년물", "bond", "KR10YT=RR", unit="%"))
    items.append(_us_policy_rate())
    items.append(_naver_market_index("WTI", "WTI 유가", "energy", "CLcv1", unit="달러"))
    items.append(_naver_market_index("GOLD", "국제 금", "metals", "GCcv1", unit="달러"))
    items.append(_naver_market_index("COPPER", "구리", "metals", "HGcv1", unit="달러"))
    as_of = next((i.as_of for i in items if i.as_of), "")
    return MacroBoard(items=items, flows=flows, as_of=as_of)


PULSE_GROUPS: dict[str, list[str]] = {
    "반도체·AI": ["005930", "000660", "403870", "183300", "NVDA", "AVGO", "TSM", "MU"],
    "원전·전력설비": ["034020", "052690", "267260", "010120", "CEG", "GEV"],
    "전기차·배터리": ["373220", "006400", "247540", "066970", "TSLA", "ALB"],
    "바이오": ["207940", "000100", "214150", "LLY", "PFE", "MRK"],
    "건설·철강": ["000720", "047040", "004020", "005490", "NUE", "CAT"],
}


@memoize(ttl=300)
def fetch_sector_pulse() -> list[SectorPulse]:
    """산업 동향 — 업종 대표 종목 등락률 평균(ETF 대용)과 개별 등락."""
    out: list[SectorPulse] = []
    for label, codes in PULSE_GROUPS.items():
        pulse = SectorPulse(key=label, label=label)
        changes: list[float] = []
        for code in codes:
            if detect_market(code) == "KR":
                quote = _naver_quote(code)
                name = quote.get("name") or ""
                change = quote.get("change_pct")
            else:
                basic = _naver_us_basic(code)
                name = basic.get("name") or code
                change = basic.get("change_pct")
            if change is None:
                continue
            changes.append(change)
            pulse.members.append({"name": name or code, "code": code,
                                  "change_pct": change,
                                  "market": "국내" if detect_market(code) == "KR" else "미국"})
        if changes:
            pulse.change_pct = sum(changes) / len(changes)
            pulse.members.sort(key=lambda m: -abs(m["change_pct"] or 0))
            pulse.note = f"대표 {len(changes)}종목 평균"
        else:
            pulse.note = "확인 불가"
        out.append(pulse)
    return out


def macro_brief(board: MacroBoard) -> list[str]:
    """지표 흐름을 문장으로 풀어 준다(규칙 기반, AI 불필요)."""
    lines: list[str] = []
    kospi, kosdaq = board.get("KOSPI"), board.get("KOSDAQ")
    if kospi and kospi.change_pct is not None:
        if kospi.change_pct <= -0.5:
            lines.append(f"코스피 {kospi.change_pct:+.2f}% 하락 — 국내 증시는 위험회피 쪽입니다. "
                         f"수출 대형주·성장주 모두 수급이 약해질 수 있습니다.")
        elif kospi.change_pct >= 0.5:
            lines.append(f"코스피 {kospi.change_pct:+.2f}% 상승 — 위험선호가 살아난 흐름입니다.")
        else:
            lines.append(f"코스피 {kospi.change_pct:+.2f}% — 방향성이 크지 않은 하루입니다.")
    if kosdaq and kosdaq.change_pct is not None and kospi and kospi.change_pct is not None:
        gap = kosdaq.change_pct - kospi.change_pct
        if gap <= -0.5:
            lines.append(f"코스닥이 코스피보다 {abs(gap):.2f}%p 더 약합니다 — 중소형·바이오·2차전지 변동성에 주의하세요.")
        elif gap >= 0.5:
            lines.append(f"코스닥이 코스피보다 {abs(gap):.2f}%p 더 강합니다 — 중소형 성장주에 순환매가 들어온 흐름입니다.")
    for flow in board.flows:
        if flow.foreign is None:
            continue
        tone = "순매수" if (flow.foreign or 0) > 0 else "순매도"
        lines.append(f"{flow.label} 시장은 외국인 {flow.foreign:+,.0f}억 {tone}, "
                     f"기관 {flow.institution or 0:+,.0f}억입니다({flow.date or '-'}).")
    sp, ndq = board.get("SP500"), board.get("NASDAQ")
    if sp and sp.change_pct is not None:
        if sp.change_pct <= -0.5:
            lines.append(f"S&P500 {sp.change_pct:+.2f}% — 글로벌 위험자산 조정. 반도체·인터넷 등 미국 연동 업종의 밸류에이션 부담이 커집니다.")
        elif sp.change_pct >= 0.5:
            lines.append(f"S&P500 {sp.change_pct:+.2f}% — 글로벌 증시가 우호적입니다.")
    if ndq and ndq.change_pct is not None and abs(ndq.change_pct) >= 0.7:
        lines.append(f"나스닥 {ndq.change_pct:+.2f}% — 기술주 흐름이 국내 반도체·2차전지·바이오 투자심리에 직접 전이됩니다.")
    fx = board.get("USDKRW")
    if fx and fx.change_pct is not None and abs(fx.change_pct) >= 0.2:
        if fx.change_pct > 0:
            lines.append(f"원/달러 {fx.value_text()}({fx.change_pct:+.2f}%) — 원화 약세입니다. "
                         f"수출주(반도체·자동차·조선) 채산성에는 우호적, 수입 원자재 비중이 큰 내수·건설·유통에는 원가 부담입니다.")
        else:
            lines.append(f"원/달러 {fx.value_text()}({fx.change_pct:+.2f}%) — 원화 강세입니다. "
                         f"수출주 마진에는 부담, 내수·수입 기업에는 우호적입니다.")
    bond = board.get("US10Y")
    if bond and bond.change is not None and abs(bond.change) >= 0.03:
        if bond.change > 0:
            lines.append(f"미 10년물 {bond.value_text()}({bond.change:+.3f}%p) — 금리 상승은 성장주(바이오·인터넷·2차전지) "
                         f"할인율과 건설·PF 조달 비용에 부담, 은행·보험에는 우호적입니다.")
        else:
            lines.append(f"미 10년물 {bond.value_text()}({bond.change:+.3f}%p) — 금리 하락은 성장주 밸류에이션에 우호적입니다.")
    wti = board.get("WTI")
    if wti and wti.change_pct is not None and abs(wti.change_pct) >= 1.5:
        lines.append(f"WTI {wti.value_text()}({wti.change_pct:+.2f}%) — 유가 상승은 운송·항공 원가 부담, "
                     f"에너지·정유 업종에는 우호적입니다.")
    copper = board.get("COPPER")
    if copper and copper.change_pct is not None and abs(copper.change_pct) >= 1.5:
        lines.append(f"구리 {copper.value_text()}({copper.change_pct:+.2f}%) — 경기 선행 지표로, "
                     f"하락은 건설·제조 수요 둔화 신호일 수 있습니다.")
    if not lines:
        lines.append("거시 지표가 아직 로드되지 않았습니다. 잠시 후 새로고침해 주세요.")
    return lines


# ------------------------------------------------------- 외국인·기관 수급 ----
@dataclass
class FlowSummary:
    code: str = ""
    rows: list[dict] = field(default_factory=list)   # {날짜, 외국인, 기관, 개인, 종가}
    foreign_sum: float | None = None
    organ_sum: float | None = None
    individual_sum: float | None = None
    foreign_hold_ratio: float | None = None
    available: bool = False
    note: str = ""

    def tag(self) -> str:
        if not self.available:
            return "⚪ 확인 불가"
        f, o = self.foreign_sum or 0, self.organ_sum or 0
        if f > 0 and o > 0:
            return "🟢 외인·기관 동반 순매수"
        if f > 0:
            return "🟢 외국인 순매수"
        if o > 0:
            return "🟡 기관 순매수"
        if f < 0 and o < 0:
            return "🔴 외인·기관 동반 순매도"
        if f < 0:
            return "🔴 외국인 순매도"
        return "⚪ 중립"

    def text(self) -> str:
        if not self.available:
            return self.note or "수급 데이터 없음"
        def qty(v):
            if v is None:
                return "-"
            if abs(v) >= 10000:
                return f"{v / 10000:+,.1f}만주"
            return f"{v:+,.0f}주"
        return f"외국인 {qty(self.foreign_sum)} · 기관 {qty(self.organ_sum)} ({len(self.rows)}거래일)"


@memoize(ttl=600)
def fetch_flow(code: str, days: int = 5) -> FlowSummary:
    """최근 거래일 외국인·기관 순매수(한국 종목만). 실패해도 예외 없음."""
    code = clean_code(code)
    out = FlowSummary(code=code)
    if detect_market(code) != "KR":
        out.note = "해외 종목은 국내 외국인·기관 수급 데이터가 제공되지 않습니다."
        return out
    data = _get_list(f"{NAVER_BASE}/{code}/trend")
    if not data:
        out.note = "수급 데이터를 가져오지 못했습니다."
        return out
    for row in data[:max(1, days)]:
        raw = str(row.get("bizdate") or "")
        out.rows.append({
            "날짜": f"{raw[:4]}-{raw[4:6]}-{raw[6:]}" if len(raw) == 8 else raw,
            "외국인": parse_num(row.get("foreignerPureBuyQuant")),
            "기관": parse_num(row.get("organPureBuyQuant")),
            "개인": parse_num(row.get("individualPureBuyQuant")),
            "종가": parse_num(row.get("closePrice")),
        })
    if not out.rows:
        out.note = "수급 데이터를 가져오지 못했습니다."
        return out
    out.available = True
    out.foreign_sum = sum(r["외국인"] or 0 for r in out.rows)
    out.organ_sum = sum(r["기관"] or 0 for r in out.rows)
    out.individual_sum = sum(r["개인"] or 0 for r in out.rows)
    ratio = data[0].get("foreignerHoldRatio")
    out.foreign_hold_ratio = parse_num(str(ratio).replace("%", "")) if ratio else None
    return out


# --------------------------------------------- 3~5년 실적·재무 추이 ----
HISTORY_METRICS = ["매출액", "영업이익", "순이익", "영업이익률", "ROE", "부채비율",
                   "EPS", "PER", "BPS", "PBR", "주당배당금", "현금흐름"]


@dataclass
class FinancialHistory:
    """연도별 재무 추이(확정 실적 기준, 마지막 열은 추정 표시)."""

    code: str = ""
    currency: str = "KRW"
    unit: str = "억원"
    years: list[str] = field(default_factory=list)
    series: dict[str, list[float | None]] = field(default_factory=dict)
    estimated: list[bool] = field(default_factory=list)
    source: str = ""
    note: str = ""

    def available(self) -> bool:
        return bool(self.years) and any(v is not None for v in self.series.get("매출액", []))

    def series_rows(self, metrics: list[str] | None = None) -> list[dict]:
        metrics = metrics or HISTORY_METRICS
        rows = []
        for i, year in enumerate(self.years):
            row = {"연도": year + ("(추정)" if i < len(self.estimated) and self.estimated[i] else "")}
            for m in metrics:
                values = self.series.get(m) or []
                row[m] = values[i] if i < len(values) else None
            rows.append(row)
        return rows


def _nav_rows_data(data: dict, key: str = "financeInfo") -> tuple[list[dict], dict]:
    info = data.get(key) or {}
    titles = info.get("trTitleList") or []
    rows = {r.get("title"): r.get("columns") or {} for r in info.get("rowList") or []}
    return titles, rows


@memoize(ttl=3600)
def _yf_operating_cashflow(code: str) -> dict[str, float]:
    """yfinance 연간 영업현금흐름(가능할 때만). {연도: 값}"""
    if os.environ.get("VI_SKIP_YFINANCE") == "1":
        return {}
    try:
        import yfinance as yf
    except Exception:
        return {}
    symbol = code if detect_market(code) != "KR" else f"{code}.KS"
    out: dict[str, float] = {}
    for sym in (symbol, code, f"{code}.KQ"):
        try:
            frame = yf.Ticker(sym).cashflow
            if frame is None or frame.empty:
                continue
            for label in ("Operating Cash Flow", "Total Cash From Operating Activities"):
                if label in frame.index:
                    for col, value in frame.loc[label].items():
                        try:
                            year = str(getattr(col, "year", str(col)[:4]))
                            num = float(value)
                            if num == num:  # NaN 제외
                                out[year] = num
                        except Exception:
                            continue
                    if out:
                        return out
        except Exception:
            continue
    return out


@memoize(ttl=3600)
def fetch_financial_history(code: str, years: int = 4) -> FinancialHistory:
    """최근 3~5개년 매출·영업이익·순이익·ROE·부채비율·PER/PBR·EPS/BPS·배당 추이.

    네이버 연간 재무(확정 3개년 + 추정 1개년)를 기본으로 쓰고,
    영업현금흐름은 yfinance가 응답할 때만 채운다(없으면 '확인 불가').
    """
    code = clean_code(code)
    market = detect_market(code)
    hist = FinancialHistory(code=code, currency="KRW" if market == "KR" else "USD",
                            unit="억원" if market == "KR" else "백만 달러")
    if market == "KR":
        data = _get_json(f"{NAVER_BASE}/{code}/finance/annual") or {}
        titles, rows = _nav_rows_data(data)
        pairs = [("매출액", "매출액"), ("영업이익", "영업이익"), ("순이익", "당기순이익"),
                 ("영업이익률", "영업이익률"), ("ROE", "ROE"), ("부채비율", "부채비율"),
                 ("EPS", "EPS"), ("PER", "PER"), ("BPS", "BPS"), ("PBR", "PBR"),
                 ("주당배당금", "주당배당금"), ("배당수익률", None)]
        hist.source = "네이버 금융 연간 재무제표"
    else:
        data = _get_json(f"{NAVER_API}/stock/{_reuters_for(code)}/finance/annual") or {}
        titles = data.get("trTitleList") or []
        rows = {r.get("title"): r.get("columns") or {} for r in data.get("rowList") or []}
        pairs = [("매출액", "매출액"), ("영업이익", "EBIT"), ("순이익", "당기순이익"),
                 ("영업이익률", None), ("ROE", "ROA"), ("부채비율", None),
                 ("EPS", None), ("PER", "PER"), ("BPS", None), ("PBR", "PBR"),
                 ("주당배당금", None), ("배당수익률", "배당수익률")]
        hist.source = "네이버 해외 연간 재무"

    if not titles:
        hist.note = "재무 추이를 가져오지 못했습니다."
        return hist

    chosen = titles[-max(2, min(years, 5)):]
    hist.years = [_label_year(t.get("title")) for t in chosen]
    hist.estimated = [str(t.get("isConsensus", "N")).upper() == "Y" for t in chosen]
    keys = [t.get("key") for t in chosen]

    def value(title: str | None, key: str):
        if not title:
            return None
        cell = (rows.get(title) or {}).get(key) or {}
        return parse_num(cell.get("value") if isinstance(cell, dict) else cell)

    for label, nav_title in pairs:
        hist.series[label] = [value(nav_title, k) for k in keys]

    if market != "KR":
        # 해외 종목은 EPS·BPS·영업이익률을 제공하지 않으므로 계산해 채운다.
        basic = _naver_us_basic(code)
        shares = basic.get("shares")
        rev, op = hist.series.get("매출액"), hist.series.get("영업이익")
        hist.series["영업이익률"] = [
            (o / r * 100) if (o is not None and r) else None for o, r in zip(op, rev)
        ]
        if shares:
            hist.series["EPS"] = [((n * 1e6 / shares) if n is not None else None)
                                  for n in hist.series.get("순이익", [])]

    # 영업현금흐름(가능할 때만) — 연도 라벨로 매칭
    ocf = _yf_operating_cashflow(code)
    if ocf:
        scale = 1.0 if market == "KR" else 1.0
        values = []
        for year in hist.years:
            raw = ocf.get(year)
            if raw is None:
                values.append(None)
            else:
                values.append(raw / 1e8 if market == "KR" else raw / 1e6 * scale)
        hist.series["현금흐름"] = values
    else:
        hist.series["현금흐름"] = [None] * len(hist.years)
        hist.note = "영업현금흐름은 외부 재무 데이터(yfinance)가 응답할 때만 표시됩니다."
    return hist


def _label_year(title: str) -> str:
    m = re.search(r"(20\d{2})", str(title or ""))
    return m.group(1) if m else str(title or "")


def compact_money(value: float | None, currency: str = "KRW") -> str:
    """재무제표 금액을 읽기 좋게: 원화 억원/조, 달러 백만/십억."""
    if value is None:
        return "-"
    if currency == "USD":
        if abs(value) >= 1e9:
            return f"${value / 1e9:,.2f}B"
        return f"${value / 1e6:,.1f}M"
    if abs(value) >= 1e12:
        return f"{value / 1e12:,.1f}조원"
    if abs(value) >= 1e8:
        return f"{value / 1e8:,.0f}억원"
    return f"{value:,.0f}원"


# ------------------------------------------- 밸류에이션 3방식 + 4단계 ----
TARGET_PER_BY_SECTOR = {
    "금융": 8.0, "자동차·조선": 7.5, "제조·철강": 9.0, "건설": 9.0, "유통·식품": 12.0,
    "통신·운송": 10.0, "원전·에너지": 14.0, "반도체": 14.0, "2차전지": 18.0,
    "인터넷·SW": 20.0, "바이오": 24.0, "기타": 12.0,
}
TARGET_PBR_BY_SECTOR = {
    "금융": 0.65, "자동차·조선": 0.9, "제조·철강": 0.9, "건설": 0.8, "유통·식품": 1.1,
    "통신·운송": 1.0, "원전·에너지": 1.6, "반도체": 1.8, "2차전지": 1.8,
    "인터넷·SW": 2.5, "바이오": 3.0, "기타": 1.0,
}
VALUATION_WEIGHTS = {
    # (PER, PBR, DCF)
    "금융": (0.3, 0.6, 0.1),
    "자산주": (0.2, 0.7, 0.1),
    "성장": (0.45, 0.15, 0.40),
    "제조": (0.5, 0.3, 0.2),
}
DCF_DISCOUNT = 0.10        # 보수적 할인율
DCF_TERMINAL_GROWTH = 0.02
DCF_YEARS = 5


@dataclass
class ValuationMethod:
    """적정가 계산 한 방식(PER·PBR·DCF)의 결과."""

    name: str
    value: float | None
    weight: float                 # 유형별 설정 가중치
    basis: str
    note: str = ""
    # --- v3 ---
    excluded: bool = False        # 계산에서 완전히 제외됐는지
    reason: str = ""              # 제외 사유(적자/음수 EPS·데이터 부족)
    effective: float = 0.0        # 남은 방식끼리 재배분한 실제 가중치(합계 100%)


@dataclass
class Valuation:
    currency: str = "KRW"
    methods: list[ValuationMethod] = field(default_factory=list)
    fair: float | None = None
    low: float | None = None
    high: float | None = None
    current: float | None = None
    tier: str = ""
    tier_key: str = ""
    rows: list[dict] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    note: str = ""
    profile: str = ""
    # --- v3 ---
    safety_margin: float | None = None                   # (적정가-현재가)/적정가*100
    excluded: list[dict] = field(default_factory=list)   # [{name, reason}]
    target_per: float = 0.0
    target_pbr: float = 0.0
    weights: tuple[float, float, float] = (0.0, 0.0, 0.0)
    price_as_of: str = ""
    finance_basis: str = ""

    def weight_summary(self) -> str:
        """실제 적용된 가중치(제외 방식은 빠지고 재배분된 값)."""
        parts = [f"{m.name.replace(' 방식', '')} {m.effective:.0%}"
                 for m in self.methods if m.value is not None]
        return " · ".join(parts) if parts else "가중치 없음"

    def margin_text(self) -> str:
        if self.safety_margin is None:
            return "안전마진 확인 불가"
        return f"안전마진 {self.safety_margin:+.1f}%"

    def exclusion_text(self) -> str:
        if not self.excluded:
            return ""
        return " / ".join(f"{e['name']} 제외 — {e['reason']}" for e in self.excluded)


def valuation_profile(snap: StockSnapshot) -> str:
    """섹터·기업 특성에 따라 밸류에이션 가중치 유형을 고른다."""
    if snap.sector == "금융":
        return "금융"
    if snap.sector == "건설" or (snap.pbr and snap.pbr <= 0.5):
        return "자산주"
    if (snap.eps is None or (snap.eps or 0) <= 0) or snap.sector in ("바이오", "2차전지", "인터넷·SW"):
        return "성장"
    return "제조"


def valuate(snap: StockSnapshot, history: FinancialHistory | None = None) -> Valuation:
    """PER·PBR·DCF 3방식 적정가 + 4단계 대응 구간표 + 안전마진.

    v3 원칙
      - 적자기업(EPS ≤ 0 또는 영업적자)은 PER 방식을 **완전히 제외**하고 사유를 남긴다.
      - 남은 방식(PBR·DCF)끼리 가중치를 100%로 재배분한다(섹터별 기본 가중치 유지).
      - 적정가 대비 현재가의 안전마진(%)을 계산한다.
    """
    cur = snap.currency or "KRW"
    profile = valuation_profile(snap)
    w_per, w_pbr, w_dcf = VALUATION_WEIGHTS[profile]
    target_per = TARGET_PER_BY_SECTOR.get(snap.sector, 12.0)
    target_pbr = TARGET_PBR_BY_SECTOR.get(snap.sector, 1.0)

    bps = snap.bps if (snap.bps or 0) > 0 else None
    operating_loss = snap.op_income is not None and snap.op_income <= 0

    # --- PER: 적자기업은 완전 제외 --------------------------------------
    per_reason = ""
    if snap.eps is None:
        per_reason = "EPS 확인 불가로 PER 평가 제외"
    elif snap.eps <= 0 and operating_loss:
        per_reason = "적자/음수 EPS로 PER 평가 제외 (영업적자 동반)"
    elif snap.eps <= 0:
        per_reason = "적자/음수 EPS로 PER 평가 제외"
    elif operating_loss:
        per_reason = "영업적자로 PER 평가 제외"
    per_value = None if per_reason else snap.eps * target_per
    pbr_value = bps * target_pbr if bps else None

    # DCF — 주당 가치
    shares = snap.shares
    if not shares and snap.market_cap and snap.price:
        shares = snap.market_cap / snap.price
    # 단위 통일: KR은 모두 '원', US는 모두 '달러'
    def _won(value, is_kr):
        if value is None or value <= 0:
            return None
        return value * 1e8 if is_kr else value

    is_kr = snap.market == "KR"
    ocf_value = _won(snap.ocf, is_kr)
    ni_value = _won(snap.net_income, is_kr)
    base_cash = None
    cash_label = ""
    if ocf_value and ni_value:
        base_cash, cash_label = (ocf_value + ni_value) / 2, "영업현금흐름·순이익 평균"
    elif ocf_value:
        base_cash, cash_label = ocf_value, "영업현금흐름"
    elif ni_value:
        base_cash, cash_label = ni_value, "순이익(현금흐름 미확인 대용)"
    dcf_value = None
    assumptions = [
        f"할인율 {DCF_DISCOUNT*100:.0f}% · 영구성장률 {DCF_TERMINAL_GROWTH*100:.0f}% · {DCF_YEARS}년 추정",
    ]
    if base_cash and shares:
        growth = snap.revenue_yoy if snap.revenue_yoy is not None else 5.0
        growth = max(-5.0, min(12.0, growth)) / 100.0
        cash = base_cash
        pv = 0.0
        for t in range(1, DCF_YEARS + 1):
            cash = cash * (1 + growth)
            pv += cash / ((1 + DCF_DISCOUNT) ** t)
        terminal = cash * (1 + DCF_TERMINAL_GROWTH) / (DCF_DISCOUNT - DCF_TERMINAL_GROWTH)
        pv += terminal / ((1 + DCF_DISCOUNT) ** DCF_YEARS)
        dcf_value = pv / shares
        assumptions.append(f"기준 현금흐름: {cash_label} {compact_money(base_cash, cur)} · "
                           f"성장률 {growth*100:+.1f}%/년 · 발행주식 약 {shares/1e6:,.1f}백만주(시총÷주가 추정)")
    else:
        assumptions.append("현금흐름·순이익이 적자이거나 주식 수를 확인할 수 없어 DCF는 계산하지 않았습니다.")

    methods = [
        ValuationMethod("PER 방식", per_value, w_per,
                        f"EPS × 목표 PER {target_per:g}배" if per_value is not None else "계산 제외",
                        "" if per_value is not None else per_reason,
                        excluded=per_value is None, reason="" if per_value is not None else per_reason),
        ValuationMethod("PBR 방식", pbr_value, w_pbr,
                        f"BPS × 목표 PBR {target_pbr:g}배" if pbr_value is not None else "계산 제외",
                        "" if pbr_value is not None else "BPS 확인 불가",
                        excluded=pbr_value is None,
                        reason="" if pbr_value is not None else "BPS 확인 불가"),
        ValuationMethod("DCF 방식", dcf_value, w_dcf,
                        "5년 FCF + 영구가치 현재가치 ÷ 주식 수" if dcf_value is not None else "계산 제외",
                        "" if dcf_value is not None else "현금흐름·주식 수 부족",
                        excluded=dcf_value is None,
                        reason="" if dcf_value is not None else "현금흐름·주식 수 부족"),
    ]
    # 남은 방식끼리 가중치 재배분(제외 방식이 0이 되어도 합계는 100%)
    total_w = sum(m.weight for m in methods if m.value is not None)
    for m in methods:
        m.effective = (m.weight / total_w) if (m.value is not None and total_w) else 0.0
    active = [m for m in methods if m.value is not None]
    fair = sum((m.value or 0.0) * m.effective for m in active) if active else None

    result = Valuation(currency=cur, methods=methods, fair=fair, current=snap.price,
                       assumptions=assumptions, profile=profile, target_per=target_per,
                       target_pbr=target_pbr, weights=(w_per, w_pbr, w_dcf),
                       excluded=[{"name": m.name, "reason": m.reason}
                                 for m in methods if m.value is None],
                       price_as_of=price_as_of_text(snap),
                       finance_basis=financial_basis_text(snap, history))
    if fair and fair > 0:
        result.low = fair * 0.90
        result.high = fair * 1.15
        result.rows = [
            {"구간": "적극 매수 검토", "가격대": f"{money(result.low, cur)} 이하",
             "판단": "가치 대비 충분히 싸다", "지금": snap.price is not None and snap.price <= result.low},
            {"구간": "분할매수 검토", "가격대": f"{money(result.low, cur)} ~ {money(fair, cur)}",
             "판단": "하단~적정가 사이", "지금": snap.price is not None and result.low < snap.price <= fair},
            {"구간": "보유 검토", "가격대": f"{money(fair, cur)} ~ {money(result.high, cur)}",
             "판단": "적정~상단, 신규 매수는 서두르지 않음",
             "지금": snap.price is not None and fair < snap.price <= result.high},
            {"구간": "신규매수 신중", "가격대": f"{money(result.high, cur)} 초과",
             "판단": "고평가 구간, 추격 매수 금지",
             "지금": snap.price is not None and snap.price > result.high},
        ]
        current_tier = next((r for r in result.rows if r["지금"]), None)
        result.tier = current_tier["구간"] if current_tier else "구간 확인 불가"
        result.tier_key = result.tier
        result.safety_margin = safety_margin(fair, snap.price)
    else:
        result.note = "EPS·BPS·현금흐름이 모두 부족해 적정가를 계산하지 못했습니다. 시나리오 참고만 하세요."

    notes = [f"가중치 유형: {profile} (설정 PER {w_per:.0%}·PBR {w_pbr:.0%}·DCF {w_dcf:.0%}"
             f" → 적용 {result.weight_summary()})"]
    if result.excluded:
        notes.append("제외: " + " / ".join(f"{e['name']}({e['reason']})" for e in result.excluded))
    if per_reason and "적자" in per_reason:
        notes.append("적자기업은 이익(EPS) 기준 PER을 쓰지 않고 PBR·DCF로 평가합니다.")
    result.note = result.note or " · ".join(notes)
    return result


# ------------------------------------------- 뉴스·공시 자동 분석(근거 고정) ----
CATEGORY_ORDER = ["최근 24시간 주요 뉴스", "공시·실적", "수주·계약", "정책·규제", "증권사 리포트"]
CATEGORY_WORDS = {
    "공시·실적": ["실적", "공시", "잠정", "분기", "영업이익", "순이익", "매출", "감사보고서", "주주총회", "유상증자", "배당"],
    "수주·계약": ["수주", "계약", "공급", "납품", "MOU", "협약", "수주잔고", "컨소시엄"],
    "정책·규제": ["정부", "규제", "법안", "관세", "국회", "금감원", "공정위", "환경부", "산업부", "보조금", "세제"],
    "증권사 리포트": ["목표주가", "투자의견", "리포트", "증권사", "상향", "하향", "커버리지", "애널리스트"],
}
POS_WORDS = ["수주", "호조", "사상 최대", "최대 실적", "흑자", "개선", "확대", "상승", "강세",
             "돌파", "목표가 상향", "수출 증가", "승인", "계약", "공급", "호재", "신고가",
             "흥행", "선정", "수혜", "반등", "상향", "체결", "진출", "호실적", "사상최대", "호평"]
NEG_WORDS = ["부진", "하락", "약세", "감소", "적자", "우려", "리스크", "규제", "조사", "소송",
             "파업", "취소", "지연", "경고", "손실", "철회", "불확실", "둔화", "급락", "폭락",
             "리콜", "제재", "관세", "분쟁", "차질", "축소", "경쟁 심화", "하향", "된서리",
             "꺾였", "꺾인", "쇼크", "악화", "후퇴", "위축", "매도", "불안", "조정", "하락세",
             "기각", "패소", "논란", "실망"]


@dataclass
class NewsItem:
    title: str
    link: str
    published: str           # 'YYYY-MM-DD HH:MM' (KST)
    source: str = ""
    category: str = "최근 24시간 주요 뉴스"
    sentiment: str = "⚪ 중립"
    note: str = ""

    def to_row(self) -> dict:
        return {"구분": self.category, "제목": self.title, "판단": self.sentiment,
                "출처": self.source, "게시": self.published, "링크": self.link}


@dataclass
class NewsDigest:
    items: list[NewsItem] = field(default_factory=list)
    generated_at: str = ""
    note: str = ""

    def by_category(self) -> dict[str, list[NewsItem]]:
        out: dict[str, list[NewsItem]] = {}
        for item in self.items:
            out.setdefault(item.category, []).append(item)
        return out

    def counts(self) -> dict[str, int]:
        out = {"🟢 호재": 0, "🔴 악재": 0, "⚪ 중립": 0}
        for item in self.items:
            out[item.sentiment] = out.get(item.sentiment, 0) + 1
        return out


def _sentiment(title: str) -> str:
    pos = sum(1 for w in POS_WORDS if w in title)
    neg = sum(1 for w in NEG_WORDS if w in title)
    if pos > neg:
        return "🟢 호재"
    if neg > pos:
        return "🔴 악재"
    return "⚪ 중립"


def _categorize(title: str, age_hours: float | None) -> str:
    for category in ("공시·실적", "수주·계약", "정책·규제", "증권사 리포트"):
        if any(w in title for w in CATEGORY_WORDS[category]):
            return category
    if age_hours is not None and age_hours <= 24:
        return "최근 24시간 주요 뉴스"
    return "최근 24시간 주요 뉴스"


_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
           "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def _parse_pubdate(text: str) -> datetime | None:
    """RFC822('Sun, 22 Nov 2026 13:45:00 GMT') → KST datetime."""
    text = str(text or "").strip()
    m = re.match(r"(?:[A-Za-z]{3},?\s+)?(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\s+"
                 r"(\d{2}):(\d{2})(?::(\d{2}))?\s*([+-]\d{4}|GMT|UTC)?", text)
    if not m:
        return None
    day, mon, year, hh, mm = int(m.group(1)), _MONTHS.get(m.group(2).title(), 0), int(m.group(3)), int(m.group(4)), int(m.group(5))
    if not mon:
        return None
    tz = m.group(7) or "GMT"
    offset = 0
    if tz not in ("GMT", "UTC"):
        try:
            sign = 1 if tz[0] == "+" else -1
            offset = sign * (int(tz[1:3]) * 60 + int(tz[3:5]))
        except Exception:
            offset = 0
    try:
        utc = datetime(year, mon, day, hh, mm, tzinfo=timezone.utc) - timedelta(minutes=offset)
        return utc.astimezone(KST)
    except Exception:
        return None


@memoize(ttl=600)
def _naver_stock_news(code: str) -> list[dict]:
    """네이버 종목 뉴스(원문 URL·시각 포함)."""
    if detect_market(code) != "KR":
        return []
    data = _get_json(f"{NAVER_BASE}/news/stock/{code}")
    rows: list[dict] = []
    for block in data if isinstance(data, list) else []:
        for item in (block or {}).get("items") or []:
            title = str(item.get("title") or "").strip()
            link = str(item.get("mobileNewsUrl") or "").strip()
            stamp = str(item.get("datetime") or "")
            if not title or not link:
                continue
            published = ""
            if len(stamp) >= 12:
                published = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[8:10]}:{stamp[10:12]}"
            rows.append({"title": title, "link": link, "source": str(item.get("officeName") or ""),
                         "published": published, "raw": stamp})
    return rows[:12]


@memoize(ttl=600)
def fetch_news_digest(query: str, code: str = "", limit: int = 12) -> NewsDigest:
    """종목·주제 뉴스를 실제 링크·게시시각과 함께 모아 분류·판단한다.

    구글뉴스 RSS + 네이버 종목뉴스(한국)만 사용하며, 없는 기사·URL은 만들지 않는다.
    """
    digest = NewsDigest(generated_at=now_kst().strftime("%Y-%m-%d %H:%M"))
    seen: set[str] = set()

    def push(title: str, link: str, published_dt: datetime | None, source: str) -> None:
        title = (title or "").strip()
        link = (link or "").strip()
        if not title or not link.startswith("http") or title in seen:
            return
        seen.add(title)
        age = None
        published = ""
        if published_dt:
            age = (now_kst() - published_dt).total_seconds() / 3600
            published = published_dt.strftime("%Y-%m-%d %H:%M")
        digest.items.append(NewsItem(
            title=title, link=link, published=published or "시각 미상", source=source,
            category=_categorize(title, age), sentiment=_sentiment(title)))

    try:
        resp = requests.get(GOOGLE_NEWS,
                            params={"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"},
                            headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code == 200:
            root = ET.fromstring(resp.text)
            for node in root.iter("item"):
                title = (node.findtext("title") or "").strip()
                link = (node.findtext("link") or "").strip()
                source = (node.findtext("source") or "")
                if title.endswith(" - " + source):
                    title = title[: -(len(source) + 3)]
                push(title, link, _parse_pubdate(node.findtext("pubDate") or ""), source or "구글뉴스")
    except Exception:
        pass

    for row in _naver_stock_news(code):
        published_dt = None
        if len(row["raw"]) >= 12:
            try:
                published_dt = datetime.strptime(row["raw"][:12], "%Y%m%d%H%M").replace(tzinfo=KST)
            except Exception:
                published_dt = None
        push(row["title"], row["link"], published_dt, row["source"] or "네이버 뉴스")

    digest.items.sort(key=lambda i: i.published if i.published != "시각 미상" else "", reverse=True)
    digest.items = digest.items[:limit]
    if not digest.items:
        digest.note = "뉴스를 가져오지 못했습니다(네트워크 또는 검색어 확인)."
    return digest


def news_impact_lines(digest: NewsDigest, sector: str = "", name: str = "") -> list[str]:
    """뉴스 묶음을 종목 영향 문장으로 요약(근거 문장은 기사 제목 그대로)."""
    if not digest.items:
        return []
    lines = []
    tone_count = digest.counts()
    lines.append(f"수집 {len(digest.items)}건 — 호재 {tone_count['🟢 호재']} · 악재 {tone_count['🔴 악재']} · "
                 f"중립 {tone_count['⚪ 중립']}.")
    for item in digest.items[:4]:
        if item.sentiment == "⚪ 중립":
            continue
        subject = name or sector or "해당 종목"
        lines.append(f"{item.sentiment} “{item.title}” ({item.published}, {item.source}) — "
                     f"{subject}에 {'우호적' if item.sentiment == '🟢 호재' else '부담'}으로 읽힐 수 있는 재료입니다.")
    if len(lines) == 1:
        lines.append("뚜렷한 호재·악재 키워드가 없는 중립 뉴스가 대부분입니다.")
    return lines


# ------------------------------------------- 이슈 → 내 종목 영향 해석 ----
@dataclass
class ImpactRow:
    code: str
    name: str
    sector: str
    score: float = 0.0
    tag: str = "⚪ 중립"
    reasons: list[str] = field(default_factory=list)
    news: list[dict] = field(default_factory=list)
    holdings: int = 0


@dataclass
class ImpactReport:
    rows: list[ImpactRow] = field(default_factory=list)
    sectors: list[ImpactRow] = field(default_factory=list)
    brief: list[str] = field(default_factory=list)
    generated_at: str = ""
    ai: str = ""

    def tag_counts(self) -> dict[str, int]:
        out = {"🟢 호재": 0, "🔴 주의": 0, "⚪ 중립": 0}
        for row in self.rows:
            out[row.tag] = out.get(row.tag, 0) + 1
        return out


# 지표별 '의미 있는 움직임' 기준
MACRO_THRESHOLD = {"KOSPI": 0.5, "KOSDAQ": 0.7, "SP500": 0.5, "NASDAQ": 0.7,
                   "USDKRW": 0.2, "US10Y": 0.03, "KR10Y": 0.03, "WTI": 1.5, "COPPER": 1.5}

# 섹터별 민감도: (지표키, 상승이 호재인가, 가중치, 상승 사유, 하락 사유)
SECTOR_MACRO_RULES: dict[str, list[tuple]] = {
    "반도체": [
        ("KOSPI", True, 2.0, "코스피 {v:+.2f}% 상승 → 국내 수출 대형주 투자심리 회복",
         "코스피 {v:+.2f}% 하락 → 반도체 대형주 수급 부담"),
        ("NASDAQ", True, 2.0, "나스닥 {v:+.2f}% 상승 → 글로벌 반도체 밸류에이션 동반 상승",
         "나스닥 {v:+.2f}% 하락 → AI·반도체 성장 기대 후퇴"),
        ("USDKRW", True, 1.5, "원/달러 {v:+.2f}% 상승(원화 약세) → 달러 결제 수출 채산성 개선",
         "원/달러 {v:+.2f}% 하락(원화 강세) → 수출 마진 축소"),
        ("US10Y", False, 1.5, "미 10년물 {v:+.3f}%p 상승 → 성장주 할인율 부담",
         "미 10년물 {v:+.3f}%p 하락 → 성장주 할인율 부담 완화"),
        ("SP500", True, 1.0, "S&P500 {v:+.2f}% 상승 → 글로벌 위험선호 개선",
         "S&P500 {v:+.2f}% 하락 → 글로벌 위험자산 회피"),
    ],
    "원전·에너지": [
        ("USDKRW", True, 2.0, "원/달러 {v:+.2f}% 상승 → 달러 표시 해외 원전 수주·기자재 채산성 개선",
         "원/달러 {v:+.2f}% 하락 → 해외 수주 채산성 소폭 악화"),
        ("US10Y", False, 1.5, "미 10년물 {v:+.3f}%p 상승 → 대형 프로젝트 조달 비용·유틸리티 밸류 부담",
         "미 10년물 {v:+.3f}%p 하락 → 프로젝트 조달 여건 개선"),
        ("WTI", True, 0.8, "WTI {v:+.2f}% 상승 → 에너지 가격 강세는 발전·에너지 업종 수익성에 우호적",
         "WTI {v:+.2f}% 하락 → 에너지 가격 약세"),
        ("SP500", True, 1.0, "S&P500 {v:+.2f}% 상승 → 전력 인프라 투자 심리 우호적",
         "S&P500 {v:+.2f}% 하락 → 인프라 투자 심리 위축"),
        ("KOSPI", True, 1.0, "코스피 {v:+.2f}% 상승 → 원전 기자재주 동반 강세",
         "코스피 {v:+.2f}% 하락 → 원전 기자재주 차익 실현 압력"),
    ],
    "건설": [
        ("US10Y", False, 1.5, "미 10년물 {v:+.3f}%p 상승 → 부동산·PF 조달 비용 부담",
         "미 10년물 {v:+.3f}%p 하락 → 조달 비용 부담 완화"),
        ("USDKRW", False, 1.0, "원/달러 {v:+.2f}% 상승 → 철근·유가 등 수입 원자재 원가 부담",
         "원/달러 {v:+.2f}% 하락 → 수입 원자재 원가 부담 완화"),
        ("COPPER", False, 0.8, "구리 {v:+.2f}% 상승 → 원자재 투입 원가 부담",
         "구리 {v:+.2f}% 하락 → 원자재 원가 부담 완화"),
        ("KOSPI", True, 1.0, "코스피 {v:+.2f}% 상승 → 건설·부동산 섹터 투자심리 개선",
         "코스피 {v:+.2f}% 하락 → 건설주 투자심리 위축"),
    ],
    "바이오": [
        ("US10Y", False, 2.0, "미 10년물 {v:+.3f}%p 상승 → 장기 현금흐름 할인 부담, 바이오 밸류에이션 압박",
         "미 10년물 {v:+.3f}%p 하락 → 바이오 밸류에이션 부담 완화"),
        ("NASDAQ", True, 2.0, "나스닥 {v:+.2f}% 상승 → 글로벌 바이오·헬스케어 투자심리 개선",
         "나스닥 {v:+.2f}% 하락 → 바이오 성장주 위험회피"),
        ("KOSDAQ", True, 1.5, "코스닥 {v:+.2f}% 상승 → 국내 바이오 중소형주 수급 개선",
         "코스닥 {v:+.2f}% 하락 → 국내 바이오주 수급 악화"),
        ("USDKRW", True, 0.5, "원/달러 {v:+.2f}% 상승 → 달러 계약·수출 비중 종목에 소폭 우호적",
         "원/달러 {v:+.2f}% 하락 → 달러 계약 이익 환산 감소"),
    ],
    "금융": [
        ("US10Y", True, 2.0, "미 10년물 {v:+.3f}%p 상승 → 예대마진·채권 운용수익 개선 기대",
         "미 10년물 {v:+.3f}%p 하락 → 이자마진 축소 우려"),
        ("KOSPI", True, 1.0, "코스피 {v:+.2f}% 상승 → 증권·보험 업종 투자심리 개선",
         "코스피 {v:+.2f}% 하락 → 금융주도 동반 약세"),
        ("USDKRW", False, 0.5, "원/달러 {v:+.2f}% 상승 → 환율 변동성·자본유출 우려",
         "원/달러 {v:+.2f}% 하락 → 외국인 자금 유입에 우호적"),
    ],
    "제조·철강": [
        ("KOSPI", True, 1.5, "코스피 {v:+.2f}% 상승 → 경기민감 제조·철강 수급 개선",
         "코스피 {v:+.2f}% 하락 → 경기민감 업종 투자심리 위축"),
        ("COPPER", True, 0.8, "구리 {v:+.2f}% 상승 → 경기 선행 수요 회복 신호",
         "구리 {v:+.2f}% 하락 → 제조 수요 둔화 우려"),
        ("US10Y", False, 1.0, "미 10년물 {v:+.3f}%p 상승 → 설비투자·건설 수요 둔화 우려",
         "미 10년물 {v:+.3f}%p 하락 → 설비투자 여건 개선"),
        ("USDKRW", True, 0.5, "원/달러 {v:+.2f}% 상승 → 수출 채산성 개선(원자재 수입 부담과 혼재)",
         "원/달러 {v:+.2f}% 하락 → 수출 채산성 소폭 악화"),
    ],
    "자동차·조선": [
        ("USDKRW", True, 2.0, "원/달러 {v:+.2f}% 상승(원화 약세) → 수출 채산성·수주 경쟁력 개선",
         "원/달러 {v:+.2f}% 하락(원화 강세) → 수출 마진 압박"),
        ("KOSPI", True, 1.5, "코스피 {v:+.2f}% 상승 → 수출 대형주 투자심리 개선",
         "코스피 {v:+.2f}% 하락 → 자동차·조선 수급 부담"),
        ("US10Y", False, 0.5, "미 10년물 {v:+.3f}%p 상승 → 미국 소비·할부금융 비용 부담",
         "미 10년물 {v:+.3f}%p 하락 → 미국 소비 여건 개선"),
        ("WTI", False, 0.5, "WTI {v:+.2f}% 상승 → 물류·운송비 부담",
         "WTI {v:+.2f}% 하락 → 운송비 부담 완화"),
    ],
    "2차전지": [
        ("NASDAQ", True, 2.0, "나스닥 {v:+.2f}% 상승 → 성장주 투자심리 회복",
         "나스닥 {v:+.2f}% 하락 → 2차전지 성장주 위험회피"),
        ("US10Y", False, 1.5, "미 10년물 {v:+.3f}%p 상승 → 성장주 할인율 부담",
         "미 10년물 {v:+.3f}%p 하락 → 성장주 할인율 부담 완화"),
        ("KOSPI", True, 1.5, "코스피 {v:+.2f}% 상승 → 2차전지 대형주 수급 개선",
         "코스피 {v:+.2f}% 하락 → 2차전지 수급 악화"),
        ("COPPER", False, 0.5, "구리 {v:+.2f}% 상승 → 소재 원가 부담",
         "구리 {v:+.2f}% 하락 → 소재 원가 부담 완화"),
    ],
    "인터넷·SW": [
        ("NASDAQ", True, 2.0, "나스닥 {v:+.2f}% 상승 → 인터넷·소프트웨어 성장주 강세",
         "나스닥 {v:+.2f}% 하락 → 성장주 밸류에이션 압박"),
        ("US10Y", False, 1.5, "미 10년물 {v:+.3f}%p 상승 → 성장주 할인율 부담",
         "미 10년물 {v:+.3f}%p 하락 → 성장주 할인율 부담 완화"),
        ("KOSPI", True, 1.0, "코스피 {v:+.2f}% 상승 → 국내 인터넷·게임 수급 개선",
         "코스피 {v:+.2f}% 하락 → 국내 인터넷·게임 약세"),
    ],
    "유통·식품": [
        ("USDKRW", False, 1.0, "원/달러 {v:+.2f}% 상승 → 수입 원자재·상품 매입 단가 상승",
         "원/달러 {v:+.2f}% 하락 → 수입 원가 부담 완화"),
        ("KOSPI", True, 0.5, "코스피 {v:+.2f}% 상승 → 소비심리·유통주 수급 개선",
         "코스피 {v:+.2f}% 하락 → 소비 관련주 약세"),
        ("US10Y", False, 0.5, "미 10년물 {v:+.3f}%p 상승 → 소비 여력 위축 우려",
         "미 10년물 {v:+.3f}%p 하락 → 소비 여력 개선"),
    ],
    "통신·운송": [
        ("WTI", False, 1.2, "WTI {v:+.2f}% 상승 → 유가·연료비 부담 확대",
         "WTI {v:+.2f}% 하락 → 연료비 부담 완화"),
        ("USDKRW", False, 0.8, "원/달러 {v:+.2f}% 상승 → 달러 결제 비용 부담",
         "원/달러 {v:+.2f}% 하락 → 달러 결제 비용 완화"),
        ("KOSPI", True, 0.5, "코스피 {v:+.2f}% 상승 → 경기민감 운송주 수급 개선",
         "코스피 {v:+.2f}% 하락 → 운송 수요 우려"),
    ],
    "기타": [
        ("KOSPI", True, 1.0, "코스피 {v:+.2f}% 상승 → 시장 전반 투자심리 개선",
         "코스피 {v:+.2f}% 하락 → 시장 전반 투자심리 위축"),
        ("NASDAQ", True, 1.0, "나스닥 {v:+.2f}% 상승 → 위험선호 개선",
         "나스닥 {v:+.2f}% 하락 → 위험선호 후퇴"),
    ],
}

SECTOR_NEWS_WORDS = {
    "반도체": ["반도체", "HBM", "D램", "낸드", "파운드리", "삼성전자", "하이닉스", "HPSP", "코미코", "AI칩"],
    "원전·에너지": ["원전", "체코", "한전", "전력", "변압기", "SMR", "두산에너빌", "송전", "전력망"],
    "건설": ["건설", "재건축", "분양", "토목", "부동산", "수주잔고", "PF"],
    "바이오": ["바이오", "제약", "임상", "FDA", "신약", "의료기기", "허가"],
    "금융": ["금리", "은행", "보험", "증권", "배당", "밸류업"],
    "제조·철강": ["철강", "자동차", "조선", "화학", "제조", "구리"],
    "자동차·조선": ["자동차", "조선", "수주", "선박", "전기차"],
    "2차전지": ["2차전지", "배터리", "양극재", "음극재", "전기차", "리튬"],
    "인터넷·SW": ["게임", "인터넷", "소프트웨어", "AI", "클라우드", "플랫폼"],
    "유통·식품": ["유통", "식품", "편의점", "소비", "면세"],
    "통신·운송": ["통신", "항공", "해운", "물류", "운임"],
    "기타": [],
}


def _sector_news(news_map: dict, sector: str) -> list[dict]:
    words = SECTOR_NEWS_WORDS.get(sector, []) + [sector.split("·")[0]]
    found: list[dict] = []
    for topic, items in (news_map or {}).items():
        for item in items or []:
            title = item.get("title", "")
            if any(w and w in title for w in words):
                found.append({**item, "topic": topic, "sentiment": _sentiment(title)})
    return found[:4]


def analyze_impact(macro: MacroBoard | None, news_map: dict,
                   holdings: list[dict] | None = None,
                   watch_sectors: list[str] | None = None) -> ImpactReport:
    """오늘의 거시 지표 + 뉴스 헤드라인 → 보유종목·관심섹터별 호재/주의/중립 해석.

    AI 없이 규칙·키워드 기반으로 동작한다(가중치 합산 + 근거 문장).
    holdings: [{code, name, sector}] — 없으면 관심 섹터 대표 종목으로 채운다.
    """
    macro = macro or MacroBoard()
    report = ImpactReport(generated_at=now_kst().strftime("%Y-%m-%d %H:%M"),
                          brief=macro_brief(macro) if macro.items else [])
    targets: list[dict] = []
    for h in holdings or []:
        code = clean_code(h.get("code", ""))
        targets.append({"code": code,
                        "name": h.get("name") or code,
                        "sector": h.get("sector") or _resolve_sector(code, h.get("name", ""), None)})
    if not targets:
        for sector in (watch_sectors or ["반도체", "원전·에너지", "바이오", "건설", "제조·철강"]):
            for rep in [i for i in UNIVERSE if i["sector"] == sector][:2]:
                targets.append({"code": rep["code"], "name": rep["name"], "sector": sector})

    for target in targets:
        sector = target["sector"] if target["sector"] in SECTOR_MACRO_RULES else "기타"
        row = ImpactRow(code=target["code"], name=target["name"], sector=target["sector"])
        score = 0.0
        for key, good_when_up, weight, up_reason, down_reason in SECTOR_MACRO_RULES[sector]:
            value = _fmt_value(key, macro)
            item = macro.get(key)
            if value is None or item is None or item.value is None:
                continue
            threshold = MACRO_THRESHOLD.get(key, 0.5)
            if abs(value) < threshold:
                continue
            direction = 1 if value > 0 else -1
            strength = min(1.6, max(0.6, abs(value) / threshold))
            sign = 1 if (direction > 0) == good_when_up else -1
            score += sign * weight * strength
            template = up_reason if direction > 0 else down_reason
            try:
                reason = template.format(v=value)
            except Exception:
                reason = template
            row.reasons.append(reason)
        news = _sector_news(news_map, sector)
        if news:
            row.news = news[:3]
            news_score = sum(1 if n["sentiment"] == "🟢 호재" else (-1 if n["sentiment"] == "🔴 악재" else 0)
                             for n in news[:3])
            score += max(-2.0, min(2.0, float(news_score)))
            for n in news[:2]:
                row.reasons.append(f"뉴스({n['sentiment'][2:] if len(n['sentiment']) > 2 else '중립'}) {n.get('title', '')}")
        row.score = round(score, 1)
        row.tag = "🟢 호재" if score >= 2.0 else ("🔴 주의" if score <= -2.0 else "⚪ 중립")
        report.rows.append(row)

    report.rows.sort(key=lambda r: -r.score)

    by_sector: dict[str, ImpactRow] = {}
    for row in report.rows:
        rep = by_sector.get(row.sector)
        if not rep:
            by_sector[row.sector] = ImpactRow(code="", name=row.sector, sector=row.sector,
                                             score=row.score, tag=row.tag,
                                             reasons=list(row.reasons[:2]), holdings=1)
        else:
            rep.holdings += 1
            rep.score = (rep.score + row.score) / 2
            rep.reasons = rep.reasons[:2]
            rep.tag = "🟢 호재" if rep.score >= 2.0 else ("🔴 주의" if rep.score <= -2.0 else "⚪ 중립")
    report.sectors = sorted(by_sector.values(), key=lambda r: -r.score)
    return report


def _fmt_value(key: str, macro: MacroBoard) -> float | None:
    item = macro.get(key)
    if not item:
        return None
    return item.change if key in ("US10Y", "KR10Y", "FED") else item.change_pct


def build_impact_prompt(report: ImpactReport, macro: MacroBoard) -> str:
    """AI 키가 있을 때만 쓰는 '이슈→종목' 해설 프롬프트(규칙 결과를 근거로 준다)."""
    macro_lines = [f"- {i.label}: {i.value_text()} ({i.change_text()})" for i in macro.items]
    rows = [f"- {r.name}({r.code}, {r.sector}) → {r.tag} / 점수 {r.score:+.1f} / "
            + " | ".join(r.reasons[:3]) for r in report.rows[:12]]
    return (
        "당신은 '투자금을 잃지 않는 것'을 최우선으로 하는 한국 가치투자 보조 AI입니다. "
        "아래 규칙 기반 분석을 참고해, 오늘의 거시 지표와 뉴스가 사용자의 보유·관심 종목에 "
        "어떤 영향을 주는지 6~8문장으로 정리하세요. 종목별로 '왜 그런지'를 숫자와 함께 쓰고, "
        "단정적 매수·매도 대신 확인할 점을 제시하세요. 데이터에 없는 사실은 만들지 마세요.\n\n"
        "[거시 지표]\n" + "\n".join(macro_lines) +
        "\n\n[규칙 기반 해석]\n" + "\n".join(rows) +
        "\n\n[시장 요약]\n" + "\n".join(report.brief)
    )


def generate_ai_text(prompt: str, provider: str | None = None) -> tuple[str | None, str]:
    """프롬프트 → AI 텍스트. 키가 없거나 실패하면 (None, 사유)."""
    conf_provider, key = ai_config()
    provider = (provider or conf_provider or "").lower()
    if not provider or not key:
        return None, "AI 키가 설정되지 않았습니다."
    try:
        if provider == "openai":
            data = _post_json("https://api.openai.com/v1/chat/completions",
                              {"model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                               "messages": [{"role": "user", "content": prompt}],
                               "temperature": 0.3},
                              {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            return data["choices"][0]["message"]["content"].strip(), ""
        if provider == "gemini":
            model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
            data = _post_json(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                {"contents": [{"parts": [{"text": prompt}]}]},
                {"Content-Type": "application/json"})
            return data["candidates"][0]["content"]["parts"][0]["text"].strip(), ""
        if provider == "openrouter":
            data = _post_json("https://openrouter.ai/api/v1/chat/completions",
                              {"model": os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
                               "messages": [{"role": "user", "content": prompt}],
                               "temperature": 0.3},
                              {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            return data["choices"][0]["message"]["content"].strip(), ""
        return None, f"지원하지 않는 AI 제공자: {provider}"
    except Exception as exc:
        return None, f"AI 해설 생성 실패({exc})"


# ===================================================================== v3 ====
# 10대 메뉴(종합현황·보유·재무·적정가·전략·시장·뉴스·위험·성과·설정) 지원 계층.
# v3의 첫 번째 원칙: '재무상태'(기업이 건강한가)와 '투자판단'(가격이 싼가)을 섞지 않는다.

HEALTH_BADGES = {
    "영업적자": "🔴 영업적자",
    "고부채": "🟠 고부채",
    "재무주의": "🟡 재무주의",
    "재무양호": "🟢 재무양호",
    "확인부족": "⚪ 확인부족",
}
HEALTH_WORDS = tuple(HEALTH_BADGES)
JUDGMENT_BADGES = {
    "저평가": "🟢 저평가",
    "적정": "🟡 적정",
    "고평가": "🔴 고평가",
    "판단보류": "⚪ 판단보류",
}
JUDGMENT_WORDS = tuple(JUDGMENT_BADGES)
ACTION_WORDS = ("매수검토", "보유", "신중", "판단보류")
# 두 축이 서로의 어휘를 쓰지 않도록 테스트에서 확인한다.
HEALTH_FORBIDDEN = ("저평가", "고평가", "적정가", "매수", "보유", "신중")
JUDGMENT_FORBIDDEN = ("적자", "부채", "재무")


def now_text() -> str:
    """화면에 표시할 갱신 시각(분 단위)."""
    return now_kst().strftime("%Y-%m-%d %H:%M")


def _amount_text(value: float | None, snap: StockSnapshot) -> str:
    """영업이익 등 금액 표기(KR은 억원 단위 입력 → 원화 요약)."""
    if value is None:
        return "-"
    if snap.market == "KR":
        return compact_money(value * 1e8)
    return compact_money(value, snap.currency or "USD")


def price_as_of_text(snap: StockSnapshot) -> str:
    """현재가 기준일/시각 — '2026-11-20 15:30 (장마감)' / '... (장중)'."""
    raw = str(snap.as_of or "").strip()
    if not raw:
        return "시세 기준 확인 불가"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", raw)
    if not m:
        return raw[:16].replace("T", " ")
    date_text = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    hh, mm = int(m.group(4)), int(m.group(5))
    today = now_kst().date().isoformat()
    close = (15, 30) if snap.market == "KR" else (16, 0)
    closed = date_text < today or (hh, mm) >= close
    # 해외 종목은 현지 시각 기준이므로 시간대를 단정하지 않고 그대로 표기한다.
    mark = "장마감" if (closed and snap.market == "KR") else ("장마감" if closed else "장중")
    return f"{date_text} {hh:02d}:{mm:02d} ({mark})"


def financial_basis_text(snap: StockSnapshot, history: "FinancialHistory | None" = None) -> str:
    """재무자료 기준 — '2025.12 확정 · 2026.12 추정'."""
    if history is not None and history.years:
        confirmed = [y for y, e in zip(history.years, history.estimated) if not e]
        estimated = [y for y, e in zip(history.years, history.estimated) if e]
        parts = []
        if confirmed:
            parts.append(f"{confirmed[-1]}.12 확정")
        if estimated:
            parts.append(f"{estimated[-1]}.12 추정")
        if not parts and history.years:
            parts.append(f"{history.years[-1]}.12")
        return " · ".join(parts)
    if snap.fiscal_year:
        year = re.search(r"(20\d{2})", str(snap.fiscal_year))
        return f"{year.group(1) if year else snap.fiscal_year}.12 확정"
    return "재무자료 기준 확인 불가"


def safety_margin(fair: float | None, price: float | None) -> float | None:
    """안전마진(%) = (적정가 - 현재가) / 적정가 * 100."""
    if not fair or price is None or fair == 0:
        return None
    return (fair - price) / fair * 100.0


def financial_health(snap: StockSnapshot) -> dict:
    """기업의 재무상태만 판정한다 — 가격(저평가/고평가) 표현 금지."""
    op, debt, roe, ocf = snap.op_income, snap.debt_ratio, snap.roe, snap.ocf
    is_fin = snap.sector == "금융"
    details: list[str] = []
    if op is not None:
        details.append(f"영업이익 {_amount_text(op, snap)} ({'흑자' if op > 0 else '적자'})")
    if roe is not None:
        details.append(f"ROE {roe:.1f}%")
    if debt is not None:
        details.append(f"부채비율 {debt:.1f}%" + (" (금융업 참고용)" if is_fin else ""))
    if ocf is not None:
        details.append("영업현금흐름 " + ("양수" if ocf > 0 else "마이너스"))
    if is_fin:
        details.append("금융업은 부채비율 대신 ROE·자본적정성으로 판단")

    label = "재무양호"
    reasons: list[str] = []
    if op is not None and op <= 0:
        label = "영업적자"
        reasons.append(f"영업이익 {_amount_text(op, snap)} — 본업에서 이익을 내지 못합니다.")
    elif (not is_fin) and debt is not None and debt > 200:
        label = "고부채"
        reasons.append(f"부채비율 {debt:.1f}%로 200% 상한을 넘습니다.")
    elif ((roe is not None and roe < 0)
          or (ocf is not None and ocf < 0)
          or ((not is_fin) and debt is not None and debt > 150)
          or (is_fin and roe is not None and roe < 3)
          or (op is not None and op > 0 and snap.op_margin is not None and snap.op_margin < 3)):
        label = "재무주의"
        if roe is not None and roe < 0:
            reasons.append(f"ROE {roe:.1f}%로 자본을 까먹고 있습니다.")
        if ocf is not None and ocf < 0:
            reasons.append("영업현금흐름이 마이너스입니다.")
        if debt is not None and not is_fin and debt > 150:
            reasons.append(f"부채비율 {debt:.1f}%로 150%를 넘습니다.")
        if is_fin and roe is not None and roe < 3:
            reasons.append(f"ROE {roe:.1f}%로 자본 효율이 낮습니다.")
        if op is not None and op > 0 and snap.op_margin is not None and snap.op_margin < 3:
            reasons.append(f"영업이익률 {snap.op_margin:.1f}%로 수익성이 얇습니다.")

    known = sum(1 for v in (op, debt, roe, ocf) if v is not None)
    if known == 0:
        label = "확인부족"
        reasons = ["영업이익·부채비율·ROE·현금흐름을 확인하지 못했습니다."]
    elif not reasons:
        reasons = ["확인된 재무 지표가 기준 안에 있습니다."]
    return {"label": label, "badge": HEALTH_BADGES[label], "reasons": reasons,
            "detail": " · ".join(details), "known": known}


def investment_judgment(snap: StockSnapshot, valuation: Valuation | None = None,
                        fair: float | None = None) -> dict:
    """가격(밸류에이션)만 판정한다 — 재무상태 어휘 금지."""
    margin = None
    if valuation is not None:
        margin = valuation.safety_margin
        fair = fair if fair is not None else valuation.fair
    if margin is None and fair:
        margin = safety_margin(fair, snap.price)
    if margin is None:
        return {"label": "판단보류", "badge": JUDGMENT_BADGES["판단보류"], "action": "판단보류",
                "margin": None, "full": f"{JUDGMENT_BADGES['판단보류']} · 판단보류",
                "reason": "적정가를 계산할 수 없어 가격 판단을 보류합니다."}
    if margin >= 20:
        label, action = "저평가", "매수검토"
        reason = f"적정가 대비 {margin:.1f}% 싸게 거래되고 있습니다."
    elif margin >= 0:
        label, action = "적정", "보유"
        reason = f"적정가 대비 {margin:.1f}% 여유가 있는 구간입니다."
    elif margin > -15:
        label, action = "고평가", "보유"
        reason = f"적정가보다 {abs(margin):.1f}% 비싸 신규 매수는 서두를 이유가 적습니다."
    else:
        label, action = "고평가", "신중"
        reason = f"적정가보다 {abs(margin):.1f}% 비싸 추격 매수는 피하는 구간입니다."
    return {"label": label, "badge": JUDGMENT_BADGES[label], "action": action, "margin": margin,
            "full": f"{JUDGMENT_BADGES[label]} · {action}", "reason": reason}


SECTOR_INVALIDATIONS: dict[str, list[str]] = {
    "원전·에너지": ["체코 등 주요 원전 수주 취소·지연 또는 수주잔고 2분기 연속 감소",
                    "전력기기(변압기 등) 수주 단가 10% 이상 하락"],
    "반도체": ["HBM·D램 고정거래가 2분기 연속 하락",
               "주요 고객사 설비투자(CAPEX) 가이던스 20% 이상 축소"],
    "건설": ["PF 우발채무 손실 확대·미착공 현장 증가", "수주잔고 20% 이상 감소"],
    "바이오": ["주력 파이프라인 임상 실패·허가 불허", "주요 품목 특허 만료·약가 인하"],
    "금융": ["ROE 5% 미만으로 하락", "대손충당금 급증·연체율 2분기 연속 상승"],
    "제조·철강": ["철강 스프레드 2분기 연속 축소", "주요 수요처(건설·자동차) 가동률 급락"],
    "자동차·조선": ["수주잔고 감소·선가 하락", "관세·통상 리스크로 주력 시장 판매 20% 이상 감소"],
    "2차전지": ["리튬·양극재 가격 급락 지속", "주요 고객사 발주 물량 20% 이상 축소"],
    "인터넷·SW": ["주력 서비스 이용자·광고 매출 2분기 연속 감소", "AI 투자 대비 수익화 지연"],
    "유통·식품": ["동일점 매출 2분기 연속 감소", "원가율 3%p 이상 상승"],
    "통신·운송": ["운임지수 30% 이상 하락", "유가 급등으로 연료비 부담 확대"],
    "기타": ["주력 사업 매출 2분기 연속 감소"],
}

DEBT_LIMIT = 200.0


def invalidation_checks(snap: StockSnapshot) -> list[dict]:
    """투자논리 훼손 조건 체크리스트 — 지금 이미 해당하는 항목은 triggered=True."""
    checks = [
        {"key": "op_loss", "label": "영업적자 전환",
         "detail": "분기 영업이익이 적자로 돌아서면 이익 성장 논리가 훼손된 것으로 봅니다.",
         "triggered": snap.op_income is not None and snap.op_income <= 0},
        {"key": "debt", "label": f"부채비율 {DEBT_LIMIT:.0f}% 상한 돌파",
         "detail": f"부채비율이 {DEBT_LIMIT:.0f}%를 넘으면 재무 리스크로 판단합니다.",
         "triggered": snap.debt_ratio is not None and snap.debt_ratio > DEBT_LIMIT},
        {"key": "ocf", "label": "영업현금흐름 적자 2분기 지속",
         "detail": "이익이 현금으로 들어오지 않으면 가치함정을 의심합니다.",
         "triggered": snap.ocf is not None and snap.ocf < 0},
        {"key": "eps", "label": "EPS 2년 연속 감소",
         "detail": "EPS가 무너지면 적정가(EPS×PER)의 기준을 다시 계산합니다.",
         "triggered": False},
    ]
    for i, text in enumerate(SECTOR_INVALIDATIONS.get(snap.sector, SECTOR_INVALIDATIONS["기타"])):
        checks.append({"key": f"sector_{i}", "label": "핵심 업황·수주 훼손",
                       "detail": text, "triggered": False})
    return checks


def build_strategy(snap: StockSnapshot, avg_price: float | None = None,
                   valuation: Valuation | None = None, filt: dict | None = None,
                   saved: dict | None = None) -> dict:
    """매매 전략 — 판단근거·1차/추가 매수·목표·손절·투자논리 훼손 조건을 한 묶음으로."""
    val = valuation if valuation is not None else valuate(snap)
    scn = build_scenarios(snap, avg_price=avg_price, valuation=val)
    saved = saved or {}
    cur = snap.currency or "KRW"
    buy1 = saved.get("buy1") or scn.get("buy1")
    buy2 = saved.get("buy2") or scn.get("buy2")
    target = saved.get("target") or scn.get("target")
    stop = saved.get("stop") or scn.get("stop")
    saved_checks = saved.get("checks") or {}
    checks = []
    for item in scn.get("invalidation_checks") or []:
        entry = dict(item)
        entry["checked"] = bool(saved_checks.get(entry["key"], entry.get("triggered", False)))
        checks.append(entry)

    def gap(value):
        if not snap.price or value is None:
            return None
        return (value / snap.price - 1) * 100

    basis = list(scn.get("judgement_basis") or [])
    if filt is not None:
        basis.append(f"가치점수 {filt.get('score')}점 · 필터 {filt.get('verdict')} — {filt.get('reason')}")
    basis.append(f"시세 기준 {price_as_of_text(snap)} · 재무자료 {val.finance_basis}")
    return {
        "code": snap.code, "name": snap.name or snap.code, "currency": cur,
        "price": snap.price, "avg_price": avg_price,
        "buy1": buy1, "buy2": buy2, "target": target, "stop": stop,
        "hold": scn.get("hold_price"),
        "buy1_gap": gap(buy1), "buy2_gap": gap(buy2), "target_gap": gap(target), "stop_gap": gap(stop),
        "basis": basis, "checks": checks, "memo": saved.get("memo", ""),
        "health": scn.get("health") or financial_health(snap),
        "judgment": scn.get("judgment") or investment_judgment(snap, val),
        "scenario": scn, "safety_margin": scn.get("safety_margin"),
        "updated": saved.get("updated", ""),
        "as_of": price_as_of_text(snap), "finance_basis": val.finance_basis,
    }


# ------------------------------------------------------- 보유·포트폴리오 ----
def _rate_for(snap: StockSnapshot, fx: float | None) -> float:
    return float(fx) if (snap.currency == "USD" and fx) else 1.0


def holdings_rows(holdings: list[dict], snapshots, fx: float | None = None,
                  dividends: list[dict] | None = None) -> list[dict]:
    """보유 종목 표 — 재무상태와 투자판단을 각각의 열로 분리한다."""
    if isinstance(snapshots, dict):
        by_code = {clean_code(k): v for k, v in snapshots.items()}
    else:
        by_code = {s.code: s for s in snapshots or []}
    div = dividend_summary(dividends or [])
    rows: list[dict] = []
    for h in holdings or []:
        code = normalize_code(str(h.get("code") or ""))
        snap = by_code.get(code) or StockSnapshot(code=code, name=str(h.get("name") or ""))
        qty = float(parse_num(h.get("qty")) or 0)
        avg = float(parse_num(h.get("avg_price")) or 0)
        rate = _rate_for(snap, fx)
        invested = qty * avg * rate
        price = snap.price
        value = qty * price * rate if price is not None else None
        profit = (value - invested) if value is not None else None
        ret = (profit / invested * 100) if (profit is not None and invested) else None
        val = valuate(snap)
        health = financial_health(snap)
        judgment = investment_judgment(snap, val)
        paid = div["by_code"].get(code) or {}
        rows.append({
            "종목": snap.name or h.get("name") or code, "코드": code, "섹터": snap.sector,
            "수량": qty, "평단가": avg, "현재가": price,
            "매입금액": invested, "평가금액": value, "평가손익": profit, "수익률(%)": ret,
            "비중(%)": None,
            "재무상태": health["badge"],
            "투자판단": judgment["full"],
            "PER": snap.per, "PBR": snap.pbr, "ROE(%)": snap.roe, "부채비율(%)": snap.debt_ratio,
            "배당수익률(%)": snap.dividend_yield,
            "수령배당(원)": paid.get("gross"),
            "적정가": val.fair, "안전마진(%)": val.safety_margin,
            "시세 기준": price_as_of_text(snap), "재무 기준": val.finance_basis,
            "통화": snap.currency,
            "_health": health, "_judgment": judgment, "_valuation": val,
        })
    total = sum(r["평가금액"] or 0 for r in rows)
    for r in rows:
        r["비중(%)"] = (r["평가금액"] / total * 100) if (total and r["평가금액"] is not None) else None
    return rows


def portfolio_summary(holdings: list[dict], snapshots, trades: list[dict] | None = None,
                      dividends: list[dict] | None = None, meta: dict | None = None,
                      fx: float | None = None) -> dict:
    """총자산·투자원금·평가손익·실현손익·배당·수수료·예수금을 한 번에 집계."""
    rows = holdings_rows(holdings, snapshots, fx=fx, dividends=dividends)
    invested = sum(r["매입금액"] for r in rows)
    market = sum(r["평가금액"] or 0 for r in rows)
    cash = float(parse_num((meta or {}).get("cash")) or 0)
    unrealized = market - invested
    ledger = trade_ledger(trades or [])
    divs = dividend_summary(dividends or [])
    realized_net = ledger["realized"] - ledger["fees"] - ledger["taxes"]
    total_return = unrealized + realized_net + divs["net"]
    capital = invested + ledger["sold_cost"]
    return {
        "rows": rows,
        "invested": invested, "market_value": market, "cash": cash,
        "total_assets": market + cash,
        "unrealized": unrealized,
        "unrealized_pct": (unrealized / invested * 100) if invested else None,
        "realized_gross": ledger["realized"], "realized": realized_net,
        "dividends_gross": divs["gross"], "dividend_tax": divs["tax"], "dividends": divs["net"],
        "fees": ledger["fees"], "taxes": ledger["taxes"] + divs["tax"],
        "total_return": total_return,
        "total_return_pct": (total_return / capital * 100) if capital else None,
        "capital_base": capital,
        "cash_ratio": (cash / (market + cash) * 100) if (market + cash) else None,
        "ledger": ledger, "dividend_detail": divs,
        "unmatched_sells": ledger["unmatched"],
    }


def portfolio_risk(summary: dict) -> dict:
    """포트폴리오 위험 진단 — 집중도·섹터 편중·재무 경고·손절선 근접."""
    rows = summary.get("rows") or []
    market = summary.get("market_value") or 0
    assets = summary.get("total_assets") or 0
    cash = summary.get("cash") or 0
    warnings: list[dict] = []

    def add(level: str, title: str, detail: str, action: str) -> None:
        warnings.append({"level": level, "title": title, "detail": detail, "action": action})

    # 1) 단일 종목 집중도
    if rows:
        top = max(rows, key=lambda r: r["평가금액"] or 0)
        weight = top["비중(%)"] or 0
        if weight >= 40:
            add("major", f"단일 종목 집중 — {top['종목']} {weight:.1f}%",
                f"{top['종목']} 한 종목이 보유 평가금액의 {weight:.1f}%입니다. "
                f"이 종목이 30% 빠지면 평가금액이 {weight * 0.3:.1f}%p 줄어듭니다.",
                f"{top['종목']} 비중을 25~30% 이하로 줄이고 다른 섹터로 분산하는 것을 검토하세요.")
        elif weight >= 25:
            add("warn", f"단일 종목 비중 높음 — {top['종목']} {weight:.1f}%",
                f"상위 종목 비중이 {weight:.1f}%입니다.", "추가 매수는 비중을 보며 나눠서 하세요.")

    # 2) 섹터 편중도
    sector_values: dict[str, float] = {}
    for r in rows:
        sector_values[r["섹터"]] = sector_values.get(r["섹터"], 0) + (r["평가금액"] or 0)
    sector_weights = {k: (v / market * 100 if market else 0) for k, v in sector_values.items()}
    if sector_weights:
        s_name, s_weight = max(sector_weights.items(), key=lambda kv: kv[1])
        if s_weight >= 50:
            add("major", f"섹터 편중 — {s_name} {s_weight:.1f}%",
                f"{s_name} 섹터가 평가금액의 절반을 넘습니다.",
                f"{s_name} 비중을 40% 이하로 낮추고, 원전·금융·바이오 등 다른 업황에 분산하세요.")
        elif s_weight >= 35:
            add("warn", f"섹터 쏠림 — {s_name} {s_weight:.1f}%",
                f"{s_name} 섹터 비중이 {s_weight:.1f}%입니다.", "신규 매수는 다른 섹터에서 찾는 편이 안전합니다.")

    # 3) 재무 경고
    for r in rows:
        label = (r.get("_health") or {}).get("label")
        weight = r["비중(%)"] or 0
        if label == "영업적자":
            add("major", f"영업적자 종목 보유 — {r['종목']} {weight:.1f}%",
                f"{(r.get('_health') or {}).get('reasons', [''])[0]}",
                "적자 지속 여부를 분기 실적으로 확인하고, 회복 근거가 없으면 비중을 줄이세요.")
        elif label == "고부채":
            add("warn", f"고부채 종목 — {r['종목']} 부채비율 {r['부채비율(%)']:.0f}%" if r["부채비율(%)"] else f"고부채 종목 — {r['종목']}",
                "부채비율 200% 초과로 금리·유동성 리스크가 큽니다.",
                "이자비용과 만기 구조를 확인하고 추가 매수는 보류하세요.")

    # 4) 손절선 근접
    for r in rows:
        avg, price = r["평단가"], r["현재가"]
        if not avg or price is None:
            continue
        stop = avg * (1 - STOP_LOSS_PCT / 100)
        if price <= stop:
            add("major", f"손절선 이탈 — {r['종목']}",
                f"현재가 {money(price, r['통화'])}가 평단 -{STOP_LOSS_PCT:.0f}%인 {money(stop, r['통화'])}를 밑돕니다.",
                "원칙(평단 -15%)을 지킬지, 실적 회복 근거로 보유할지 지금 정하세요.")
        elif price <= stop * 1.05:
            add("warn", f"손절선 근접 — {r['종목']}",
                f"현재가 {money(price, r['통화'])}가 손절선 {money(stop, r['통화'])}의 5% 이내입니다.",
                "다음 분기 실적과 업황 뉴스를 확인하며 대응을 미리 정해 두세요.")

    # 5) 고평가 구간 비중
    over = [r for r in rows if (r.get("_judgment") or {}).get("label") == "고평가" and (r["비중(%)"] or 0) >= 15]
    if over:
        names = ", ".join(f"{r['종목']} {r['비중(%)']:.0f}%" for r in over[:3])
        add("info", "고평가 구간 비중", f"적정가보다 비싼 종목이 {names} 있습니다.",
            "추가 매수 대신 목표가 도달 시 분할 익절을 준비하세요.")

    # 6) 현금 여력·분산
    if assets and cash / assets * 100 < 5:
        add("info", "예수금 여력 부족", f"총자산 중 현금 비중이 {cash / assets * 100:.1f}%입니다.",
            "급락 시 분할 매수 여력을 남겨 두는 편이 좋습니다.")
    if 0 < len(rows) < 3:
        add("warn", "분산 부족", f"보유 종목이 {len(rows)}개뿐입니다.",
            "섹터가 다른 3~5종목으로 나누면 개별 악재의 충격이 줄어듭니다.")

    major = sum(1 for w in warnings if w["level"] == "major")
    warn = sum(1 for w in warnings if w["level"] == "warn")
    info = sum(1 for w in warnings if w["level"] == "info")
    score = max(0, 100 - 20 * major - 8 * warn - 2 * info)
    level = "양호" if score >= 80 else ("보통" if score >= 60 else "주의")

    suggestions: list[str] = []
    if sector_weights:
        target = max(sector_weights.items(), key=lambda kv: kv[1])
        if target[1] >= 35:
            suggestions.append(f"{target[0]} {target[1]:.0f}% → 30~35% 이하로 축소")
    suggestions.append("단일 종목 25%, 단일 섹터 35%를 넘지 않게 유지")
    suggestions.append("현금 5~10%는 급락장 분할 매수용으로 남겨 두기")
    suggestions.append("손절선(평단 -15%)에 닿은 종목은 실적 근거를 다시 확인")
    return {"score": score, "level": level, "warnings": warnings,
            "sector_weights": sector_weights, "suggestions": suggestions,
            "major": major, "warn": warn, "info": info}


# ------------------------------------------------- 실현손익·배당·수수료 ----
def _norm_side(value) -> str:
    s = str(value or "").strip().lower()
    if s in ("매도", "sell", "s", "매도(sell)"):
        return "매도"
    return "매수"


def trade_ledger(trades: list[dict]) -> dict:
    """거래 내역 → 이동평균법 실현손익·수수료·세금·월별 집계.

    매수 기록 없이 매도만 있는 경우는 원가를 알 수 없으므로 실현손익에서 제외하고
    unmatched 로 따로 센다(없는 이익·손실을 만들지 않는다).
    """
    items: list[dict] = []
    for raw in trades or []:
        code = normalize_code(str(raw.get("code") or ""))
        qty = parse_num(raw.get("qty")) or 0
        price = parse_num(raw.get("price")) or 0
        if not code or qty <= 0 or price <= 0:
            continue
        items.append({
            "date": str(raw.get("date") or "").strip(),
            "month": str(raw.get("date") or "")[:7],
            "code": code, "name": str(raw.get("name") or "").strip(),
            "side": _norm_side(raw.get("side")),
            "qty": qty, "price": price,
            "fee": parse_num(raw.get("fee")) or 0.0,
            "tax": parse_num(raw.get("tax")) or 0.0,
            "memo": str(raw.get("memo") or "").strip(),
        })
    items.sort(key=lambda x: (x["date"], 0 if x["side"] == "매수" else 1, x["code"]))
    book: dict[str, dict] = {}
    sells: list[dict] = []
    by_month: dict[str, dict] = {}
    buy_amount = sell_amount = fees = taxes = 0.0
    unmatched = 0
    for it in items:
        month = by_month.setdefault(it["month"] or "-", {
            "월": it["month"] or "-", "매수금액": 0.0, "매도금액": 0.0,
            "실현손익": 0.0, "수수료": 0.0, "세금": 0.0})
        fees += it["fee"]
        taxes += it["tax"]
        month["수수료"] += it["fee"]
        month["세금"] += it["tax"]
        state = book.setdefault(it["code"], {"qty": 0.0, "cost": 0.0})
        if it["side"] == "매수":
            state["qty"] += it["qty"]
            state["cost"] += it["qty"] * it["price"]
            buy_amount += it["qty"] * it["price"]
            month["매수금액"] += it["qty"] * it["price"]
            continue
        matched_qty = min(it["qty"], state["qty"])
        avg_cost = (state["cost"] / state["qty"]) if state["qty"] else 0.0
        sold_cost = avg_cost * matched_qty
        realized = (matched_qty * it["price"]) - sold_cost
        is_matched = matched_qty > 0
        if not is_matched:
            unmatched += 1
        else:
            state["qty"] -= matched_qty
            state["cost"] -= sold_cost
            if state["qty"] < 1e-9:
                state["qty"], state["cost"] = 0.0, 0.0
            month["실현손익"] += realized
        sell_amount += it["qty"] * it["price"]
        month["매도금액"] += it["qty"] * it["price"]
        sells.append({
            "일자": it["date"], "월": it["month"] or "-", "종목": it["name"] or it["code"],
            "코드": it["code"], "수량": it["qty"], "매도단가": it["price"],
            "평균원가": round(avg_cost, 2) if is_matched else None,
            "매도원가": sold_cost if is_matched else None,
            "실현손익": realized if is_matched else None,
            "수수료": it["fee"], "세금": it["tax"],
            "원가확인": "확인" if is_matched else "매수기록 없음",
        })
    realized_total = sum(r["실현손익"] or 0 for r in sells)
    return {
        "items": items, "sells": sells, "realized": realized_total,
        "buy_amount": buy_amount, "sell_amount": sell_amount,
        "fees": fees, "taxes": taxes,
        "sold_cost": sum(r["매도원가"] or 0 for r in sells),
        "unmatched": unmatched, "by_month": by_month,
        "holdings_hint": {c: {"qty": s["qty"], "avg": (s["cost"] / s["qty"]) if s["qty"] else 0}
                          for c, s in book.items()},
    }


def dividend_summary(dividends: list[dict]) -> dict:
    """배당 내역 집계(총액·세금·순액·월별·종목별)."""
    rows: list[dict] = []
    by_month: dict[str, float] = {}
    by_code: dict[str, dict] = {}
    gross_total = tax_total = 0.0
    for raw in dividends or []:
        code = normalize_code(str(raw.get("code") or ""))
        gross = parse_num(raw.get("gross"))
        tax = parse_num(raw.get("tax")) or 0.0
        if gross is None:
            per_share = parse_num(raw.get("per_share")) or 0.0
            qty = parse_num(raw.get("qty")) or 0.0
            gross = per_share * qty if (per_share and qty) else 0.0
        net = parse_num(raw.get("net"))
        net = (gross - tax) if net is None else net
        date = str(raw.get("date") or "").strip()
        entry = {"date": date, "month": date[:7] or "-", "code": code,
                 "name": str(raw.get("name") or "").strip(), "gross": gross, "tax": tax,
                 "net": net, "per_share": parse_num(raw.get("per_share")),
                 "memo": str(raw.get("memo") or "").strip()}
        rows.append(entry)
        gross_total += gross
        tax_total += tax
        by_month[entry["month"]] = by_month.get(entry["month"], 0.0) + gross
        bucket = by_code.setdefault(code, {"name": entry["name"], "gross": 0.0, "tax": 0.0,
                                           "net": 0.0, "count": 0})
        bucket["gross"] += gross
        bucket["tax"] += tax
        bucket["net"] += net
        bucket["count"] += 1
        if entry["name"]:
            bucket["name"] = entry["name"]
    rows.sort(key=lambda r: r["date"], reverse=True)
    return {"rows": rows, "gross": gross_total, "tax": tax_total, "net": gross_total - tax_total,
            "by_month": by_month, "by_code": by_code}


def monthly_performance(ledger: dict, divs: dict) -> list[dict]:
    """월별 실현손익·배당·비용·순수익(입력된 거래·배당 기준)."""
    months = {m: dict(v) for m, v in (ledger.get("by_month") or {}).items()}
    for month, gross in (divs.get("by_month") or {}).items():
        bucket = months.setdefault(month, {"월": month, "매수금액": 0.0, "매도금액": 0.0,
                                           "실현손익": 0.0, "수수료": 0.0, "세금": 0.0})
        bucket["배당"] = gross
    for bucket in months.values():
        bucket.setdefault("배당", 0.0)
        bucket["순수익"] = (bucket.get("실현손익", 0.0) + bucket.get("배당", 0.0)
                            - bucket.get("수수료", 0.0) - bucket.get("세금", 0.0))
    return [months[m] for m in sorted(months) if m != "-"]


def quarterly_performance(monthly: list[dict]) -> list[dict]:
    """월별 → 분기별 집계."""
    quarters: dict[str, dict] = {}
    for row in monthly or []:
        month = row.get("월", "")
        if len(month) < 7:
            continue
        year, mon = month[:4], int(month[5:7])
        key = f"{year}-Q{(mon - 1) // 3 + 1}"
        bucket = quarters.setdefault(key, {"분기": key, "매수금액": 0.0, "매도금액": 0.0,
                                           "실현손익": 0.0, "배당": 0.0,
                                           "수수료·세금": 0.0, "순수익": 0.0})
        bucket["매수금액"] += row.get("매수금액", 0.0)
        bucket["매도금액"] += row.get("매도금액", 0.0)
        bucket["실현손익"] += row.get("실현손익", 0.0)
        bucket["배당"] += row.get("배당", 0.0)
        bucket["수수료·세금"] += row.get("수수료", 0.0) + row.get("세금", 0.0)
        bucket["순수익"] += row.get("순수익", 0.0)
    return [quarters[k] for k in sorted(quarters)]


def reconcile_holdings(holdings: list[dict], trades: list[dict]) -> list[dict]:
    """시트 보유 수량과 거래 내역 합계가 어긋나는 종목을 찾는다."""
    implied: dict[str, float] = {}
    for raw in trades or []:
        code = normalize_code(str(raw.get("code") or ""))
        qty = parse_num(raw.get("qty")) or 0
        if not code or qty <= 0:
            continue
        sign = 1 if _norm_side(raw.get("side")) == "매수" else -1
        implied[code] = implied.get(code, 0) + sign * qty
    held = {normalize_code(str(h.get("code") or "")): (parse_num(h.get("qty")) or 0)
            for h in holdings or []}
    out = []
    for code in sorted(set(implied) | set(held)):
        sheet_qty = held.get(code, 0.0)
        trade_qty = implied.get(code, 0.0)
        if abs(sheet_qty - trade_qty) > 1e-6:
            out.append({"코드": code, "보유 수량": sheet_qty, "거래내역 수량": trade_qty,
                        "차이": sheet_qty - trade_qty})
    return out


# ------------------------------------------- 거래·배당·관심종목 저장소 ----
EXTRA_FILES = {
    "trades": "trades.json",
    "dividends": "dividends.json",
    "watchlist": "watchlist.json",
    "strategies": "strategies.json",
    "portfolio_meta": "portfolio_meta.json",
    "performance": "performance.json",
}


def _extra_path(kind: str) -> Path:
    return Path(os.environ.get("VI_DATA_DIR") or (BASE_DIR / "data")) / EXTRA_FILES[kind]


def extra_sheet_enabled(kind: str = "trades") -> bool:
    """기존 Apps Script가 임의 탭 이름을 받아 주는 경우에만 켠다(Secrets VI_EXTRA_SHEET=1).

    확인되지 않은 상태에서 시트에 쓰면 보유종목·추천추적 탭을 건드릴 수 있으므로
    기본값은 로컬 저장이다. 켜 두어도 시트 오류가 나면 조용히 로컬로 되돌아간다.
    """
    return os.environ.get("VI_EXTRA_SHEET", "").strip() == "1" and storage_mode() == "gsheet"


def normalize_extra_row(kind: str, row: dict) -> dict:
    if kind == "trades":
        return {
            "date": str(row.get("일자") or row.get("date") or "").strip(),
            "code": normalize_code(str(row.get("종목코드") or row.get("code") or "")),
            "name": str(row.get("종목명") or row.get("name") or "").strip(),
            "side": _norm_side(row.get("구분") or row.get("side")),
            "qty": parse_num(row.get("수량")) if "수량" in row else parse_num(row.get("qty")),
            "price": parse_num(row.get("단가")) if "단가" in row else parse_num(row.get("price")),
            "fee": parse_num(row.get("수수료")) if "수수료" in row else parse_num(row.get("fee")),
            "tax": parse_num(row.get("세금")) if "세금" in row else parse_num(row.get("tax")),
            "memo": str(row.get("메모") or row.get("memo") or "").strip(),
        }
    if kind == "dividends":
        return {
            "date": str(row.get("일자") or row.get("date") or "").strip(),
            "code": normalize_code(str(row.get("종목코드") or row.get("code") or "")),
            "name": str(row.get("종목명") or row.get("name") or "").strip(),
            "gross": parse_num(row.get("배당금")) if "배당금" in row else parse_num(row.get("gross")),
            "tax": parse_num(row.get("세금")) if "세금" in row else parse_num(row.get("tax")),
            "per_share": parse_num(row.get("주당배당금")) if "주당배당금" in row else parse_num(row.get("per_share")),
            "memo": str(row.get("메모") or row.get("memo") or "").strip(),
        }
    if kind == "watchlist":
        return {
            "code": normalize_code(str(row.get("종목코드") or row.get("code") or "")),
            "name": str(row.get("종목명") or row.get("name") or "").strip(),
            "added": str(row.get("등록일") or row.get("added") or today_str()),
            "note": str(row.get("메모") or row.get("note") or "").strip(),
        }
    return {str(k): v for k, v in (row or {}).items()}


def _extra_sheet_record(kind: str, row: dict) -> dict:
    if kind == "trades":
        return {"일자": row.get("date", ""), "종목코드": row.get("code", ""),
                "종목명": row.get("name", ""), "구분": row.get("side", "매수"),
                "수량": row.get("qty"), "단가": row.get("price"),
                "수수료": row.get("fee"), "세금": row.get("tax"), "메모": row.get("memo", "")}
    if kind == "dividends":
        return {"일자": row.get("date", ""), "종목코드": row.get("code", ""),
                "종목명": row.get("name", ""), "배당금": row.get("gross"),
                "세금": row.get("tax"), "메모": row.get("memo", "")}
    if kind == "watchlist":
        return {"종목코드": row.get("code", ""), "종목명": row.get("name", ""),
                "등록일": row.get("added", ""), "메모": row.get("note", "")}
    return dict(row or {})


def load_extra(kind: str):
    """거래·배당·관심종목·전략·예수금 저장소(기본 로컬 JSON, 시트는 선택)."""
    if kind not in EXTRA_FILES:
        raise ValueError(f"알 수 없는 저장 종류: {kind}")
    if kind in ("trades", "dividends", "watchlist") and extra_sheet_enabled(kind):
        try:
            data = _gsheet_call("GET", kind)
            records = data.get("records") or []
            return [normalize_extra_row(kind, r) for r in records if isinstance(r, dict)]
        except StorageError:
            pass  # 시트 미지원 → 로컬로 계속
    path = _extra_path(kind)
    if not path.exists():
        return {} if kind == "portfolio_meta" else ({} if kind == "strategies" else [])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StorageError(f"{EXTRA_FILES[kind]} 를 읽지 못했습니다({exc})") from exc
    if kind in ("portfolio_meta", "strategies"):
        return payload if isinstance(payload, dict) else {}
    rows = payload if isinstance(payload, list) else []
    return [normalize_extra_row(kind, r) for r in rows if isinstance(r, dict)]


def save_extra(kind: str, payload):
    """거래·배당·관심종목·전략·예수금 저장(시트가 켜져 있으면 시트, 실패하면 로컬)."""
    if kind not in EXTRA_FILES:
        raise ValueError(f"알 수 없는 저장 종류: {kind}")
    if kind in ("trades", "dividends", "watchlist") and extra_sheet_enabled(kind):
        try:
            _gsheet_call("POST", kind, [_extra_sheet_record(kind, r) for r in payload])
            return payload
        except StorageError:
            pass  # 시트 미지원 → 로컬 저장으로 계속
    path = _extra_path(kind)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        raise StorageError(f"{EXTRA_FILES[kind]} 에 저장하지 못했습니다({exc})") from exc
    return payload


def load_meta() -> dict:
    raw = load_extra("portfolio_meta")
    meta = raw if isinstance(raw, dict) else {}
    return {"cash": float(parse_num(meta.get("cash")) or 0),
            "cash_note": str(meta.get("cash_note") or ""),
            "updated": str(meta.get("updated") or "")}


def save_meta(cash: float, note: str = "") -> dict:
    meta = {"cash": float(cash or 0), "cash_note": str(note or ""), "updated": now_text()}
    save_extra("portfolio_meta", meta)
    return meta


def load_watchlist() -> list[dict]:
    return [r for r in load_extra("watchlist") if r.get("code")]


def add_watchlist(code: str, name: str = "", note: str = "") -> tuple[bool, list[dict]]:
    rows = load_watchlist()
    target = normalize_code(code)
    if any(r["code"] == target for r in rows):
        return False, rows
    rows.append({"code": target, "name": name or target, "added": today_str(), "note": note})
    save_extra("watchlist", rows)
    return True, rows


def remove_watchlist(code: str) -> list[dict]:
    target = normalize_code(code)
    rows = [r for r in load_watchlist() if r["code"] != target]
    save_extra("watchlist", rows)
    return rows


def load_strategies() -> dict:
    payload = load_extra("strategies")
    return payload if isinstance(payload, dict) else {}


def save_strategy(code: str, data: dict) -> dict:
    all_rows = load_strategies()
    entry = {k: data.get(k) for k in ("buy1", "buy2", "target", "stop", "memo", "checks")}
    entry["updated"] = now_text()
    all_rows[normalize_code(code)] = entry
    save_extra("strategies", all_rows)
    return entry


def load_performance() -> list[dict]:
    rows = load_extra("performance")
    return sorted([r for r in rows if isinstance(r, dict) and r.get("date")],
                  key=lambda r: r["date"])


def record_performance(entry: dict) -> list[dict]:
    rows = [r for r in load_performance() if r.get("date") != entry.get("date")]
    rows.append(entry)
    rows.sort(key=lambda r: r["date"])
    save_extra("performance", rows[-400:])
    return rows


# --------------------------------------------------- CSV · JSON 백업 ----
def rows_to_csv(rows: list[dict], columns: list[str] | None = None) -> str:
    """엑셀에서 바로 열리는 CSV(UTF-8 BOM)."""
    import csv
    import io
    rows = rows or []
    columns = columns or (list(rows[0].keys()) if rows else [])
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    return "\ufeff" + buf.getvalue()


def csv_to_rows(text: str) -> list[dict]:
    import csv
    import io
    if not text:
        return []
    clean = text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(clean))
    return [dict(r) for r in reader]


def backup_bundle(include_sheet: bool = True) -> dict:
    bundle = {
        "app": "가치투자 스튜디오 v3", "version": 3, "exported_at": now_text(),
        "trades": load_extra("trades"), "dividends": load_extra("dividends"),
        "watchlist": load_extra("watchlist"), "meta": load_extra("portfolio_meta"),
        "strategies": load_extra("strategies"), "performance": load_extra("performance"),
    }
    if include_sheet:
        try:
            bundle["holdings"] = load_holdings(force=True)
        except StorageError:
            bundle["holdings"] = []
        try:
            bundle["picks"] = load_picks(force=True)
        except StorageError:
            bundle["picks"] = []
    return bundle


def restore_bundle(payload: dict, include_holdings: bool = True) -> dict:
    """백업 JSON 복원. 시트 보유종목·추천추적은 include_holdings=True 일 때만 덮어쓴다."""
    counts: dict[str, int] = {}
    for kind in ("trades", "dividends", "watchlist", "strategies", "performance"):
        if kind in payload:
            value = payload[kind]
            save_extra(kind, value)
            counts[kind] = len(value) if isinstance(value, list) else 1
    meta = payload.get("meta", payload.get("portfolio_meta"))
    if isinstance(meta, dict):
        save_extra("portfolio_meta", meta)
        counts["meta"] = 1
    if include_holdings and isinstance(payload.get("holdings"), list):
        rows = [r for r in (_normalize_holding(x) for x in payload["holdings"] if isinstance(x, dict)) if r]
        save_holdings(rows)
        counts["holdings"] = len(rows)
    if include_holdings and isinstance(payload.get("picks"), list):
        rows = [r for r in (_normalize_pick(x) for x in payload["picks"] if isinstance(x, dict)) if r]
        save_picks(rows)
        counts["picks"] = len(rows)
    return counts


# --------------------------------------------------- 시장 뉴스 분류 ----
def digest_from_news_map(news_map: dict, limit: int = 40) -> NewsDigest:
    """주제별 뉴스 묶음 → 카테고리·호재/악재가 붙은 NewsDigest(실제 URL·게시시각만)."""
    digest = NewsDigest(generated_at=now_text())
    seen: set[str] = set()
    for topic, items in (news_map or {}).items():
        for item in items or []:
            title = str(item.get("title") or "").strip()
            link = str(item.get("link") or "").strip()
            if not title or not link.startswith("http") or title in seen:
                continue
            seen.add(title)
            dt = _parse_pubdate(item.get("published") or "")
            published = dt.strftime("%Y-%m-%d %H:%M") if dt else (str(item.get("published") or "")[:16] or "시각 미상")
            age = ((now_kst() - dt).total_seconds() / 3600) if dt else None
            category = _categorize(title, age)
            if topic in CATEGORY_ORDER:
                category = topic
            digest.items.append(NewsItem(title=title, link=link, published=published,
                                         source=str(item.get("source") or "뉴스"),
                                         category=category, sentiment=_sentiment(title)))
    digest.items.sort(key=lambda i: i.published, reverse=True)
    digest.items = digest.items[:limit]
    if not digest.items:
        digest.note = "뉴스를 가져오지 못했습니다(네트워크 확인)."
    return digest


def recent_news(digest: NewsDigest, hours: int = 24) -> list[NewsItem]:
    """최근 N시간 이내 게시된 기사만(시각 미상은 제외)."""
    cutoff = now_kst() - timedelta(hours=hours)
    out = []
    for item in digest.items:
        try:
            stamp = datetime.strptime(item.published, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        except Exception:
            continue
        if stamp >= cutoff:
            out.append(item)
    return out
