"""
fetch_data_extended.py — choonsimi-data-backup 200종목 일별 데이터 수집
═══════════════════════════════════════════════════════════════════════════
역할: pykrx로 KRX 데이터 수집 → 1개월 백업 운영

베이스: choonsimi-premium의 fetch_data.py
변경점 (3가지 보강):
  1. 거래대금 fallback (종가×거래량) — 인증 모드 컬럼 누락 대응
  2. 영업일 결정 — pykrx KOSPI 시세 마지막 날짜 사용 (macro.json 의존 제거)
  3. PATHS — _extended 접미사

생성/갱신 파일:
  data/history_extended.csv       — 200종목 일별 OHLCV + 거래대금 + 등락률
  data/market_flow_extended.json  — 종목별 외국인/기관/개인 일별 수급
  data/fundamental_extended.json  — 종목별 시가총액·발행주식수

사용법:
  python fetch_data_extended.py                  # 마지막 데이터 다음날부터 어제까지 추가
  python fetch_data_extended.py --backfill 60   # 60 영업일치 초기 수집
  python fetch_data_extended.py --date 20260520 # 특정 날짜만 수집

GitHub Actions 매일 08:30 KST 실행 (장 시작 30분 전, 어제 데이터 수집).

API 사용: pykrx (KRX 데이터, 무료, rate limit ~7 req/sec)
예상 실행시간:
  - 신규(60일 backfill): 200종목 × 3호출 × 0.15s ≈ 15분
  - 일일(1일치): 200종목 × 3호출 × 0.15s ≈ 5분 (호출 자체 시간 포함)
═══════════════════════════════════════════════════════════════════════════
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone, timedelta

import pandas as pd

try:
    from pykrx import stock as krx
except ImportError:
    print("[FATAL] pykrx 설치 필요: pip install pykrx")
    sys.exit(1)


KST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

PATHS = {
    "STOCKS":  os.path.join(DATA_DIR, "stocks_meta_extended.json"),
    "HISTORY": os.path.join(DATA_DIR, "history_extended.csv"),
    "FLOW":    os.path.join(DATA_DIR, "market_flow_extended.json"),
    "FUND":    os.path.join(DATA_DIR, "fundamental_extended.json"),
}

RATE_LIMIT_SEC = 0.15  # pykrx rate limit


# ═══════════════════════════════════════════════════════════════════════════
# pykrx KRX 로그인 (KRX_ID/KRX_PW 환경변수)
# ═══════════════════════════════════════════════════════════════════════════
def try_krx_login():
    krx_id = os.environ.get("KRX_ID", "")
    krx_pw = os.environ.get("KRX_PW", "")
    if not (krx_id and krx_pw):
        print("[AUTH] KRX_ID/KRX_PW 없음 — 무인증 모드 (일부 데이터 제한 가능)")
        return False
    print(f"[AUTH] KRX_ID/KRX_PW 환경변수 설정됨 — pykrx 자동 인식 시도")
    return True


# ═══════════════════════════════════════════════════════════════════════════
# 종목 메타 로드
# ═══════════════════════════════════════════════════════════════════════════
def load_stocks_meta():
    if not os.path.exists(PATHS["STOCKS"]):
        raise FileNotFoundError(
            f"필수 파일 없음: {PATHS['STOCKS']}\n"
            f"먼저 select_200_stocks.py를 실행해서 종목 메타를 생성하세요."
        )
    with open(PATHS["STOCKS"], encoding="utf-8") as f:
        meta = json.load(f)
    return meta


# ═══════════════════════════════════════════════════════════════════════════
# OHLCV 수집 — pykrx (거래대금 fallback 포함)
# ═══════════════════════════════════════════════════════════════════════════
def fetch_ohlcv_with_cap(code: str, start: str, end: str) -> tuple[pd.DataFrame, dict]:
    """
    OHLCV + 시가총액 동시 수집.
    거래대금 컬럼 누락 시 종가×거래량으로 직접 계산 (인증 모드 대응).
    반환: (OHLCV DataFrame, 최신 시총 dict)
    """
    df = pd.DataFrame()
    cap_info = {}

    # 1. OHLCV
    try:
        raw = krx.get_market_ohlcv(start, end, code)
        if raw is not None and not raw.empty:
            # 거래대금 컬럼 fallback (3중 안전망)
            # 1순위: 직접 컬럼 ('거래대금' or 'Amount')
            # 2순위: 종가 × 거래량 계산
            if "거래대금" in raw.columns:
                trdval = raw["거래대금"]
            elif "Amount" in raw.columns:
                trdval = raw["Amount"]
            else:
                # fallback: 종가 × 거래량
                close_col = "종가" if "종가" in raw.columns else ("Close" if "Close" in raw.columns else None)
                vol_col   = "거래량" if "거래량" in raw.columns else ("Volume" if "Volume" in raw.columns else None)
                if close_col and vol_col:
                    trdval = raw[close_col] * raw[vol_col]
                else:
                    trdval = pd.Series([0]*len(raw), index=raw.index)

            df = raw.reset_index()
            
            # 컬럼명 매핑 (한글/영문 둘 다 대응)
            rename_map = {
                "날짜": "date", "Date": "date",
                "시가": "open", "Open": "open",
                "고가": "high", "High": "high",
                "저가": "low",  "Low": "low",
                "종가": "close", "Close": "close",
                "거래량": "volume", "Volume": "volume",
                "등락률": "change_rate", "ChangeRate": "change_rate", "ChagesRatio": "change_rate",
            }
            df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
            df["trade_value"] = trdval.values
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            df["code"] = code
    except Exception as e:
        print(f"  [ERR] OHLCV {code}: {e}")

    # 2. 시가총액 + 상장주식수
    try:
        cap_df = krx.get_market_cap(end, end, code)
        if cap_df is not None and not cap_df.empty:
            r = cap_df.iloc[-1]
            # 한글/영문 컬럼 대응
            shares_val = r.get("상장주식수", r.get("Shares", 0))
            mcap_val   = r.get("시가총액",  r.get("Marcap", r.get("MarketCap", 0)))
            cap_info = {
                "shares":     int(shares_val or 0),
                "market_cap": int(mcap_val or 0),
            }
    except Exception as e:
        # 무인증 모드/일시 오류엔 빈 dict
        cap_info = {}

    return df, cap_info


# ═══════════════════════════════════════════════════════════════════════════
# 투자자별 수급 — pykrx
# ═══════════════════════════════════════════════════════════════════════════
def fetch_investor_flow(code: str, start: str, end: str) -> dict:
    """
    종목별 일별 외국인/기관/개인 순매수 (원).
    반환: {date_str: {foreign, institution, individual}}
    """
    try:
        df = krx.get_market_trading_value_by_date(start, end, code)
        if df is None or df.empty:
            return {}
        df = df.reset_index().rename(columns={"날짜": "date", "Date": "date"})
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        out = {}
        for _, r in df.iterrows():
            out[r["date"]] = {
                "foreign":     float(r.get("외국인합계", r.get("외국인", 0)) or 0),
                "institution": float(r.get("기관합계", 0) or 0),
                "individual":  float(r.get("개인", 0) or 0),
            }
        return out
    except Exception as e:
        print(f"  [ERR] Flow {code}: {e}")
        return {}


def compute_ownership_5d_change(code: str, end: str) -> float:
    """
    외국인 지분율 5거래일 변화 (%p).
    pykrx get_exhaustion_rates_of_foreign_investor 사용.
    """
    try:
        end_dt = datetime.strptime(end, "%Y%m%d")
        start_dt = end_dt - timedelta(days=15)  # 영업일 6일 확보 위해 캘린더 15일
        start = start_dt.strftime("%Y%m%d")
        df = krx.get_exhaustion_rates_of_foreign_investor(start, end, code)
        if df is None or df.empty or len(df) < 6:
            return 0.0
        # 컬럼명 한글/영문 대응
        col = "지분율" if "지분율" in df.columns else ("Rate" if "Rate" in df.columns else df.columns[-1])
        latest = float(df[col].iloc[-1])
        prior  = float(df[col].iloc[-6])
        return round(latest - prior, 4)
    except Exception:
        return 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 기존 history_extended.csv 읽기 + 마지막 날짜 확인
# ═══════════════════════════════════════════════════════════════════════════
def get_last_date_in_history() -> str | None:
    """history_extended.csv에서 가장 최근 date 반환 (YYYYMMDD), 없으면 None."""
    if not os.path.exists(PATHS["HISTORY"]):
        print(f"[DEBUG] history_extended.csv 없음")
        return None
    try:
        for enc in ["utf-8-sig", "utf-8", "cp949"]:
            try:
                df = pd.read_csv(PATHS["HISTORY"], dtype={"code": str}, encoding=enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            print("[DEBUG] history_extended.csv 인코딩 실패")
            return None

        if df.empty or "date" not in df.columns:
            print(f"[DEBUG] history_extended.csv 비었거나 date 컬럼 없음")
            return None

        last = pd.to_datetime(df["date"], errors="coerce").max()
        if pd.isna(last):
            return None
        result = last.strftime("%Y%m%d")
        print(f"[DEBUG] history_extended.csv 마지막 날짜: {result} ({len(df)}행)")
        return result
    except Exception as e:
        print(f"[ERR] last_date 읽기 실패: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# 영업일 결정 — KOSPI 지수 마지막 날짜 = KRX 영업일 (공휴일 자동 회피)
# ═══════════════════════════════════════════════════════════════════════════
def get_last_business_day_via_kospi() -> str | None:
    """
    pykrx로 KOSPI 지수 OHLCV 받아서 마지막 거래일 = KRX 영업일.
    공휴일·임시휴장 모두 자동 반영.
    반환: 'YYYYMMDD' 또는 None.
    """
    try:
        today = datetime.now(KST).strftime("%Y%m%d")
        start = (datetime.now(KST) - timedelta(days=10)).strftime("%Y%m%d")
        # KOSPI 지수 코드 = 1001
        df = krx.get_index_ohlcv_by_date(start, today, "1001")
        if df is not None and not df.empty:
            last_date = df.index[-1]
            result = pd.to_datetime(last_date).strftime("%Y%m%d")
            return result
    except Exception as e:
        print(f"[DEBUG] KOSPI 지수 조회 실패: {e}")
    return None


def get_last_business_day_fallback() -> str:
    """fallback: 어제부터 거슬러 평일 찾기 (공휴일 미반영)."""
    end_dt = datetime.now(KST).date() - timedelta(days=1)
    while end_dt.weekday() >= 5:  # 5=토, 6=일
        end_dt -= timedelta(days=1)
    return end_dt.strftime("%Y%m%d")


def get_smart_end_date() -> str:
    """영업일 결정 (KRX 공휴일 회피)."""
    result = get_last_business_day_via_kospi()
    if result:
        print(f"[CAL] 영업일 (KOSPI 지수 기준): {result}")
        return result
    result = get_last_business_day_fallback()
    print(f"[CAL] 영업일 (평일 fallback): {result}")
    return result


# ═══════════════════════════════════════════════════════════════════════════
# 파일 갱신 함수들
# ═══════════════════════════════════════════════════════════════════════════
def append_history(new_df: pd.DataFrame):
    """history_extended.csv에 신규 데이터 append, 중복 제거."""
    if new_df.empty:
        return
    cols = ["date", "code", "name", "open", "high", "low", "close",
            "volume", "trade_value", "change_rate"]
    new_df = new_df[[c for c in cols if c in new_df.columns]]

    if os.path.exists(PATHS["HISTORY"]):
        old = pd.read_csv(PATHS["HISTORY"], dtype={"code": str}, encoding="utf-8-sig")
        merged = pd.concat([old, new_df], ignore_index=True)
        merged = merged.drop_duplicates(subset=["date", "code"], keep="last")
        merged = merged.sort_values(["code", "date"]).reset_index(drop=True)
    else:
        merged = new_df.sort_values(["code", "date"]).reset_index(drop=True)

    merged.to_csv(PATHS["HISTORY"], index=False, encoding="utf-8-sig")
    print(f"  → history_extended.csv: {len(merged)}행 (신규 {len(new_df)}행)")


def update_market_flow(updates: dict):
    """market_flow_extended.json 갱신 (종목별 dict merge, 최대 90일 유지)."""
    existing = {}
    if os.path.exists(PATHS["FLOW"]):
        try:
            with open(PATHS["FLOW"], encoding="utf-8-sig") as f:
                existing = json.load(f)
        except Exception:
            pass

    for code, code_flow in updates.items():
        if code not in existing:
            existing[code] = {}
        existing[code].update(code_flow)
        # 최대 90일 유지 (1개월 운영 + 여유분)
        sorted_dates = sorted(existing[code].keys())
        if len(sorted_dates) > 90:
            for d in sorted_dates[:-90]:
                existing[code].pop(d, None)

    with open(PATHS["FLOW"], "w", encoding="utf-8-sig") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    print(f"  → market_flow_extended.json: {len(existing)}종목")


def update_fundamental(updates: dict):
    """fundamental_extended.json 갱신."""
    existing = {}
    if os.path.exists(PATHS["FUND"]):
        try:
            with open(PATHS["FUND"], encoding="utf-8-sig") as f:
                existing = json.load(f)
        except Exception:
            pass

    for code, info in updates.items():
        if code not in existing:
            existing[code] = {}
        existing[code].update(info)

    with open(PATHS["FUND"], "w", encoding="utf-8-sig") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    print(f"  → fundamental_extended.json: {len(existing)}종목")


# ═══════════════════════════════════════════════════════════════════════════
# 수집 범위 결정
# ═══════════════════════════════════════════════════════════════════════════
def determine_date_range(args, last_date: str | None) -> tuple[str | None, str | None]:
    """수집 범위 결정 (YYYYMMDD, YYYYMMDD). 공휴일/주말 자동 회피."""
    end_yyyymmdd = get_smart_end_date()
    end_dt = datetime.strptime(end_yyyymmdd, "%Y%m%d").date()

    if args.date:
        return args.date, args.date

    if args.backfill:
        # backfill N 영업일 ≈ 캘린더 N * 1.5
        start_dt = end_dt - timedelta(days=int(args.backfill * 1.5))
        return start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d")

    if last_date:
        ld = datetime.strptime(last_date, "%Y%m%d").date()
        start_dt = ld + timedelta(days=1)
        if start_dt > end_dt:
            return None, None  # 이미 최신
        return start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d")

    # 신규 — backfill 60일 기본
    start_dt = end_dt - timedelta(days=90)
    return start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d")


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", type=int, default=None,
                        help="N 영업일치 초기 수집")
    parser.add_argument("--date", type=str, default=None,
                        help="특정 날짜만 (YYYYMMDD)")
    args = parser.parse_args()

    print(f"[START fetch_data_extended] {datetime.now(KST).isoformat()}")
    print("=" * 70)

    # KRX 로그인 시도
    try_krx_login()

    # 1. 종목 메타
    stocks_meta = load_stocks_meta()
    print(f"[META] {len(stocks_meta)}종목 대상")

    # 2. 수집 범위
    last = get_last_date_in_history()
    start, end = determine_date_range(args, last)
    if start is None:
        print(f"[SKIP] 이미 최신 데이터 (last={last}). 수집 불필요.")
        return
    print(f"[RANGE] {start} ~ {end} (last_in_csv={last})")
    print("=" * 70)

    # 3. 종목별 수집
    all_ohlcv = []
    all_flow = {}
    all_fund = {}
    fail_count = 0
    for i, m in enumerate(stocks_meta, 1):
        code, name = m["code"], m["name"]
        print(f"[{i:>3}/{len(stocks_meta)}] {code} {name}")

        # OHLCV + 시가총액
        df_o, cap_info = fetch_ohlcv_with_cap(code, start, end)
        if not df_o.empty:
            df_o["name"] = name
            all_ohlcv.append(df_o)
        else:
            fail_count += 1
        if cap_info:
            all_fund[code] = cap_info
        time.sleep(RATE_LIMIT_SEC)

        # Investor Flow
        flow = fetch_investor_flow(code, start, end)
        if flow:
            # 외국인 지분율 5d 변화 (가장 최근 날짜만)
            own_chg = compute_ownership_5d_change(code, end)
            last_d = max(flow.keys())
            flow[last_d]["ownership_5d_change"] = own_chg
            time.sleep(RATE_LIMIT_SEC)
            all_flow[code] = flow
        time.sleep(RATE_LIMIT_SEC)

    print("=" * 70)
    print(f"[SUMMARY] OHLCV 성공 {len(all_ohlcv)}, 실패 {fail_count} / 총 {len(stocks_meta)}")

    # 4. 파일 갱신
    if all_ohlcv:
        merged_ohlcv = pd.concat(all_ohlcv, ignore_index=True)
        append_history(merged_ohlcv)
    if all_flow:
        update_market_flow(all_flow)
    if all_fund:
        update_fundamental(all_fund)

    print(f"[DONE fetch_data_extended] {datetime.now(KST).isoformat()}")


if __name__ == "__main__":
    main()
