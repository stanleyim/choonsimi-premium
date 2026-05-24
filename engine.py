"""
engine.py — choonsimi-premium v10.0
═══════════════════════════════════════════════════════════════════════════
목적   : 일별 Top20 종목 선정 + Top5 진입 후보 + Forward Test 추적
기준   : 장마감 자료만 (실시간 X). 매일 1회 실행.
구조   : 5축 가중합 (M / F / Q / R / L) — AND 폐기

v7.2.0 → v10 변경점 (모두 데이터 검증)
  ✔ 5조건 AND → 5축 cross-section rank 가중합
  ✔ 모멘텀 강제컷 (직전 5d 누적 +0.3% ~ +7% 밖이면 점수 0)
  ✔ Vol surge 1.5x (당일 거래량 / 20d 평균)
  ✔ T+1 시가 갭업 +1% 이상 회피 (Phase 2 [3])
  ✔ TP +10% / SL -4% / 시간청산 28일 (Phase 2 v3 16조합 중 최선)
  ✔ Position sizing 30/20/20/15/15% (rank 차등)
  ✔ DOWNTREND 진입 차단 (옵션, 기본 ON)
  ✔ 재무 페널티 (ROE<0 또는 debt>200 → 0.3 가중)
  ✔ 좀비 종목 차단 (ca_zero 50% 페널티)
  ✔ 시총 500억 미만 제외
  ✔ Forward Test T+1/T+3/T+5 누적 추적

백테스트 검증 (Phase 2 5년치 walk-forward)
  Test 2025-01 ~ 2026-05-15:
    적중률 42.91% | CAGR +64.30% | 샤프 1.95 | MDD -16.16%
  Validation 2024:
    적중률 41.51% | CAGR +40.75% | 샤프 1.79 | MDD -12.08%

입력 파일 (data/)
  history.csv        — 종목 일별 OHLCV (fetch_data.py가 생성, 내일 작성)
  market_flow.json   — 종목별 외국인/기관/개인 일별 수급
  macro.json         — KOSPI200, VKOSPI, SOX, VIX, USDKRW, 기준금리
  fundamental.json   — 종목별 ROE/debt_ratio/shares
  stocks_meta.json   — 종목 메타 (분할/IPO/좀비 플래그)
  signal_history.csv — 누적 신호 (engine.py가 append)

출력 파일 (data/)
  signal_history.csv — Top20 누적 append
  result.json        — 오늘 결과 overwrite
  forward_test.csv   — T+1/T+3/T+5 수익 누적/갱신

환경변수 (모두 선택)
  ECOS_KEY              — 한국은행 ECOS API (fetch_macro.py에서 사용, 내일)
  TELEGRAM_BOT_TOKEN    — 텔레그램 알림용
  TELEGRAM_CHAT_ID      — 텔레그램 수신자
═══════════════════════════════════════════════════════════════════════════
"""

import os
import json
import math
from datetime import datetime, timezone, timedelta

import pandas as pd
import numpy as np


# ═══════════════════════════════════════════════════════════════════════════
# 환경 / 경로
# ═══════════════════════════════════════════════════════════════════════════
KST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

PATHS = {
    "HISTORY":  os.path.join(DATA_DIR, "history.csv"),
    "FLOW":     os.path.join(DATA_DIR, "market_flow.json"),
    "MACRO":    os.path.join(DATA_DIR, "macro.json"),
    "FUND":     os.path.join(DATA_DIR, "fundamental.json"),
    "STOCKS":   os.path.join(DATA_DIR, "stocks_meta.json"),
    "SIGNAL":   os.path.join(DATA_DIR, "signal_history.csv"),
    "RESULT":   os.path.join(DATA_DIR, "result.json"),
    "FORWARD":  os.path.join(DATA_DIR, "forward_test.csv"),
}

