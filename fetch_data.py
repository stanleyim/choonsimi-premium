"""
fetch_data.py — choonsimi-premium 일별 종목 데이터 수집
═══════════════════════════════════════════════════════════════════════════
역할: pykrx로 KRX 데이터 수집 → engine.py가 read할 파일 생성/갱신

생성/갱신 파일:
  data/history.csv       — 종목별 일별 OHLCV + 거래대금 + 등락률
  data/market_flow.json  — 종목별 외국인/기관/개인 일별 수급
  data/fundamental.json  — 종목별 시가총액·발행주식수 (시총 계산용)

사용법:
  python fetch_data.py                  # 마지막 데이터 다음날부터 어제까지 추가
  python fetch_data.py --backfill 250   # 250 영업일치 초기 수집
  python fetch_data.py --date 20260520  # 특정 날짜만 수집

GitHub Actions 매일 08:30 KST 실행 (장 시작 30분 전, 어제 데이터 수집).

API 사용: pykrx (KRX 데이터, 무료, rate limit ~10 req/sec)
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
    "STOCKS":   os.path.join(DATA_DIR, "stocks_meta.json"),
    "HISTORY":  os.path.join(DATA_DIR, "history.csv"),
    "FLOW":     os.path.join(DATA_DIR, "market_flow.json"),
    "FUND":     os.path.join(DATA_DIR, "fundamental.json"),
}

RATE_LIMIT_SEC = 0.15  # pykrx rate limit


# ═══════════════════════════════════════════════════════════════════════════
# pykrx 인증 시도 (KRX_ID/KRX_PW가 있으면 — 일부 API에 필요)
# ═══════════════════════════════════════════════════════════════════════════
def try_krx_login():
    krx_id = os.environ.get("KRX_ID", "")
    krx_pw = os.environ.get("KRX_PW", "")
    if not (krx_id and krx_pw):
        print("[AUTH] KRX_ID/KRX_PW 없음 — 무인증 모드 (일부 데이터 제한 가능)")
        return False
    # pykrx에 로그인 인터페이스가 있다면 시도
    try:
        from pykrx.website import krx as krx_web
        if hasattr(krx_web, "login"):
            krx_web.login(krx_id, krx_pw)
            print("[AUTH] pykrx KRX 로그인 성공")
            return True
    except Exception as e:
        print(f"[AUTH] pykrx login 시도 실패: {e}")
    # 환경변수만 설정 — 일부 fork는 자동 인식
    print("[AUTH] KRX_ID/KRX_PW 환경변수 설정됨 — pykrx가 자동 인식 시도")
    return True


# ═══════════════════════════════════════════════════════════════════════════
# 종목 메타 로드
# ═══════════════════════════════════════════════════════════════════════════
def load_stocks_meta():
    if not os.path.exists(PATHS["STOCKS"]):
        raise FileNotFoundError(f"필수 파일: {PATHS['STOCKS']}")
    with open(PATHS["STOCKS"], encoding="utf-8") as f:
        meta = json.load(f)
    return meta


# ═══════════════════════════════════════════════════════════════════════════
# OHLCV 수집 — pykrx
# ═══════════════════════════════════════════════════════════════════════════
def fetch_ohlcv_with_cap(code: str, start: str, end: str) -> tuple[pd.DataFrame, dict]:
    """
    OHLCV + 시가총액 동시 수집.
    pykrx get_market_ohlcv는 시총 미포함이라 별도 get_market_cap 시도.
    반환: (OHLCV DataFrame, 최신 시총 dict)
    """
    df = pd.DataFrame()
    cap_info = {}

    # 1. OHLCV (무인증)
    try:
        df = krx.get_market_ohlcv(start, end, code)
        if not df.empty:
            df = df.reset_index().rename(columns={
                "날짜": "date", "시가": "open", "고가": "high",
                "저가": "low", "종가": "close",
                "거래량": "volume", "거래대금": "trade_value",
                "등락률": "change_rate",
            })
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            df["code"] = code
    except Exception as e:
        print(f"  [ERR] OHLCV {code}: {e}")

    # 2. 시가총액 + 상장주식수 (인증 필요할 수 있음)
    try:
        cap_df = krx.get_market_cap(end, end, code)
        if not cap_df.empty:
            r = cap_df.iloc[-1]
            cap_info = {
                "shares": int(r.get("상장주식수", 0)),
                "market_cap": int(r.get("시가총액", 0)),
            }
    except Exception as e:
        # 무인증 모드에선 실패 가능 — 종가 × 평균 shares로 대체
        cap_info = {}

    return df, cap_info


def fetch_ohlcv(code: str, start: str, end: str) -> pd.DataFrame:
    """OHLCV만 (기존 호환)."""
    df, _ = fetch_ohlcv_with_cap(code, start, end)
    return df


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
        if df.empty:
            return {}
        df = df.reset_index().rename(columns={"날짜": "date"})
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        out = {}
        for _, r in df.iterrows():
            out[r["date"]] = {
                "foreign": float(r.get("외국인합계", r.get("외국인", 0)) or 0),
                "institution": float(r.get("기관합계", 0) or 0),
                "individual": float(r.get("개인", 0) or 0),
            }
        return out
    except Exception as e:
        print(f"  [ERR] Flow {code}: {e}")
        return {}


def compute_ownership_5d_change(code: str, end: str) -> float:
    """
    외국인 지분율 5거래일 변화 (%p).
    pykrx get_exhaustion_rates_of_foreign_investor 또는 직접 보유율 조회.
    """
    try:
        # 6 영업일 전부터 end까지 외국인 보유율
        end_dt = datetime.strptime(end, "%Y%m%d")
        start_dt = end_dt - timedelta(days=15)  # 영업일 6일 확보 위해 캘린더 15일
        start = start_dt.strftime("%Y%m%d")
        df = krx.get_exhaustion_rates_of_foreign_investor(start, end, code)
        if df is None or df.empty or len(df) < 6:
            return 0.0
        latest = float(df["지분율"].iloc[-1])
        prior = float(df["지분율"].iloc[-6])
        return latest - prior
    except Exception:
        return 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 시가총액 / 발행주식수 — pykrx
# ═══════════════════════════════════════════════════════════════════════════
def fetch_market_cap(code: str, date: str) -> dict:
    """
    시가총액·발행주식수 (특정 날짜).
    date: 'YYYYMMDD'
    반환: {shares, market_cap}
    """
    try:
        df = krx.get_market_cap(date, date, code)
        if df.empty:
            return {}
        r = df.iloc[-1]
        return {
            "shares": int(r.get("상장주식수", 0)),
            "market_cap": int(r.get("시가총액", 0)),
        }
    except Exception as e:
        print(f"  [ERR] MarketCap {code}: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════════════════
# 기존 history.csv read + 마지막 날짜 확인
# ═══════════════════════════════════════════════════════════════════════════
def get_last_date_in_history() -> str | None:
    """history.csv에서 가장 최근 date 반환 (YYYYMMDD), 없으면 None."""
    if not os.path.exists(PATHS["HISTORY"]):
        print(f"[DEBUG] history.csv 없음: {PATHS['HISTORY']}")
        return None
    try:
        # 다양한 인코딩 시도
        for enc in ["utf-8-sig", "utf-8", "cp949"]:
            try:
                df = pd.read_csv(PATHS["HISTORY"], dtype={"code": str}, encoding=enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            print("[DEBUG] history.csv 인코딩 모두 실패")
            return None
        
        if df.empty or "date" not in df.columns:
            print(f"[DEBUG] history.csv 비었거나 date 컬럼 없음. 컬럼: {list(df.columns)}")
            return None
        
        last = pd.to_datetime(df["date"], errors="coerce").max()
        if pd.isna(last):
            print("[DEBUG] history.csv date 파싱 실패")
            return None
        
        result = last.strftime("%Y%m%d")
        print(f"[DEBUG] history.csv 마지막 날짜: {result} ({len(df)}행)")
        return result
    except Exception as e:
        print(f"[ERR] last_date 읽기 실패: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# 영업일 결정 (공휴일/주말 자동 회피)
# ═══════════════════════════════════════════════════════════════════════════
def get_last_business_day_from_macro() -> str | None:
    """
    macro.json의 KOSPI200 마지막 날짜 = KRX 공식 영업일 (가장 정확).
    반환: 'YYYYMMDD' 또는 None.
    """
    macro_path = os.path.join(DATA_DIR, "macro.json")
    if not os.path.exists(macro_path):
        return None
    try:
        with open(macro_path, encoding="utf-8-sig") as f:
            macro = json.load(f)
        # KOSPI200은 KRX 영업일에만 갱신됨
        kospi_dates = sorted(macro.get("KOSPI200", {}).keys())
        if kospi_dates:
            iso_date = kospi_dates[-1]   # 'YYYY-MM-DD'
            return iso_date.replace("-", "")  # 'YYYYMMDD'
    except Exception as e:
        print(f"[DEBUG] macro.json 읽기 실패: {e}")
    return None


def get_last_business_day_fallback() -> str:
    """
    fallback: 어제부터 거슬러 평일 찾기 (공휴일은 못 거름).
    반환: 'YYYYMMDD'.
    """
    end_dt = datetime.now(KST).date() - timedelta(days=1)
    while end_dt.weekday() >= 5:  # 5=토, 6=일
        end_dt -= timedelta(days=1)
    return end_dt.strftime("%Y%m%d")


def get_smart_end_date() -> str:
    """
    영업일 결정 우선순위:
    1. macro.json의 KOSPI200 마지막 날짜 (KRX 공휴일 자동 회피)
    2. 어제부터 거슬러 평일 (fallback, 공휴일 미반영)
    """
    result = get_last_business_day_from_macro()
    if result:
        print(f"[CAL] 영업일 (macro.json 기준): {result}")
        return result
    result = get_last_business_day_fallback()
    print(f"[CAL] 영업일 (평일 fallback): {result}")
    return result


def append_history(new_df: pd.DataFrame):
    """history.csv에 신규 데이터 append, 중복 제거."""
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
    print(f"  → history.csv: {len(merged)}행 (신규 {len(new_df)}행)")


def update_market_flow(updates: dict):
    """market_flow.json 갱신 (종목별 dict merge)."""
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
        # 최대 60일 유지 (메모리 절약)
        sorted_dates = sorted(existing[code].keys())
        if len(sorted_dates) > 60:
            for d in sorted_dates[:-60]:
                existing[code].pop(d, None)

    with open(PATHS["FLOW"], "w", encoding="utf-8-sig") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    print(f"  → market_flow.json: {len(existing)}종목")


def update_fundamental(updates: dict):
    """fundamental.json 갱신."""
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
    print(f"  → fundamental.json: {len(existing)}종목")


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════
def determine_date_range(args, last_date: str | None) -> tuple[str, str]:
    """수집 범위 결정 (YYYYMMDD, YYYYMMDD). 공휴일/주말 자동 회피."""
    # 영업일 기준 end_dt (공휴일·주말 회피)
    end_yyyymmdd = get_smart_end_date()
    end_dt = datetime.strptime(end_yyyymmdd, "%Y%m%d").date()

    if args.date:
        return args.date, args.date

    if args.backfill:
        # backfill N 영업일 ≈ 캘린더 N * 1.5
        start_dt = end_dt - timedelta(days=int(args.backfill * 1.5))
        return start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d")

    if last_date:
        # 마지막 날짜 다음날부터 영업일까지
        ld = datetime.strptime(last_date, "%Y%m%d").date()
        start_dt = ld + timedelta(days=1)
        if start_dt > end_dt:
            return None, None  # 이미 최신
        return start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d")

    # 신규 (history.csv 없음) — backfill 60일 기본
    start_dt = end_dt - timedelta(days=90)
    return start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", type=int, default=None,
                        help="N 영업일치 초기 수집")
    parser.add_argument("--date", type=str, default=None,
                        help="특정 날짜만 (YYYYMMDD)")
    args = parser.parse_args()

    print(f"[START fetch_data] {datetime.now(KST).isoformat()}")
    
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

    # 3. 종목별 수집
    all_ohlcv = []
    all_flow = {}
    all_fund = {}
    for i, m in enumerate(stocks_meta, 1):
        code, name = m["code"], m["name"]
        print(f"[{i:>2}/{len(stocks_meta)}] {code} {name}")

        # OHLCV + 시가총액 (한 번에)
        df_o, cap_info = fetch_ohlcv_with_cap(code, start, end)
        if not df_o.empty:
            df_o["name"] = name
            all_ohlcv.append(df_o)
        if cap_info:
            all_fund[code] = cap_info
        time.sleep(RATE_LIMIT_SEC)

        # Investor Flow (KRX 로그인 필요할 수 있음)
        flow = fetch_investor_flow(code, start, end)
        if flow:
            # ownership 5d change 추가 (가장 최근 날짜만)
            if end:
                own_chg = compute_ownership_5d_change(code, end)
                last_d = max(flow.keys())
                flow[last_d]["ownership_5d_change"] = own_chg
                time.sleep(RATE_LIMIT_SEC)
            all_flow[code] = flow
        time.sleep(RATE_LIMIT_SEC)

    # 4. 파일 갱신
    if all_ohlcv:
        merged_ohlcv = pd.concat(all_ohlcv, ignore_index=True)
        append_history(merged_ohlcv)
    if all_flow:
        update_market_flow(all_flow)
    if all_fund:
        update_fundamental(all_fund)

    print(f"[DONE fetch_data] {datetime.now(KST).isoformat()}")


if __name__ == "__main__":
    main()
