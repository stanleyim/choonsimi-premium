import os, shutil
import pandas as pd

PREM = "/content/premium"
REPO = "/content/repo"
HIST = PREM + "/data/history.csv"
BACKUP = PREM + "/data/history_raw_backup.csv"
ADJ_DIR = REPO + "/data/ohlcv_adj"
RAW_DIR = REPO + "/data/ohlcv"

# 1. 백업 (1회, 이미 있으면 안 덮음)
if os.path.exists(BACKUP):
    print("[SKIP] backup exists:", BACKUP)
else:
    shutil.copy(HIST, BACKUP)
    print("[OK] backup ->", BACKUP)

# 2. 기존 history 로드 (universe + date 집합 기준)
h = pd.read_csv(HIST, dtype={"code": str}, encoding="utf-8-sig")
h["code"] = h["code"].str.zfill(6)
h["date"] = pd.to_datetime(h["date"])
codes55 = sorted(h["code"].unique())
name_map = h.drop_duplicates("code").set_index("code")["name"].to_dict()
key_set = set(zip(h["code"], h["date"]))
print("history: codes=%d rows=%d date=%s~%s" % (len(codes55), len(h), h["date"].min().date(), h["date"].max().date()))

# 3. 필요한 연도만 (2025, 2026)
years = sorted(h["date"].dt.year.unique())
adj = pd.concat([pd.read_parquet(ADJ_DIR + "/year=%d.parquet" % y) for y in years], ignore_index=True)
raw = pd.concat([pd.read_parquet(RAW_DIR + "/year=%d.parquet" % y) for y in years], ignore_index=True)
adj["code"] = adj["code"].str.zfill(6)
raw["code"] = raw["code"].str.zfill(6)
adj["date"] = pd.to_datetime(adj["date"])
raw["date"] = pd.to_datetime(raw["date"])

# 4. 55종목 필터
adj = adj[adj["code"].isin(codes55)]
raw = raw[raw["code"].isin(codes55)][["code", "date", "trade_value"]]

# 5. adjusted OHLCV + raw trade_value 병합
m = adj.merge(raw, on=["code", "date"], how="left")

# 6. 기존 history의 (code,date) 집합으로 제한 (row/date 동일 유지)
m["key"] = list(zip(m["code"], m["date"]))
m = m[m["key"].isin(key_set)].drop(columns=["key"])

# 7. name 복원 + 컬럼 순서 = 기존 history 동일
m["name"] = m["code"].map(name_map)
cols = ["date", "code", "name", "open", "high", "low", "close", "volume", "change_rate", "trade_value"]
m = m[cols].sort_values(["code", "date"]).reset_index(drop=True)
m["date"] = m["date"].dt.strftime("%Y-%m-%d")

# 8. 검증
print("--- VERIFY ---")
print("new codes:", m["code"].nunique(), "(expect 55)")
print("new rows:", len(m), "(orig", len(h), ")")
print("date:", m["date"].min(), "~", m["date"].max())
print("trade_value NaN:", m["trade_value"].isna().sum())
print("close NaN:", m["close"].isna().sum())

# 9. row 정합 경고 (덮어쓰기 전 판단용)
if len(m) != len(h):
    print("[WARN] row count diff: new=%d orig=%d (일부 date 누락 가능)" % (len(m), len(h)))

# 10. 저장 (검증 통과 시)
OUT = PREM + "/data/history_adj_preview.csv"
m.to_csv(OUT, index=False, encoding="utf-8-sig")
print("[OK] preview saved ->", OUT, "(history.csv 아직 안 덮음)")
