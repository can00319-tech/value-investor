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
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
GOOGLE_NEWS = "https://news.google.com/rss/search"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}
TIMEOUT = (4.0, 12.0)

SECTOR_ORDER = ["원전·에너지", "건설", "반도체", "바이오", "금융", "제조·철강", "기타"]


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


_UNIT = {"조": 1_000_000_000_000, "억": 100_000_000, "만": 10_000}


def parse_korean_amount(text) -> float | None:
    """'1조 4,362억' → 1,436,200,000,000 / '8,922억' → 89,220,000,000."""
    if text is None:
        return None
    s = str(text).replace(",", "").replace(" ", "")
    if s in {"", "-", "N/A"}:
        return None
    total = 0.0
    found = False
    for m in re.finditer(r"([\d.]+)(조|억|만)?", s):
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
    market_cap: float | None = None
    high52: float | None = None
    low52: float | None = None
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
    """종목명·종목코드·티커 입력 → {code, name, market}. 찾지 못하면 None."""
    q = str(text or "").strip()
    if not q:
        return None
    if re.fullmatch(r"\d{6}(\.(KS|KQ))?", q.upper()):
        return {"code": clean_code(q), "name": "", "market": "KR"}
    for item in UNIVERSE:
        if item["name"].replace(" ", "").lower() == q.replace(" ", "").lower():
            return {"code": item["code"], "name": item["name"], "market": item["market"]}
    if re.fullmatch(r"[A-Za-z][A-Za-z.\-]{0,6}", q):
        return {"code": q.upper(), "name": "", "market": "US"}
    data = _get_json("https://ac.stock.naver.com/ac", {"q": q, "target": "stock"})
    for item in (data or {}).get("items") or []:
        code = clean_code(item.get("code") or "")
        name = str(item.get("name") or "")
        if re.fullmatch(r"\d{6}", code) and (q.replace(" ", "") in name.replace(" ", "") or name == q):
            return {"code": code, "name": name, "market": "KR"}
    return None


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


def _get_json(url: str, params: dict | None = None) -> dict | None:
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


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


def _naver_integration(code: str) -> dict:
    """네이버 종합 정보: PER·PBR·EPS·BPS·시총·52주 최고/최저."""
    out: dict = {}
    data = _get_json(f"{NAVER_BASE}/{code}/integration")
    if not data:
        return out
    if not out.get("name"):
        out["name"] = data.get("stockName") or ""
    infos = {t.get("code"): t.get("value") for t in data.get("totalInfos") or []}
    out["per"] = parse_num(infos.get("per"))
    out["pbr"] = parse_num(infos.get("pbr"))
    out["eps"] = parse_num(infos.get("eps"))
    out["bps"] = parse_num(infos.get("bps"))
    out["market_cap"] = parse_korean_amount(infos.get("marketValue"))
    out["high52"] = parse_num(infos.get("highPriceOf52Weeks"))
    out["low52"] = parse_num(infos.get("lowPriceOf52Weeks"))
    if out.get("price") is None:
        out["price"] = parse_num(infos.get("lastClosePrice"))
    return out


def _naver_annual(code: str, period: str = "annual") -> dict:
    """네이버 연간 재무: 영업이익·매출·ROE·부채비율·영업이익률(+전년 대비 성장률).

    추정치(isConsensus='Y')가 아닌 가장 최근 확정 실적 열을 쓴다.
    """
    out: dict = {}
    data = _get_json(f"{NAVER_BASE}/{code}/finance/{period}")
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
    out["op_margin"] = value_at("영업이익률", cur)
    out["roe"] = value_at("ROE", cur)
    out["debt_ratio"] = value_at("부채비율", cur)
    out["per_annual"] = value_at("PER", cur)
    out["pbr_annual"] = value_at("PBR", cur)
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
        "원전·에너지": ["에너빌", "원전", "한전", "전력", "파워", "일렉트릭", "에너지"],
        "건설": ["건설", "E&A", "이앤에이"],
        "반도체": ["반도체", "마이크론", "코미코", "하이닉스", "전자", "원익", "SEMI"],
        "바이오": ["바이오", "약품", "제약", "클래시스", "메디"],
        "금융": ["금융", "은행", "지주", "증권", "보험", "화재"],
        "제조·철강": ["제철", "포스코", "철강", "기아", "자동차", "화학"],
    }
    for sector, words in keyword.items():
        if any(w in text for w in words):
            return sector
    return "기타"