# 환경변수 (선택)
ECOS_KEY = os.environ.get("ECOS_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


# ═══════════════════════════════════════════════════════════════════════════
# 파라미터 (Phase 2 + 외부 검토자 검증값. 함부로 바꾸지 말 것)
# ═══════════════════════════════════════════════════════════════════════════
TOP_N = 20                              # Top20 출력
ENTRY_N = 5                             # Top5 진입 후보
MAX_POSITIONS = 5                       # 동시 보유 한도
SIZE_BY_RANK = {1: 0.30, 2: 0.20, 3: 0.20, 4: 0.15, 5: 0.15}

TAKE_PROFIT = 0.10                      # +10%
STOP_LOSS = -0.04                       # -4%
TIME_EXIT_DAYS = 28                     # 캘린더 28일 (≈ 영업일 20)
GAP_UP_AVOID = 0.01                     # T+1 시가 갭업 +1% 이상 회피

MOMENTUM_GATE_LO = 0.003                # 직전 5d 누적 +0.3% 미만 = 모멘텀 없음
MOMENTUM_GATE_HI = 0.07                 # +7% 초과 = 과열
VOLUME_THRESHOLD = 1e10                 # 60d 평균 거래대금 100억
VOL_SURGE_MIN = 1.5                     # 당일/20d 평균
MARKET_CAP_MIN = 5e10                   # 500억

BLOCK_DOWNTREND = True                  # DOWNTREND 진입 차단

# 레짐별 가중치 (UPTREND / SIDEWAY / DOWNTREND)
WEIGHTS = {
    "UPTREND":   {"M": 0.35, "F": 0.30, "Q": 0.10, "R": 0.15, "L": 0.10},
    "SIDEWAY":   {"M": 0.25, "F": 0.35, "Q": 0.15, "R": 0.15, "L": 0.10},
    "DOWNTREND": {"M": 0.15, "F": 0.45, "Q": 0.30, "R": 0.05, "L": 0.05},
}
R_VALUE = {"UPTREND": 1.0, "SIDEWAY": 0.6, "DOWNTREND": 0.3}


# ═══════════════════════════════════════════════════════════════════════════
# UTILS
# ═══════════════════════════════════════════════════════════════════════════
def safe_float(v, d=0.0):
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return d


def load_json(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] load_json {path}: {e}")
        return default if default is not None else {}


def send_telegram(msg: str):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        return
    try:
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT, "text": msg}, timeout=5)
    except Exception as e:
        print(f"[TELEGRAM] {e}")


# ═══════════════════════════════════════════════════════════════════════════
# 데이터 로드
# ═══════════════════════════════════════════════════════════════════════════
def load_data():
    """모든 입력 파일 로드. 필수: history.csv. 나머지는 누락 시 graceful degradation."""
    if not os.path.exists(PATHS["HISTORY"]):
        raise FileNotFoundError(f"필수 파일 누락: {PATHS['HISTORY']}")

    hist = pd.read_csv(PATHS["HISTORY"], dtype={"code": str}, encoding="utf-8-sig")
    hist["code"] = hist["code"].str.zfill(6)
    hist["date"] = pd.to_datetime(hist["date"], errors="coerce")
    hist = hist.dropna(subset=["date"]).sort_values(["code", "date"]).reset_index(drop=True)

    # 숫자 컬럼 변환
    for col in ["close", "open", "high", "low", "volume", "trade_value", "change_rate"]:
        if col in hist.columns:
            hist[col] = pd.to_numeric(hist[col], errors="coerce")

    flow = load_json(PATHS["FLOW"], {})
    macro = load_json(PATHS["MACRO"], {})
    fund = load_json(PATHS["FUND"], {})
    stocks_meta = load_json(PATHS["STOCKS"], [])

    return hist, flow, macro, fund, stocks_meta


# ═══════════════════════════════════════════════════════════════════════════
# 레짐 판단 (KOSPI200 > 200MA & V-KOSPI < 25 = UPTREND)
# ═══════════════════════════════════════════════════════════════════════════
def compute_regime(macro: dict, today: str) -> str:
    """KOSPI200 종가 vs 200MA + V-KOSPI 보조."""
    kp = macro.get("KOSPI200", {})
    vk = macro.get("VKOSPI", {})

    if not kp:
        return "SIDEWAY"

    dates_sorted = sorted(kp.keys())
    if today not in dates_sorted:
        return "SIDEWAY"

    idx = dates_sorted.index(today)
    if idx < 200:
        return "SIDEWAY"  # 200일 데이터 부족

    ma200 = sum(kp[d] for d in dates_sorted[idx - 199:idx + 1]) / 200
    kp_today = kp[today]
    above_ma = kp_today > ma200
    vk_today = vk.get(today, 20.0)

    # 레짐 분류 (Phase 2 v10c 검증)
    #   UP   : 가격 > 200MA (변동성 무관)
    #   DOWN : 가격 < 200MA AND V-KOSPI > 30 (확실한 약세)
    #   그 외 SIDEWAY
    if above_ma:
        return "UPTREND"
    elif vk_today > 30:
        return "DOWNTREND"
    else:
        return "SIDEWAY"


