"""
fetch_macro.py — choonsimi-premium 매크로 데이터 수집
═══════════════════════════════════════════════════════════════════════════
역할: 매일 매크로 지표 수집 → macro.json 생성/갱신

수집:
  KOSPI200, VKOSPI         — pykrx
  SOX, VIX, USDKRW         — FinanceDataReader (yahoo)
  BASE_RATE (기준금리)      — ECOS (선택, ECOS_KEY 필요)
  US10Y (미 10년물)         — FinanceDataReader

사용: python fetch_macro.py [--backfill 250]
═══════════════════════════════════════════════════════════════════════════
"""
import os, sys, json, time, argparse
from datetime import datetime, timezone, timedelta
import pandas as pd

try:
    from pykrx import stock as krx
    import FinanceDataReader as fdr
except ImportError:
    print("[FATAL] pykrx, FinanceDataReader 설치 필요")
    sys.exit(1)

import requests

KST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
MACRO_PATH = os.path.join(DATA_DIR, "macro.json")
ECOS_KEY = os.environ.get("ECOS_KEY", "")


def fetch_kospi200(start: str, end: str) -> dict:
    try:
        df = krx.get_index_ohlcv(start, end, "1028")  # KOSPI200 = 1028
        return {d.strftime("%Y-%m-%d"): float(c)
                for d, c in df["종가"].items() if not pd.isna(c)}
    except Exception as e:
        print(f"[ERR] KOSPI200: {e}")
        return {}


def fetch_vkospi(start: str, end: str) -> dict:
    try:
        df = krx.get_index_ohlcv(start, end, "1330", name_display=False)
        if df.empty:
            print("[ERR] VKOSPI: KRX 응답 empty (카테고리 불일치)")
            return {}
        return {d.strftime("%Y-%m-%d"): float(c)
                for d, c in df["종가"].items() if not pd.isna(c)}
    except Exception as e:
        print(f"[ERR] VKOSPI: {e}")
        return {}


def fetch_fdr(symbol: str, start: str, end: str) -> dict:
    """FDR — Yahoo Finance: ^SOX, ^VIX, USD/KRW=X, US10YT=X 등."""
    try:
        sd = f"{start[:4]}-{start[4:6]}-{start[6:]}"
        ed = f"{end[:4]}-{end[4:6]}-{end[6:]}"
        df = fdr.DataReader(symbol, sd, ed)
        if df.empty:
            return {}
        col = "Close" if "Close" in df.columns else df.columns[0]
        return {d.strftime("%Y-%m-%d"): float(v)
                for d, v in df[col].items() if not pd.isna(v)}
    except Exception as e:
        print(f"[ERR] FDR {symbol}: {e}")
        return {}


def fetch_ecos_base_rate(start: str, end: str) -> dict:
    """ECOS — 한국은행 기준금리 (월별)."""
    if not ECOS_KEY:
        return {}
    try:
        # 통계코드: 722Y001 기준금리, 0101000 항목
        url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{ECOS_KEY}/json/kr/1/1000/"
               f"722Y001/D/{start}/{end}/0101000")
        r = requests.get(url, timeout=10)
        data = r.json()
        rows = data.get("StatisticSearch", {}).get("row", [])
        out = {}
        for x in rows:
            d = x["TIME"]
            iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            out[iso] = float(x["DATA_VALUE"])
        return out
    except Exception as e:
        print(f"[ERR] ECOS: {e}")
        return {}


def merge_into_macro(existing: dict, key: str, new: dict, max_days: int = 600):
    """기존 macro[key]에 new 머지, 최근 max_days만 유지."""
    if key not in existing:
        existing[key] = {}
    existing[key].update(new)
    if len(existing[key]) > max_days:
        sorted_dates = sorted(existing[key].keys())
        for d in sorted_dates[:-max_days]:
            existing[key].pop(d, None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", type=int, default=None)
    args = parser.parse_args()

    print(f"[START fetch_macro] {datetime.now(KST).isoformat()}")

    today_kst = datetime.now(KST).date()
    end_dt = today_kst - timedelta(days=1)
    # 평일 회피 (공휴일은 한국 캘린더 라이브러리 없이 못 거름)
    while end_dt.weekday() >= 5:  # 5=토, 6=일
        end_dt -= timedelta(days=1)
    
    if args.backfill:
        start_dt = end_dt - timedelta(days=int(args.backfill * 1.5))
    else:
        start_dt = end_dt - timedelta(days=10)  # 일상 운영: 최근 10일만

    start = start_dt.strftime("%Y%m%d")
    end = end_dt.strftime("%Y%m%d")
    print(f"[RANGE] {start} ~ {end}")

    # 기존 macro.json 로드
    macro = {}
    if os.path.exists(MACRO_PATH):
        try:
            with open(MACRO_PATH, encoding="utf-8-sig") as f:
                macro = json.load(f)
        except Exception:
            pass

    # 수집
    print("[1/5] KOSPI200")
    merge_into_macro(macro, "KOSPI200", fetch_kospi200(start, end))
    time.sleep(0.2)

    print("[2/5] VKOSPI")
    merge_into_macro(macro, "VKOSPI", fetch_vkospi(start, end))
    time.sleep(0.2)

    print("[3/5] SOX (^SOX)")
    merge_into_macro(macro, "SOX", fetch_fdr("^SOX", start, end))

    print("[4/5] VIX (^VIX)")
    merge_into_macro(macro, "VIX", fetch_fdr("^VIX", start, end))

    print("[5/5] USDKRW (USD/KRW)")
    merge_into_macro(macro, "USDKRW", fetch_fdr("USD/KRW", start, end))

    if ECOS_KEY:
        print("[ECOS] BASE_RATE")
        merge_into_macro(macro, "BASE_RATE", fetch_ecos_base_rate(start, end))
    else:
        print("[ECOS] SKIP (ECOS_KEY 미설정)")

    # 저장
    with open(MACRO_PATH, "w", encoding="utf-8-sig") as f:
        json.dump(macro, f, ensure_ascii=False, indent=2)

    sizes = {k: len(v) for k, v in macro.items()}
    print(f"[DONE] macro.json keys: {sizes}")


if __name__ == "__main__":
    main()