@memoize(ttl=300)
def fetch_snapshot(code: str, sector: str | None = None, name_hint: str | None = None) -> StockSnapshot:
    """한 종목의 시세·재무 요약. 네이버 → yfinance → 야후 차트 순으로 보완한다.

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
        for attr in ("per", "pbr", "eps", "bps", "market_cap", "high52", "low52"):
            if getattr(snap, attr) is None and integ.get(attr) is not None:
                setattr(snap, attr, integ[attr])
        if integ:
            snap.sources.append("네이버 금융 투자지표")

        annual = _naver_annual(code)
        for attr in ("roe", "debt_ratio", "op_income", "op_margin", "revenue",
                     "revenue_yoy", "op_yoy", "fiscal_year"):
            if annual.get(attr) is not None:
                setattr(snap, attr, annual[attr])
        if snap.per is None and annual.get("per_annual") is not None:
            snap.per = annual["per_annual"]
        if snap.pbr is None and annual.get("pbr_annual") is not None:
            snap.pbr = annual["pbr_annual"]
        if annual:
            snap.sources.append("네이버 금융 재무제표")

        suffix = "KQ" if "코스닥" in str(quote.get("exchange", "")) else "KS"
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
        info = _yfinance_info([code])
        if info:
            _from_yf(info, snap)
            snap.sources.append("yfinance")
        fallback = _yahoo_quote(code)
        if fallback.get("price"):
            if snap.price is None:
                snap.price = fallback["price"]
            if snap.change_pct is None:
                snap.change_pct = fallback.get("change_pct")
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


def fetch_snapshots(items: list[dict], workers: int = 8) -> list[StockSnapshot]:
    """여러 종목 병렬 조회. items: [{code, sector, name}]"""
    results: dict[str, StockSnapshot] = {}
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as pool:
        futures = {
            pool.submit(fetch_snapshot, it["code"], it.get("sector"), it.get("name")): it["code"]
            for it in items
        }
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                results[code] = fut.result()
            except Exception as exc:  # 방어적: 한 종목 실패가 전체를 막지 않는다
                snap = StockSnapshot(code=clean_code(code))
                snap.notes.append(f"조회 실패: {exc}")
                results[code] = snap
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


def build_scenarios(snap: StockSnapshot, avg_price: float | None = None) -> dict:
    """상승 / 기본 / 경계 3단 시나리오와 각 단계의 대응 기준을 만든다.

    - 상승: 목표가(현재가 +20%와 적정가 중 높은 값, 최대 +60%) 도달 → 분할 익절
    - 기본: 적정가 밴드(EPS×12·BPS×1) 내 → 보유하며 실적 확인
    - 경계: 평단 -15% 이탈 · 영업이익 적자 전환 · 부채비율 200% 초과 → 비중 축소
    """
    price = snap.price
    cur = snap.currency or "KRW"
    band = fair_values(snap)
    fair = band["fair"]
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
            "basis": "적정가 = (BPS×1배 + EPS×12배) 평균" if fair else "EPS·BPS 확인 불가",
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
    per_grade = grade(snap.per, 10, 20) if (snap.per or 0) > 0 else ("적자 또는 미확인", "⚪")
    out.append(("PER", *per_grade))
    pbr_grade = grade(snap.pbr, 1.0, 2.0) if snap.pbr is not None else ("확인 불가", "⚪")
    out.append(("PBR", *pbr_grade))
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


def _normalize_holding(row: dict) -> dict | None:
    code = str(row.get("종목코드") or row.get("code") or "").strip()
    if not code:
        return None
    qty = parse_num(row.get("수량") if "수량" in row else row.get("qty"))
    avg = parse_num(row.get("평단가") if "평단가" in row else row.get("avg_price"))
    return {
        "code": clean_code(code),
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
        "code": clean_code(code),
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
UNIVERSE: list[dict] = [
    # 원전·에너지
    {"code": "034020", "name": "두산에너빌리티", "sector": "원전·에너지", "market": "KR"},
    {"code": "052690", "name": "한전기술", "sector": "원전·에너지", "market": "KR"},
    {"code": "051600", "name": "한전KPS", "sector": "원전·에너지", "market": "KR"},
    {"code": "267260", "name": "HD현대일렉트릭", "sector": "원전·에너지", "market": "KR"},
    {"code": "010120", "name": "LS ELECTRIC", "sector": "원전·에너지", "market": "KR"},
    {"code": "006910", "name": "보성파워텍", "sector": "원전·에너지", "market": "KR"},
    # 건설
    {"code": "000720", "name": "현대건설", "sector": "건설", "market": "KR"},
    {"code": "047040", "name": "대우건설", "sector": "건설", "market": "KR"},
    {"code": "006360", "name": "GS건설", "sector": "건설", "market": "KR"},
    {"code": "028050", "name": "삼성E&A", "sector": "건설", "market": "KR"},
    # 반도체
    {"code": "005930", "name": "삼성전자", "sector": "반도체", "market": "KR"},
    {"code": "000660", "name": "SK하이닉스", "sector": "반도체", "market": "KR"},
    {"code": "042700", "name": "한미반도체", "sector": "반도체", "market": "KR"},
    {"code": "240810", "name": "원익IPS", "sector": "반도체", "market": "KR"},
    {"code": "067310", "name": "하나마이크론", "sector": "반도체", "market": "KR"},
    {"code": "183300", "name": "코미코", "sector": "반도체", "market": "KR"},
    {"code": "009150", "name": "삼성전기", "sector": "반도체", "market": "KR"},
    # 바이오
    {"code": "207940", "name": "삼성바이오로직스", "sector": "바이오", "market": "KR"},
    {"code": "000100", "name": "유한양행", "sector": "바이오", "market": "KR"},
    {"code": "069620", "name": "대웅제약", "sector": "바이오", "market": "KR"},
    {"code": "302440", "name": "SK바이오사이언스", "sector": "바이오", "market": "KR"},
    {"code": "214150", "name": "클래시스", "sector": "바이오", "market": "KR"},
    {"code": "004310", "name": "현대약품", "sector": "바이오", "market": "KR"},
    # 금융
    {"code": "105560", "name": "KB금융", "sector": "금융", "market": "KR"},
    {"code": "055550", "name": "신한지주", "sector": "금융", "market": "KR"},
    {"code": "316140", "name": "우리금융지주", "sector": "금융", "market": "KR"},
    {"code": "138040", "name": "메리츠금융지주", "sector": "금융", "market": "KR"},
    {"code": "005830", "name": "DB손해보험", "sector": "금융", "market": "KR"},
    # 제조·철강
    {"code": "005490", "name": "POSCO홀딩스", "sector": "제조·철강", "market": "KR"},
    {"code": "004020", "name": "현대제철", "sector": "제조·철강", "market": "KR"},
    {"code": "005380", "name": "현대차", "sector": "제조·철강", "market": "KR"},
    {"code": "000270", "name": "기아", "sector": "제조·철강", "market": "KR"},
    # 미국
    {"code": "NVDA", "name": "NVIDIA", "sector": "반도체", "market": "US"},
    {"code": "AMD", "name": "AMD", "sector": "반도체", "market": "US"},
    {"code": "MU", "name": "Micron", "sector": "반도체", "market": "US"},
    {"code": "INTC", "name": "Intel", "sector": "반도체", "market": "US"},
    {"code": "AVGO", "name": "Broadcom", "sector": "반도체", "market": "US"},
    {"code": "CEG", "name": "Constellation Energy", "sector": "원전·에너지", "market": "US"},
    {"code": "VST", "name": "Vistra", "sector": "원전·에너지", "market": "US"},
    {"code": "CCJ", "name": "Cameco", "sector": "원전·에너지", "market": "US"},
    {"code": "PFE", "name": "Pfizer", "sector": "바이오", "market": "US"},
    {"code": "JNJ", "name": "Johnson & Johnson", "sector": "바이오", "market": "US"},
    {"code": "MRK", "name": "Merck", "sector": "바이오", "market": "US"},
    {"code": "AMGN", "name": "Amgen", "sector": "바이오", "market": "US"},
    {"code": "CAT", "name": "Caterpillar", "sector": "건설", "market": "US"},
    {"code": "VMC", "name": "Vulcan Materials", "sector": "건설", "market": "US"},
    {"code": "JPM", "name": "JPMorgan Chase", "sector": "금융", "market": "US"},
    {"code": "BAC", "name": "Bank of America", "sector": "금융", "market": "US"},
    {"code": "MET", "name": "MetLife", "sector": "금융", "market": "US"},
    {"code": "NUE", "name": "Nucor", "sector": "제조·철강", "market": "US"},
    {"code": "X", "name": "United States Steel", "sector": "제조·철강", "market": "US"},
    {"code": "F", "name": "Ford", "sector": "제조·철강", "market": "US"},
]

DEFAULT_UNIVERSE_CODES = [
    "034020", "052690", "051600", "010120",      # 원전
    "000720", "047040", "028050",                # 건설
    "005930", "042700", "183300",                # 반도체
    "207940", "000100", "214150",                # 바이오
    "105560", "055550",                          # 금융
    "005490", "004020",                          # 제조
    "CEG", "JPM", "PFE",                         # 미국 대표
]


def universe_items(codes: list[str] | None = None, sectors: list[str] | None = None) -> list[dict]:
    """코드 목록 또는 섹터 목록으로 조회 대상 종목을 고른다(없으면 기본 세트)."""
    by_code = {item["code"]: item for item in UNIVERSE}
    if codes:
        out = []
        for code in codes:
            c = clean_code(code)
            item = by_code.get(c) or {"code": c, "name": "", "sector": "기타", "market": detect_market(c)}
            out.append(dict(item))
        return out
    items = [dict(i) for i in UNIVERSE]
    if sectors:
        items = [i for i in items if i["sector"] in sectors]
    else:
        want = set(DEFAULT_UNIVERSE_CODES)
        items = [i for i in items if i["code"] in want]
    return items


def scan_universe(items: list[dict], rules: FilterRules) -> list[dict]:
    """유니버스 스캔 → 필터 결과가 붙은 행 목록(점수 내림차순)."""
    snaps = fetch_snapshots(items)
    rows = []
    for snap in snaps:
        verdict = run_filter(snap, rules)
        rows.append({"snap": snap, "filter": verdict})
    rows.sort(key=lambda r: (-(r["filter"]["passed"]), -r["filter"]["score"], r["snap"].name))
    return rows