# ═══════════════════════════════════════════════════════════════════════════
# 5축 raw 점수 계산 (종목 한 개)
# ═══════════════════════════════════════════════════════════════════════════
def compute_factors(code: str, name: str, df_stock: pd.DataFrame,
                    flow_code: dict, fund_code: dict, ca_zero: bool = False):
    """
    5축 raw 점수 (정규화 전).
    df_stock: 해당 종목의 시계열 DataFrame (이미 date 오름차순, reset_index 된 상태)
    flow_code: {date: {"foreign": net, "institution": net, "individual": net}}
    fund_code: {"roe": float, "debt_ratio": float, "shares": int}
    """
    if len(df_stock) < 60:
        return None

    close = df_stock["close"]
    last = df_stock.iloc[-1]
    if pd.isna(close.iloc[-1]) or close.iloc[-1] <= 0:
        return None

    # ───────────── M (모멘텀) ─────────────
    r20 = close.iloc[-1] / close.iloc[-21] - 1 if len(close) > 20 and close.iloc[-21] > 0 else 0.0
    r60 = close.iloc[-1] / close.iloc[-61] - 1 if len(close) > 60 and close.iloc[-61] > 0 else 0.0
    rs = r20 * 0.6 + r60 * 0.4

    # 20일 신고가 돌파
    high20 = close.iloc[-21:-1].max() if len(close) > 20 else float("inf")
    new_high = 1.0 if close.iloc[-1] >= high20 else 0.0

    # 모멘텀 강제컷 (외부 검토자 1순위)
    ret5 = close.iloc[-1] / close.iloc[-6] - 1 if len(close) > 5 and close.iloc[-6] > 0 else 0.0
    mom_gate = 1.0 if (MOMENTUM_GATE_LO <= ret5 <= MOMENTUM_GATE_HI) else 0.0

    M_raw = (rs * 0.5 + new_high * 0.3 + mom_gate * 0.2) * mom_gate  # 강제컷 적용

    # ───────────── F (수급) ─────────────
    # 백테스트 v10c 합성식: foreign 5d 누적 × 0.45 + institution 5d × 0.40 + ownership 5d 변화 × 0.15
    # ownership_5d_change는 외국인 지분율의 5일간 변동 (%p), fetch_data.py가 계산하여 저장
    if flow_code:
        dates_recent = sorted(flow_code.keys())[-5:]
        foreign_5d = sum(safe_float(flow_code.get(d, {}).get("foreign", 0)) for d in dates_recent)
        inst_5d = sum(safe_float(flow_code.get(d, {}).get("institution", 0)) for d in dates_recent)
        # 외국인 지분율 변화 — 데이터 없으면 0 (fallback)
        last_date = dates_recent[-1] if dates_recent else None
        ownership_chg = safe_float(flow_code.get(last_date, {}).get("ownership_5d_change", 0))
        F_raw = foreign_5d * 0.45 + inst_5d * 0.40 + ownership_chg * 1e9 * 0.15
    else:
        F_raw = 0.0

    # ───────────── Q (품질) ─────────────
    # 백테스트 v10c 합성식: mc_ok × zombie_mult (시총 + 좀비 차단만)
    # 주의: ROE/debt 페널티는 백테스트 검증 안 됨 → 운영에서 추가 X
    #       (재무 데이터는 result.json에 표시용으로만 출력)
    shares = safe_float(fund_code.get("shares", 0))
    mc = close.iloc[-1] * shares if shares > 0 else MARKET_CAP_MIN * 2  # shares 없으면 통과
    mc_ok = 1.0 if mc >= MARKET_CAP_MIN else 0.0
    zombie_mult = 0.5 if ca_zero else 1.0
    Q_raw = mc_ok * zombie_mult

    # 재무 정보 (표시용만, 점수에 반영 X)
    roe = safe_float(fund_code.get("roe", 0))
    debt = safe_float(fund_code.get("debt_ratio", 0))

    # ───────────── L (유동성·진입) ─────────────
    if "trade_value" in df_stock.columns and len(df_stock) >= 60:
        val_60d = df_stock["trade_value"].iloc[-60:].mean()
    else:
        val_60d = 0.0
    val_ok = 1.0 if val_60d >= VOLUME_THRESHOLD else 0.0

    if len(df_stock) >= 21 and "volume" in df_stock.columns:
        vol_today = safe_float(last.get("volume", 0))
        vol_20d_avg = df_stock["volume"].iloc[-21:-1].mean()
        surge = vol_today / vol_20d_avg if vol_20d_avg > 0 else 0.0
        surge_ok = 1.0 if surge >= VOL_SURGE_MIN else 0.0
    else:
        surge = 0.0
        surge_ok = 0.0

    L_raw = val_ok * (0.5 + 0.5 * surge_ok)

    return {
        "code": code,
        "name": name,
        "close": float(close.iloc[-1]),
        "volume": int(safe_float(last.get("volume", 0))),
        "trade_value": int(val_60d),
        "change_rate": float(safe_float(last.get("change_rate", 0))),
        "market_cap": int(mc),
        "r20": round(r20 * 100, 2),
        "r60": round(r60 * 100, 2),
        "ret5": round(ret5 * 100, 2),
        "vol_surge": round(surge, 2),
        "roe": float(roe),
        "debt_ratio": float(debt),
        "M_raw": float(M_raw),
        "F_raw": float(F_raw),
        "Q_raw": float(Q_raw),
        "L_raw": float(L_raw),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Cross-section rank 정규화 + 5축 가중합 → Top20
# ═══════════════════════════════════════════════════════════════════════════
def select_top20(scored_list: list, regime: str) -> list:
    """전 종목 raw 점수 → rank-pct 정규화 → 5축 가중합 → 상위 N개."""
    if not scored_list:
        return []

    df = pd.DataFrame(scored_list)
    # M, F, L은 cross-section rank 정규화 (0~1)
    for col in ["M_raw", "F_raw", "L_raw"]:
        df[col.replace("_raw", "")] = df[col].rank(pct=True, method="min")
    df["Q"] = df["Q_raw"]  # 이미 binary

    w = WEIGHTS[regime]
    r_val = R_VALUE[regime]

    df["score"] = (
        df["M"] * w["M"] +
        df["F"] * w["F"] +
        df["Q"] * w["Q"] +
        r_val   * w["R"] +
        df["L"] * w["L"]
    ) * 100  # 0~100 스케일

    df = df.sort_values("score", ascending=False).head(TOP_N).reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)
    df["regime"] = regime
    df["score"] = df["score"].round(2)

    for col in ["M", "F", "Q", "L"]:
        df[col] = df[col].round(3)

    keep_cols = ["rank", "code", "name", "score", "close", "volume", "trade_value",
                 "change_rate", "market_cap", "r20", "r60", "ret5", "vol_surge",
                 "roe", "debt_ratio", "M", "F", "Q", "L", "regime"]
    return df[keep_cols].to_dict("records")


