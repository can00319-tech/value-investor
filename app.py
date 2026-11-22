"""📈 가치투자 스튜디오 v2 — 보유 점검 · 저평가 우량주 발굴 · 이슈/종목 분석 · 추천 추적.

동철님의 투자 원칙("저가 우량주를 찾고, 투자금을 잃지 않는 것")을 기준으로
보유 종목과 후보 종목을 같은 잣대로 점검하는 Streamlit 앱.

v2 추가: 6개월/1년 주가 차트(20·60일선) · 3~5년 실적 추이 · PER/PBR/DCF 3방식 밸류에이션과
4단계 대응 구간 · 거시/산업 지표판 · 이슈→내 종목 영향 해석 · 근거(URL·시각) 붙은 뉴스 분석 ·
종목명 자동 검색 · 400종목 유니버스 스캔.

실행:  streamlit run app.py
배포:  Streamlit Community Cloud + 구글 시트(Apps Script 웹 앱) 연동
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import vi_core as core

st.set_page_config(page_title="가치투자 스튜디오", page_icon="📈", layout="wide")

try:  # Streamlit Cloud Secrets → 환경변수 (로컬 실행 시에는 없음)
    core.apply_secrets(st.secrets)
except Exception:
    pass

CSS = """
<style>
  .block-container {padding-top: 1.1rem; max-width: 1320px;}
  .vi-header {
    background: linear-gradient(115deg, #0b1b34 0%, #14294b 55%, #1d3f74 100%);
    border-radius: 16px; padding: 22px 26px 18px 26px; margin-bottom: 14px;
    box-shadow: 0 6px 22px rgba(11, 27, 52, .25);
  }
  .vi-header h1 {color: #ffffff; font-size: 1.72rem; margin: 0 0 .25rem 0; letter-spacing: -.4px;}
  .vi-header p {color: #b9cbe8; margin: 0; font-size: .93rem;}
  .vi-header .tag {
    display: inline-block; margin-top: .6rem; padding: 3px 10px; border-radius: 999px;
    background: rgba(255,255,255,.12); color: #e8f0ff; font-size: .78rem;
  }
  .vi-card {
    border: 1px solid #e3e8f0; border-left: 5px solid #14294b; border-radius: 12px;
    padding: 14px 16px; background: #fbfcfe; height: 100%;
  }
  .vi-card h4 {margin: 0 0 .35rem 0; font-size: 1rem; color: #14294b;}
  .vi-card .price {font-size: 1.12rem; font-weight: 700; color: #14294b; margin-bottom: .35rem;}
  .vi-card ul {margin: .2rem 0 0 1.0rem; padding: 0;}
  .vi-card li {font-size: .84rem; color: #33415c; margin-bottom: .2rem; line-height: 1.35;}
  .vi-up {border-left-color: #12805c;}
  .vi-flat {border-left-color: #c48a05;}
  .vi-down {border-left-color: #b3261e;}
  .vi-pill {
    display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: .76rem;
    font-weight: 600; margin-right: 4px;
  }
  .vi-pass {background: #e5f6ee; color: #0d6b4c;}
  .vi-fail {background: #fdeceb; color: #9c1c16;}
  .vi-wait {background: #fdf4e0; color: #8a6205;}
  .vi-note {font-size: .82rem; color: #5b6b85;}
  .vi-tier {width: 100%; border-collapse: collapse; font-size: .86rem;}
  .vi-tier th, .vi-tier td {border-bottom: 1px solid #e3e8f0; padding: 7px 9px; text-align: left;}
  .vi-tier tr.now {background: #eef4ff; font-weight: 700;}
  .vi-news {font-size: .84rem; margin-bottom: .45rem; line-height: 1.35;}
  .vi-pos {background: #e5f6ee; color: #0d6b4c; border-radius: 6px; padding: 1px 6px; font-size: .76rem;}
  .vi-neg {background: #fdeceb; color: #9c1c16; border-radius: 6px; padding: 1px 6px; font-size: .76rem;}
  .vi-mid {background: #eef1f6; color: #445; border-radius: 6px; padding: 1px 6px; font-size: .76rem;}
  a {text-decoration: none;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ------------------------------------------------------------- 공통 도우미 ----
def header() -> None:
    counts = core.universe_size()
    st.markdown(
        '<div class="vi-header">'
        "<h1>📈 가치투자 스튜디오 <span style='font-size:1rem;opacity:.75'>v2</span></h1>"
        "<p>저가 우량주를 찾고, 투자금을 잃지 않는 것 — 보유 점검 · 가치함정 필터 · 이슈/종목 분석 · 추천 추적</p>"
        f'<span class="tag">기준일 {core.today_str()} · 유니버스 {len(core.UNIVERSE)}종목'
        f"(코스피 {counts.get('KOSPI', 0)}·코스닥 {counts.get('KOSDAQ', 0)}·미국 {counts.get('US', 0)})"
        " · 네이버금융·네이버뉴스·구글뉴스RSS·뉴욕연준</span>"
        "</div>",
        unsafe_allow_html=True,
    )


def ensure_state() -> None:
    for key, value in (("holdings", None), ("picks", None), ("store_error", ""),
                       ("scan_rows", None), ("analysis", None)):
        st.session_state.setdefault(key, value)


def load_store(kind: str, force: bool = False):
    """저장소(시트/로컬)에서 목록을 읽어 세션에 보관."""
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


def money(value, currency="KRW") -> str:
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


def health_table(snap: core.StockSnapshot) -> None:
    rows = core.health_comment(snap)
    st.table(pd.DataFrame(rows, columns=["지표", "평가", "신호"]))


def news_block(items: list[dict], empty_text: str = "관련 뉴스를 찾지 못했습니다.") -> None:
    if not items:
        st.caption(empty_text)
        return
    for item in items:
        st.markdown(
            f"- [{item.get('title', '')}]({item.get('link', '')}) "
            f"<span class='vi-note'>{item.get('source', '')}</span>",
            unsafe_allow_html=True)


def register_pick(snap: core.StockSnapshot, scn: dict, rationale: str) -> None:
    picks = load_store("picks")
    if any(p["code"] == snap.code for p in picks):
        st.info(f"{snap.label} 은(는) 이미 추천 추적에 등록되어 있습니다.")
        return
    picks.append({
        "date": core.today_str(),
        "code": snap.code,
        "name": snap.name or snap.code,
        "entry_price": snap.price,
        "rationale": rationale,
        "target": scn.get("target"),
        "stop": scn.get("stop"),
        "memo": "",
    })
    if persist("picks", picks, f"{snap.label} 을(를) 추천 추적에 등록했습니다."):
        st.caption("📈 '추천 추적 성적표' 탭에서 수익률과 목표·경계 도달 여부를 확인할 수 있습니다.")


# ------------------------------------------------------------------ 사이드바 ----
def sidebar(rules: core.FilterRules) -> core.FilterRules:
    with st.sidebar:
        st.markdown("### ⚙️ 설정")
        if core.storage_mode() == "gsheet":
            st.success("🗂️ 저장소: 구글 시트 연동 중")
        else:
            st.info("🗂️ 저장소: 로컬 파일 (미연결)")
        st.caption(core.storage_target())
        if st.button("🔄 시트·시세 새로고침", key="refresh_all", width="stretch"):
            core.clear_cache()
            for fn in (core.fetch_macro, core.fetch_sector_pulse, core.fetch_price_history,
                       core.fetch_financial_history, core.fetch_flow, core.fetch_news_digest):
                try:
                    fn.cache_clear()  # type: ignore[attr-defined]
                except Exception:
                    pass
            for key in ("holdings", "picks", "scan_rows", "analysis"):
                st.session_state[key] = None
            st.rerun()

        st.divider()
        st.markdown("### 🎯 투자 원칙")
        st.caption("저가 우량주 · 투자금을 잃지 않는 것 · 관심 섹터: 원전·건설·바이오·반도체(+금융·제조)")
        st.caption("경계선은 평단 대비 -15%(투자금 보존) 기준으로 계산합니다.")

        st.divider()
        st.markdown("### 🧪 가치함정 필터 기준")
        with st.form("filter_form"):
            st.number_input("PER 상한", min_value=1.0, max_value=60.0,
                            value=float(rules.max_per), step=0.5, key="f_per")
            st.number_input("PBR 상한", min_value=0.1, max_value=10.0,
                            value=float(rules.max_pbr), step=0.1, key="f_pbr")
            st.number_input("부채비율 상한(%)", min_value=10.0, max_value=500.0,
                            value=float(rules.max_debt), step=10.0, key="f_debt")
            st.number_input("ROE 하한(%)", min_value=-20.0, max_value=40.0,
                            value=float(rules.min_roe), step=0.5, key="f_roe")
            st.checkbox("영업이익 흑자 조건 적용", value=rules.require_op_profit, key="f_op")
            st.checkbox("영업현금흐름 양수 조건 적용", value=rules.require_ocf, key="f_ocf")
            st.form_submit_button("기준 적용", width="stretch")
        st.caption("이 기준은 2️⃣ 저평가 우량주 발굴에 적용됩니다.")

        st.divider()
        if core.ai_available():
            st.success(f"🤖 AI 해설: {core.ai_config()[0]} 연결됨")
        else:
            st.info("🤖 AI 해설: 키 없음")
            st.caption("OPENAI_API_KEY · GEMINI_API_KEY · OPENROUTER_API_KEY 중 하나를 "
                       "Streamlit Secrets(또는 환경변수)에 넣으면 AI 해설이 추가됩니다. "
                       "키가 없어도 차트·밸류에이션·이슈 해석은 100% 동작합니다.")

        st.divider()
        st.caption("⚠️ 정보 제공용이며 투자 자문이 아닙니다. 최종 매매 판단과 책임은 본인에게 있습니다.")

    if st.session_state.get("store_error"):
        st.warning(f"저장소 연결 문제: {st.session_state['store_error']}")
    return core.FilterRules(
        max_per=float(st.session_state.get("f_per", rules.max_per)),
        max_pbr=float(st.session_state.get("f_pbr", rules.max_pbr)),
        max_debt=float(st.session_state.get("f_debt", rules.max_debt)),
        min_roe=float(st.session_state.get("f_roe", rules.min_roe)),
        require_op_profit=bool(st.session_state.get("f_op", rules.require_op_profit)),
        require_ocf=bool(st.session_state.get("f_ocf", rules.require_ocf)),
    )


# ------------------------------------------------------------- 차트 도우미 ----
PRICE_COLOR = "#1f4e9c"
MA20_COLOR = "#e08a00"
MA60_COLOR = "#8a4fbf"


def price_chart(hist: core.PriceHistory, currency: str, months: int = 12, height: int = 320):
    """6개월/1년 주가 + 20일·60일 이동평균 (인터랙티브)."""
    import altair as alt
    window = hist.window(months)
    rows = window.to_rows()
    if not rows:
        return None
    df = pd.DataFrame(rows)
    long = df.melt(id_vars=["날짜"], value_vars=["종가", "20일선", "60일선"],
                   var_name="구분", value_name="가격").dropna()
    price_scale = alt.Scale(zero=False)
    base = alt.Chart(long).encode(
        x=alt.X("날짜:T", title="날짜"),
        y=alt.Y("가격:Q", title=f"주가({'원' if currency == 'KRW' else 'USD'})", scale=price_scale),
        color=alt.Color("구분:N", title=None,
                        scale=alt.Scale(domain=["종가", "20일선", "60일선"],
                                        range=[PRICE_COLOR, MA20_COLOR, MA60_COLOR])),
        tooltip=[alt.Tooltip("날짜:T", title="날짜"),
                 alt.Tooltip("구분:N", title="구분"),
                 alt.Tooltip("가격:Q", title="가격", format=",.2f")],
    )
    line = base.mark_line(strokeWidth=2).transform_filter(alt.datum["구분"] == "종가")
    ma = base.mark_line(strokeWidth=1.2, opacity=0.85).transform_filter(alt.datum["구분"] != "종가")
    chart = (line + ma).properties(height=height).interactive()
    return chart


def trend_chart(rows: list[dict], field: str, title: str, unit: str = "",
                kind: str = "bar", color: str = PRICE_COLOR, height: int = 190):
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


def show_chart(chart) -> None:
    if chart is None:
        st.caption("데이터 없음")
        return
    st.altair_chart(chart, width="stretch")


# --------------------------------------------------------- 밸류에이션 블록 ----
def valuation_block(snap: core.StockSnapshot, val: core.Valuation) -> None:
    cur = val.currency
    st.markdown("#### 💰 적정가 — 3가지 방식")
    rows = []
    for m in val.methods:
        rows.append({
            "방식": m.name,
            "적정가": money(m.value, cur) if m.value else "계산 불가",
            "가중치": f"{m.weight:.0%}" if m.value else "-",
            "근거": m.basis,
            "비고": m.note,
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption(val.note + " · " + " / ".join(val.assumptions))

    if val.fair:
        st.markdown(f"**종합 적정가 {money(val.fair, cur)}** "
                    f"(현재가 {money(snap.price, cur)} · 밴드 {money(val.low, cur)}~{money(val.high, cur)})")
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
                   "DCF는 영업현금흐름(없으면 순이익)을 기준으로 한 보수적 가정입니다.")
    else:
        st.info(val.note)


# ------------------------------------------------------------- 수급·배당 ----
def flow_block(flow: core.FlowSummary, currency: str = "KRW") -> None:
    st.markdown(f"**수급** · {flow.tag()}")
    if not flow.available:
        st.caption(flow.note or "수급 데이터 없음")
        return
    df = pd.DataFrame(flow.rows)
    st.dataframe(df, hide_index=True, width="stretch",
                 column_config={
                     "외국인": st.column_config.NumberColumn(format="%,d"),
                     "기관": st.column_config.NumberColumn(format="%,d"),
                     "개인": st.column_config.NumberColumn(format="%,d"),
                     "종가": st.column_config.NumberColumn(format="%.0f"),
                 })
    st.caption(f"{flow.text()}"
               + (f" · 외국인 보유율 {flow.foreign_hold_ratio:.2f}%" if flow.foreign_hold_ratio else ""))


def dividend_block(snap: core.StockSnapshot, hist: core.FinancialHistory | None = None) -> None:
    st.markdown("**배당**")
    cols = st.columns(3)
    cols[0].metric("배당수익률", "-" if snap.dividend_yield is None else f"{snap.dividend_yield:.2f}%")
    cols[1].metric("주당배당금", money(snap.dps, snap.currency) if snap.dps else "-")
    cols[2].metric("기준 연도", snap.dividend_year or "-")
    if hist and hist.series.get("주당배당금") and any(v for v in hist.series["주당배당금"]):
        rows = hist.series_rows(["주당배당금", "배당수익률"])
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.caption("연도별 배당 이력은 데이터 제공처에 없으면 표시하지 않습니다.")


def news_digest_block(digest: core.NewsDigest, name: str = "", market_news: bool = False) -> None:
    """뉴스·공시 — 실제 URL과 게시 시각만 표시한다(없는 기사는 만들지 않음)."""
    if not digest.items:
        st.caption(digest.note or "뉴스를 가져오지 못했습니다.")
        return
    counts = digest.counts()
    st.caption(f"수집 {len(digest.items)}건 · 🟢 호재 {counts['🟢 호재']} · 🔴 악재 {counts['🔴 악재']} · "
               f"⚪ 중립 {counts['⚪ 중립']} · 기준 {digest.generated_at}")
    groups = digest.by_category()
    labels = [c for c in core.CATEGORY_ORDER if c in groups]
    for category in labels:
        st.markdown(f"**{category}**")
        for item in groups[category][:6]:
            cls = {"🟢 호재": "vi-pos", "🔴 악재": "vi-neg"}.get(item.sentiment, "vi-mid")
            st.markdown(
                f"<div class='vi-news'><span class='{cls}'>{item.sentiment}</span> "
                f"<a href='{item.link}' target='_blank'>{item.title}</a><br>"
                f"<span class='vi-note'>{item.source} · 게시 {item.published}</span></div>",
                unsafe_allow_html=True)


# ------------------------------------------------------- 종목 상세 렌더링 ----
def render_detail(snap: core.StockSnapshot, rules: core.FilterRules, key: str,
                  avg_price: float | None = None, quotes: dict | None = None,
                  show_news: bool = True) -> None:
    """한 종목의 상세 — 시세·시나리오·밸류에이션·추이·수급·배당·뉴스."""
    filt = core.run_filter(snap, rules)
    with st.spinner(f"{snap.name or snap.code} 상세 데이터를 불러오는 중..."):
        hist = core.fetch_price_history(snap.code, months=12)
        trend = core.fetch_financial_history(snap.code, years=4)
        flow = core.fetch_flow(snap.code)
        val = core.valuate(snap, trend)
        digest = core.fetch_news_digest(snap.name or snap.code, code=snap.code if snap.market == "KR" else "",
                                        limit=12) if show_news else core.NewsDigest()
        scn = core.build_scenarios(snap, avg_price=avg_price, valuation=val)

    stats = hist.stats()
    st.markdown(f"### {snap.name}({snap.code}) · {snap.sector} · "
                f"{'한국' if snap.market == 'KR' else '미국'}")
    m = st.columns(5)
    m[0].metric("현재가", money(snap.price, snap.currency), pct_text(snap.change_pct))
    m[1].metric("PER / PBR", f"{value_or(snap.per)} / {value_or(snap.pbr)}")
    m[2].metric("ROE / 부채비율", f"{value_or(snap.roe, '{:.1f}%')} / {value_or(snap.debt_ratio, '{:.1f}%')}")
    m[3].metric("배당수익률", "-" if snap.dividend_yield is None else f"{snap.dividend_yield:.2f}%",
                f"DPS {money(snap.dps, snap.currency) if snap.dps else '-'}", delta_color="off")
    m[4].metric("가치점수", f"{filt['score']}점", filt["verdict"], delta_color="off")
    if snap.as_of:
        st.caption(f"시세 기준 {snap.as_of} · 출처 {', '.join(snap.sources) or '-'}")

    st.markdown("#### 🟢🟡🔴 3단 시나리오")
    scenario_cards(scn)

    valuation_block(snap, val)

    st.markdown("#### 📈 주가 흐름")
    if hist.closes:
        c1, c2 = st.columns([3, 1])
        with c2:
            period = st.radio("기간", ["6개월", "1년"], index=1, horizontal=True,
                              key=f"{key}_period")
        months = 6 if period == "6개월" else 12
        show_chart(price_chart(hist, snap.currency, months=months))
        s = stats or {}
        k = st.columns(4)
        k[0].metric("6개월 수익률", pct_text(s.get("return_6m")))
        k[1].metric("1년 수익률", pct_text(s.get("return_1y")))
        k[2].metric("20일선", money(s.get("ma20"), snap.currency))
        k[3].metric("60일선", money(s.get("ma60"), snap.currency))
        if s.get("position_pct") is not None:
            pos = max(0.0, min(100.0, s["position_pct"]))
            st.progress(pos / 100,
                        text=f"52주 레인지 {money(s.get('low52'), snap.currency)} ~ "
                             f"{money(s.get('high52'), snap.currency)} · 현재 {pos:.0f}% 위치")
        if hist.note:
            st.caption(hist.note)
    else:
        st.caption(hist.note or "주가 흐름 데이터 없음")

    st.markdown(f"#### 📊 3~5년 실적·재무 추이 ({trend.source or '데이터 없음'})")
    if trend.available():
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
        g7, g8, g9 = st.columns(3)
        with g7:
            show_chart(trend_chart(rows, "PER", "PER 추이", "배", kind="line"))
        with g8:
            show_chart(trend_chart(rows, "PBR", "PBR 추이", "배", kind="line", color="#8a4fbf"))
        with g9:
            show_chart(trend_chart(rows, "현금흐름", "영업현금흐름(가능 시)", unit))
        g10, g11, g12 = st.columns(3)
        with g10:
            show_chart(trend_chart(rows, "EPS", "EPS", snap.currency, kind="line"))
        with g11:
            show_chart(trend_chart(rows, "BPS", "BPS", snap.currency, kind="line"))
        with g12:
            show_chart(trend_chart(rows, "주당배당금", "주당배당금(DPS)", snap.currency))
        with st.expander("연도별 표로 보기"):
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        if trend.note:
            st.caption(trend.note)
        est = [y for i, y in enumerate(trend.years)
               if i < len(trend.estimated) and trend.estimated[i]]
        if est:
            st.caption(f"{', '.join(est)}년은 증권가 추정치(컨센서스)입니다. 나머지는 확정 실적입니다.")
    else:
        st.caption(trend.note or "재무 추이 데이터 없음")
        with st.expander("현재 지표만 보기"):
            health_table(snap)

    col_left, col_right = st.columns(2)
    with col_left:
        st.markdown("#### 🧾 수급")
        flow_block(flow, snap.currency)
    with col_right:
        st.markdown("#### 💵 배당")
        dividend_block(snap, trend)

    if show_news:
        st.markdown("#### 📰 뉴스·공시 자동 분석")
        news_digest_block(digest, snap.name)
        for line in core.news_impact_lines(digest, snap.sector, snap.name):
            st.caption(line)

    b1, b2 = st.columns([1, 2])
    with b1:
        if st.button("📈 이 종목을 추천 추적에 등록", key=f"{key}_register"):
            register_pick(snap, scn, f"상세분석 · 가치점수 {filt['score']}점 · {filt['reason']}")
    with b2:
        st.caption("등록 시 현재가·목표가·경계선이 저장되어 이후 수익률을 자동 추적합니다.")


# ------------------------------------------------- 1) 보유 종목 & 시나리오 점검 ----
def tab_portfolio(rules: core.FilterRules) -> None:
    st.subheader("🛡️ 보유 종목 & 시나리오 점검")
    holdings = load_store("holdings")

    if not holdings:
        st.info("보유 종목이 아직 없습니다. 구글 시트 '보유종목' 탭에 종목을 넣거나 아래에서 추가하면 "
                "평가손익·밸류에이션·시나리오가 계산됩니다.")
        with st.expander("화면 미리보기용 예시 데이터(저장 안 함)", expanded=False):
            st.write(pd.DataFrame([
                {"종목": "두산에너빌리티", "코드": "034020", "수량": 10, "평단가": 98000},
                {"종목": "대우건설", "코드": "047040", "수량": 40, "평단가": 20000},
                {"종목": "KB금융", "코드": "105560", "수량": 10, "평단가": 70000},
            ]))
        render_holding_manager(holdings)
        return

    with st.spinner("보유 종목 시세·재무를 불러오는 중..."):
        snapshots = core.fetch_snapshots(
            [{"code": h["code"], "sector": None, "name": h["name"]} for h in holdings])
    by_code = {s.code: s for s in snapshots}
    fx = core.usd_krw()

    total_cost = total_value = 0.0
    rows, details = [], []
    for h in holdings:
        snap = by_code.get(core.clean_code(h["code"])) or core.StockSnapshot(code=h["code"])
        qty, avg = float(h.get("qty") or 0), float(h.get("avg_price") or 0)
        rate = fx if snap.currency == "USD" else 1.0
        cost_krw = qty * avg * rate
        value_krw = qty * (snap.price or 0) * rate
        total_cost += cost_krw
        total_value += value_krw
        profit = value_krw - cost_krw
        ret = (profit / cost_krw * 100) if cost_krw else None
        scn = core.build_scenarios(snap, avg_price=avg)
        details.append((h, snap, scn, ret))
        premium = scn.get("premium_pct")
        if snap.op_income is not None and snap.op_income <= 0:
            signal = "🔴 영업적자"
        elif premium is not None and premium >= 100:
            signal = "🔴 고평가"
        elif premium is not None and premium >= 30:
            signal = "🟡 부담"
        elif premium is not None and premium <= -20:
            signal = "🟢 저평가"
        else:
            signal = "⚪ 중립"
        rows.append({
            "종목": snap.name or h["name"], "코드": snap.code, "수량": qty, "평단가": avg,
            "현재가": snap.price, "평가금액(원)": value_krw, "평가손익(원)": profit,
            "수익률(%)": ret, "비중(%)": None, "PER": snap.per, "PBR": snap.pbr,
            "부채비율(%)": snap.debt_ratio, "ROE(%)": snap.roe,
            "배당수익률(%)": snap.dividend_yield, "DPS": snap.dps,
            "적정가": scn.get("fair"), "신호": signal,
        })

    if total_value:
        for row in rows:
            row["비중(%)"] = row["평가금액(원)"] / total_value * 100

    profit_total = total_value - total_cost
    ret_total = (profit_total / total_cost * 100) if total_cost else 0.0
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("총 매수금액", f"{total_cost:,.0f}원")
    m2.metric("총 평가금액", f"{total_value:,.0f}원")
    m3.metric("평가손익", f"{profit_total:+,.0f}원", f"{ret_total:+.1f}%")
    m4.metric("보유 종목", f"{len(holdings)}종목")
    div_avg = [r["배당수익률(%)"] for r in rows if r["배당수익률(%)"] is not None]
    m5.metric("평균 배당수익률", f"{sum(div_avg) / len(div_avg):.2f}%" if div_avg else "-")

    table_state = st.dataframe(
        pd.DataFrame(rows), hide_index=True, width="stretch",
        on_select="rerun", selection_mode="single-row", key="holdings_table",
        column_config={
            "평단가": st.column_config.NumberColumn(format="%.0f"),
            "현재가": st.column_config.NumberColumn(format="%.2f"),
            "평가금액(원)": st.column_config.NumberColumn(format="%.0f"),
            "평가손익(원)": st.column_config.NumberColumn(format="%.0f"),
            "수익률(%)": st.column_config.NumberColumn(format="%.1f"),
            "비중(%)": st.column_config.NumberColumn(format="%.1f"),
            "PER": st.column_config.NumberColumn(format="%.2f"),
            "PBR": st.column_config.NumberColumn(format="%.2f"),
            "부채비율(%)": st.column_config.NumberColumn(format="%.1f"),
            "ROE(%)": st.column_config.NumberColumn(format="%.1f"),
            "배당수익률(%)": st.column_config.NumberColumn(format="%.2f"),
            "DPS": st.column_config.NumberColumn(format="%.0f"),
            "적정가": st.column_config.NumberColumn(format="%.0f"),
        },
    )
    st.caption("평가금액·손익은 원화 환산 기준입니다(해외 종목은 원/달러 환율 적용). "
               "적정가는 PER·PBR·DCF 세 방식을 섹터 특성에 맞게 가중한 값입니다.")

    st.divider()
    st.markdown("#### 🔍 종목 상세 (표에서 행을 클릭하거나 아래에서 선택)")
    clicked = list(getattr(getattr(table_state, "selection", None), "rows", []) or [])
    if clicked:
        st.session_state["holding_detail_pick"] = int(clicked[0])
    labels = [f"{h.get('name') or h['code']} ({h['code']})" for h in holdings]
    pick = st.selectbox("상세 볼 종목", range(len(holdings)), format_func=lambda i: labels[i],
                        key="holding_detail_pick")
    h = holdings[pick]
    snap = by_code.get(core.clean_code(h["code"])) or core.StockSnapshot(code=h["code"])
    render_detail(snap, rules, key=f"hold_{snap.code}", avg_price=float(h.get("avg_price") or 0) or None)

    st.divider()
    with st.expander("🧭 보유 종목 한눈 요약 (시나리오 기준)", expanded=False):
        for h, snap, scn, ret in details:
            st.markdown(
                f"- **{snap.name or h['name']}** · 현재 {money(snap.price, snap.currency)} · "
                f"수익률 {pct_text(ret)} · 상승 {scn['상승']['range_text']} / "
                f"기본 {scn['기본']['range_text']} / 경계 {scn['경계']['range_text']}")

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
                            others = [h for h in holdings if core.clean_code(h["code"]) != new["code"]]
                            if persist("holdings", others + [new],
                                       f"{new['name'] or new['code']}({new['code']}) 종목을 저장했습니다."):
                                st.rerun()

        with edit_col:
            st.markdown("**수정 / 삭제**")
            if not holdings:
                st.caption("보유 종목이 없습니다.")
            else:
                labels = [f"{h.get('name') or h['code']} ({h['code']})" for h in holdings]
                pick = st.selectbox("종목 선택", range(len(holdings)),
                                    format_func=lambda i: labels[i], key="edit_pick")
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


# -------------------------------------------- 2) 저평가 우량주 발굴 (가치함정 필터) ----
def tab_screener(rules: core.FilterRules) -> None:
    st.subheader("🔍 저평가 우량주 발굴 (가치함정 필터)")
    st.caption(f"유니버스 {len(core.UNIVERSE)}종목을 같은 기준으로 훑습니다. "
               f"통과/탈락 사유와 종합 가치점수(0~100)를 함께 보여줍니다. 현재 기준: {rules.summary()}")

    f1, f2, f3 = st.columns([1.1, 1.6, 1])
    with f1:
        markets = st.multiselect("시장", core.MARKET_LABELS[1:],
                                 default=["코스피", "코스닥", "미국"], key="scan_markets")
    with f2:
        sectors = st.multiselect("섹터 (비우면 전체)", core.SECTOR_ORDER, default=[], key="scan_sectors")
    with f3:
        scope = st.radio("스캔 범위", ["빠른 스캔(150)", "전체 스캔", "상위 60"], index=0, key="scan_scope")

    extra = st.text_input("직접 입력 종목 추가 (쉼표로 구분: 이름·코드·티커)",
                          placeholder="예: HPSP, 클래시스, 엔비디아", key="scan_extra")

    if st.button("🔍 스캔 실행", key="scan_btn", type="primary"):
        limit = {"빠른 스캔(150)": 150, "전체 스캔": None, "상위 60": 60}[scope]
        items = core.universe_items(sectors=sectors or None, markets=markets or None, limit=limit)
        known = {i["code"] for i in items}
        for raw in [c.strip() for c in extra.split(",") if c.strip()]:
            found = core.search_stocks(raw, limit=1)
            if not found:
                st.warning(f"'{raw}' 종목을 찾지 못했습니다.")
                continue
            item = found[0]
            if item["code"] not in known:
                sector = item.get("sector") or core.search_sector(item["code"]) or "기타"
                items.append({"code": item["code"], "name": item["name"],
                              "sector": sector, "market": item.get("board") or "US"})
                known.add(item["code"])
        bar = st.progress(0.0, text=f"{len(items)}개 종목 스캔 준비...")

        def on_progress(done: int, total: int) -> None:
            try:
                bar.progress(min(1.0, done / max(1, total)),
                             text=f"{done}/{total} 종목 조회 중...")
            except Exception:
                pass

        rows = core.scan_universe(items, rules, workers=10, progress=on_progress)
        bar.empty()
        st.session_state["scan_rows"] = rows
        st.session_state["scan_meta"] = {"markets": markets, "sectors": sectors, "scope": scope}

    rows = st.session_state.get("scan_rows")
    if not rows:
        st.info("'🔍 스캔 실행'을 누르면 후보 종목을 훑어 통과 종목과 점수를 보여줍니다. "
                "150종목 기준 대략 30~60초 걸립니다.")
        return

    passed = [r for r in rows if r["filter"]["verdict"] == "통과"]
    wait = [r for r in rows if r["filter"]["verdict"] == "판정 보류"]
    failed = [r for r in rows if r["filter"]["verdict"] not in ("통과", "판정 보류")]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("스캔 종목", f"{len(rows)}개")
    m2.metric("✅ 통과", f"{len(passed)}개")
    m3.metric("⚠️ 판정 보류", f"{len(wait)}개")
    m4.metric("❌ 탈락", f"{len(failed)}개")

    table = []
    for r in rows:
        s, f = r["snap"], r["filter"]
        table.append({
            "판정": f["verdict"], "종목": s.name or s.code, "코드": s.code, "섹터": s.sector,
            "현재가": s.price, "PER": s.per, "PBR": s.pbr, "부채비율(%)": s.debt_ratio,
            "ROE(%)": s.roe, "배당수익률(%)": s.dividend_yield, "가치점수": f["score"], "사유": f["reason"],
        })
    df = pd.DataFrame(table)
    scan_state = st.dataframe(
        df, hide_index=True, width="stretch", height=420,
        on_select="rerun", selection_mode="single-row", key="scan_table",
        column_config={
            "현재가": st.column_config.NumberColumn(format="%.2f"),
            "PER": st.column_config.NumberColumn(format="%.2f"),
            "PBR": st.column_config.NumberColumn(format="%.2f"),
            "부채비율(%)": st.column_config.NumberColumn(format="%.1f"),
            "ROE(%)": st.column_config.NumberColumn(format="%.1f"),
            "배당수익률(%)": st.column_config.NumberColumn(format="%.2f"),
            "가치점수": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d"),
        })

    candidates = passed + wait
    st.markdown("#### ✅ 기준 통과·보류 종목 — 상세 보기")
    if not candidates:
        st.caption("이번 기준을 모두 통과한 종목이 없습니다. 기준을 조금 완화하거나(PBR 1.5→2.0 등) "
                   "직접 입력으로 종목을 추가해 다시 스캔해 보세요.")
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
    chosen = candidates[pick]
    render_detail(chosen["snap"], rules, key=f"scan_{chosen['snap'].code}")

    with st.expander("통과 종목 요약표", expanded=False):
        st.dataframe(pd.DataFrame([
            {"종목": r["snap"].name, "코드": r["snap"].code, "섹터": r["snap"].sector,
             "가치점수": r["filter"]["score"], "사유": r["filter"]["reason"]}
            for r in candidates]), hide_index=True, width="stretch")


# ----------------------------------- 3) 국내외 이슈 & 개별 종목 정밀 분석 ----
def macro_dashboard() -> core.MacroBoard:
    with st.spinner("거시 지표를 불러오는 중..."):
        macro = core.fetch_macro()
    st.markdown("#### 🌐 오늘의 시장 지표")
    row1 = macro.items[:6]
    cols = st.columns(len(row1))
    for col, item in zip(cols, row1):
        with col:
            st.metric(item.label, item.value_text(), item.change_text(),
                      delta_color="normal" if item.ok else "off")
            st.caption(item.extra or (item.as_of[:16].replace("T", " ") if item.as_of else "확인 불가"))
    row2 = macro.items[6:]
    if row2:
        cols2 = st.columns(len(row2))
        for col, item in zip(cols2, row2):
            with col:
                st.metric(item.label, item.value_text(), item.change_text())
                st.caption(item.as_of[:16].replace("T", " ") if item.as_of else "확인 불가")

    if macro.flows:
        st.markdown("**시장 수급 (단위: 억원)**")
        st.dataframe(pd.DataFrame([{
            "시장": f.label, "기준일": f.date, "외국인": f.foreign, "기관": f.institution,
            "개인": f.personal, "거래대금": core.compact_money(f.trading_value) if f.trading_value else "-",
        } for f in macro.flows]), hide_index=True, width="stretch")
    st.caption(f"출처: 네이버 금융 지수·시장지표, 뉴욕 연준(EFFR) · 기준 {macro.as_of or '-'} · "
               f"지표 {macro.ok_count()}/{len(macro.items)}개 확인")
    return macro


def sector_pulse_block() -> None:
    st.markdown("**산업 동향 (업종 대표 종목 등락 평균)**")
    with st.spinner("산업별 대표 종목을 확인하는 중..."):
        pulses = core.fetch_sector_pulse()
    cols = st.columns(len(pulses))
    for col, pulse in zip(cols, pulses):
        with col:
            st.metric(pulse.label, pulse.text() if pulse.change_pct is not None else "확인 불가",
                      delta_color="off")
            members = ", ".join(f"{m['name']} {m['change_pct']:+.1f}%" for m in pulse.members[:3])
            st.caption(members if members else pulse.note)


def holdings_news_block(holdings: list[dict]) -> None:
    st.markdown("#### 📰 보유종목 관련 뉴스 자동분석")
    if not holdings:
        st.caption("보유 종목이 등록되면 종목별 뉴스·공시·리포트를 근거(URL·시각)와 함께 모아 분류합니다.")
        return
    st.caption("실제 기사 URL과 피드 게시 시각만 표시합니다. 호재/악재 판단은 제목 키워드 기반 참고용입니다.")
    for h in holdings[:6]:
        code = core.clean_code(h["code"])
        digest = core.fetch_news_digest(h.get("name") or code, code=code if core.detect_market(code) == "KR" else "",
                                        limit=8)
        with st.expander(f"{h.get('name') or code} ({code}) · 수집 {len(digest.items)}건", expanded=False):
            news_digest_block(digest, h.get("name", ""))
            for line in core.news_impact_lines(digest):
                st.caption(line)


def impact_block(macro: core.MacroBoard, holdings: list[dict]) -> None:
    st.markdown("#### 🧭 이슈 → 내 종목·관심섹터 영향 해석")
    st.caption("거시 지표 움직임과 오늘 뉴스 헤드라인을 규칙·키워드로 연결한 해석입니다(AI 키 없이 동작).")
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

    st.markdown("**🤖 AI 해설 (선택)**")
    if not core.ai_available():
        st.caption("AI 키가 없어 규칙 기반 해석만 표시합니다. 키를 넣으면 이 자리에 문장형 해설이 추가됩니다.")
    else:
        if st.button("AI 해설 생성", key="impact_ai"):
            with st.spinner("AI가 해설을 작성하는 중..."):
                text, err = core.generate_ai_text(core.build_impact_prompt(report, macro))
            if text:
                st.markdown(text)
            else:
                st.error(err)
    return report


def tab_analysis(rules: core.FilterRules) -> None:
    st.subheader("🌐 이슈 & 종목 분석")
    macro = macro_dashboard()
    st.divider()
    sector_pulse_block()
    st.divider()
    holdings = load_store("holdings")
    impact_block(macro, holdings)
    st.divider()
    holdings_news_block(holdings)
    st.divider()

    st.markdown("#### 🔎 개별 종목 정밀 분석")
    st.caption("종목명·티커·종목코드 아무거나 입력하세요(예: HPSP, 삼성전자, 현대차, 엔비디아, NVDA).")
    with st.form("analyze_form"):
        c1, c2 = st.columns([3, 1])
        query = c1.text_input("종목 검색", placeholder="예: HPSP / 삼성전자 / 엔비디아", key="an_query")
        submitted = c2.form_submit_button("검색·분석", width="stretch", type="primary")
    if submitted and query.strip():
        found = core.search_stocks(query, limit=6)
        if not found:
            st.error(f"'{query}' 종목을 찾지 못했습니다. 다른 이름이나 6자리 코드로 시도해 주세요.")
        elif len(found) == 1:
            item = found[0]
            with st.spinner(f"{item['name']} 데이터를 불러오는 중..."):
                snap = core.fetch_snapshot(item["code"], name_hint=item.get("name"), deep=True)
            st.session_state["analysis"] = {"snapshot": snap, "candidates": []}
        else:
            st.session_state["analysis"] = {"snapshot": None, "candidates": found, "query": query}

    data = st.session_state.get("analysis") or {}
    candidates = data.get("candidates") or []
    if candidates:
        st.markdown(f"**'{data.get('query', '')}' 검색 결과** — 종목을 선택하면 상세 분석을 보여줍니다.")
        labels = [f"{c['name']} ({c['code']}) · {c.get('board') or c.get('market', '')}"
                  for c in candidates]
        pick = st.selectbox("검색 결과", range(len(candidates)), format_func=lambda i: labels[i],
                            key="analysis_pick")
        if st.button("이 종목 분석", key="analysis_go"):
            item = candidates[pick]
            with st.spinner(f"{item['name']} 데이터를 불러오는 중..."):
                snap = core.fetch_snapshot(item["code"], name_hint=item.get("name"), deep=True)
            st.session_state["analysis"] = {"snapshot": snap, "candidates": []}
            data = st.session_state["analysis"]

    snap = data.get("snapshot")
    if not snap:
        return
    render_detail(snap, rules, key=f"an_{snap.code}")


# ------------------------------------------------- 4) 추천 추적 성적표 ----
def tab_tracker() -> None:
    st.subheader("📈 추천 추적 성적표")
    picks = load_store("picks")
    st.caption("발굴·분석으로 등록한 종목을 등록일·등록가·선정근거·목표/경계 기준과 함께 저장하고, "
               "현재가 대비 누적 수익률과 달성 상태를 자동 추적합니다.")

    if not picks:
        st.info("아직 등록한 추천이 없습니다. 2️⃣ 저평가 우량주 발굴 또는 3️⃣ 개별 종목 분석에서 "
                "'추천 추적에 등록'을 누르면 여기에 쌓입니다.")
    else:
        with st.spinner("등록 종목 현재가를 확인하는 중..."):
            snaps = core.fetch_snapshots(
                [{"code": p["code"], "sector": None, "name": p["name"]} for p in picks])
        by_code = {s.code: s for s in snaps}

        rows, returns, hits, warnings = [], [], 0, 0
        for p in picks:
            snap = by_code.get(core.clean_code(p["code"])) or core.StockSnapshot(code=p["code"])
            entry = p.get("entry_price")
            price = snap.price
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
            rows.append({
                "등록일": p.get("date", ""), "종목": snap.name or p.get("name", ""),
                "코드": snap.code, "등록가": entry, "현재가": price, "수익률(%)": ret,
                "목표가": target, "경계가": stop, "상태": status, "경과": "-",
                "배당수익률(%)": snap.dividend_yield, "선정근거": p.get("rationale", ""),
            })

        avg_ret = sum(returns) / len(returns) if returns else 0.0
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("등록 종목", f"{len(picks)}개")
        m2.metric("평균 수익률", f"{avg_ret:+.1f}%")
        m3.metric("목표 달성", f"{hits}개")
        m4.metric("경계 이탈", f"{warnings}개")

        st.dataframe(
            pd.DataFrame(rows), hide_index=True, width="stretch",
            column_config={
                "등록가": st.column_config.NumberColumn(format="%.2f"),
                "현재가": st.column_config.NumberColumn(format="%.2f"),
                "수익률(%)": st.column_config.NumberColumn(format="%.1f"),
                "목표가": st.column_config.NumberColumn(format="%.0f"),
                "경계가": st.column_config.NumberColumn(format="%.0f"),
                "배당수익률(%)": st.column_config.NumberColumn(format="%.2f"),
            })
        st.caption("상태는 목표가 도달 → 🎯, 경계가 이탈 → ⚠️ 로 표시됩니다. "
                   "수익률은 등록가 대비 현재가 기준입니다.")

        with st.expander("근거 자세히 보기"):
            for p in picks:
                snap = by_code.get(core.clean_code(p["code"])) or core.StockSnapshot(code=p["code"])
                st.markdown(f"- **{snap.name or p.get('name')}** ({p['code']}) · {p.get('date')} · "
                            f"등록가 {money(p.get('entry_price'), snap.currency)} · "
                            f"목표 {money(p.get('target'), snap.currency)} / 경계 {money(p.get('stop'), snap.currency)}"
                            f"\n  <span class='vi-note'>{p.get('rationale', '')}</span>",
                            unsafe_allow_html=True)

        with st.expander("🗑️ 추천 기록 삭제"):
            labels = [f"{p.get('date')} · {p.get('name') or p['code']} ({p['code']})" for p in picks]
            pick = st.selectbox("삭제할 기록", range(len(picks)),
                                format_func=lambda i: labels[i], key="pick_del_idx")
            confirm = st.checkbox("정말 삭제합니다 (되돌릴 수 없음)", key="pick_del_confirm")
            if st.button("선택 기록 삭제", disabled=not confirm, key="pick_del_btn"):
                remaining = [p for i, p in enumerate(picks) if i != pick]
                if persist("picks", remaining, "삭제했습니다."):
                    st.rerun()

    st.divider()
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


# ------------------------------------------------------------------- 실행 ----
def main() -> None:
    ensure_state()
    header()
    base_rules = core.FilterRules()
    rules = sidebar(base_rules)

    tab1, tab2, tab3, tab4 = st.tabs([
        "🛡️ 보유 종목 & 시나리오",
        "🔍 저평가 우량주 발굴",
        "🌐 이슈 & 종목 분석",
        "📈 추천 추적 성적표",
    ])
    with tab1:
        tab_portfolio(rules)
    with tab2:
        tab_screener(rules)
    with tab3:
        tab_analysis(rules)
    with tab4:
        tab_tracker()

    st.divider()
    st.caption(f"저장소: {core.storage_label()} · {core.storage_target()} · "
               f"시세·재무: 네이버 금융(한국/해외) / yfinance 보조 · 뉴스: 네이버·구글뉴스 RSS · "
               f"캐시 5~10분 · 본 화면은 정보 제공용입니다.")


if __name__ == "__main__":
    main()
