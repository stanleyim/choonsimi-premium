"""
fetch_dart.py — choonsimi-premium 분기 재무 데이터 수집
═══════════════════════════════════════════════════════════════════════════
역할: DART API → 종목별 ROE / debt_ratio / 영업이익 / 순이익 / 자본
출력: data/fundamental.json (engine.py가 표시용으로 read, 점수 영향 X)

choonsimi v2.5.0 기반, premium 구조로 변경:
  ✔ 경로: data/ 디렉터리 사용
  ✔ 종목 풀: data/stocks_meta.json (55종목 한정)
  ✔ 출력 구조: list → dict (code key) + _meta (engine.py 호환)
  ✔ merge 방식: fetch_data.py의 shares와 충돌 없이 merge

스킵 로직: 오늘 이미 수집되면 즉시 종료
fallback: 최신 분기 → 직전 분기 → 그 전 분기 순서로 시도

환경변수: DART_API_KEY (필수)
═══════════════════════════════════════════════════════════════════════════
"""

import os
import sys
import json
import time
import zipfile
import io
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


DART_BASE = "https://opendart.fss.or.kr/api"
KST = timezone(timedelta(hours=9))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

PATHS = {
    "STOCKS":     os.path.join(DATA_DIR, "stocks_meta.json"),
    "FUND":       os.path.join(DATA_DIR, "fundamental.json"),
    "CORP_CACHE": os.path.join(DATA_DIR, "corp_map_cache.json"),
}

SLEEP_SEC = 0.10
TIMEOUT = 15
WORKERS = 3

REPRT_MAP = {
    "1Q": "11013", "2Q": "11012",
    "3Q": "11014", "4Q": "11011",
}


def get_fallback_quarters() -> list:
    """현재 시점 기준 데이터 받을 분기 fallback 순서."""
    now = datetime.now(KST)
    y, m = now.year, now.month
    if m <= 3:
        base = [(y-1, "4Q"), (y-1, "3Q")]
    elif m <= 6:
        base = [(y, "1Q"), (y-1, "4Q")]
    elif m <= 9:
        base = [(y, "2Q"), (y, "1Q")]
    else:
        base = [(y, "3Q"), (y, "2Q")]
    return base


def to_int(v) -> int:
    try:
        return int(str(v or "0").replace(",", "").strip() or "0")
    except Exception:
        return 0


def load_stocks_meta() -> dict:
    """stocks_meta.json → {code: name}."""
    if not os.path.exists(PATHS["STOCKS"]):
        raise FileNotFoundError(f"필수 파일: {PATHS['STOCKS']}")
    with open(PATHS["STOCKS"], encoding="utf-8-sig") as f:
        meta = json.load(f)
    return {str(m["code"]).zfill(6): m["name"] for m in meta}


def get_dart_key() -> str:
    key = os.environ.get("DART_API_KEY", "")
    if not key:
        raise RuntimeError("DART_API_KEY 환경변수 필요")
    return key


def get_corp_codes(key: str) -> dict:
    """DART corp_code 매핑 (캐시 사용)."""
    if os.path.exists(PATHS["CORP_CACHE"]):
        try:
            with open(PATHS["CORP_CACHE"], "r", encoding="utf-8-sig") as f:
                cached = json.load(f)
            if len(cached) > 100:
                print(f"[CACHE] corp_code: {len(cached)}종목")
                return cached
        except Exception:
            pass

    try:
        res = requests.get(
            f"{DART_BASE}/corpCode.xml",
            params={"crtfc_key": key}, timeout=30,
        )
        res.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(res.content))
        root = ET.fromstring(zf.read("CORPCODE.xml"))
        corp_map = {}
        for item in root.findall("list"):
            sc = item.findtext("stock_code", "").strip()
            cc = item.findtext("corp_code", "").strip()
            if sc and len(sc) == 6:
                corp_map[sc] = cc
        with open(PATHS["CORP_CACHE"], "w", encoding="utf-8-sig") as f:
            json.dump(corp_map, f, ensure_ascii=False)
        print(f"[FETCH] corp_code: {len(corp_map)}종목")
        return corp_map
    except Exception as e:
        print(f"[ERR] corp_code 다운로드 실패: {e}")
        return {}