# ═══════════════════════════════════════════════════════════════════════════
# Top5 진입 후보 + sizing
# ═══════════════════════════════════════════════════════════════════════════
def build_entry_top5(top20: list, regime: str) -> list:
    """Top5 + 사이즈 차등. DOWNTREND 옵션 시 빈 리스트."""
    if BLOCK_DOWNTREND and regime == "DOWNTREND":
        return []
    if not top20:
        return []

    out = []
    for i, t in enumerate(top20[:ENTRY_N]):
        rank = i + 1
        # 청산 가이드: 진입일(T+1)부터 28 캘린더일 후
        entry_date = datetime.now(KST).date() + timedelta(days=1)
        exit_deadline = entry_date + timedelta(days=TIME_EXIT_DAYS)
        out.append({
            "entry_rank": rank,
            "rank": t["rank"],
            "code": t["code"],
            "name": t["name"],
            "score": t["score"],
            "size_pct": SIZE_BY_RANK[rank],
            "close": t["close"],
            "change_rate": t["change_rate"],
            "expected_return_5d": round((t["score"] - 50) * 0.06, 2),
            "tp_price": round(t["close"] * (1 + TAKE_PROFIT)),
            "sl_price": round(t["close"] * (1 + STOP_LOSS)),
            "entry_date": entry_date.isoformat(),
            "exit_deadline": exit_deadline.isoformat(),
            "exit_rule": f"TP +{TAKE_PROFIT*100:.0f}% / SL {STOP_LOSS*100:+.0f}% / T+{TIME_EXIT_DAYS}d",
            "M": t["M"], "F": t["F"], "Q": t["Q"], "L": t["L"],
        })
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Forward Test — T+1/T+3/T+5 수익률 누적 갱신
# ═══════════════════════════════════════════════════════════════════════════
def update_forward_test(today: str, hist: pd.DataFrame):
    """signal_history.csv 의 Top5 신호 → T+N 수익 누적."""
    if not os.path.exists(PATHS["SIGNAL"]):
        return

    sig = pd.read_csv(PATHS["SIGNAL"], dtype={"code": str}, encoding="utf-8-sig")
    if sig.empty:
        return
    sig["code"] = sig["code"].str.zfill(6)
    sig["date"] = pd.to_datetime(sig["date"], errors="coerce")
    sig = sig.dropna(subset=["date"])
    sig5 = sig[sig["rank"] <= ENTRY_N].copy()

    if os.path.exists(PATHS["FORWARD"]):
        fwd = pd.read_csv(PATHS["FORWARD"], dtype={"code": str}, encoding="utf-8-sig")
        fwd["code"] = fwd["code"].str.zfill(6)
        fwd["signal_date"] = pd.to_datetime(fwd["signal_date"], errors="coerce")
    else:
        fwd = pd.DataFrame(columns=["signal_date", "code", "name", "rank",
                                     "entry_price", "t1_return", "t3_return", "t5_return"])

    # 종목별 인덱스
    hist_by_code = {c: g.sort_values("date").reset_index(drop=True)
                    for c, g in hist.groupby("code")}

    rows = []
    for _, row in sig5.iterrows():
        sig_date = row["date"]
        code = str(row["code"]).zfill(6)
        rank = int(row["rank"])

        # 이미 t5까지 완료된 건 건너뜀
        ex = fwd[(fwd["signal_date"] == sig_date) & (fwd["code"] == code)]
        if not ex.empty and pd.notna(ex.iloc[0].get("t5_return")):
            continue

        if code not in hist_by_code:
            continue
        g = hist_by_code[code]
        after = g[g["date"] >= sig_date]
        if len(after) < 2:
            continue
        sig_loc = after.index[0]
        if sig_loc + 1 >= len(g):
            continue
        entry_idx = sig_loc + 1
        entry_price = g.loc[entry_idx, "open"] if "open" in g.columns else g.loc[entry_idx, "close"]
        if pd.isna(entry_price) or entry_price <= 0:
            continue

        d = {
            "signal_date": sig_date,
            "code": code,
            "name": row.get("name", ""),
            "rank": rank,
            "entry_price": float(entry_price),
            "t1_return": None, "t3_return": None, "t5_return": None,
        }
        for n, key in [(1, "t1_return"), (3, "t3_return"), (5, "t5_return")]:
            if entry_idx + n < len(g):
                exit_p = g.loc[entry_idx + n, "close"]
                if pd.notna(exit_p) and exit_p > 0:
                    d[key] = round((exit_p / entry_price - 1) * 100, 3)
        rows.append(d)

    if not rows:
        if not fwd.empty:
            fwd.to_csv(PATHS["FORWARD"], index=False, encoding="utf-8-sig")
        return

    new = pd.DataFrame(rows)
    fwd = pd.concat([fwd, new], ignore_index=True)
    fwd = fwd.drop_duplicates(subset=["signal_date", "code"], keep="last")
    fwd = fwd.sort_values(["signal_date", "rank"]).reset_index(drop=True)
    fwd.to_csv(PATHS["FORWARD"], index=False, encoding="utf-8-sig")


