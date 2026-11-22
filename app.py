"""📈 가치투자 스튜디오 v3 — 주식투자 종합관리 시스템(10대 메뉴).

동철님의 투자 원칙("저가 우량주를 찾고, 투자금을 잃지 않는 것")을 기준으로
보유·후보·전략·위험·성과를 한 화면에서 관리하는 Streamlit 앱.

v3 핵심
  · 적정주가: 적자기업은 PER을 완전히 제외하고 PBR·DCF로만 평가 + 안전마진(%)
  · 재무상태(기업 건전성)와 투자판단(가격)을 열·배지로 완전 분리
  · 실현손익·배당·수수료·세금·예수금 반영(거래/배당/예수금은 로컬 저장 + CSV/JSON 백업)
  · 모든 표·상세에 현재가 기준일/시각, 재무자료 기준, 마지막 갱신 시각 표시
  · 종목별 판단근거·1차/추가 매수·목표·손절·투자논리 훼손 조건까지 전략으로 관리
  · 10대 메뉴: 종합현황 · 보유 · 재무 · 적정가 · 전략 · 시장 · 뉴스 · 위험 · 성과 · 설정

실행:  streamlit run app.py
배포:  Streamlit Community Cloud + 구글 시트(보유종목·추천추적) 연동
"""

from __future__ import annotations

import importlib
import json
from datetime import date

import pandas as pd
import streamlit as st

import vi_core as core

# Streamlit Cloud는 파일 변경 시 앱 스크립트만 다시 실행하고 import된 모듈은 캐시된 채로 둔다.
# 배포 직후 구버전 vi_core가 남아 있으면 v3 기능이 없는 상태로 오류가 나므로, 한 번 새로 읽는다.
if not hasattr(core, "now_text"):  # pragma: no cover - 배포 환경 전용 안전장치
    core = importlib.reload(core)

st.set_page_config(page_title="가치투자 스튜디오 v3", page_icon="📈", layout="wide")

try:  # Streamlit Cloud Secrets → 환경변수 (로컬 실행 시에는 없음)
    core.apply_secrets(st.secrets)
except Exception:
    pass

MENU_LABELS = [
    "1. 종합 투자현황",
    "2. 보유종목 관리",
    "3. 기업 재무분석",
    "4. 적정주가 분석",
    "5. 매매 전략 관리",
    "6. 시장 및 산업 분석",
    "7. 뉴스 및 공시 분석",
    "8. 위험관리 및 투자진단",
    "9. 투자성과 분석",
    "10. 설정 및 데이터 관리",
]