def fetch_financial_one(key, corp_code, stock_code, year, reprt_code) -> dict:
    """단일 분기 재무 한 종목."""
    result = {"code": stock_code}
    try:
        data = None
        for fs_div in ["CFS", "OFS"]:
            res = requests.get(
                f"{DART_BASE}/fnlttSinglAcntAll.json",
                params={
                    "crtfc_key":  key,
                    "corp_code":  corp_code,
                    "bsns_year":  str(year),
                    "reprt_code": reprt_code,
                    "fs_div":     fs_div,
                },
                timeout=TIMEOUT,
            )
            try:
                d = res.json()
            except Exception:
                continue
            if d.get("status") == "000" and d.get("list"):
                data = d
                break

        if not data:
            return {}

        items = data.get("list", [])
        found = {"equity": None, "total_debt": None, "net_income": None,
                 "op_profit": None, "op_profit_prev": None}

        for item in items:
            acct = item.get("account_nm", "").strip()
            cur = to_int(item.get("thstrm_amount"))
            prev = to_int(item.get("frmtrm_amount"))
            if found["equity"] is None and acct in ["자본총계", "자본 합계"]:
                found["equity"] = cur
            if found["total_debt"] is None and acct in ["부채총계", "부채 합계"]:
                found["total_debt"] = cur
            if found["net_income"] is None and acct in [
                "당기순이익(손실)", "당기순이익", "분기순이익(손실)", "분기순이익"]:
                found["net_income"] = cur
            if found["op_profit"] is None and acct in ["영업이익(손실)", "영업이익"]:
                found["op_profit"] = cur
                found["op_profit_prev"] = prev

        eq, ni, td = found["equity"], found["net_income"], found["total_debt"]
        op, opp = found["op_profit"], found["op_profit_prev"]

        if eq is not None:
            result["equity"] = eq
        if td is not None:
            result["total_debt"] = td
        if ni is not None:
            result["net_income"] = ni

        result["op_growth"] = (round((op - opp) / abs(opp) * 100, 2)
                                if op and opp and abs(opp) > 0 else 0)

        if eq and abs(eq) > 0 and ni is not None:
            result["roe"] = round(ni / eq * 100, 2)
        if eq and abs(eq) > 0 and td is not None:
            result["debt_ratio"] = round(td / eq * 100, 2)

        return result if len(result) > 1 else {}
    except Exception:
        return {}


def fetch_financial_with_fallback(key, corp_code, stock_code, name_map) -> dict:
    """fallback 분기 순서로 시도."""
    quarters = get_fallback_quarters()
    for year, quarter in quarters:
        reprt_code = REPRT_MAP[quarter]
        data = fetch_financial_one(key, corp_code, stock_code, year, reprt_code)
        if data:
            data["year"] = year
            data["quarter"] = quarter
            data["reprt_code"] = reprt_code
            data["name"] = name_map.get(stock_code, "")
            data["dart_updated"] = datetime.now(KST).strftime("%Y-%m-%d")
            return data
        time.sleep(SLEEP_SEC)
    return {}


def load_existing_fund() -> dict:
    """기존 fundamental.json (fetch_data.py가 채운 shares 등 보존)."""
    if not os.path.exists(PATHS["FUND"]):
        return {}
    try:
        with open(PATHS["FUND"], encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def save_merged_fund(existing: dict, updates: dict, meta: dict):
    """
    existing: 기존 fund dict (shares 등 보존)
    updates: 새 DART 데이터 {code: {...}}
    meta: _meta 키에 저장할 메타 정보
    """
    # 종목별 merge (DART 키만 update — shares 등 다른 키 보존)
    for code, dart_data in updates.items():
        if code not in existing:
            existing[code] = {}
        # _meta는 top-level 별도 키이므로 종목 데이터에 영향 없음
        existing[code].update(dart_data)

    existing["_meta"] = meta

    with open(PATHS["FUND"], "w", encoding="utf-8-sig") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


def run():
    print(f"[START fetch_dart] {datetime.now(KST).isoformat()}")

    try:
        key = get_dart_key()
    except Exception as e:
        print(f"[FATAL] {e}")
        sys.exit(0)  # 0으로 종료 (workflow 진행 보장)

    today = datetime.now(KST).strftime("%Y-%m-%d")
    quarters = get_fallback_quarters()
    year, quarter = quarters[0]

    # 스킵 로직: 오늘 _meta.date 일치하면 종료
    existing = load_existing_fund()
    meta_existing = existing.get("_meta", {})
    if meta_existing.get("date") == today and meta_existing.get("count", 0) >= 10:
        print(f"[SKIP] 오늘 ({today}) 이미 수집됨 ({meta_existing['count']}종목)")
        return

    # 종목 풀
    try:
        name_map = load_stocks_meta()
        target = list(name_map.keys())
        print(f"[META] {len(target)}종목 대상")
    except Exception as e:
        print(f"[ERR] stocks_meta 로드 실패: {e}")
        return

    if not target:
        print("[ERR] 대상 종목 없음")
        return

    # corp_code 매핑
    corp_map = get_corp_codes(key)
    if not corp_map:
        print("[ERR] corp_map 비어있음")
        return

    valid = [(c, corp_map[c]) for c in target if c in corp_map]
    missing = [c for c in target if c not in corp_map]
    if missing:
        print(f"[WARN] corp_code 매핑 누락: {len(missing)}종목 → {missing[:5]}...")
    print(f"[TARGET] {len(valid)}종목 | fallback: {[f'{y}{q}' for y, q in quarters[:3]]}")

    # 병렬 수집
    results = {}
    error_cnt, done = 0, 0

    def _fetch(args):
        code, corp_code = args
        return code, fetch_financial_with_fallback(key, corp_code, code, name_map)

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(_fetch, (c, cc)): c for c, cc in valid}
        for future in as_completed(futures):
            done += 1
            code, data = future.result()
            if data:
                results[code] = data
            else:
                error_cnt += 1
            if done % 20 == 0:
                print(f"[PROG] {done}/{len(valid)} 성공={len(results)} 실패={error_cnt}")

    # 저장 (merge)
    meta = {
        "date":    today,
        "year":    year,
        "quarter": quarter,
        "count":   len(results),
        "errors":  error_cnt,
        "updated_at": datetime.now(KST).isoformat(),
    }
    save_merged_fund(existing, results, meta)

    print(f"[DONE fetch_dart] 성공={len(results)} 실패={error_cnt}")


if __name__ == "__main__":
    run()