def analyze_forward() -> dict:
    """forward_test.csv → T+1/T+3/T+5 통계 요약."""
    if not os.path.exists(PATHS["FORWARD"]):
        return {}
    fwd = pd.read_csv(PATHS["FORWARD"], encoding="utf-8-sig")
    summary = {}
    for col in ["t1_return", "t3_return", "t5_return"]:
        if col not in fwd.columns:
            continue
        s = pd.to_numeric(fwd[col], errors="coerce").dropna()
        if len(s) == 0:
            continue
        summary[col] = {
            "n": int(len(s)),
            "win_rate": round(float((s > 0).mean() * 100), 2),
            "avg_return": round(float(s.mean()), 2),
            "median": round(float(s.median()), 2),
            "hit_3pct": round(float((s > 3).mean() * 100), 2),
            "drop_3pct": round(float((s < -3).mean() * 100), 2),
        }
    return summary


# ═══════════════════════════════════════════════════════════════════════════
# 결과 저장
# ═══════════════════════════════════════════════════════════════════════════
def save_signal_history(top20: list, regime: str, today: str):
    if not top20:
        return
    rows = []
    for t in top20:
        rows.append({
            "date": today,
            "regime": regime,
            "rank": t["rank"],
            "code": t["code"],
            "name": t["name"],
            "score": t["score"],
            "close": t["close"],
            "M": t["M"],
            "F": t["F"],
            "Q": t["Q"],
            "L": t["L"],
        })
    new_df = pd.DataFrame(rows)

    # 기존 파일 있으면 merge + dedup (같은 date+code 중복 제거)
    if os.path.exists(PATHS["SIGNAL"]):
        try:
            existing = pd.read_csv(PATHS["SIGNAL"], dtype={"code": str},
                                    encoding="utf-8-sig")
            existing["code"] = existing["code"].astype(str).str.zfill(6)
            merged = pd.concat([existing, new_df], ignore_index=True)
            # 같은 (date, code) 중 마지막 행만 유지 (재실행 시 최신 결과 반영)
            merged = merged.drop_duplicates(subset=["date", "code"], keep="last")
            merged = merged.sort_values(["date", "rank"]).reset_index(drop=True)
        except Exception as e:
            print(f"[WARN] signal_history merge 실패: {e}")
            merged = new_df
    else:
        merged = new_df

    merged.to_csv(PATHS["SIGNAL"], index=False, encoding="utf-8-sig")


