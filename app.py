"""📈 가치투자 스튜디오 — 보유 점검 · 저평가 우량주 발굴 · 종목 분석 · 추천 추적.

동철님의 투자 원칙("저가 우량주를 찾고, 투자금을 잃지 않는 것")을 기준으로
보유 종목과 후보 종목을 같은 잣대로 점검하는 Streamlit 앱.

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
  .block-container {padding-top: 1.1rem; max-width: 1250px;}
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
  a {text-decoration: none;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ------------------------------------------------------------- 공통 도우미 ----
def header() -> None:
    st.markdown(
        '<div class="vi-header">'
        "<h1>📈 가치투자 스튜디오</h1>"
        "<p>저가 우량주를 찾고, 투자금을 잃지 않는 것 — 보유 점검 · 가치함정 필터 · 종목 분석 · 추천 추적</p>"
        f'<span class="tag">기준일 {core.today_str()} · 데이터 네이버금융·야후파이낸스·뉴스RSS</span>'
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
            st.success(f"🤖 AI 심층 리포트: {core.ai_config()[0]} 연결됨")
        else:
            st.info("🤖 AI 심층 리포트: 키 없음")
            st.caption("OPENAI_API_KEY · GEMINI_API_KEY · OPENROUTER_API_KEY 중 하나를 "
                       "Streamlit Secrets(또는 환경변수)에 넣으면 3️⃣ 탭에서 AI 리포트가 추가됩니다. "
                       "키가 없어도 정량·시나리오 분석은 100% 동작합니다.")

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
            "적정가": scn.get("fair"), "신호": signal,
        })

    if total_value:
        for row in rows:
            row["비중(%)"] = row["평가금액(원)"] / total_value * 100

    profit_total = total_value - total_cost
    ret_total = (profit_total / total_cost * 100) if total_cost else 0.0
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("총 매수금액", f"{total_cost:,.0f}원")
    m2.metric("총 평가금액", f"{total_value:,.0f}원")
    m3.metric("평가손익", f"{profit_total:+,.0f}원", f"{ret_total:+.1f}%")
    m4.metric("보유 종목", f"{len(holdings)}종목")

    st.dataframe(
        pd.DataFrame(rows), hide_index=True, width="stretch",
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
            "적정가": st.column_config.NumberColumn(format="%.0f"),
        },
    )
    st.caption("평가금액·손익은 원화 환산 기준입니다(해외 종목은 원/달러 환율 적용). "
               "적정가 = (BPS×1배 + EPS×12배) 평균.")

    st.markdown("#### 종목별 시나리오와 근거")
    for h, snap, scn, ret in details:
        filt = core.run_filter(snap, rules)
        badge = {"통과": "vi-pass", "탈락": "vi-fail"}.get(filt["verdict"], "vi-wait")
        label = (f"{snap.name or h['name']} · {money(snap.price, snap.currency)} "
                 f"({pct_text(snap.change_pct)}) · 수익률 {pct_text(ret)} · 가치점수 {filt['score']}점")
        with st.expander(label, expanded=False):
            st.markdown(
                f"<span class='vi-pill {badge}'>{filt['verdict']}</span>"
                f"<span class='vi-note'>평단 {money(h.get('avg_price'), snap.currency)} · "
                f"PER {value_or(snap.per)} · PBR {value_or(snap.pbr)} · "
                f"ROE {value_or(snap.roe, '{:.1f}%')} · 부채비율 {value_or(snap.debt_ratio, '{:.1f}%')} · "
                f"출처 {', '.join(snap.sources) or '-'}</span>",
                unsafe_allow_html=True)
            scenario_cards(scn)
            left, right = st.columns(2)
            with left:
                st.markdown("**재무 건전성**")
                health_table(snap)
                if filt.get("note"):
                    st.caption(filt["note"])
            with right:
                st.markdown("**관련 최신 뉴스**")
                news_block(core.fetch_news(snap.name or h["name"], 4))
            if snap.notes:
                st.caption("데이터 참고: " + " · ".join(dict.fromkeys(snap.notes)))

    render_holding_manager(holdings)


def render_holding_manager(holdings: list[dict]) -> None:
    with st.expander("➕ 보유 종목 관리 (추가 · 수정 · 삭제)", expanded=False):
        add_col, edit_col = st.columns(2)
        with add_col:
            st.markdown("**종목 추가**")
            with st.form("add_holding"):
                code = st.text_input("종목코드 (예: 034020, AAPL)", key="add_code")
                name = st.text_input("종목명 (선택)", key="add_name")
                qty = st.number_input("수량", min_value=0.0, step=1.0, value=1.0, key="add_qty")
                avg = st.number_input("평단가", min_value=0.0, step=100.0, value=0.0, key="add_avg")
                memo = st.text_input("메모 (선택)", key="add_memo")
                if st.form_submit_button("추가", width="stretch"):
                    if not code.strip():
                        st.error("종목코드를 입력하세요.")
                    elif qty <= 0 or avg <= 0:
                        st.error("수량과 평단가는 0보다 커야 합니다.")
                    else:
                        new = {"code": core.clean_code(code), "name": name.strip(),
                               "qty": qty, "avg_price": avg, "memo": memo.strip()}
                        others = [h for h in holdings if core.clean_code(h["code"]) != new["code"]]
                        if persist("holdings", others + [new],
                                   f"{new['name'] or new['code']} 종목을 저장했습니다."):
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
    st.caption("관심 섹터 대표 종목과 직접 입력 종목을 같은 기준으로 훑습니다. "
               "통과/탈락 사유와 종합 가치점수(0~100)를 함께 보여줍니다. "
               "현재 기준: " + rules.summary())

    sector_col, extra_col = st.columns([1, 1.4])
    with sector_col:
        sectors = st.multiselect(
            "스캔할 섹터", core.SECTOR_ORDER,
            default=["원전·에너지", "건설", "반도체", "바이오", "금융", "제조·철강"],
            key="scan_sectors")
    with extra_col:
        extra = st.text_input("직접 입력 종목 (쉼표로 구분: 코드·종목명·미국 티커)",
                              placeholder="예: 000660, 클래시스, NVDA", key="scan_extra")

    if st.button("🔍 스캔 실행", key="scan_btn", type="primary"):
        items = core.universe_items(sectors=sectors or None)
        if extra.strip():
            extra_codes = [c.strip() for c in extra.split(",") if c.strip()]
            known = {i["code"] for i in items}
            for raw in extra_codes:
                resolved = core.resolve_query(raw) or {"code": core.clean_code(raw), "name": "", "market": None}
                code = resolved["code"]
                if code not in known:
                    items.append({"code": code, "name": resolved.get("name", ""),
                                  "sector": "기타", "market": resolved.get("market") or core.detect_market(code)})
                    known.add(code)
        with st.spinner(f"{len(items)}개 종목 스캔 중... (캐시에 없으면 20~40초)"):
            st.session_state["scan_rows"] = core.scan_universe(items, rules)

    rows = st.session_state.get("scan_rows")
    if not rows:
        st.info("'🔍 스캔 실행'을 누르면 후보 종목을 훑어 통과 종목과 점수를 보여줍니다.")
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
            "ROE(%)": s.roe, "가치점수": f["score"], "사유": f["reason"],
        })
    st.dataframe(
        pd.DataFrame(table), hide_index=True, width="stretch", height=430,
        column_config={
            "현재가": st.column_config.NumberColumn(format="%.2f"),
            "PER": st.column_config.NumberColumn(format="%.2f"),
            "PBR": st.column_config.NumberColumn(format="%.2f"),
            "부채비율(%)": st.column_config.NumberColumn(format="%.1f"),
            "ROE(%)": st.column_config.NumberColumn(format="%.1f"),
            "가치점수": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d"),
        })

    st.markdown("#### ✅ 기준 통과 종목 — 상세 근거와 추천 등록")
    if not passed and not wait:
        st.caption("이번 기준을 모두 통과한 종목이 없습니다. 기준을 조금 완화하거나(예: PBR 1.5→2.0) "
                   "직접 입력으로 종목을 추가해 다시 스캔해 보세요.")
    for r in passed + wait:
        s, f = r["snap"], r["filter"]
        scn = core.build_scenarios(s)
        with st.expander(f"{s.name or s.code} · 가치점수 {f['score']}점 · {f['verdict']}", expanded=bool(passed) and r is passed[0]):
            left, right = st.columns([1.15, 1])
            with left:
                st.markdown("**필터 판정**")
                st.table(pd.DataFrame(f["checks"])[["name", "status", "text"]]
                         .rename(columns={"name": "기준", "status": "판정", "text": "근거"}))
                st.caption("점수 구성: " + " · ".join(f"{k} {v:.0f}" for k, v in f["score_parts"].items()))
                if f.get("note"):
                    st.caption(f["note"])
            with right:
                st.markdown("**밸류에이션·재무**")
                st.write(pd.DataFrame([
                    ["현재가", money(s.price, s.currency)],
                    ["PER / PBR", f"{value_or(s.per)} / {value_or(s.pbr)}"],
                    ["ROE / 부채비율", f"{value_or(s.roe, '{:.1f}%')} / {value_or(s.debt_ratio, '{:.1f}%')}"],
                    ["영업이익", value_or(s.op_income, "{:,.0f}")],
                    ["영업현금흐름", value_or(s.ocf, "{:,.0f}")],
                    ["적정가 / 목표가", f"{money(scn.get('fair'), s.currency)} / {money(scn.get('target'), s.currency)}"],
                ], columns=["항목", "값"]))
                st.caption("출처: " + (", ".join(s.sources) or "-"))
            rationale = f"가치점수 {f['score']}점 · {f['reason']}"
            if st.button("📈 이 종목을 추천 추적에 등록", key=f"reg_{s.code}"):
                register_pick(s, scn, rationale)


# ----------------------------------- 3) 국내외 이슈 & 개별 종목 정밀 분석 ----
def tab_analysis(rules: core.FilterRules) -> None:
    st.subheader("🌐 국내외 이슈 & 개별 종목 정밀 분석")

    with st.expander("📰 오늘의 시장 이슈와 관심 섹터 영향", expanded=True):
        if st.button("🔄 뉴스 새로 불러오기", key="news_refresh"):
            core.fetch_market_news.cache_clear()  # type: ignore[attr-defined]
        with st.spinner("주요 뉴스를 모으는 중..."):
            news_map = core.fetch_market_news()
        impacts = core.sector_impact(news_map)
        if impacts:
            st.markdown("**관심 섹터 영향 포인트**")
            for sector, lines in impacts.items():
                if sector in ("원전·에너지", "건설", "바이오", "반도체"):
                    st.markdown(f"- **{sector}**: " + " / ".join(lines))
        cols = st.columns(2)
        topics = list(news_map.items())
        for idx, (topic, items) in enumerate(topics):
            with cols[idx % 2]:
                st.markdown(f"**{topic}**")
                news_block(items, "해당 주제의 최신 뉴스가 없습니다.")

    st.divider()
    st.markdown("#### 🔎 개별 종목 정밀 분석")
    st.caption("한국 종목코드·종목명 또는 미국 티커를 입력하면 재무 건전성 + 밸류에이션 + "
               "상승/기본/하락 3단 시나리오를 만들어 줍니다. AI 키가 있으면 심층 리포트가 추가됩니다.")
    with st.form("analyze_form"):
        c1, c2 = st.columns([3, 1])
        query = c1.text_input("종목코드 · 종목명 · 티커",
                              placeholder="예: 034020, 두산에너빌리티, CEG", key="an_query")
        submitted = c2.form_submit_button("분석하기", width="stretch", type="primary")
    if submitted:
        resolved = core.resolve_query(query)
        if not resolved:
            st.error("종목을 찾지 못했습니다. 6자리 종목코드나 미국 티커(예: NVDA)로 입력해 주세요.")
        else:
            with st.spinner(f"{query} 데이터를 불러오는 중..."):
                snap = core.fetch_snapshot(resolved["code"], name_hint=resolved.get("name"))
            st.session_state["analysis"] = {
                "snapshot": snap,
                "filter": core.run_filter(snap, rules),
                "scenarios": core.build_scenarios(snap),
                "ai": None,
            }

    data = st.session_state.get("analysis")
    if not data:
        return
    snap: core.StockSnapshot = data["snapshot"]
    filt = data["filter"]
    scn = data["scenarios"]

    st.markdown(f"### {snap.name}({snap.code}) · {snap.sector}")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("현재가", money(snap.price, snap.currency), pct_text(snap.change_pct))
    k2.metric("PER / PBR", f"{value_or(snap.per)} / {value_or(snap.pbr)}")
    k3.metric("ROE", value_or(snap.roe, "{:.1f}%"))
    k4.metric("가치점수", f"{filt['score']}점", filt["verdict"], delta_color="off")
    if snap.currency == "USD":
        st.caption("미국 종목은 달러 기준으로 표시됩니다.")

    left, right = st.columns(2)
    with left:
        st.markdown("**재무 건전성**")
        health_table(snap)
        if filt.get("note"):
            st.caption(filt["note"])
    with right:
        st.markdown("**밸류에이션·가치함정 판정**")
        st.table(pd.DataFrame(filt["checks"])[["name", "status", "text"]]
                 .rename(columns={"name": "기준", "status": "판정", "text": "근거"}))
        st.caption("점수 구성: " + " · ".join(f"{k} {v:.0f}" for k, v in filt["score_parts"].items()))

    st.markdown("#### 3단 시나리오 대응 기준")
    scenario_cards(scn)

    b1, b2 = st.columns([1, 1])
    with b1:
        if st.button("📈 이 종목을 추천 추적에 등록", key="analysis_register"):
            register_pick(snap, scn, f"정밀분석 · 가치점수 {filt['score']}점 · {filt['reason']}")
    with b2:
        st.caption("등록 시 현재가·목표가·경계선이 함께 저장되어 이후 수익률을 자동 추적합니다.")

    st.markdown("**관련 뉴스**")
    news_block(core.fetch_news(snap.name or snap.code, 5))

    st.divider()
    st.markdown("#### 🤖 AI 심층 리포트")
    if not core.ai_available():
        st.info("AI 키가 설정되지 않아 정량·시나리오 분석만 제공합니다. "
                "Streamlit Secrets에 OPENAI_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY 중 하나를 "
                "넣으면 이 자리에 AI 심층 리포트가 생성됩니다.")
    else:
        if st.button("AI 리포트 생성", key="ai_btn"):
            with st.spinner("AI가 리포트를 작성하는 중..."):
                text, err = core.generate_ai_report(snap, scn, filt)
            data["ai"] = (text, err)
        if data.get("ai"):
            text, err = data["ai"]
            if text:
                st.markdown(text)
            else:
                st.error(err)


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
            days = "-"
            try:
                days = f"{(date.today() - date.fromisoformat(p['date'])).days}일"
            except Exception:
                pass
            rows.append({
                "등록일": p.get("date", ""), "종목": snap.name or p.get("name", ""),
                "코드": snap.code, "등록가": entry, "현재가": price, "수익률(%)": ret,
                "목표가": target, "경계가": stop, "상태": status, "경과": days,
                "선정근거": p.get("rationale", ""),
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
            code = c1.text_input("종목코드·티커", key="pick_add_code")
            entry = c2.number_input("등록가 (0이면 현재가)", min_value=0.0, step=100.0,
                                    value=0.0, key="pick_add_entry")
            target = c3.number_input("목표가 (0이면 자동)", min_value=0.0, step=100.0,
                                     value=0.0, key="pick_add_target")
            rationale = st.text_input("선정 근거", key="pick_add_reason")
            if st.form_submit_button("등록", width="stretch"):
                if not code.strip():
                    st.error("종목코드를 입력하세요.")
                else:
                    with st.spinner("데이터 확인 중..."):
                        snap = core.fetch_snapshot(core.clean_code(code))
                    scn = core.build_scenarios(snap)
                    entry_price = entry or snap.price
                    if not entry_price:
                        st.error("현재가를 가져오지 못했습니다. 등록가를 직접 입력하세요.")
                    else:
                        picks_now = load_store("picks")
                        picks_now.append({
                            "date": core.today_str(), "code": snap.code,
                            "name": snap.name or snap.code, "entry_price": entry_price,
                            "rationale": rationale or "직접 등록", "target": target or scn.get("target"),
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
               f"시세·재무: 네이버 금융(한국) / yfinance·야후 파이낸스(미국) · "
               "캐시 5분 · 본 화면은 정보 제공용입니다.")


if __name__ == "__main__":
    main()