CSS = """
<style>
  .block-container {padding-top: 1.0rem; max-width: 1380px;}
  .vi-header {
    background: linear-gradient(115deg, #0b1b34 0%, #14294b 55%, #1d3f74 100%);
    border-radius: 16px; padding: 20px 24px 16px 24px; margin-bottom: 12px;
    box-shadow: 0 6px 22px rgba(11, 27, 52, .25);
  }
  .vi-header h1 {color: #ffffff; font-size: 1.6rem; margin: 0 0 .25rem 0; letter-spacing: -.4px;}
  .vi-header p {color: #b9cbe8; margin: 0; font-size: .9rem;}
  .vi-header .tag {
    display: inline-block; margin-top: .5rem; margin-right: 6px; padding: 3px 10px; border-radius: 999px;
    background: rgba(255,255,255,.12); color: #e8f0ff; font-size: .77rem;
  }
  .vi-card {
    border: 1px solid #e3e8f0; border-left: 5px solid #14294b; border-radius: 12px;
    padding: 13px 15px; background: #fbfcfe; height: 100%;
  }
  .vi-card h4 {margin: 0 0 .3rem 0; font-size: .98rem; color: #14294b;}
  .vi-card .price {font-size: 1.08rem; font-weight: 700; color: #14294b; margin-bottom: .3rem;}
  .vi-card ul {margin: .2rem 0 0 1.0rem; padding: 0;}
  .vi-card li {font-size: .83rem; color: #33415c; margin-bottom: .18rem; line-height: 1.34;}
  .vi-up {border-left-color: #12805c;}
  .vi-flat {border-left-color: #c48a05;}
  .vi-down {border-left-color: #b3261e;}
  .vi-pill {
    display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: .76rem;
    font-weight: 600; margin-right: 5px;
  }
  .vi-pass {background: #e5f6ee; color: #0d6b4c;}
  .vi-fail {background: #fdeceb; color: #9c1c16;}
  .vi-wait {background: #fdf4e0; color: #8a6205;}
  .vi-note {font-size: .81rem; color: #5b6b85;}
  .vi-tier {width: 100%; border-collapse: collapse; font-size: .85rem;}
  .vi-tier th, .vi-tier td {border-bottom: 1px solid #e3e8f0; padding: 6px 9px; text-align: left;}
  .vi-tier tr.now {background: #eef4ff; font-weight: 700;}
  .vi-news {font-size: .84rem; margin-bottom: .45rem; line-height: 1.35;}
  .vi-pos {background: #e5f6ee; color: #0d6b4c; border-radius: 6px; padding: 1px 6px; font-size: .76rem;}
  .vi-neg {background: #fdeceb; color: #9c1c16; border-radius: 6px; padding: 1px 6px; font-size: .76rem;}
  .vi-mid {background: #eef1f6; color: #445; border-radius: 6px; padding: 1px 6px; font-size: .76rem;}
  .vi-split {display: flex; gap: 10px; flex-wrap: wrap; margin: .3rem 0 .6rem 0;}
  .vi-box {border: 1px solid #e3e8f0; border-radius: 10px; padding: 8px 12px; font-size: .84rem; background: #fbfcfe;}
  .vi-box b {color: #14294b;}
  a {text-decoration: none;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

HEALTH_KIND = {"영업적자": "fail", "고부채": "fail", "재무주의": "wait",
               "재무양호": "pass", "확인부족": "mid"}
JUDGMENT_KIND = {"저평가": "pass", "적정": "wait", "고평가": "fail", "판단보류": "mid"}
LEVEL_ICON = {"major": "🔴", "warn": "🟠", "info": "🔵"}

HOLD_COLUMNS = ["종목", "코드", "수량", "평단가", "현재가", "평가금액", "비중(%)", "평가손익",
                "수익률(%)", "재무상태", "투자판단", "PER", "PBR", "배당수익률(%)",
                "수령배당(원)", "적정가", "안전마진(%)", "시세 기준", "재무 기준"]

TRADE_CSV = {"date": "일자", "code": "종목코드", "name": "종목명", "side": "구분",
             "qty": "수량", "price": "단가", "fee": "수수료", "tax": "세금", "memo": "메모"}
DIVIDEND_CSV = {"date": "일자", "code": "종목코드", "name": "종목명", "gross": "배당금",
                "tax": "세금", "per_share": "주당배당금", "memo": "메모"}


# ------------------------------------------------------------- 공통 도우미 ----
def header() -> None:
    counts = core.universe_size()
    st.markdown(
        '<div class="vi-header">'
        "<h1>📈 가치투자 스튜디오 <span style='font-size:1rem;opacity:.75'>v3</span></h1>"
        "<p>주식투자 종합관리 — 저가 우량주를 찾고, 투자금을 잃지 않는 것. "
        "보유·재무·적정가·전략·시장·뉴스·위험·성과를 10개 메뉴에서.</p>"
        f'<span class="tag">기준일 {core.today_str()}</span>'
        f'<span class="tag">유니버스 {len(core.UNIVERSE)}종목 (코스피 {counts.get("KOSPI", 0)}'
        f'·코스닥 {counts.get("KOSDAQ", 0)}·미국 {counts.get("US", 0)})</span>'
        f'<span class="tag">마지막 갱신 {st.session_state.get("refresh_time", "-")}</span>'
        '<span class="tag">자동매매 없음 · 정보 제공용</span>'
        "</div>",
        unsafe_allow_html=True,
    )


def ensure_state() -> None:
    defaults = {
        "holdings": None, "picks": None, "store_error": "", "scan_rows": None,
        "analysis": None, "refresh_time": core.now_text(), "menu": MENU_LABELS[0],
        "snap_cache": None, "f_per": 15.0, "f_pbr": 1.5, "f_debt": 200.0,
        "f_roe": 5.0, "f_op": True, "f_ocf": True, "extra_error": "",
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def current_rules() -> core.FilterRules:
    base = core.FilterRules()
    return core.FilterRules(
        max_per=float(st.session_state.get("f_per", base.max_per)),
        max_pbr=float(st.session_state.get("f_pbr", base.max_pbr)),
        max_debt=float(st.session_state.get("f_debt", base.max_debt)),
        min_roe=float(st.session_state.get("f_roe", base.min_roe)),
        require_op_profit=bool(st.session_state.get("f_op", base.require_op_profit)),
        require_ocf=bool(st.session_state.get("f_ocf", base.require_ocf)),
    )


def load_store(kind: str, force: bool = False):
    """저장소(구글 시트/로컬)에서 보유·추천 목록을 읽어 세션에 보관."""
    if force or st.session_state.get(kind) is None:
        try:
            rows = core.load_holdings() if kind == "holdings" else core.load_picks()
            st.session_state[kind] = rows
            st.session_state["store_error"] = ""
        except core.StorageError as exc:
            st.session_state[kind] = []
            st.session_state["store_error"] = str(exc)
    return st.session_state[kind] or []


def persist(kind: str, rows: list[dict], ok_message: str) -> bool:
    try:
        if kind == "holdings":
            core.save_holdings(rows)
        else:
            core.save_picks(rows)
        st.session_state[kind] = rows
        st.session_state["store_error"] = ""
        st.success(ok_message)
        return True
    except core.StorageError as exc:
        st.error(f"저장하지 못했습니다: {exc}")
        return False


def extra(kind: str):
    """거래·배당·관심종목·전략·예수금(로컬 저장). 파일을 못 읽어도 세션 값으로 계속 동작."""
    failed = st.session_state.get("extra_failed") or set()
    if kind in failed:
        return st.session_state.get(f"extra_{kind}",
                                    {} if kind in ("portfolio_meta", "strategies") else [])
    try:
        value = core.load_extra(kind)
    except core.StorageError as exc:
        st.session_state["extra_error"] = str(exc)
        return st.session_state.get(f"extra_{kind}",
                                    {} if kind in ("portfolio_meta", "strategies") else [])
    st.session_state[f"extra_{kind}"] = value
    return value


def persist_extra(kind: str, payload, ok_message: str) -> bool:
    st.session_state[f"extra_{kind}"] = payload
    try:
        core.save_extra(kind, payload)
        failed = set(st.session_state.get("extra_failed") or set())
        failed.discard(kind)
        st.session_state["extra_failed"] = failed
        st.session_state["extra_error"] = ""
        st.success(ok_message)
        return True
    except core.StorageError as exc:
        failed = set(st.session_state.get("extra_failed") or set())
        failed.add(kind)
        st.session_state["extra_failed"] = failed
        st.warning(f"파일 저장에 실패해 이번 세션 메모리에 보관했습니다({exc}). "
                   "화면은 그대로 쓸 수 있으니 10번 메뉴에서 백업을 내려받아 두세요.")
        return True


def meta_state() -> dict:
    raw = extra("portfolio_meta")
    meta = raw if isinstance(raw, dict) else {}
    return {"cash": float(core.parse_num(meta.get("cash")) or 0),
            "cash_note": str(meta.get("cash_note") or ""),
            "updated": str(meta.get("updated") or "")}


def money(value, currency: str = "KRW") -> str:
    return core.money(value, currency)


def pct_text(value) -> str:
    return "-" if value is None else f"{value:+.1f}%"


def value_or(value, fmt="{:.2f}") -> str:
    if value is None:
        return "-"
    try:
        return fmt.format(value)
    except Exception:
        return str(value)


def pretty_list(items: list[str]) -> str:
    return "".join(f"<li>{item}</li>" for item in items)


def pick_cols(rows: list[dict], columns: list[str]) -> list[dict]:
    return [{c: r.get(c) for c in columns} for r in rows]


def badge(label: str, kind: str = "mid") -> str:
    cls = {"pass": "vi-pass", "fail": "vi-fail", "wait": "vi-wait"}.get(kind, "vi-mid")
    return f"<span class='vi-pill {cls}'>{label}</span>"


def badges_line(health: dict, judgment: dict) -> None:
    st.markdown(
        "<div class='vi-split'>"
        f"<div class='vi-box'><b>재무상태</b> · {badge(health['badge'], HEALTH_KIND.get(health['label'], 'mid'))}"
        f"<span class='vi-note'>기업의 재무 건전성</span></div>"
        f"<div class='vi-box'><b>투자판단</b> · {badge(judgment['full'], JUDGMENT_KIND.get(judgment['label'], 'mid'))}"
        f"<span class='vi-note'>가격(적정가 대비) 판단</span></div>"
        "</div>",
        unsafe_allow_html=True,
    )


def basis_caption(rows: list[dict], extra_text: str = "") -> str:
    """표·카드 아래에 붙는 기준일 안내."""
    stamps = sorted({r.get("시세 기준") for r in rows
                     if r.get("시세 기준") and "확인 불가" not in str(r.get("시세 기준"))})
    fins = sorted({r.get("재무 기준") for r in rows
                   if r.get("재무 기준") and "확인 불가" not in str(r.get("재무 기준"))})
    parts = []
    if stamps:
        parts.append(f"현재가 기준 {stamps[-1]}")
    if fins:
        parts.append(f"재무자료 기준 {fins[-1]}")
    parts.append(f"마지막 갱신 {st.session_state.get('refresh_time', '-')}")
    if extra_text:
        parts.append(extra_text)
    return " · ".join(parts)


def snapshots_for(holdings: list[dict], force: bool = False):
    """보유 종목 시세·재무를 세션 캐시로 조회(원/달러 환율 포함)."""
    codes = [core.clean_code(h.get("code") or "") for h in holdings]
    cache_key = ",".join(codes)
    cache = st.session_state.get("snap_cache")
    if not force and cache and cache.get("key") == cache_key:
        return cache["by_code"], cache["fx"]
    with st.spinner("보유 종목 시세·재무를 불러오는 중..."):
        snaps = core.fetch_snapshots(
            [{"code": h.get("code"), "sector": None, "name": h.get("name")} for h in holdings])
    by_code = {s.code: s for s in snaps}
    fx = core.usd_krw() if any(s.currency == "USD" for s in by_code.values()) else 1.0
    st.session_state["snap_cache"] = {"key": cache_key, "by_code": by_code, "fx": fx,
                                     "at": core.now_text()}
    return by_code, fx


def avg_price_of(code: str, holdings: list[dict]) -> float | None:
    target = core.clean_code(code)
    for h in holdings:
        if core.clean_code(h.get("code") or "") == target:
            value = core.parse_num(h.get("avg_price"))
            return float(value) if value else None
    return None


def refresh_all() -> None:
    core.clear_cache()
    for fn in (core.fetch_macro, core.fetch_sector_pulse, core.fetch_price_history,
               core.fetch_financial_history, core.fetch_flow, core.fetch_news_digest,
               core.fetch_market_news, core.fetch_news, core.usd_krw):
        try:
            fn.cache_clear()  # type: ignore[attr-defined]
        except Exception:
            pass
    for key in ("holdings", "picks", "scan_rows", "analysis", "snap_cache"):
        st.session_state[key] = None
    for key in [k for k in list(st.session_state.keys()) if str(k).startswith("extra_")]:
        del st.session_state[key]
    st.session_state["refresh_time"] = core.now_text()
    st.rerun()


def stock_picker(key: str, holdings: list[dict], include_watch: bool = True,
                 label: str = "종목 선택") -> core.StockSnapshot | None:
    """보유·관심종목에서 고르거나 이름·티커·코드로 검색해 스냅샷을 만든다."""
    options: list[dict] = []
    seen: set[str] = set()
    for h in holdings:
        code = core.clean_code(h.get("code") or "")
        if not code or code in seen:
            continue
        seen.add(code)
        options.append({"code": code, "name": h.get("name") or code, "tag": "보유"})
    if include_watch:
        try:
            for w in core.load_watchlist():
                if w["code"] in seen:
                    continue
                seen.add(w["code"])
                options.append({"code": w["code"], "name": w.get("name") or w["code"], "tag": "관심"})
        except core.StorageError:
            pass
    labels = [f"{o['tag']} · {o['name']} ({o['code']})" for o in options] + ["🔎 직접 검색"]
    idx = st.selectbox(label, range(len(labels)), format_func=lambda i: labels[i], key=f"{key}_sel")
    code = hint = ""
    if idx >= len(options):
        query = st.text_input("종목명·티커·종목코드", key=f"{key}_q",
                              placeholder="예: 삼성전자, HPSP, 034020, NVDA")
        found = st.session_state.get(f"{key}_found") or []
        if st.button("검색", key=f"{key}_go"):
            found = core.search_stocks(query, limit=6) if query.strip() else []
            st.session_state[f"{key}_found"] = found
            if query.strip() and not found:
                st.warning(f"'{query}' 종목을 찾지 못했습니다.")
        if not found:
            return None
        flabels = [f"{f['name']} ({f['code']}) · {f.get('board') or f.get('market', '')}" for f in found]
        pick = st.selectbox("검색 결과", range(len(flabels)), format_func=lambda i: flabels[i],
                            key=f"{key}_fsel")
        code, hint = found[pick]["code"], found[pick].get("name") or ""
    else:
        code, hint = options[idx]["code"], options[idx]["name"]
    if not code:
        return None
    with st.spinner("시세·재무를 불러오는 중..."):
        return core.fetch_snapshot(code, name_hint=hint)


# ------------------------------------------------------------- 차트 도우미 ----
PRICE_COLOR = "#1f4e9c"
MA20_COLOR = "#e08a00"
MA60_COLOR = "#8a4fbf"


def price_chart(hist: core.PriceHistory, currency: str, months: int = 12, height: int = 300):
    """6개월/1년 주가 + 20일·60일 이동평균."""
    import altair as alt
    rows = hist.window(months).to_rows()
    if not rows:
        return None
    df = pd.DataFrame(rows)
    long = df.melt(id_vars=["날짜"], value_vars=["종가", "20일선", "60일선"],
                   var_name="구분", value_name="가격").dropna()
    base = alt.Chart(long).encode(
        x=alt.X("날짜:T", title="날짜"),
        y=alt.Y("가격:Q", title=f"주가({'원' if currency == 'KRW' else 'USD'})", scale=alt.Scale(zero=False)),
        color=alt.Color("구분:N", title=None,
                        scale=alt.Scale(domain=["종가", "20일선", "60일선"],
                                        range=[PRICE_COLOR, MA20_COLOR, MA60_COLOR])),
        tooltip=[alt.Tooltip("날짜:T", title="날짜"), alt.Tooltip("구분:N", title="구분"),
                 alt.Tooltip("가격:Q", title="가격", format=",.2f")],
    )
    line = base.mark_line(strokeWidth=2).transform_filter(alt.datum["구분"] == "종가")
    ma = base.mark_line(strokeWidth=1.2, opacity=0.85).transform_filter(alt.datum["구분"] != "종가")
    return (line + ma).properties(height=height).interactive()


def trend_chart(rows: list[dict], field: str, title: str, unit: str = "",
                kind: str = "bar", color: str = PRICE_COLOR, height: int = 170):
    """연도별 재무 추이 막대/선 차트."""
    import altair as alt
    data = [{"연도": r.get("연도"), "값": r.get(field)} for r in rows if r.get(field) is not None]
    if not data:
        return None
    df = pd.DataFrame(data)
    if kind == "bar":
        mark = alt.Chart(df).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
            x=alt.X("연도:N", title=None, sort=None),
            y=alt.Y("값:Q", title=unit),
            color=alt.condition(alt.datum["값"] >= 0, alt.value(color), alt.value("#b3261e")),
            tooltip=[alt.Tooltip("연도:N"), alt.Tooltip("값:Q", format=",.2f")],
        )
    else:
        mark = alt.Chart(df).mark_line(point=True, strokeWidth=2, color=color).encode(
            x=alt.X("연도:N", title=None, sort=None),
            y=alt.Y("값:Q", title=unit, scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("연도:N"), alt.Tooltip("값:Q", format=",.2f")],
        )
    return mark.properties(height=height, title=alt.TitleParams(title, fontSize=12, anchor="start"))


def money_bar(rows: list[dict], label_key: str, value_key: str, title: str,
              unit: str = "원", height: int = 240, horizontal: bool = False,
              color: str | None = None):
    import altair as alt
    data = [{"항목": r.get(label_key), "값": r.get(value_key) or 0} for r in rows]
    if not data:
        return None
    df = pd.DataFrame(data)
    color_enc = (alt.value(color) if color else
                 alt.condition(alt.datum["값"] >= 0, alt.value("#12805c"), alt.value("#b3261e")))
    tooltip = [alt.Tooltip("항목:N"), alt.Tooltip("값:Q", format=",.0f")]
    if horizontal:
        chart = alt.Chart(df).mark_bar().encode(
            y=alt.Y("항목:N", title=None, sort=None),
            x=alt.X("값:Q", title=unit),
            color=color_enc, tooltip=tooltip)
    else:
        chart = alt.Chart(df).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
            x=alt.X("항목:N", title=None, sort=None),
            y=alt.Y("값:Q", title=unit),
            color=color_enc, tooltip=tooltip)
    return chart.properties(height=height, title=alt.TitleParams(title, fontSize=12, anchor="start"))


def sector_pie(weights: dict, title: str = "", height: int = 240):
    import altair as alt
    data = [{"섹터": k, "비중": v} for k, v in (weights or {}).items() if v]
    if not data:
        return None
    df = pd.DataFrame(data)
    return alt.Chart(df).mark_arc(innerRadius=55).encode(
        theta=alt.Theta("비중:Q"),
        color=alt.Color("섹터:N", title=None),
        tooltip=[alt.Tooltip("섹터:N"), alt.Tooltip("비중:Q", format=".1f")],
    ).properties(height=height, title=alt.TitleParams(title, fontSize=12, anchor="start"))


def show_chart(chart, empty: str = "데이터 없음") -> None:
    if chart is None:
        st.caption(empty)
        return
    st.altair_chart(chart, width="stretch")


def _last_value(hist: core.FinancialHistory, key: str):
    values = [v for v in (hist.series.get(key) or []) if v is not None]
    return values[-1] if values else None


def _yoy(hist: core.FinancialHistory, key: str):
    values = [v for v in (hist.series.get(key) or []) if v is not None]
    if len(values) < 2 or not values[-2]:
        return None
    return (values[-1] - values[-2]) / abs(values[-2]) * 100


# --------------------------------------------------------- 시나리오·전략 ----
def scenario_cards(scn: dict) -> None:
    cols = st.columns(3)
    styles = {"상승": "vi-up", "기본": "vi-flat", "경계": "vi-down"}
    for col, key in zip(cols, ["상승", "기본", "경계"]):
        item = scn[key]
        expected = item.get("expected")
        expected_text = "" if expected is None else f' <span class="vi-note">현재가 대비 {expected:+.1f}%</span>'
        with col:
            st.markdown(
                f'<div class="vi-card {styles[key]}">'
                f"<h4>{item['title']}</h4>"
                f'<div class="price">{item["range_text"]}{expected_text}</div>'
                f'<div class="vi-note">{item.get("basis", "")}</div>'
                f"<ul>{pretty_list(item['triggers'])}</ul>"
                f'<div class="vi-note" style="margin-top:.5rem"><b>대응</b> {item["stance"]}</div>'
                "</div>",
                unsafe_allow_html=True,
            )


def plan_block(scn: dict) -> None:
    """v3: 판단 근거 · 1차/추가 매수 · 목표 · 손절 · 투자논리 훼손 조건."""
    cur = scn.get("currency", "KRW")
    st.markdown("#### 🧭 판단 근거 · 매수/목표/손절 · 투자논리 훼손 조건")
    c = st.columns(4)
    c[0].metric("1차 매수 기준", money(scn.get("buy1"), cur))
    c[1].metric("추가 매수 기준", money(scn.get("buy2"), cur))
    c[2].metric("목표 가격", money(scn.get("target"), cur))
    c[3].metric("손절 가격", money(scn.get("stop"), cur))
    left, right = st.columns(2)
    with left:
        st.markdown("**판단 근거**")
        st.markdown("".join(f"- {line}\n" for line in scn.get("judgement_basis") or []) or "- 근거 데이터 없음")
    with right:
        st.markdown("**투자논리 훼손 조건 (하나라도 발생하면 논리 재검토)**")
        st.markdown("".join(f"- {line}\n" for line in scn.get("invalidations") or []) or "- 없음")
    st.caption("매수 기준은 적정가 -10%(현재가가 그보다 낮으면 현재가), 추가 매수는 1차 기준 -8%입니다. "
               "손절은 평단 -15%(미보유 시 현재가 -12%)입니다.")


# ------------------------------------------------------------- 밸류에이션 ----
def valuation_block(snap: core.StockSnapshot, val: core.Valuation) -> None:
    cur = val.currency
    st.markdown("#### 💰 적정가 — PER · PBR · DCF 3방식 + 안전마진")
    rows = []
    for m in val.methods:
        rows.append({
            "방식": m.name,
            "적정가": money(m.value, cur) if m.value is not None else "계산 제외",
            "적용 가중치": f"{m.effective:.0%}" if m.value is not None else "-",
            "설정 가중치": f"{m.weight:.0%}",
            "근거": m.basis,
            "비고": m.reason if m.value is None else m.note,
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    if val.excluded:
        st.info("**제외된 방식** — " + " / ".join(f"{e['name']}: {e['reason']}" for e in val.excluded)
                + f". 남은 방식의 가중치를 100%로 재배분했습니다(적용 {val.weight_summary()}).")
    st.caption(val.note + " · " + " / ".join(val.assumptions))

    if val.fair:
        c = st.columns(4)
        c[0].metric("종합 적정가", money(val.fair, cur))
        c[1].metric("현재가", money(snap.price, cur))
        c[2].metric("안전마진", f"{val.safety_margin:+.1f}%" if val.safety_margin is not None else "-")
        c[3].metric("적정가 밴드", f"{money(val.low, cur)} ~ {money(val.high, cur)}")
        table_rows = []
        for r in val.rows:
            table_rows.append(
                f"<tr class='{'now' if r['지금'] else ''}'>"
                f"<td>{'▶ ' if r['지금'] else ''}{r['구간']}</td>"
                f"<td>{r['가격대']}</td><td>{r['판단']}</td></tr>")
        st.markdown(
            "<table class='vi-tier'><thead><tr><th>대응 구간</th><th>가격대</th><th>의미</th></tr></thead>"
            f"<tbody>{''.join(table_rows)}</tbody></table>",
            unsafe_allow_html=True)
        st.caption(f"현재가 기준 판단: **{val.tier}** — 하단은 적정가 -10%, 상단은 +15% 구간입니다. "
                   f"안전마진 = (적정가-현재가)/적정가 × 100. 시세 기준 {val.price_as_of} · "
                   f"재무자료 기준 {val.finance_basis}")
    else:
        st.warning(val.note)


def health_table(snap: core.StockSnapshot) -> None:
    rows = core.health_comment(snap)
    st.table(pd.DataFrame(rows, columns=["지표", "평가", "신호"]))


# --------------------------------------------------------------- 재무분석 ----
def trend_charts_block(snap: core.StockSnapshot, trend: core.FinancialHistory,
                       compact: bool = False) -> None:
    """3~5년 실적·재무 추이 차트(주요 6개 + 추가 지표)."""
    if not trend.available():
        st.caption(trend.note or "재무 추이 데이터 없음")
        with st.expander("현재 지표만 보기"):
            health_table(snap)
        return
    rows = trend.series_rows()
    unit = trend.unit
    g1, g2, g3 = st.columns(3)
    with g1:
        show_chart(trend_chart(rows, "매출액", "매출액", unit))
    with g2:
        show_chart(trend_chart(rows, "영업이익", "영업이익", unit))
    with g3:
        show_chart(trend_chart(rows, "순이익", "순이익", unit))
    g4, g5, g6 = st.columns(3)
    with g4:
        show_chart(trend_chart(rows, "영업이익률", "영업이익률", "%", kind="line"))
    with g5:
        show_chart(trend_chart(rows, "ROE", "ROE", "%", kind="line", color="#12805c"))
    with g6:
        show_chart(trend_chart(rows, "부채비율", "부채비율", "%", kind="line", color="#b3261e"))
    with st.expander("추가 지표 차트 (PER·PBR·EPS·BPS·배당·현금흐름)", expanded=not compact):
        h1, h2, h3 = st.columns(3)
        with h1:
            show_chart(trend_chart(rows, "PER", "PER 추이", "배", kind="line"))
        with h2:
            show_chart(trend_chart(rows, "PBR", "PBR 추이", "배", kind="line", color="#8a4fbf"))
        with h3:
            show_chart(trend_chart(rows, "현금흐름", "영업현금흐름(가능 시)", unit))
        h4, h5, h6 = st.columns(3)
        with h4:
            show_chart(trend_chart(rows, "EPS", "EPS", snap.currency, kind="line"))
        with h5:
            show_chart(trend_chart(rows, "BPS", "BPS", snap.currency, kind="line"))
        with h6:
            show_chart(trend_chart(rows, "주당배당금", "주당배당금(DPS)", snap.currency))
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    est = [y for i, y in enumerate(trend.years)
           if i < len(trend.estimated) and trend.estimated[i]]
    caption = "확정 실적 기준"
    if est:
        caption += f" · {', '.join(est)}년은 증권가 추정치(컨센서스)"
    if trend.note:
        caption += f" · {trend.note}"
    st.caption(caption)


def financial_analysis_block(snap: core.StockSnapshot, key: str, compact: bool = False) -> None:
    """기업 재무분석 — 실적·성장성·수익성·건전성·배당·3~5년 추이·수급."""
    with st.spinner(f"{snap.name or snap.code} 재무 데이터를 불러오는 중..."):
        trend = core.fetch_financial_history(snap.code, years=4)
        flow = core.fetch_flow(snap.code, days=5)
    health = core.financial_health(snap)
    latest_label = ""
    if trend.years:
        latest_label = f"{trend.years[-1]}년 {'추정' if (trend.estimated and trend.estimated[-1]) else '확정'}"
    st.markdown(f"#### 📊 {snap.name}({snap.code}) · {snap.sector} · "
                f"{'한국' if snap.market == 'KR' else '미국'}")
    st.caption(f"재무자료 기준 {core.financial_basis_text(snap, trend)} · "
               f"현재가 기준 {core.price_as_of_text(snap)} · 출처 {trend.source or '-'}"
               + (f" · 아래 실적 수치는 {latest_label} 기준" if latest_label else ""))

    rev = _last_value(trend, "매출액")
    op = _last_value(trend, "영업이익")
    ni = _last_value(trend, "순이익")
    margin = _last_value(trend, "영업이익률")
    roe = _last_value(trend, "ROE")
    unit = trend.unit
    m = st.columns(5)
    m[0].metric("매출액", f"{rev:,.0f}{unit}" if rev is not None else "-", pct_text(_yoy(trend, "매출액")))
    m[1].metric("영업이익", f"{op:,.0f}{unit}" if op is not None else "-", pct_text(_yoy(trend, "영업이익")))
    m[2].metric("순이익", f"{ni:,.0f}{unit}" if ni is not None else "-", pct_text(_yoy(trend, "순이익")))
    m[3].metric("영업이익률", value_or(margin, "{:.1f}%"), delta_color="off")
    m[4].metric("ROE", value_or(roe, "{:.1f}%"), delta_color="off")

    st.markdown(f"**재무상태** · {badge(health['badge'], HEALTH_KIND.get(health['label'], 'mid'))} "
                f"<span class='vi-note'>{health['detail']}</span>", unsafe_allow_html=True)
    for reason in health["reasons"]:
        st.caption("· " + reason)

    st.markdown("**수익성 · 성장성 · 재무건전성**")
    check_rows = []
    check_rows.append({"항목": "성장성(매출)", "값": pct_text(snap.revenue_yoy),
                       "해석": "매출이 늘고 있음" if (snap.revenue_yoy or 0) > 0 else "매출 감소"})
    check_rows.append({"항목": "성장성(영업이익)", "값": pct_text(snap.op_yoy),
                       "해석": "이익이 늘고 있음" if (snap.op_yoy or 0) > 0 else "이익 감소"})
    check_rows.append({"항목": "수익성(영업이익률)", "값": value_or(snap.op_margin, "{:.1f}%"),
                       "해석": "본업 수익성" if (snap.op_margin or 0) >= 5 else "수익성 얇음"})
    check_rows.append({"항목": "수익성(ROE)", "값": value_or(snap.roe, "{:.1f}%"),
                       "해석": "자본 효율" if (snap.roe or 0) >= 8 else "자본 효율 낮음"})
    check_rows.append({"항목": "건전성(부채비율)", "값": value_or(snap.debt_ratio, "{:.1f}%"),
                       "해석": "부채 적정" if (snap.debt_ratio or 0) <= 150 else "부채 높음"})
    check_rows.append({"항목": "현금흐름(영업)", "값": core.compact_money(snap.ocf, snap.currency)
                       if snap.ocf is not None else "확인 불가",
                       "해석": "현금 창출" if (snap.ocf or 0) > 0 else "현금 유출"})
    st.dataframe(pd.DataFrame(check_rows), hide_index=True, width="stretch")

    st.markdown("**연도별 실적 추이 (3~5년)**")
    trend_charts_block(snap, trend, compact=compact)

    st.markdown("**배당 이력**")
    d = st.columns(3)
    d[0].metric("배당수익률", "-" if snap.dividend_yield is None else f"{snap.dividend_yield:.2f}%")
    d[1].metric("주당배당금", money(snap.dps, snap.currency) if snap.dps else "-")
    d[2].metric("기준 연도", snap.dividend_year or "-")
    has_div = bool(trend.available() and any(v is not None for v in trend.series.get("주당배당금") or []))
    if has_div:
        st.dataframe(pd.DataFrame(trend.series_rows(["주당배당금", "배당수익률"])),
                     hide_index=True, width="stretch")
    else:
        st.caption("연도별 배당 이력은 데이터 제공처에 없으면 표시하지 않습니다.")

    st.markdown("**최근 5거래일 수급**")
    st.markdown(f"수급 · {flow.tag()}")
    if flow.available:
        st.dataframe(pd.DataFrame(flow.rows), hide_index=True, width="stretch",
                     column_config={
                         "외국인": st.column_config.NumberColumn(format="%,d"),
                         "기관": st.column_config.NumberColumn(format="%,d"),
                         "개인": st.column_config.NumberColumn(format="%,d"),
                         "종가": st.column_config.NumberColumn(format="%.0f"),
                     })
        st.caption(flow.text() + (f" · 외국인 보유율 {flow.foreign_hold_ratio:.2f}%"
                                  if flow.foreign_hold_ratio else ""))
    else:
        st.caption(flow.note or "수급 데이터 없음")


# ------------------------------------------------------------- 뉴스 블록 ----
def news_digest_block(digest: core.NewsDigest, name: str = "") -> None:
    """뉴스·공시 — 실제 URL과 게시 시각만 표시한다(없는 기사는 만들지 않음)."""
    if not digest.items:
        st.caption(digest.note or "뉴스를 가져오지 못했습니다.")
        return
    counts = digest.counts()
    st.caption(f"수집 {len(digest.items)}건 · 🟢 호재 {counts['🟢 호재']} · 🔴 악재 {counts['🔴 악재']} · "
               f"⚪ 중립 {counts['⚪ 중립']} · 기준 {digest.generated_at}")
    groups = digest.by_category()
    for category in [c for c in core.CATEGORY_ORDER if c in groups]:
        st.markdown(f"**{category}**")
        for item in groups[category][:6]:
            cls = {"🟢 호재": "vi-pos", "🔴 악재": "vi-neg"}.get(item.sentiment, "vi-mid")
            st.markdown(
                f"<div class='vi-news'><span class='{cls}'>{item.sentiment}</span> "
                f"<a href='{item.link}' target='_blank'>{item.title}</a><br>"
                f"<span class='vi-note'>{item.source} · 게시 {item.published}</span></div>",
                unsafe_allow_html=True)


def news_impact_block(digest: core.NewsDigest, sector: str = "", name: str = "") -> None:
    for line in core.news_impact_lines(digest, sector, name):
        st.markdown(f"- {line}")


# ------------------------------------------------------------- 상세 화면 ----
def render_detail(snap: core.StockSnapshot, rules: core.FilterRules, key: str,
                  avg_price: float | None = None, show_news: bool = True) -> None:
    """한 종목의 상세 — 기준일·배지·시나리오·전략·밸류에이션·차트·재무·수급·뉴스."""
    filt = core.run_filter(snap, rules)
    with st.spinner(f"{snap.name or snap.code} 상세 데이터를 불러오는 중..."):
        hist = core.fetch_price_history(snap.code, months=12)
        trend = core.fetch_financial_history(snap.code, years=4)
        flow = core.fetch_flow(snap.code, days=5)
        val = core.valuate(snap, trend)
        digest = core.fetch_news_digest(snap.name or snap.code,
                                        code=snap.code if snap.market == "KR" else "",
                                        limit=12) if show_news else core.NewsDigest()
        scn = core.build_scenarios(snap, avg_price=avg_price, valuation=val)

    health = scn.get("health") or core.financial_health(snap)
    judgment = scn.get("judgment") or core.investment_judgment(snap, val)
    stats = hist.stats()

    st.markdown(f"### {snap.name}({snap.code}) · {snap.sector} · "
                f"{'한국' if snap.market == 'KR' else '미국'}")
    st.caption(f"현재가 기준 {core.price_as_of_text(snap)} · 재무자료 기준 "
               f"{core.financial_basis_text(snap, trend)} · 가치점수 {filt['score']}점({filt['verdict']}) · "
               f"마지막 갱신 {st.session_state.get('refresh_time', '-')}")
    badges_line(health, judgment)

    m = st.columns(5)
    m[0].metric("현재가", money(snap.price, snap.currency), pct_text(snap.change_pct))
    m[1].metric("PER / PBR", f"{value_or(snap.per)} / {value_or(snap.pbr)}")
    m[2].metric("ROE / 부채비율", f"{value_or(snap.roe, '{:.1f}%')} / {value_or(snap.debt_ratio, '{:.1f}%')}")
    m[3].metric("배당수익률", "-" if snap.dividend_yield is None else f"{snap.dividend_yield:.2f}%",
                f"DPS {money(snap.dps, snap.currency) if snap.dps else '-'}", delta_color="off")
    m[4].metric("적정가 / 안전마진", money(val.fair, snap.currency),
                f"{val.safety_margin:+.1f}%" if val.safety_margin is not None else "-", delta_color="off")

    st.markdown("#### 🟢🟡🔴 3단 시나리오")
    scenario_cards(scn)
    plan_block(scn)

    valuation_block(snap, val)

    st.markdown("#### 📊 3~5년 실적·재무 추이")
    trend_charts_block(snap, trend, compact=True)

    st.markdown("#### 📈 주가 흐름")
    if hist.closes:
        c1, c2 = st.columns([3, 1])
        with c2:
            period = st.radio("기간", ["6개월", "1년"], index=1, horizontal=True, key=f"{key}_period")
        show_chart(price_chart(hist, snap.currency, months=6 if period == "6개월" else 12))
        s = stats or {}
        k = st.columns(4)
        k[0].metric("6개월 수익률", pct_text(s.get("return_6m")))
        k[1].metric("1년 수익률", pct_text(s.get("return_1y")))
        k[2].metric("20일선", money(s.get("ma20"), snap.currency))
        k[3].metric("60일선", money(s.get("ma60"), snap.currency))
        if s.get("position_pct") is not None:
            pos = max(0.0, min(100.0, s["position_pct"]))
            st.progress(pos / 100, text=f"52주 레인지 {money(s.get('low52'), snap.currency)} ~ "
                                        f"{money(s.get('high52'), snap.currency)} · 현재 {pos:.0f}% 위치")
        if hist.note:
            st.caption(hist.note)
    else:
        st.caption(hist.note or "주가 흐름 데이터 없음")

    st.markdown("#### 🧾 수급 · 💵 배당")
    col_left, col_right = st.columns(2)
    with col_left:
        st.markdown(f"수급 · {flow.tag()}")
        if flow.available:
            st.dataframe(pd.DataFrame(flow.rows), hide_index=True, width="stretch",
                         column_config={"외국인": st.column_config.NumberColumn(format="%,d"),
                                        "기관": st.column_config.NumberColumn(format="%,d"),
                                        "개인": st.column_config.NumberColumn(format="%,d"),
                                        "종가": st.column_config.NumberColumn(format="%.0f")})
            st.caption(flow.text())
        else:
            st.caption(flow.note or "수급 데이터 없음")
    with col_right:
        st.markdown(f"배당수익률 **{'-' if snap.dividend_yield is None else f'{snap.dividend_yield:.2f}%'}** · "
                    f"주당배당금 **{money(snap.dps, snap.currency) if snap.dps else '-'}** · "
                    f"기준 연도 **{snap.dividend_year or '-'}**")
        has_div = bool(trend.available() and any(v is not None for v in trend.series.get("주당배당금") or []))
        if has_div:
            st.dataframe(pd.DataFrame(trend.series_rows(["주당배당금", "배당수익률"])),
                         hide_index=True, width="stretch")
        else:
            st.caption("연도별 배당 이력은 데이터 제공처에 없으면 표시하지 않습니다.")

    if show_news:
        st.markdown("#### 📰 뉴스·공시 자동 분석")
        news_digest_block(digest, snap.name)
        news_impact_block(digest, snap.sector, snap.name)

    b1, b2, b3 = st.columns([1, 1, 2])
    with b1:
        if st.button("📈 추천 추적에 등록", key=f"{key}_register"):
            register_pick(snap, scn, f"상세분석 · 가치점수 {filt['score']}점 · {filt['reason']}")
    with b2:
        if st.button("⭐ 관심종목에 추가", key=f"{key}_watch"):
            try:
                added, _ = core.add_watchlist(snap.code, snap.name)
                st.success(f"{snap.name} 을(를) 관심종목에 추가했습니다." if added
                           else f"{snap.name} 은(는) 이미 관심종목에 있습니다.")
            except core.StorageError as exc:
                st.error(f"관심종목 저장 실패: {exc}")
    with b3:
        st.caption("등록 시 현재가·목표가·경계선이 저장되어 이후 수익률을 자동 추적합니다. "
                   "관심종목은 4·5번 메뉴의 종목 선택 목록에 나타납니다.")


def register_pick(snap: core.StockSnapshot, scn: dict, rationale: str) -> None:
    picks = load_store("picks")
    if any(p["code"] == snap.code for p in picks):
        st.info(f"{snap.label} 은(는) 이미 추천 추적에 등록되어 있습니다.")
        return
    picks.append({
        "date": core.today_str(), "code": snap.code, "name": snap.name or snap.code,
        "entry_price": snap.price, "rationale": rationale,
        "target": scn.get("target"), "stop": scn.get("stop"), "memo": "",
    })
    if persist("picks", picks, f"{snap.label} 을(를) 추천 추적에 등록했습니다."):
        st.caption("9️⃣ 투자성과 분석의 '추천 추적 성적표'에서 수익률과 목표·경계 도달 여부를 확인할 수 있습니다.")


# =========================================================== 1. 종합현황 ====
def menu_overview(rules: core.FilterRules) -> None:
    st.subheader("🏠 1. 종합 투자현황")
    holdings = load_store("holdings")
    trades = extra("trades")
    dividends = extra("dividends")
    meta = meta_state()
    if not holdings and not meta.get("cash"):
        st.info("아직 보유 종목·예수금이 없습니다. **2. 보유종목 관리**에서 종목을 추가하고, "
                "**10. 설정 및 데이터 관리**에서 예수금을 입력하면 총자산·수익률이 계산됩니다.")
        with st.expander("화면 미리보기용 예시(저장되지 않습니다)"):
            st.write(pd.DataFrame([
                {"종목": "두산에너빌리티", "코드": "034020", "수량": 10, "평단가": 98000},
                {"종목": "대우건설", "코드": "047040", "수량": 40, "평단가": 20000},
                {"종목": "KB금융", "코드": "105560", "수량": 10, "평단가": 70000},
            ]))
        st.caption("예수금·거래·배당은 10번 메뉴에서 입력하고, CSV/JSON으로 내보내 백업할 수 있습니다.")
        return

    by_code, fx = snapshots_for(holdings)
    summary = core.portfolio_summary(holdings, by_code, trades, dividends, meta, fx=fx)
    risk = core.portfolio_risk(summary)

    r1 = st.columns(4)
    r1[0].metric("총자산", f"{summary['total_assets']:,.0f}원",
                 help="평가금액 + 예수금")
    r1[1].metric("평가금액", f"{summary['market_value']:,.0f}원",
                 f"{summary['unrealized_pct']:+.1f}%" if summary["unrealized_pct"] is not None else None)
    r1[2].metric("예수금", f"{summary['cash']:,.0f}원",
                 f"{summary['cash_ratio']:.1f}%" if summary["cash_ratio"] is not None else None,
                 delta_color="off")
    r1[3].metric("투자원금(보유 매입)", f"{summary['invested']:,.0f}원", help="보유 종목의 매입금액 합계")

    r2 = st.columns(4)
    r2[0].metric("평가손익", f"{summary['unrealized']:+,.0f}원",
                 f"{summary['unrealized_pct']:+.1f}%" if summary["unrealized_pct"] is not None else None)
    r2[1].metric("실현손익(순)", f"{summary['realized']:+,.0f}원",
                 f"총 {summary['realized_gross']:+,.0f}원 - 비용", delta_color="off")
    r2[2].metric("누적배당", f"{summary['dividends']:,.0f}원",
                 f"세전 {summary['dividends_gross']:,.0f}원", delta_color="off")
    r2[3].metric("수수료·세금", f"{summary['fees'] + summary['taxes']:,.0f}원",
                 f"수수료 {summary['fees']:,.0f} · 세금 {summary['taxes']:,.0f}", delta_color="off")

    r3 = st.columns(4)
    r3[0].metric("총 순수익", f"{summary['total_return']:+,.0f}원")
    r3[1].metric("총 순수익률", f"{summary['total_return_pct']:+.1f}%"
                 if summary["total_return_pct"] is not None else "-",
                 help="(평가손익 + 실현손익 - 비용 + 배당) / (보유 매입원금 + 매도원가)")
    r3[2].metric("보유 종목", f"{len(holdings)}종목")
    r3[3].metric("위험 점수", f"{risk['score']}점 · {risk['level']}",
                 f"경고 {risk['major'] + risk['warn']}건", delta_color="off")

    st.caption(basis_caption(summary["rows"],
                             f"총 순수익률 분모 {summary['capital_base']:,.0f}원(보유 매입원금+매도원가)"))

    if summary["unmatched_sells"]:
        st.warning(f"매수 기록 없이 매도만 있는 거래 {summary['unmatched_sells']}건은 실현손익에서 제외했습니다. "
                   "9번 메뉴에서 매수 내역을 함께 입력하면 정확해집니다.")

    left, right = st.columns([1.1, 1])
    with left:
        st.markdown("#### 자산 구성")
        comp = [{"구분": "주식 평가금액", "금액": summary["market_value"]},
                {"구분": "예수금", "금액": summary["cash"]}]
        show_chart(money_bar(comp, "구분", "금액", "총자산 구성", height=170,
                             horizontal=True, color="#1f4e9c"))
        if summary["rows"]:
            st.dataframe(pd.DataFrame(pick_cols(summary["rows"],
                                                ["종목", "평가금액", "비중(%)", "평가손익", "수익률(%)",
                                                 "재무상태", "투자판단"])),
                         hide_index=True, width="stretch",
                         column_config={
                             "평가금액": st.column_config.NumberColumn(format="%.0f"),
                             "비중(%)": st.column_config.NumberColumn(format="%.1f"),
                             "평가손익": st.column_config.NumberColumn(format="%.0f"),
                             "수익률(%)": st.column_config.NumberColumn(format="%.1f"),
                         })
        else:
            st.caption("보유 종목이 없습니다. 2번 메뉴에서 추가하세요.")
    with right:
        st.markdown("#### 섹터 비중")
        show_chart(sector_pie(risk["sector_weights"], "보유 평가금액 기준"))

    st.markdown("#### ⚠️ 지금 확인할 것")
    priority = [w for w in risk["warnings"] if w["level"] in ("major", "warn")][:5]
    if not priority:
        st.success("집중도·재무·손절선에서 큰 경고가 없습니다. 분기 실적과 뉴스로 논리를 점검하세요.")
    for w in priority:
        st.markdown(f"{LEVEL_ICON.get(w['level'], '•')} **{w['title']}** — {w['detail']}  \n"
                    f"<span class='vi-note'>→ {w['action']}</span>", unsafe_allow_html=True)
    st.caption("자세한 진단은 8번 메뉴 '위험관리 및 투자진단'에서 볼 수 있습니다.")


# =========================================================== 2. 보유종목 ====
def menu_holdings(rules: core.FilterRules) -> None:
    st.subheader("📋 2. 보유종목 관리")
    holdings = load_store("holdings")
    if not holdings:
        st.info("보유 종목이 아직 없습니다. 아래에서 추가하거나, 4번 메뉴에서 발굴한 종목을 관심종목에 담아 보세요.")
        with st.expander("화면 미리보기용 예시 데이터(저장 안 함)", expanded=False):
            st.write(pd.DataFrame([
                {"종목": "두산에너빌리티", "코드": "034020", "수량": 10, "평단가": 98000},
                {"종목": "대우건설", "코드": "047040", "수량": 40, "평단가": 20000},
                {"종목": "KB금융", "코드": "105560", "수량": 10, "평단가": 70000},
            ]))
        render_holding_manager(holdings)
        return

    by_code, fx = snapshots_for(holdings)
    dividends = extra("dividends")
    rows = core.holdings_rows(holdings, by_code, fx, dividends)
    total_cost = sum(r["매입금액"] for r in rows)
    total_value = sum(r["평가금액"] or 0 for r in rows)
    profit = total_value - total_cost
    ret = (profit / total_cost * 100) if total_cost else 0.0

    m = st.columns(5)
    m[0].metric("총 매수금액", f"{total_cost:,.0f}원")
    m[1].metric("총 평가금액", f"{total_value:,.0f}원")
    m[2].metric("평가손익", f"{profit:+,.0f}원", f"{ret:+.1f}%")
    m[3].metric("보유 종목", f"{len(holdings)}종목")
    div_avg = [r["배당수익률(%)"] for r in rows if r["배당수익률(%)"] is not None]
    m[4].metric("평균 배당수익률", f"{sum(div_avg) / len(div_avg):.2f}%" if div_avg else "-")

    table_state = st.dataframe(
        pd.DataFrame(pick_cols(rows, HOLD_COLUMNS)), hide_index=True, width="stretch",
        on_select="rerun", selection_mode="single-row", key="holdings_table",
        column_config={
            "평단가": st.column_config.NumberColumn(format="%.0f"),
            "현재가": st.column_config.NumberColumn(format="%.2f"),
            "평가금액": st.column_config.NumberColumn(format="%.0f"),
            "비중(%)": st.column_config.NumberColumn(format="%.1f"),
            "평가손익": st.column_config.NumberColumn(format="%.0f"),
            "수익률(%)": st.column_config.NumberColumn(format="%.1f"),
            "PER": st.column_config.NumberColumn(format="%.2f"),
            "PBR": st.column_config.NumberColumn(format="%.2f"),
            "배당수익률(%)": st.column_config.NumberColumn(format="%.2f"),
            "수령배당(원)": st.column_config.NumberColumn(format="%.0f"),
            "적정가": st.column_config.NumberColumn(format="%.0f"),
            "안전마진(%)": st.column_config.NumberColumn(format="%.1f"),
            "재무상태": st.column_config.TextColumn(width="small"),
            "투자판단": st.column_config.TextColumn(width="small"),
        },
    )
    st.caption("**재무상태**는 기업의 재무 건전성(영업적자·고부채·재무주의·재무양호), "
               "**투자판단**은 가격(저평가·적정·고평가 및 매수검토·보유·신중)입니다. 두 열은 서로 다른 기준입니다. "
               + basis_caption(rows, "평가금액·손익은 원화 환산(해외 종목은 원/달러 적용)"))

    st.divider()
    st.markdown("#### 🔍 종목 상세 (표에서 행을 클릭하거나 아래에서 선택)")
    clicked = list(getattr(getattr(table_state, "selection", None), "rows", []) or [])
    if clicked:
        st.session_state["holding_detail_pick"] = int(clicked[0])
    labels = [f"{h.get('name') or h['code']} ({core.clean_code(h['code'])})" for h in holdings]
    pick = st.selectbox("상세 볼 종목", range(len(holdings)), format_func=lambda i: labels[i],
                        key="holding_detail_pick")
    h = holdings[pick]
    snap = by_code.get(core.clean_code(h["code"])) or core.StockSnapshot(code=h["code"])
    avg = core.parse_num(h.get("avg_price"))
    render_detail(snap, rules, key=f"hold_{snap.code}",
                  avg_price=float(avg) if avg else None)

    st.divider()
    with st.expander("🧭 보유 종목 한눈 요약 (시나리오 기준)", expanded=False):
        for r in rows:
            st.markdown(
                f"- **{r['종목']}** · 현재 {money(r['현재가'], r['통화'])} · 수익률 {pct_text(r['수익률(%)'])} · "
                f"{r['재무상태']} / {r['투자판단']} · 적정가 {money(r['적정가'], r['통화'])}")

    render_holding_manager(holdings)


def render_holding_manager(holdings: list[dict]) -> None:
    with st.expander("➕ 보유 종목 관리 (추가 · 수정 · 삭제)", expanded=False):
        add_col, edit_col = st.columns(2)
        with add_col:
            st.markdown("**종목 추가** (종목명·티커·코드 모두 가능)")
            with st.form("add_holding"):
                code = st.text_input("종목 (예: HPSP, 삼성전자, 034020, AAPL)", key="add_code")
                qty = st.number_input("수량", min_value=0.0, step=1.0, value=1.0, key="add_qty")
                avg = st.number_input("평단가", min_value=0.0, step=100.0, value=0.0, key="add_avg")
                memo = st.text_input("메모 (선택)", key="add_memo")
                if st.form_submit_button("추가", width="stretch"):
                    if not code.strip():
                        st.error("종목을 입력하세요.")
                    elif qty <= 0 or avg <= 0:
                        st.error("수량과 평단가는 0보다 커야 합니다.")
                    else:
                        found = core.search_stocks(code, limit=1)
                        if not found:
                            st.error(f"'{code}' 종목을 찾지 못했습니다.")
                        else:
                            item = found[0]
                            new = {"code": item["code"], "name": item["name"],
                                   "qty": qty, "avg_price": avg, "memo": memo.strip()}
                            others = [h for h in holdings
                                      if core.clean_code(h["code"]) != new["code"]]
                            if persist("holdings", others + [new],
                                       f"{new['name'] or new['code']}({new['code']}) 종목을 저장했습니다."):
                                st.rerun()

        with edit_col:
            st.markdown("**수정 / 삭제**")
            if not holdings:
                st.caption("보유 종목이 없습니다.")
            else:
                labels = [f"{h.get('name') or h['code']} ({core.clean_code(h['code'])})" for h in holdings]
                pick = st.selectbox("종목 선택", range(len(holdings)), format_func=lambda i: labels[i],
                                    key="edit_pick")
                target = holdings[pick]
                with st.form("edit_holding"):
                    new_qty = st.number_input("수량", min_value=0.0, step=1.0,
                                              value=float(target.get("qty") or 0), key="edit_qty")
                    new_avg = st.number_input("평단가", min_value=0.0, step=100.0,
                                              value=float(target.get("avg_price") or 0), key="edit_avg")
                    new_memo = st.text_input("메모", value=target.get("memo", ""), key="edit_memo")
                    saved = st.form_submit_button("수정 저장", width="stretch")
                if saved:
                    updated = [dict(h) for h in holdings]
                    updated[pick].update({"qty": new_qty, "avg_price": new_avg, "memo": new_memo})
                    if persist("holdings", updated, "수정했습니다."):
                        st.rerun()
                confirm = st.checkbox("정말 삭제합니다 (되돌릴 수 없음)", key="del_confirm")
                if st.button("선택 종목 삭제", disabled=not confirm, key="del_btn", width="stretch"):
                    remaining = [h for i, h in enumerate(holdings) if i != pick]
                    if persist("holdings", remaining, "삭제했습니다."):
                        st.rerun()


# =========================================================== 3. 재무분석 ====
def menu_financials(rules: core.FilterRules) -> None:
    st.subheader("📊 3. 기업 재무분석")
    holdings = load_store("holdings")
    st.caption("실적·성장성·수익성·재무건전성·배당 이력·3~5년 추이·최근 5거래일 수급을 한 화면에서 봅니다.")
    snap = stock_picker("fin", holdings)
    if snap is None:
        st.info("종목을 선택하거나 이름·티커로 검색하세요. 보유 종목은 목록에 자동으로 나옵니다.")
        return
    financial_analysis_block(snap, "fin", compact=False)


# =========================================================== 4. 적정주가 ====
def menu_valuation(rules: core.FilterRules) -> None:
    st.subheader("💰 4. 적정주가 분석 · 저평가 우량주 발굴")
    holdings = load_store("holdings")

    st.markdown("### 종목 적정가 분석")
    snap = stock_picker("val", holdings)
    if snap is not None:
        with st.spinner("적정가를 계산하는 중..."):
            trend = core.fetch_financial_history(snap.code, years=4)
            val = core.valuate(snap, trend)
        health = core.financial_health(snap)
        judgment = core.investment_judgment(snap, val)
        st.markdown(f"#### {snap.name}({snap.code}) · {snap.sector}")
        st.caption(f"현재가 기준 {core.price_as_of_text(snap)} · 재무자료 기준 "
                   f"{core.financial_basis_text(snap, trend)}")
        badges_line(health, judgment)
        valuation_block(snap, val)
        filt = core.run_filter(snap, rules)
        st.caption(f"가치함정 필터: {filt['verdict']} · {filt['score']}점 — {filt['reason']}")
        if st.button("⭐ 이 종목을 관심종목에 추가", key=f"val_watch_{snap.code}"):
            try:
                added, _ = core.add_watchlist(snap.code, snap.name)
                st.success("관심종목에 추가했습니다." if added else "이미 관심종목에 있습니다.")
            except core.StorageError as exc:
                st.error(f"관심종목 저장 실패: {exc}")
    else:
        st.info("종목을 선택하면 PER·PBR·DCF 3방식 적정가와 안전마진, 4단계 대응 구간을 보여줍니다.")

    st.divider()
    st.markdown("### 🔍 407종목 저평가 우량주 스크리너")
    st.caption(f"유니버스 {len(core.UNIVERSE)}종목을 같은 기준으로 훑고, 통과/탈락 사유와 가치점수(0~100)를 "
               f"함께 보여줍니다. 현재 기준: {rules.summary()}")

    f1, f2, f3 = st.columns([1.1, 1.6, 1])
    with f1:
        markets = st.multiselect("시장", core.MARKET_LABELS[1:],
                                 default=["코스피", "코스닥", "미국"], key="scan_markets")
    with f2:
        sectors = st.multiselect("섹터 (비우면 전체)", core.SECTOR_ORDER, default=[], key="scan_sectors")
    with f3:
        scope = st.radio("스캔 범위", ["빠른 스캔(150)", "전체 스캔", "상위 60"], index=0, key="scan_scope")
    extra_codes = st.text_input("직접 입력 종목 추가 (쉼표로 구분: 이름·코드·티커)",
                                placeholder="예: HPSP, 클래시스, 엔비디아", key="scan_extra")

    if st.button("🔍 스캔 실행", key="scan_btn", type="primary"):
        limit = {"빠른 스캔(150)": 150, "전체 스캔": None, "상위 60": 60}[scope]
        items = core.universe_items(sectors=sectors or None, markets=markets or None, limit=limit)
        known = {i["code"] for i in items}
        for raw in [c.strip() for c in extra_codes.split(",") if c.strip()]:
            found = core.search_stocks(raw, limit=1)
            if not found:
                st.warning(f"'{raw}' 종목을 찾지 못했습니다.")
                continue
            item = found[0]
            if item["code"] not in known:
                sector = item.get("sector") or core.search_sector(item["code"]) or "기타"
                items.append({"code": item["code"], "name": item["name"], "sector": sector,
                              "market": item.get("board") or "US"})
                known.add(item["code"])
        bar = st.progress(0.0, text=f"{len(items)}개 종목 스캔 준비...")

        def on_progress(done: int, total: int) -> None:
            try:
                bar.progress(min(1.0, done / max(1, total)), text=f"{done}/{total} 종목 조회 중...")
            except Exception:
                pass

        rows = core.scan_universe(items, rules, workers=10, progress=on_progress)
        bar.empty()
        st.session_state["scan_rows"] = rows
        st.session_state["scan_meta"] = {"markets": markets, "sectors": sectors, "scope": scope}

    rows = st.session_state.get("scan_rows")
    if not rows:
        st.info("'🔍 스캔 실행'을 누르면 후보를 훑어 통과 종목과 점수를 보여줍니다. "
                "150종목 기준 대략 30~60초 걸립니다.")
        render_watchlist()
        return

    passed = [r for r in rows if r["filter"]["verdict"] == "통과"]
    wait = [r for r in rows if r["filter"]["verdict"] == "판정 보류"]
    failed = [r for r in rows if r["filter"]["verdict"] not in ("통과", "판정 보류")]
    m = st.columns(4)
    m[0].metric("스캔 종목", f"{len(rows)}개")
    m[1].metric("✅ 통과", f"{len(passed)}개")
    m[2].metric("⚠️ 판정 보류", f"{len(wait)}개")
    m[3].metric("❌ 탈락", f"{len(failed)}개")

    table = []
    for r in rows:
        s, f = r["snap"], r["filter"]
        val = core.valuate(s)
        health = core.financial_health(s)
        judgment = core.investment_judgment(s, val)
        table.append({
            "판정": f["verdict"], "종목": s.name or s.code, "코드": s.code, "섹터": s.sector,
            "현재가": s.price, "PER": s.per, "PBR": s.pbr, "부채비율(%)": s.debt_ratio,
            "ROE(%)": s.roe, "가치점수": f["score"],
            "재무상태": health["badge"], "투자판단": judgment["full"],
            "적정가": val.fair, "안전마진(%)": val.safety_margin, "사유": f["reason"],
        })
    scan_state = st.dataframe(
        pd.DataFrame(table), hide_index=True, width="stretch", height=430,
        on_select="rerun", selection_mode="single-row", key="scan_table",
        column_config={
            "현재가": st.column_config.NumberColumn(format="%.2f"),
            "PER": st.column_config.NumberColumn(format="%.2f"),
            "PBR": st.column_config.NumberColumn(format="%.2f"),
            "부채비율(%)": st.column_config.NumberColumn(format="%.1f"),
            "ROE(%)": st.column_config.NumberColumn(format="%.1f"),
            "가치점수": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d"),
            "적정가": st.column_config.NumberColumn(format="%.0f"),
            "안전마진(%)": st.column_config.NumberColumn(format="%.1f"),
        })
    st.caption("재무상태와 투자판단은 서로 다른 기준입니다. 스캔은 네이버 데이터 기준(빠른 조회)이라 "
               "영업현금흐름·DCF는 확인되는 종목만 계산됩니다.")

    candidates = passed + wait
    st.markdown("#### ✅ 기준 통과·보류 종목 — 상세 보기")
    if not candidates:
        st.caption("이번 기준을 모두 통과한 종목이 없습니다. 기준을 조금 완화하거나 직접 입력으로 종목을 추가해 보세요.")
        render_watchlist()
        return
    labels = [f"{r['snap'].name or r['snap'].code} · {r['snap'].sector} · "
              f"{r['filter']['score']}점 · {r['filter']['verdict']}" for r in candidates]
    clicked = list(getattr(getattr(scan_state, "selection", None), "rows", []) or [])
    if clicked and 0 <= int(clicked[0]) < len(rows):
        code = rows[int(clicked[0])]["snap"].code
        for idx, cand in enumerate(candidates):
            if cand["snap"].code == code:
                st.session_state["scan_detail_pick"] = idx
                break
    pick = st.selectbox("상세 볼 종목", range(len(candidates)), format_func=lambda i: labels[i],
                        key="scan_detail_pick")
    chosen = candidates[pick]["snap"]
    w1, w2 = st.columns([1, 3])
    with w1:
        if st.button("⭐ 관심종목에 추가", key=f"scan_watch_{chosen.code}"):
            try:
                added, _ = core.add_watchlist(chosen.code, chosen.name)
                st.success("관심종목에 추가했습니다." if added else "이미 관심종목에 있습니다.")
            except core.StorageError as exc:
                st.error(f"관심종목 저장 실패: {exc}")
    with w2:
        st.caption("관심종목은 5번(매매 전략)·3번(재무분석) 메뉴의 종목 선택 목록에 나타납니다.")
    render_detail(chosen, rules, key=f"scan_{chosen.code}")

    with st.expander("통과 종목 요약표", expanded=False):
        st.dataframe(pd.DataFrame([
            {"종목": r["snap"].name, "코드": r["snap"].code, "섹터": r["snap"].sector,
             "가치점수": r["filter"]["score"], "사유": r["filter"]["reason"]}
            for r in candidates]), hide_index=True, width="stretch")

    render_watchlist()


def render_watchlist() -> None:
    st.divider()
    st.markdown("### ⭐ 관심종목 (워치리스트)")
    try:
        watch = core.load_watchlist()
    except core.StorageError as exc:
        st.error(f"관심종목을 읽지 못했습니다: {exc}")
        return
    if not watch:
        st.caption("아직 관심종목이 없습니다. 위의 '⭐ 관심종목에 추가'를 누르면 여기에 쌓입니다.")
        return
    st.dataframe(pd.DataFrame([{"종목": w.get("name"), "코드": w["code"],
                                "등록일": w.get("added"), "메모": w.get("note")} for w in watch]),
                 hide_index=True, width="stretch")
    labels = [f"{w.get('name') or w['code']} ({w['code']})" for w in watch]
    pick = st.selectbox("삭제할 관심종목", range(len(watch)), format_func=lambda i: labels[i],
                        key="watch_del_pick")
    confirm = st.checkbox("관심종목에서 삭제합니다", key="watch_del_confirm")
    if st.button("삭제", disabled=not confirm, key="watch_del_btn"):
        try:
            core.remove_watchlist(watch[pick]["code"])
            st.success("삭제했습니다.")
            st.rerun()
        except core.StorageError as exc:
            st.error(f"삭제하지 못했습니다: {exc}")


# =========================================================== 5. 매매전략 ====
def menu_strategy(rules: core.FilterRules) -> None:
    st.subheader("🧭 5. 매매 전략 관리")
    holdings = load_store("holdings")
    st.caption("종목별로 판단 근거, 1차·추가 매수, 목표매도, 손절 가격과 투자논리 훼손 조건을 정해 두고 "
               "분기마다 같은 기준으로 점검합니다.")
    snap = stock_picker("stg", holdings)
    if snap is None:
        st.info("전략을 세울 종목을 선택하세요.")
        return
    avg = avg_price_of(snap.code, holdings)
    filt = core.run_filter(snap, rules)
    with st.spinner("적정가·전략을 계산하는 중..."):
        val = core.valuate(snap)
        saved = (extra("strategies") or {}).get(snap.code, {})
        plan = core.build_strategy(snap, avg_price=avg, valuation=val, filt=filt, saved=saved)

    st.markdown(f"#### {snap.name}({snap.code}) · {snap.sector} · "
                f"{'보유 중 (평단 ' + money(avg, snap.currency) + ')' if avg else '미보유'}")
    st.caption(f"현재가 기준 {plan['as_of']} · 재무자료 기준 {plan['finance_basis']} · "
               f"마지막 갱신 {st.session_state.get('refresh_time', '-')}")
    badges_line(plan["health"], plan["judgment"])

    cur = plan["currency"]
    m = st.columns(6)
    m[0].metric("현재가", money(snap.price, cur))
    m[1].metric("1차 매수", money(plan["buy1"], cur),
                f"{plan['buy1_gap']:+.1f}%" if plan["buy1_gap"] is not None else None, delta_color="off")
    m[2].metric("추가 매수", money(plan["buy2"], cur),
                f"{plan['buy2_gap']:+.1f}%" if plan["buy2_gap"] is not None else None, delta_color="off")
    m[3].metric("보유 기준(적정가)", money(plan["hold"], cur),
                f"안전마진 {plan['safety_margin']:+.1f}%" if plan["safety_margin"] is not None else None,
                delta_color="off")
    m[4].metric("목표(매도)", money(plan["target"], cur),
                f"{plan['target_gap']:+.1f}%" if plan["target_gap"] is not None else None, delta_color="off")
    m[5].metric("손절", money(plan["stop"], cur),
                f"{plan['stop_gap']:+.1f}%" if plan["stop_gap"] is not None else None, delta_color="off")

    with st.form(f"strategy_form_{snap.code}"):
        st.markdown("**가격 기준 조정** (0이면 자동 계산값 사용)")
        c1, c2, c3, c4 = st.columns(4)
        buy1 = c1.number_input("1차 매수", min_value=0.0, step=100.0,
                               value=float(plan["buy1"] or 0), key=f"stg_buy1_{snap.code}")
        buy2 = c2.number_input("추가 매수", min_value=0.0, step=100.0,
                               value=float(plan["buy2"] or 0), key=f"stg_buy2_{snap.code}")
        target = c3.number_input("목표(매도)", min_value=0.0, step=100.0,
                                 value=float(plan["target"] or 0), key=f"stg_target_{snap.code}")
        stop = c4.number_input("손절", min_value=0.0, step=100.0,
                               value=float(plan["stop"] or 0), key=f"stg_stop_{snap.code}")
        memo = st.text_area("전략 메모 (왜 이 가격에 사고, 무엇을 확인할지)",
                            value=plan.get("memo", ""), height=90, key=f"stg_memo_{snap.code}")
        submitted = st.form_submit_button("💾 전략 저장", width="stretch")
    if submitted:
        checks = {item["key"]: bool(st.session_state.get(f"chk_{snap.code}_{item['key']}", item["checked"]))
                  for item in plan["checks"]}
        if persist_extra("strategies", {**(extra("strategies") or {}), snap.code: {
                "buy1": buy1 or None, "buy2": buy2 or None, "target": target or None,
                "stop": stop or None, "memo": memo, "checks": checks,
                "updated": core.now_text()}}, "전략을 저장했습니다."):
            st.rerun()

    if snap.price and plan["stop"] and snap.price <= plan["stop"]:
        st.warning(f"현재가 {money(snap.price, cur)}이(가) 이미 손절선 {money(plan['stop'], cur)}"
                   f"(평단 -15%)을 밑돌고 있습니다. 원칙을 지킬지, 실적 회복 근거로 보유할지 정해 두세요.")

    st.markdown("**판단 근거**")
    for line in plan["basis"]:
        st.markdown(f"- {line}")

    st.markdown("**투자논리 훼손 조건 체크리스트**")
    st.caption("이미 해당되는 항목은 체크된 상태로 표시됩니다. 하나라도 새로 체크되면 보유 논리를 다시 세우세요.")
    for item in plan["checks"]:
        cols = st.columns([1, 3])
        with cols[0]:
            st.checkbox(item["label"], value=bool(item["checked"]),
                        key=f"chk_{snap.code}_{item['key']}")
        with cols[1]:
            st.caption(item["detail"])

    st.markdown("**시나리오**")
    scenario_cards(plan["scenario"])


# =========================================================== 6. 시장분석 ====
def menu_market(rules: core.FilterRules) -> None:
    st.subheader("🌐 6. 시장 및 산업 분석")
    with st.spinner("거시 지표를 불러오는 중..."):
        macro = core.fetch_macro()
    st.markdown("#### 🌐 오늘의 시장 지표")
    row1 = macro.items[:6]
    cols = st.columns(len(row1)) if row1 else []
    for col, item in zip(cols, row1):
        with col:
            delta = item.change_text() if (item.change is not None or item.change_pct is not None) else None
            st.metric(item.label, item.value_text(), delta)
            st.caption(item.extra or (item.as_of[:16].replace("T", " ") if item.as_of else "확인 불가"))
    row2 = macro.items[6:]
    if row2:
        cols2 = st.columns(len(row2))
        for col, item in zip(cols2, row2):
            with col:
                delta = item.change_text() if (item.change is not None or item.change_pct is not None) else None
                st.metric(item.label, item.value_text(), delta)
                st.caption(item.as_of[:16].replace("T", " ") if item.as_of else "확인 불가")
    st.caption(f"출처: 네이버 금융 지수·시장지표, 뉴욕 연준(EFFR) · 기준 {macro.as_of or '-'} · "
               f"지표 {macro.ok_count()}/{len(macro.items)}개 확인 · 마지막 갱신 "
               f"{st.session_state.get('refresh_time', '-')}")

    if macro.flows:
        st.markdown("**시장 수급 (단위: 억원)**")
        st.dataframe(pd.DataFrame([{
            "시장": f.label, "기준일": f.date, "외국인": f.foreign, "기관": f.institution,
            "개인": f.personal,
            "거래대금": core.compact_money(f.trading_value) if f.trading_value else "-",
        } for f in macro.flows]), hide_index=True, width="stretch",
            column_config={
                "외국인": st.column_config.NumberColumn(format="%,d"),
                "기관": st.column_config.NumberColumn(format="%,d"),
                "개인": st.column_config.NumberColumn(format="%,d"),
            })

    st.markdown("**금리·환율·원자재**")
    rate_items = [i for i in macro.items if i.key in ("KR10Y", "US10Y", "FED", "USDKRW", "WTI", "GOLD", "COPPER")]
    st.dataframe(pd.DataFrame([{"지표": i.label, "값": i.value_text(), "변동": i.change_text(),
                                "기준": i.as_of[:16].replace("T", " ") if i.as_of else "-",
                                "출처": i.source} for i in rate_items]),
                 hide_index=True, width="stretch")

    st.divider()
    st.markdown("#### 🏭 산업 동향 (업종 대표 종목 등락 평균)")
    with st.spinner("산업별 대표 종목을 확인하는 중..."):
        pulses = core.fetch_sector_pulse()
    cols = st.columns(len(pulses)) if pulses else []
    for col, pulse in zip(cols, pulses):
        with col:
            st.metric(pulse.label, pulse.text() if pulse.change_pct is not None else "확인 불가",
                      delta_color="off")
            members = ", ".join(f"{m['name']} {m['change_pct']:+.1f}%" for m in pulse.members[:3])
            st.caption(members if members else pulse.note)

    st.divider()
    st.markdown("#### 🧭 이슈 → 내 종목·관심섹터 영향")
    holdings = load_store("holdings")
    with st.spinner("영향을 계산하는 중..."):
        news_map = core.fetch_market_news()
        report = core.analyze_impact(macro, news_map, holdings=holdings)
    counts = report.tag_counts()
    c = st.columns(4)
    c[0].metric("분석 종목", f"{len(report.rows)}개")
    c[1].metric("🟢 호재", f"{counts['🟢 호재']}개")
    c[2].metric("🔴 주의", f"{counts['🔴 주의']}개")
    c[3].metric("⚪ 중립", f"{counts['⚪ 중립']}개")
    if report.sectors:
        st.dataframe(pd.DataFrame([{
            "섹터": s.sector, "판단": s.tag, "점수": round(s.score, 1), "종목 수": s.holdings,
            "핵심 근거": " / ".join(s.reasons[:2]),
        } for s in report.sectors]), hide_index=True, width="stretch")
    for row in report.rows[:12]:
        with st.expander(f"{row.tag} {row.name} ({row.code}) · {row.sector} · 점수 {row.score:+.1f}",
                         expanded=False):
            for reason in row.reasons[:6]:
                st.markdown(f"- {reason}")
            if row.news:
                st.caption("관련 헤드라인: " + " / ".join(n.get("title", "") for n in row.news))
    st.markdown("**시장 요약**")
    for line in report.brief:
        st.markdown(f"- {line}")

    if core.ai_available():
        if st.button("🤖 AI 해설 생성", key="impact_ai"):
            with st.spinner("AI가 해설을 작성하는 중..."):
                text, err = core.generate_ai_text(core.build_impact_prompt(report, macro))
            if text:
                st.markdown(text)
            else:
                st.error(err)
    else:
        st.caption("AI 키를 넣으면 이 자리에 문장형 해설이 추가됩니다(10번 메뉴 안내). "
                   "키가 없어도 규칙 기반 해석은 그대로 동작합니다.")


# =========================================================== 7. 뉴스분석 ====
def menu_news(rules: core.FilterRules) -> None:
    st.subheader("📰 7. 뉴스 및 공시 분석")
    st.caption("실제 기사 URL과 피드 게시 시각만 표시합니다. 없는 기사·링크·시각은 만들지 않습니다. "
               "호재/악재는 제목 키워드 기반 참고 판단입니다.")
    with st.spinner("시장 뉴스를 모으는 중..."):
        news_map = core.fetch_market_news(per_topic=4)
    digest = core.digest_from_news_map(news_map, limit=48)
    counts = digest.counts()
    m = st.columns(4)
    m[0].metric("수집 기사", f"{len(digest.items)}건")
    m[1].metric("🟢 호재", f"{counts['🟢 호재']}건")
    m[2].metric("🔴 악재", f"{counts['🔴 악재']}건")
    m[3].metric("⚪ 중립", f"{counts['⚪ 중립']}건")
    st.caption(f"수집 기준 {digest.generated_at} · 출처 Google News RSS(원문 링크·게시 시각 그대로)")

    recent = core.recent_news(digest, hours=24)
    st.markdown("#### ⏰ 최근 24시간 주요 뉴스")
    if recent:
        for item in recent[:10]:
            cls = {"🟢 호재": "vi-pos", "🔴 악재": "vi-neg"}.get(item.sentiment, "vi-mid")
            st.markdown(
                f"<div class='vi-news'><span class='{cls}'>{item.sentiment}</span> "
                f"<a href='{item.link}' target='_blank'>{item.title}</a><br>"
                f"<span class='vi-note'>{item.source} · 게시 {item.published} · {item.category}</span></div>",
                unsafe_allow_html=True)
    else:
        st.caption("최근 24시간 내 게시 시각이 확인된 기사가 없습니다(피드 시각 기준).")

    st.divider()
    st.markdown("#### 🗂️ 분류별 뉴스·공시")
    news_digest_block(digest)

    st.divider()
    st.markdown("#### 📌 주가 영향 요약")
    news_impact_block(digest)

    st.divider()
    holdings = load_store("holdings")
    st.markdown("#### 📰 보유종목 관련 뉴스 자동분석")
    if not holdings:
        st.caption("보유 종목이 등록되면 종목별 뉴스·공시·리포트를 근거(URL·시각)와 함께 모아 분류합니다.")
        return
    for h in holdings[:6]:
        code = core.clean_code(h["code"])
        digest_h = core.fetch_news_digest(h.get("name") or code,
                                          code=code if core.detect_market(code) == "KR" else "",
                                          limit=8)
        with st.expander(f"{h.get('name') or code} ({code}) · 수집 {len(digest_h.items)}건",
                         expanded=False):
            news_digest_block(digest_h, h.get("name", ""))
            news_impact_block(digest_h, "", h.get("name", ""))


# =========================================================== 8. 위험진단 ====
def menu_risk(rules: core.FilterRules) -> None:
    st.subheader("🛡️ 8. 위험관리 및 투자진단")
    holdings = load_store("holdings")
    if not holdings:
        st.info("보유 종목이 없어 진단할 포트폴리오가 없습니다. 2번 메뉴에서 종목을 추가하세요.")
        return
    by_code, fx = snapshots_for(holdings)
    summary = core.portfolio_summary(holdings, by_code, extra("trades"), extra("dividends"),
                                     meta_state(), fx=fx)
    risk = core.portfolio_risk(summary)

    m = st.columns(4)
    m[0].metric("위험 점수", f"{risk['score']}점", risk["level"], delta_color="off")
    m[1].metric("경고", f"{risk['major']}건", f"주의 {risk['warn']} · 참고 {risk['info']}")
    top = max(summary["rows"], key=lambda r: r["비중(%)"] or 0) if summary["rows"] else None
    m[2].metric("최대 단일 종목", f"{top['종목']} {top['비중(%)']:.1f}%" if top else "-")
    if risk["sector_weights"]:
        s_name, s_weight = max(risk["sector_weights"].items(), key=lambda kv: kv[1])
        m[3].metric("최대 섹터", f"{s_name} {s_weight:.1f}%")
    st.caption(basis_caption(summary["rows"], f"총자산 {summary['total_assets']:,.0f}원 중 예수금 "
                                             f"{summary['cash']:,.0f}원({summary['cash_ratio'] or 0:.1f}%)"))

    st.markdown("#### 진단 결과")
    if not risk["warnings"]:
        st.success("집중도·섹터 편중·재무·손절선에서 걸리는 항목이 없습니다.")
    for w in risk["warnings"]:
        st.markdown(f"{LEVEL_ICON.get(w['level'], '•')} **{w['title']}**  \n"
                    f"{w['detail']}  \n<span class='vi-note'>→ {w['action']}</span>",
                    unsafe_allow_html=True)

    left, right = st.columns(2)
    with left:
        st.markdown("#### 섹터 비중")
        show_chart(sector_pie(risk["sector_weights"], "평가금액 기준"))
    with right:
        st.markdown("#### 자산 구성")
        comp = [{"구분": "주식", "금액": summary["market_value"]},
                {"구분": "예수금", "금액": summary["cash"]}]
        show_chart(money_bar(comp, "구분", "금액", "총자산 구성", height=170,
                             horizontal=True, color="#1f4e9c"))
        st.markdown("#### 분산 제안")
        for line in risk["suggestions"]:
            st.markdown(f"- {line}")

    st.markdown("#### 종목별 위험 체크")
    rows = []
    for r in summary["rows"]:
        avg, price = r["평단가"], r["현재가"]
        stop = avg * 0.85 if avg else None
        rows.append({
            "종목": r["종목"], "비중(%)": r["비중(%)"], "수익률(%)": r["수익률(%)"],
            "재무상태": r["재무상태"], "투자판단": r["투자판단"],
            "손절선": stop, "현재가": price,
            "손절선 대비(%)": ((price / stop - 1) * 100) if (price and stop) else None,
            "적자/고부채": "해당" if (r.get("_health") or {}).get("label") in ("영업적자", "고부채") else "",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={
                     "비중(%)": st.column_config.NumberColumn(format="%.1f"),
                     "수익률(%)": st.column_config.NumberColumn(format="%.1f"),
                     "손절선": st.column_config.NumberColumn(format="%.0f"),
                     "현재가": st.column_config.NumberColumn(format="%.0f"),
                     "손절선 대비(%)": st.column_config.NumberColumn(format="%.1f"),
                 })
    st.caption("손절선은 평단 -15% 원칙입니다. '손절선 대비'가 0% 아래면 이미 원칙을 벗어난 상태입니다.")


# =========================================================== 9. 성과분석 ====
def menu_performance(rules: core.FilterRules) -> None:
    st.subheader("📈 9. 투자성과 분석")
    trades = extra("trades")
    dividends = extra("dividends")
    ledger = core.trade_ledger(trades)
    divs = core.dividend_summary(dividends)

    m = st.columns(4)
    m[0].metric("누적 실현손익", f"{ledger['realized']:+,.0f}원",
                f"매도 {len(ledger['sells'])}건", delta_color="off")
    m[1].metric("누적 배당", f"{divs['gross']:,.0f}원", f"세후 {divs['net']:,.0f}원", delta_color="off")
    m[2].metric("수수료·세금", f"{ledger['fees'] + ledger['taxes'] + divs['tax']:,.0f}원",
                f"거래 {ledger['fees'] + ledger['taxes']:,.0f} · 배당세 {divs['tax']:,.0f}",
                delta_color="off")
    m[3].metric("거래 건수", f"{len(ledger['items'])}건",
                f"매수 {sum(1 for t in ledger['items'] if t['side'] == '매수')} · "
                f"매도 {sum(1 for t in ledger['items'] if t['side'] == '매도')}", delta_color="off")
    if ledger["unmatched"]:
        st.warning(f"매수 기록 없이 매도만 있는 거래 {ledger['unmatched']}건은 실현손익에서 제외했습니다.")

    monthly = core.monthly_performance(ledger, divs)
    quarterly = core.quarterly_performance(monthly)
    st.markdown("#### 월별 실현손익 · 배당 · 비용 (입력된 거래·배당 기준)")
    if monthly:
        show_chart(money_bar(monthly, "월", "순수익", "월별 순수익(실현손익 + 배당 - 비용)"))
        st.dataframe(pd.DataFrame(monthly), hide_index=True, width="stretch",
                     column_config={
                         "매수금액": st.column_config.NumberColumn(format="%.0f"),
                         "매도금액": st.column_config.NumberColumn(format="%.0f"),
                         "실현손익": st.column_config.NumberColumn(format="%.0f"),
                         "배당": st.column_config.NumberColumn(format="%.0f"),
                         "수수료": st.column_config.NumberColumn(format="%.0f"),
                         "세금": st.column_config.NumberColumn(format="%.0f"),
                         "순수익": st.column_config.NumberColumn(format="%.0f"),
                     })
    else:
        st.caption("거래·배당 내역을 입력하면 월별 흐름이 표시됩니다.")

    st.markdown("#### 분기별 집계")
    if quarterly:
        st.dataframe(pd.DataFrame(quarterly), hide_index=True, width="stretch",
                     column_config={
                         "매수금액": st.column_config.NumberColumn(format="%.0f"),
                         "매도금액": st.column_config.NumberColumn(format="%.0f"),
                         "실현손익": st.column_config.NumberColumn(format="%.0f"),
                         "배당": st.column_config.NumberColumn(format="%.0f"),
                         "수수료·세금": st.column_config.NumberColumn(format="%.0f"),
                         "순수익": st.column_config.NumberColumn(format="%.0f"),
                     })
    else:
        st.caption("집계할 내역이 없습니다.")

    st.divider()
    st.markdown("#### 자산 스냅샷 (오늘 기록 → 월별 총자산 변화)")
    holdings = load_store("holdings")
    if holdings:
        by_code, fx = snapshots_for(holdings)
        summary = core.portfolio_summary(holdings, by_code, trades, dividends, meta_state(), fx=fx)
        c1, c2 = st.columns([1, 3])
        with c1:
            if st.button("📸 오늘 자산 기록", key="snap_record"):
                core.record_performance({
                    "date": core.today_str(), "total_assets": summary["total_assets"],
                    "market_value": summary["market_value"], "cash": summary["cash"],
                    "invested": summary["invested"], "unrealized": summary["unrealized"],
                    "recorded_at": core.now_text(),
                })
                st.success(f"{core.today_str()} 자산을 기록했습니다.")
        with c2:
            st.caption("기록이 2개 이상이면 월별 총자산·수익률 표가 만들어집니다. 로컬 저장이라 "
                       "앱이 재시작되면 사라질 수 있으니 10번 메뉴에서 JSON으로 백업하세요.")
        try:
            perf = core.load_performance()
        except core.StorageError as exc:
            st.warning(f"자산 기록을 읽지 못했습니다: {exc}")
            perf = []
        if len(perf) >= 2:
            df = pd.DataFrame(perf)
            df["수익률(%)"] = df["total_assets"].pct_change() * 100
            st.dataframe(df, hide_index=True, width="stretch",
                         column_config={
                             "total_assets": st.column_config.NumberColumn("총자산", format="%.0f"),
                             "market_value": st.column_config.NumberColumn("평가금액", format="%.0f"),
                             "cash": st.column_config.NumberColumn("예수금", format="%.0f"),
                             "invested": st.column_config.NumberColumn("매입원금", format="%.0f"),
                             "unrealized": st.column_config.NumberColumn("평가손익", format="%.0f"),
                             "수익률(%)": st.column_config.NumberColumn(format="%.2f"),
                         })
            show_chart(trend_chart([{"연도": r["date"], "값": r["total_assets"]} for r in perf],
                                   "값", "총자산 추이", "원", kind="line", height=220))
        elif perf:
            st.caption(f"기록 1개({perf[0]['date']}). 다음 기록부터 변화가 표시됩니다.")

    st.divider()
    st.markdown("#### 🎯 추천 추적 성적표")
    render_picks_tracker()

    st.divider()
    st.markdown("#### ✍️ 거래·배당 내역 입력 / 수정")
    trade_col, div_col = st.columns(2)
    with trade_col:
        render_trade_editor(trades, ledger)
    with div_col:
        render_dividend_editor(dividends)


def render_picks_tracker() -> None:
    picks = load_store("picks")
    if not picks:
        st.info("아직 등록한 추천이 없습니다. 4번(적정주가)·2번(보유종목) 상세에서 '추천 추적에 등록'을 누르면 여기에 쌓입니다.")
    else:
        with st.spinner("등록 종목 현재가를 확인하는 중..."):
            snaps = core.fetch_snapshots(
                [{"code": p["code"], "sector": None, "name": p["name"]} for p in picks])
        by_code = {s.code: s for s in snaps}
        rows, returns, hits, warnings = [], [], 0, 0
        for p in picks:
            snap = by_code.get(core.clean_code(p["code"])) or core.StockSnapshot(code=p["code"])
            entry, price = p.get("entry_price"), snap.price
            ret = ((price / entry - 1) * 100) if (entry and price) else None
            if ret is not None:
                returns.append(ret)
            target, stop = p.get("target"), p.get("stop")
            if target and price and price >= target:
                status = "🎯 목표 달성"
                hits += 1
            elif stop and price and price <= stop:
                status = "⚠️ 경계 이탈"
                warnings += 1
            elif ret is None:
                status = "⚪ 확인 불가"
            elif ret >= 0:
                status = "🟢 진행 중(+)"
            else:
                status = "🟡 진행 중(-)"
            val = core.valuate(snap)
            rows.append({
                "등록일": p.get("date", ""), "종목": snap.name or p.get("name", ""),
                "코드": snap.code, "등록가": entry, "현재가": price, "수익률(%)": ret,
                "목표가": target, "경계가": stop, "상태": status, "적정가": val.fair,
                "안전마진(%)": val.safety_margin, "선정근거": p.get("rationale", ""),
            })
        avg_ret = sum(returns) / len(returns) if returns else 0.0
        m = st.columns(4)
        m[0].metric("등록 종목", f"{len(picks)}개")
        m[1].metric("평균 수익률", f"{avg_ret:+.1f}%")
        m[2].metric("목표 달성", f"{hits}개")
        m[3].metric("경계 이탈", f"{warnings}개")
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={
                         "등록가": st.column_config.NumberColumn(format="%.2f"),
                         "현재가": st.column_config.NumberColumn(format="%.2f"),
                         "수익률(%)": st.column_config.NumberColumn(format="%.1f"),
                         "목표가": st.column_config.NumberColumn(format="%.0f"),
                         "경계가": st.column_config.NumberColumn(format="%.0f"),
                         "적정가": st.column_config.NumberColumn(format="%.0f"),
                         "안전마진(%)": st.column_config.NumberColumn(format="%.1f"),
                     })
        st.caption("상태는 목표가 도달 → 🎯, 경계가 이탈 → ⚠️ 입니다. 수익률은 등록가 대비 현재가 기준입니다.")

        with st.expander("🗑️ 추천 기록 삭제"):
            labels = [f"{p.get('date')} · {p.get('name') or p['code']} ({p['code']})" for p in picks]
            pick = st.selectbox("삭제할 기록", range(len(picks)), format_func=lambda i: labels[i],
                                key="pick_del_idx")
            confirm = st.checkbox("정말 삭제합니다 (되돌릴 수 없음)", key="pick_del_confirm")
            if st.button("선택 기록 삭제", disabled=not confirm, key="pick_del_btn"):
                remaining = [p for i, p in enumerate(picks) if i != pick]
                if persist("picks", remaining, "삭제했습니다."):
                    st.rerun()

    with st.expander("➕ 추천 기록 직접 추가", expanded=False):
        with st.form("add_pick"):
            c1, c2, c3 = st.columns(3)
            code = c1.text_input("종목명·코드·티커", key="pick_add_code")
            entry = c2.number_input("등록가 (0이면 현재가)", min_value=0.0, step=100.0,
                                    value=0.0, key="pick_add_entry")
            target = c3.number_input("목표가 (0이면 자동)", min_value=0.0, step=100.0,
                                     value=0.0, key="pick_add_target")
            rationale = st.text_input("선정 근거", key="pick_add_reason")
            if st.form_submit_button("등록", width="stretch"):
                if not code.strip():
                    st.error("종목을 입력하세요.")
                else:
                    found = core.search_stocks(code, limit=1)
                    if not found:
                        st.error(f"'{code}' 종목을 찾지 못했습니다.")
                    else:
                        with st.spinner("데이터 확인 중..."):
                            snap = core.fetch_snapshot(found[0]["code"], name_hint=found[0]["name"])
                        scn = core.build_scenarios(snap, valuation=core.valuate(snap))
                        entry_price = entry or snap.price
                        if not entry_price:
                            st.error("현재가를 가져오지 못했습니다. 등록가를 직접 입력하세요.")
                        else:
                            picks_now = load_store("picks")
                            picks_now.append({
                                "date": core.today_str(), "code": snap.code,
                                "name": snap.name or snap.code, "entry_price": entry_price,
                                "rationale": rationale or "직접 등록",
                                "target": target or scn.get("target"),
                                "stop": scn.get("stop"), "memo": "",
                            })
                            if persist("picks", picks_now, "추천 기록을 추가했습니다."):
                                st.rerun()


def render_trade_editor(trades: list[dict], ledger: dict) -> None:
    st.markdown("**거래 내역 (매수·매도)**")
    with st.form("add_trade"):
        c1, c2 = st.columns(2)
        t_date = c1.date_input("일자", value=date.fromisoformat(core.today_str()), key="tr_date")
        t_side = c2.selectbox("구분", ["매수", "매도"], key="tr_side")
        c3, c4 = st.columns(2)
        t_code = c3.text_input("종목 (이름·코드·티커)", key="tr_code")
        t_qty = c4.number_input("수량", min_value=0.0, step=1.0, value=1.0, key="tr_qty")
        c5, c6, c7 = st.columns(3)
        t_price = c5.number_input("단가", min_value=0.0, step=100.0, value=0.0, key="tr_price")
        t_fee = c6.number_input("수수료", min_value=0.0, step=100.0, value=0.0, key="tr_fee")
        t_tax = c7.number_input("세금", min_value=0.0, step=100.0, value=0.0, key="tr_tax")
        t_memo = st.text_input("메모", key="tr_memo")
        if st.form_submit_button("➕ 거래 추가", width="stretch"):
            if not t_code.strip():
                st.error("종목을 입력하세요.")
            elif t_qty <= 0 or t_price <= 0:
                st.error("수량과 단가는 0보다 커야 합니다.")
            else:
                found = core.search_stocks(t_code, limit=1)
                if not found:
                    st.error(f"'{t_code}' 종목을 찾지 못했습니다.")
                else:
                    new = {"date": t_date.isoformat(), "code": found[0]["code"],
                           "name": found[0]["name"], "side": t_side, "qty": t_qty,
                           "price": t_price, "fee": t_fee, "tax": t_tax, "memo": t_memo.strip()}
                    if persist_extra("trades", (trades or []) + [new], "거래를 추가했습니다."):
                        st.rerun()

    if not trades:
        st.caption("거래를 입력하면 실현손익·수수료·월별 성과가 자동 계산됩니다.")
        return
    st.markdown("**입력된 거래**")
    rows = [{**{k: t.get(k) for k in ("date", "code", "name", "side", "qty", "price", "fee", "tax")},
             "메모": t.get("memo", "")} for t in trades]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"qty": st.column_config.NumberColumn("수량", format="%.2f"),
                                "price": st.column_config.NumberColumn("단가", format="%.0f"),
                                "fee": st.column_config.NumberColumn("수수료", format="%.0f"),
                                "tax": st.column_config.NumberColumn("세금", format="%.0f")})

    with st.expander("✏️ 거래 수정 / 삭제"):
        labels = [f"{i+1}. {t.get('date')} {t.get('side')} {t.get('name') or t.get('code')} "
                  f"{t.get('qty')}주 @{t.get('price')}" for i, t in enumerate(trades)]
        idx = st.selectbox("거래 선택", range(len(trades)), format_func=lambda i: labels[i],
                           key="tr_edit_sel")
        target = trades[idx]
        with st.form(f"edit_trade_{idx}"):
            e_date = st.date_input("일자", value=date.fromisoformat(target["date"])
                                   if target.get("date") else date.fromisoformat(core.today_str()),
                                   key=f"tr_e_date_{idx}")
            e_side = st.selectbox("구분", ["매수", "매도"],
                                  index=0 if core._norm_side(target.get("side")) == "매수" else 1,
                                  key=f"tr_e_side_{idx}")
            e_qty = st.number_input("수량", min_value=0.0, step=1.0,
                                    value=float(target.get("qty") or 0), key=f"tr_e_qty_{idx}")
            e_price = st.number_input("단가", min_value=0.0, step=100.0,
                                      value=float(target.get("price") or 0), key=f"tr_e_price_{idx}")
            e_fee = st.number_input("수수료", min_value=0.0, step=100.0,
                                    value=float(target.get("fee") or 0), key=f"tr_e_fee_{idx}")
            e_tax = st.number_input("세금", min_value=0.0, step=100.0,
                                    value=float(target.get("tax") or 0), key=f"tr_e_tax_{idx}")
            e_memo = st.text_input("메모", value=target.get("memo", ""), key=f"tr_e_memo_{idx}")
            saved = st.form_submit_button("수정 저장", width="stretch")
        if saved:
            updated = [dict(t) for t in trades]
            updated[idx].update({"date": e_date.isoformat(), "side": e_side, "qty": e_qty,
                                 "price": e_price, "fee": e_fee, "tax": e_tax, "memo": e_memo})
            if persist_extra("trades", updated, "거래를 수정했습니다."):
                st.rerun()
        confirm = st.checkbox("선택한 거래를 삭제합니다", key="tr_del_confirm")
        if st.button("거래 삭제", disabled=not confirm, key="tr_del_btn"):
            if persist_extra("trades", [t for i, t in enumerate(trades) if i != idx], "삭제했습니다."):
                st.rerun()


def render_dividend_editor(dividends: list[dict]) -> None:
    st.markdown("**배당 내역**")
    with st.form("add_dividend"):
        c1, c2 = st.columns(2)
        d_date = c1.date_input("일자", value=date.fromisoformat(core.today_str()), key="dv_date")
        d_code = c2.text_input("종목 (이름·코드·티커)", key="dv_code")
        c3, c4, c5 = st.columns(3)
        d_gross = c3.number_input("배당금(세전)", min_value=0.0, step=1000.0, value=0.0, key="dv_gross")
        d_tax = c4.number_input("세금", min_value=0.0, step=100.0, value=0.0, key="dv_tax")
        d_memo = c5.text_input("메모", key="dv_memo")
        if st.form_submit_button("➕ 배당 추가", width="stretch"):
            if not d_code.strip():
                st.error("종목을 입력하세요.")
            elif d_gross <= 0:
                st.error("배당금을 입력하세요.")
            else:
                found = core.search_stocks(d_code, limit=1)
                if not found:
                    st.error(f"'{d_code}' 종목을 찾지 못했습니다.")
                else:
                    new = {"date": d_date.isoformat(), "code": found[0]["code"],
                           "name": found[0]["name"], "gross": d_gross, "tax": d_tax,
                           "memo": d_memo.strip()}
                    if persist_extra("dividends", (dividends or []) + [new], "배당을 추가했습니다."):
                        st.rerun()

    if not dividends:
        st.caption("배당을 입력하면 누적배당과 종목별 수령액이 1·2번 메뉴에 반영됩니다.")
        return
    st.dataframe(pd.DataFrame([{"일자": d.get("date"), "종목": d.get("name") or d.get("code"),
                                "배당금": core.parse_num(d.get("gross")),
                                "세금": core.parse_num(d.get("tax")),
                                "메모": d.get("memo", "")} for d in dividends]),
                 hide_index=True, width="stretch",
                 column_config={"배당금": st.column_config.NumberColumn(format="%.0f"),
                                "세금": st.column_config.NumberColumn(format="%.0f")})
    with st.expander("✏️ 배당 수정 / 삭제"):
        labels = [f"{i+1}. {d.get('date')} {d.get('name') or d.get('code')} "
                  f"{core.parse_num(d.get('gross')) or 0:,.0f}원" for i, d in enumerate(dividends)]
        idx = st.selectbox("배당 선택", range(len(dividends)), format_func=lambda i: labels[i],
                           key="dv_edit_sel")
        target = dividends[idx]
        with st.form(f"edit_dividend_{idx}"):
            e_date = st.date_input("일자", value=date.fromisoformat(target["date"])
                                   if target.get("date") else date.fromisoformat(core.today_str()),
                                   key=f"dv_e_date_{idx}")
            e_gross = st.number_input("배당금(세전)", min_value=0.0, step=1000.0,
                                      value=float(core.parse_num(target.get("gross")) or 0),
                                      key=f"dv_e_gross_{idx}")
            e_tax = st.number_input("세금", min_value=0.0, step=100.0,
                                    value=float(core.parse_num(target.get("tax")) or 0),
                                    key=f"dv_e_tax_{idx}")
            e_memo = st.text_input("메모", value=target.get("memo", ""), key=f"dv_e_memo_{idx}")
            saved = st.form_submit_button("수정 저장", width="stretch")
        if saved:
            updated = [dict(d) for d in dividends]
            updated[idx].update({"date": e_date.isoformat(), "gross": e_gross,
                                 "tax": e_tax, "memo": e_memo})
            if persist_extra("dividends", updated, "배당을 수정했습니다."):
                st.rerun()
        confirm = st.checkbox("선택한 배당을 삭제합니다", key="dv_del_confirm")
        if st.button("배당 삭제", disabled=not confirm, key="dv_del_btn"):
            if persist_extra("dividends", [d for i, d in enumerate(dividends) if i != idx],
                             "삭제했습니다."):
                st.rerun()


# =========================================================== 10. 설정 ====
def menu_settings(rules: core.FilterRules) -> None:
    st.subheader("⚙️ 10. 설정 및 데이터 관리")

    st.markdown("### 🗂️ 저장소 연결 상태")
    s1, s2 = st.columns([2, 1])
    with s1:
        if core.storage_mode() == "gsheet":
            st.success("구글 시트 연동 중 — 보유종목·추천추적은 기존 시트를 그대로 사용합니다.")
        else:
            st.info("로컬 파일 모드 — 보유종목·추천추적은 data/ 폴더의 JSON에 저장됩니다.")
        st.caption(f"대상: {core.storage_target()}")
        st.markdown(
            "**거래·배당·예수금·관심종목·전략**은 로컬 저장(`data/*.json`)입니다. "
            "구글 시트의 `보유종목`·`추천추적` 탭은 v2와 똑같이 유지되고, 손대지 않습니다.  \n"
            "로컬 파일은 앱이 재시작되면 초기화될 수 있으니 아래 **백업 내보내기**로 주기적으로 보관하세요.")
        if core.extra_sheet_enabled():
            st.info("Secrets의 VI_EXTRA_SHEET=1 이 켜져 있어, Apps Script가 받아 주는 경우 "
                    "거래·배당·관심종목도 시트에 함께 저장합니다(실패하면 자동으로 로컬).")
        else:
            st.caption("기존 Apps Script가 임의 탭 이름을 지원하는지 확인되지 않아 기본은 로컬 저장입니다. "
                       "시트에도 함께 남기려면 Secrets에 `VI_EXTRA_SHEET=1`을 넣으세요(시트 구조는 바뀌지 않습니다).")
    with s2:
        if st.button("🔄 시트·시세 새로고침", key="refresh_settings", width="stretch"):
            refresh_all()
        st.caption(f"마지막 갱신 {st.session_state.get('refresh_time', '-')}")

    st.divider()
    st.markdown("### 💵 예수금(현금) 설정")
    meta = meta_state()
    with st.form("cash_form"):
        c1, c2 = st.columns([1, 2])
        cash = c1.number_input("예수금(원)", min_value=0.0, step=10000.0,
                               value=float(meta.get("cash") or 0), key="cash_input")
        note = c2.text_input("메모 (선택)", value=meta.get("cash_note", ""), key="cash_note")
        if st.form_submit_button("저장", width="stretch"):
            try:
                core.save_meta(cash, note)
                st.success(f"예수금 {cash:,.0f}원을 저장했습니다.")
            except core.StorageError as exc:
                st.error(f"저장하지 못했습니다: {exc}")
    st.caption(f"마지막 저장 {meta.get('updated') or '-'} · 총자산 = 평가금액 + 예수금")

    st.divider()
    st.markdown("### 🧪 가치함정 필터 기준")
    st.caption("4번 메뉴 '저평가 우량주 발굴'에 적용됩니다. 기준을 바꾸면 다시 스캔하세요.")
    f1, f2 = st.columns(2)
    with f1:
        st.slider("PER 상한", 1.0, 60.0, step=0.5, key="f_per")
        st.slider("PBR 상한", 0.1, 10.0, step=0.1, key="f_pbr")
        st.slider("부채비율 상한(%)", 10.0, 500.0, step=10.0, key="f_debt")
    with f2:
        st.slider("ROE 하한(%)", -20.0, 40.0, step=0.5, key="f_roe")
        st.checkbox("영업이익 흑자 조건 적용", key="f_op")
        st.checkbox("영업현금흐름 양수 조건 적용", key="f_ocf")
        st.caption(f"현재 기준: {current_rules().summary()}")

    st.divider()
    st.markdown("### 💾 백업 내보내기 / 불러오기")
    bundle = core.backup_bundle(include_sheet=False)
    bundle["holdings"] = load_store("holdings")
    bundle["picks"] = load_store("picks")
    trades = extra("trades")
    dividends = extra("dividends")
    e1, e2, e3 = st.columns(3)
    with e1:
        st.download_button(
            "📥 전체 백업 (JSON)",
            data=json.dumps(bundle, ensure_ascii=False, indent=2),
            file_name=f"value-investor-backup-{core.today_str()}.json",
            mime="application/json", width="stretch")
    with e2:
        st.download_button(
            "📥 거래내역 (CSV)",
            data=core.rows_to_csv([{label: t.get(key) for key, label in TRADE_CSV.items()}
                                   for t in trades], list(TRADE_CSV.values())),
            file_name=f"trades-{core.today_str()}.csv", mime="text/csv", width="stretch")
    with e3:
        st.download_button(
            "📥 배당내역 (CSV)",
            data=core.rows_to_csv([{label: d.get(key) for key, label in DIVIDEND_CSV.items()}
                                   for d in dividends], list(DIVIDEND_CSV.values())),
            file_name=f"dividends-{core.today_str()}.csv", mime="text/csv", width="stretch")
    st.caption("JSON 백업에는 보유종목·추천추적·거래·배당·관심종목·전략·예수금·자산기록이 모두 들어갑니다. "
               "CSV는 엑셀에서 열어 수정한 뒤 그대로 다시 불러올 수 있습니다.")

    upload = st.file_uploader("백업 파일 불러오기 (JSON 또는 CSV)", type=["json", "csv"], key="restore_file")
    if upload is not None:
        raw = upload.read().decode("utf-8-sig")
        if upload.name.lower().endswith(".json"):
            try:
                payload = json.loads(raw)
            except Exception as exc:
                st.error(f"JSON을 읽지 못했습니다: {exc}")
                payload = None
            if isinstance(payload, dict):
                st.write({k: (len(v) if isinstance(v, (list, dict)) else v)
                          for k, v in payload.items() if k in
                          ("holdings", "picks", "trades", "dividends", "watchlist", "strategies",
                           "performance", "meta", "exported_at", "app")})
                overwrite = st.checkbox("구글 시트의 보유종목·추천추적까지 덮어씁니다", key="imp_sheet")
                if st.button("JSON 불러오기 실행", key="imp_go"):
                    try:
                        counts = core.restore_bundle(payload, include_holdings=overwrite)
                        st.success("불러왔습니다: " + ", ".join(f"{k} {v}건" for k, v in counts.items()))
                        for key in ("holdings", "picks", "snap_cache", "scan_rows"):
                            st.session_state[key] = None
                        for key in [k for k in list(st.session_state.keys())
                                    if str(k).startswith("extra_")]:
                            del st.session_state[key]
                    except core.StorageError as exc:
                        st.error(f"불러오지 못했습니다: {exc}")
        else:
            kind = st.radio("CSV 종류", ["거래내역", "배당내역"], horizontal=True, key="csv_kind")
            rows = [core.normalize_extra_row("trades" if kind == "거래내역" else "dividends", r)
                    for r in core.csv_to_rows(raw)]
            st.write(f"{len(rows)}건을 읽었습니다.")
            if rows:
                st.dataframe(pd.DataFrame(rows[:10]), hide_index=True, width="stretch")
            if st.button("CSV 불러오기 실행", key="csv_go"):
                if persist_extra("trades" if kind == "거래내역" else "dividends", rows,
                                 f"{kind} {len(rows)}건을 저장했습니다."):
                    st.rerun()

    st.divider()
    st.markdown("### 🔎 데이터 정합성 점검")
    holdings = load_store("holdings")
    diffs = core.reconcile_holdings(holdings, trades)
    if not diffs:
        st.success("보유 수량과 거래 내역 수량이 일치합니다(기록이 있는 종목 기준).")
    else:
        st.warning("보유 수량과 거래 내역 합계가 다른 종목이 있습니다. 거래를 일부만 입력했거나 "
                   "보유 수량을 직접 고친 경우입니다. 실현손익은 거래 내역만으로 계산됩니다.")
        st.dataframe(pd.DataFrame(diffs), hide_index=True, width="stretch")

    st.divider()
    st.markdown("### 🤖 AI 심층 리포트 키 안내")
    if core.ai_available():
        st.success(f"AI 키 연결됨: {core.ai_config()[0]}")
    else:
        st.info("AI 키가 없습니다. 없어도 차트·밸류에이션·이슈 해석은 100% 동작합니다.")
    st.caption("Streamlit Cloud → Settings → Secrets 에 아래 중 하나를 넣으면 6번 메뉴의 'AI 해설' 버튼이 켜집니다.")
    st.code('OPENAI_API_KEY = "sk-..."\n# GEMINI_API_KEY = "..."\n# OPENROUTER_API_KEY = "..."', language="toml")

    st.divider()
    st.markdown("### ℹ️ 앱 정보")
    st.caption(f"가치투자 스튜디오 v3 · 유니버스 {len(core.UNIVERSE)}종목 · 저장소 {core.storage_label()} · "
               f"기준일 {core.today_str()} · 자동매매 기능 없음(정보 제공용, 투자 자문 아님).")


# ------------------------------------------------------------------- 실행 ----
def sidebar() -> str:
    with st.sidebar:
        st.markdown("### 📈 가치투자 스튜디오 v3")
        menu = st.radio("메뉴", MENU_LABELS, key="menu", label_visibility="collapsed")
        st.divider()
        if core.storage_mode() == "gsheet":
            st.success("🗂️ 보유·추천: 구글 시트 연동")
        else:
            st.info("🗂️ 보유·추천: 로컬 파일")
        st.caption(core.storage_target())
        st.caption("거래·배당·예수금·관심종목은 로컬 저장 (10번에서 백업)")
        if st.button("🔄 시트·시세 새로고침", key="refresh_all", width="stretch"):
            refresh_all()
        st.divider()
        st.markdown("### 🎯 투자 원칙")
        st.caption("저가 우량주 · 투자금을 잃지 않는 것 · 관심 섹터: 원전·건설·바이오·반도체(+금융·제조)")
        st.caption("손절 기준은 평단 대비 -15%(투자금 보존)입니다.")
        st.divider()
        st.markdown("### 🧪 현재 필터")
        st.caption(current_rules().summary())
        st.caption("기준 변경은 10번 메뉴에서.")
        st.divider()
        if core.ai_available():
            st.success(f"🤖 AI 해설: {core.ai_config()[0]} 연결됨")
        else:
            st.caption("🤖 AI 해설: 키 없음 (없어도 100% 동작)")
        st.divider()
        st.caption("⚠️ 정보 제공용이며 투자 자문이 아닙니다. 최종 매매 판단과 책임은 본인에게 있습니다.")
    if st.session_state.get("store_error"):
        st.warning(f"저장소 연결 문제: {st.session_state['store_error']}")
    if st.session_state.get("extra_error"):
        st.warning(f"로컬 데이터 파일 문제: {st.session_state['extra_error']}")
    return menu


def main() -> None:
    ensure_state()
    header()
    rules = current_rules()
    menu = sidebar()

    if menu.startswith("1."):
        menu_overview(rules)
    elif menu.startswith("2."):
        menu_holdings(rules)
    elif menu.startswith("3."):
        menu_financials(rules)
    elif menu.startswith("4."):
        menu_valuation(rules)
    elif menu.startswith("5."):
        menu_strategy(rules)
    elif menu.startswith("6."):
        menu_market(rules)
    elif menu.startswith("7."):
        menu_news(rules)
    elif menu.startswith("8."):
        menu_risk(rules)
    elif menu.startswith("9."):
        menu_performance(rules)
    else:
        menu_settings(rules)

    st.divider()
    st.caption(f"저장소: {core.storage_label()} · {core.storage_target()} · "
               f"시세·재무: 네이버 금융(한국/해외) / yfinance 보조 · 뉴스: 네이버·구글뉴스 RSS · "
               f"캐시 5~10분 · 자동매매 없음 · 본 화면은 정보 제공용입니다.")


if __name__ == "__main__":
    main()