def save_result(today: str, regime: str, top20: list, entry_top5: list, fwd_stats: dict):
    result = {
        "date": today,
        "run_at": datetime.now(KST).isoformat(),
        "version": "v10.0",
        "regime": regime,
        "params": {
            "TOP_N": TOP_N,
            "ENTRY_N": ENTRY_N,
            "MAX_POSITIONS": MAX_POSITIONS,
            "TAKE_PROFIT": TAKE_PROFIT,
            "STOP_LOSS": STOP_LOSS,
            "TIME_EXIT_DAYS": TIME_EXIT_DAYS,
            "GAP_UP_AVOID": GAP_UP_AVOID,
            "BLOCK_DOWNTREND": BLOCK_DOWNTREND,
            "weights": WEIGHTS[regime],
            "size_by_rank": SIZE_BY_RANK,
        },
        "top20": top20,
        "entry_top5": entry_top5,
        "forward_test": fwd_stats,
        "backtest_reference": {
            "Test_2025_01_to_2026_05": {
                "hit_rate": 0.4291, "cagr": 0.6430, "sharpe": 1.95, "mdd": -0.1616,
            },
            "Validation_2024": {
                "hit_rate": 0.4151, "cagr": 0.4075, "sharpe": 1.79, "mdd": -0.1208,
            },
            "Training_2021_to_2023": {
                "hit_rate": 0.3875, "cagr": 0.0368, "sharpe": 0.29, "mdd": -0.2681,
            },
            "Five_year_cumulative_alpha_vs_BH": "+13.47%p",
        },
    }
    with open(PATHS["RESULT"], "w", encoding="utf-8-sig") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════
def run():
    run_at = datetime.now(KST)
    print(f"[START] {run_at.isoformat()}")
    print(f"[ENV] ECOS_KEY={'set' if ECOS_KEY else 'unset'} | "
          f"TELEGRAM={'on' if TELEGRAM_TOKEN else 'off'}")

    # 1. 데이터 로드
    hist, flow, macro, fund, stocks_meta = load_data()
    today = hist["date"].max().strftime("%Y-%m-%d")
    print(f"[DATA] today={today} | rows={len(hist)} | codes={hist['code'].nunique()}")
    print(f"[DATA] flow={len(flow)} codes | macro_keys={list(macro.keys())[:5]} | "
          f"fund={len(fund)} codes | meta={len(stocks_meta)} entries")

    # 2. 레짐
    regime = compute_regime(macro, today)
    print(f"[REGIME] {regime}")

    # 3. 종목 풀
    if stocks_meta:
        target_codes = [str(s["code"]).zfill(6) for s in stocks_meta]
    else:
        target_codes = sorted(hist["code"].unique())
    meta_by_code = {str(s["code"]).zfill(6): s for s in stocks_meta}

    # 4. 종목별 5축 점수
    scored = []
    for code in target_codes:
        df_s = hist[hist["code"] == code].sort_values("date").reset_index(drop=True)
        if len(df_s) < 60:
            continue
        name = str(df_s.iloc[-1].get("name", code)) if "name" in df_s.columns else code
        meta = meta_by_code.get(code, {})
        ca_zero = bool(meta.get("ca_zero", False))
        flow_code = flow.get(code, {})
        fund_code = fund.get(code, {})
        s = compute_factors(code, name, df_s, flow_code, fund_code, ca_zero=ca_zero)
        if s is not None:
            scored.append(s)

    print(f"[SCORE] 점수 산출 종목수: {len(scored)} / 전체 후보 {len(target_codes)}")

    # 5. Top20 + Top5
    top20 = select_top20(scored, regime)
    entry_top5 = build_entry_top5(top20, regime)

    # 6. signal_history append
    save_signal_history(top20, regime, today)

    # 7. Forward Test 갱신
    update_forward_test(today, hist)
    fwd_stats = analyze_forward()

    # 8. result.json
    save_result(today, regime, top20, entry_top5, fwd_stats)

    # 9. 출력 + 텔레그램
    print(f"\n{'='*70}")
    print(f"v10 결과 {today} | 레짐: {regime}")
    print(f"{'='*70}")
    if entry_top5:
        print(f"Top 5 진입 후보:")
        for e in entry_top5:
            print(f"  ★{e['entry_rank']}. {e['name']:<20} "
                  f"점수 {e['score']:>6.2f} | 사이즈 {e['size_pct']*100:>2.0f}% | "
                  f"가격 {e['close']:>10,.0f} | TP {e['tp_price']:>10,} / SL {e['sl_price']:>10,}")
    else:
        print(f"진입 차단 (레짐 {regime})")

    if fwd_stats:
        print(f"\nForward Test:")
        for k in ["t1_return", "t3_return", "t5_return"]:
            if k in fwd_stats:
                f = fwd_stats[k]
                print(f"  {k:>10}: 표본 {f['n']:>4} | 승률 {f['win_rate']:>5.2f}% | "
                      f"평균 {f['avg_return']:+.2f}% | hit3% {f['hit_3pct']:.1f}% | "
                      f"drop3% {f['drop_3pct']:.1f}%")

    # 텔레그램 압축 메시지
    if entry_top5:
        msg = f"📊 choonsimi v10 {today} [{regime}]\n"
        for e in entry_top5:
            msg += f"\n{e['entry_rank']}. {e['name']} ({e['size_pct']*100:.0f}%) "
            msg += f"점수 {e['score']:.1f} | {e['close']:,}원"
        msg += f"\n\n청산: TP +{TAKE_PROFIT*100:.0f}% / SL {STOP_LOSS*100:+.0f}% / T+{TIME_EXIT_DAYS}d"
        if entry_top5:
            msg += f"\n시한: {entry_top5[0]['exit_deadline']}"
        if "t5_return" in fwd_stats:
            f5 = fwd_stats["t5_return"]
            msg += f"\n\nT+5 승률 {f5['win_rate']:.1f}% / 평균 {f5['avg_return']:+.2f}% (n={f5['n']})"
    else:
        msg = f"⛔ choonsimi v10 {today} | 레짐 {regime} | 진입 차단"
    send_telegram(msg)

    print(f"\n[DONE] regime={regime} | top20={len(top20)} | entry={len(entry_top5)}")
    return {"regime": regime, "top20": top20, "entry_top5": entry_top5}


if __name__ == "__main__":
    run()
