"""
a2_isolation.py — A2 (WEIGHTS regime switching) identifiability test
═══════════════════════════════════════════════════════════════════════════
β patch: universe baseline 추가
  · mean_universe = 그날 scored된 전 종목 평균 forward return
  · alpha_control = control_top20 - universe
  · alpha_treat   = treat_top20 - universe
  → WEIGHTS가 universe 대비 진짜 alpha 생성하는지 식별 가능
═══════════════════════════════════════════════════════════════════════════
"""

import os
import sys
import json
from datetime import datetime
from collections import defaultdict

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine


WEIGHTS_CONTROL = {"M": 0.25, "F": 0.25, "Q": 0.25, "R": 0.0, "L": 0.25}
R_VALUE_CONTROL = 0.0

HORIZONS = [1, 3, 5]
START_DATE = pd.Timestamp("2021-01-01")
END_DATE = pd.Timestamp("2026-05-15")
TOP_N = 20

OUTPUT_PATH = os.path.join(engine.DATA_DIR, "a2_test_result.json")


def precompute_hist_by_code(hist_full):
    return {
        c: g.sort_values("date").reset_index(drop=True)
        for c, g in hist_full.groupby("code")
    }


def slice_code_asof(g_sorted, asof_date):
    dates_arr = g_sorted["date"].values
    idx = np.searchsorted(dates_arr, np.datetime64(asof_date), side="right")
    return g_sorted.iloc[:idx]


def slice_flow_asof(flow_full, asof_str):
    out = {}
    for code, dates_dict in flow_full.items():
        if not isinstance(dates_dict, dict):
            continue
        out[code] = {d: v for d, v in dates_dict.items() if d <= asof_str}
    return out


def rank_all_with_weights(scored_list, weights, r_value):
    if not scored_list:
        return []
    df = pd.DataFrame(scored_list)
    for col in ["M_raw", "F_raw", "L_raw"]:
        df[col.replace("_raw", "")] = df[col].rank(pct=True, method="min")
    df["Q"] = df["Q_raw"]
    df["score"] = (
        df["M"] * weights["M"]
        + df["F"] * weights["F"]
        + df["Q"] * weights["Q"]
        + r_value * weights["R"]
        + df["L"] * weights["L"]
    ) * 100
    df = df.sort_values(["score", "code"], ascending=[False, True]).reset_index(drop=True)
    df["rank_all"] = range(1, len(df) + 1)
    return df[["code", "score", "rank_all"]].to_dict("records")


def compute_factors_for_date(hist_by_code, flow_full, fund, meta_by_code, asof_date):
    asof_str = asof_date.strftime("%Y-%m-%d") if hasattr(asof_date, 'strftime') else pd.Timestamp(asof_date).strftime("%Y-%m-%d")
    flow_asof = slice_flow_asof(flow_full, asof_str)
    scored = []
    for code, g_full in hist_by_code.items():
        df_s = slice_code_asof(g_full, asof_date)
        if len(df_s) < 60:
            continue
        df_s = df_s.reset_index(drop=True)
        meta = meta_by_code.get(code, {})
        ca_zero = bool(meta.get("ca_zero", False))
        flow_code = flow_asof.get(code, {})
        fund_code = fund.get(code, {})
        s = engine.compute_factors(code, code, df_s, flow_code, fund_code, ca_zero=ca_zero)
        if s is not None:
            scored.append(s)
    return scored


def compute_daily_metrics(control_list, treat_list, top_n=TOP_N):
    if not control_list or not treat_list:
        return None
    df_c = pd.DataFrame(control_list).set_index("code")
    df_t = pd.DataFrame(treat_list).set_index("code")
    common = df_c.index.intersection(df_t.index)
    if len(common) < 10:
        return None
    rho, _ = spearmanr(df_c.loc[common, "rank_all"], df_t.loc[common, "rank_all"])
    rho = float(rho) if not np.isnan(rho) else None
    top_c = set(df_c.sort_values("rank_all").head(top_n).index)
    top_t = set(df_t.sort_values("rank_all").head(top_n).index)
    union = top_c | top_t
    jaccard = len(top_c & top_t) / len(union) if union else 0.0
    return {
        "rho": rho,
        "jaccard": jaccard,
        "top_control": sorted(top_c),
        "top_treat": sorted(top_t),
    }


def compute_forward_returns(g_sorted, signal_date, horizons):
    after_mask = g_sorted["date"] > signal_date
    after = g_sorted[after_mask]
    if len(after) < 2:
        return {h: None for h in horizons}
    entry_loc = after.index[0]
    if "open" in g_sorted.columns and pd.notna(g_sorted.loc[entry_loc, "open"]):
        entry_price = g_sorted.loc[entry_loc, "open"]
    else:
        entry_price = g_sorted.loc[entry_loc, "close"]
    if pd.isna(entry_price) or entry_price <= 0:
        return {h: None for h in horizons}
    out = {}
    for h in horizons:
        exit_loc = entry_loc + h
        if exit_loc < len(g_sorted):
            exit_p = g_sorted.loc[exit_loc, "close"]
            if pd.notna(exit_p) and exit_p > 0:
                out[h] = float(exit_p / entry_price - 1)
            else:
                out[h] = None
        else:
            out[h] = None
    return out


def compute_ev_delta(daily_records, hist_by_code):
    rows_c = defaultdict(list)
    rows_t = defaultdict(list)
    rows_u = defaultdict(list)
    for rec in daily_records:
        sig_date = rec["date"]
        for code in rec.get("universe_codes", []):
            if code in hist_by_code:
                fwd = compute_forward_returns(hist_by_code[code], sig_date, HORIZONS)
                for h, r in fwd.items():
                    if r is not None:
                        rows_u[h].append(r)
        for code in rec["top_control"]:
            if code in hist_by_code:
                fwd = compute_forward_returns(hist_by_code[code], sig_date, HORIZONS)
                for h, r in fwd.items():
                    if r is not None:
                        rows_c[h].append(r)
        for code in rec["top_treat"]:
            if code in hist_by_code:
                fwd = compute_forward_returns(hist_by_code[code], sig_date, HORIZONS)
                for h, r in fwd.items():
                    if r is not None:
                        rows_t[h].append(r)

    out = {}
    for h in HORIZONS:
        c_arr = np.array(rows_c[h]) if rows_c[h] else np.array([])
        t_arr = np.array(rows_t[h]) if rows_t[h] else np.array([])
        u_arr = np.array(rows_u[h]) if rows_u[h] else np.array([])
        out[f"T+{h}"] = {
            "n_universe": int(len(u_arr)),
            "n_control": int(len(c_arr)),
            "n_treat": int(len(t_arr)),
            "mean_universe": float(u_arr.mean()) if len(u_arr) else None,
            "mean_control": float(c_arr.mean()) if len(c_arr) else None,
            "mean_treat": float(t_arr.mean()) if len(t_arr) else None,
            "delta_ev": (float(t_arr.mean() - c_arr.mean())
                         if len(c_arr) and len(t_arr) else None),
            "alpha_control": (float(c_arr.mean() - u_arr.mean())
                              if len(c_arr) and len(u_arr) else None),
            "alpha_treat": (float(t_arr.mean() - u_arr.mean())
                            if len(t_arr) and len(u_arr) else None),
            "winrate_universe": float((u_arr > 0).mean()) if len(u_arr) else None,
            "winrate_control": float((c_arr > 0).mean()) if len(c_arr) else None,
            "winrate_treat": float((t_arr > 0).mean()) if len(t_arr) else None,
        }
    return out


def classify_a2(rho_mean, j_mean, delta_ev_t5):
    if rho_mean is None or j_mean is None:
        return "INDETERMINATE"
    if rho_mean > 0.95 and j_mean > 0.85:
        return "CASE_1_NOISE"
    if (delta_ev_t5 is not None) and (delta_ev_t5 > 0) and (rho_mean < 0.85):
        return "CASE_3_STRUCTURAL_ENGINE"
    return "CASE_2_CONDITIONAL_ALPHA"


def run():
    t0 = datetime.now()
    print(f"[A2 ISOLATION] start {t0.isoformat()}")
    hist, flow, macro, fund, stocks_meta = engine.load_data()
    print(f"[DATA] hist rows={len(hist)} | codes={hist['code'].nunique()} | "
          f"flow={len(flow)} | fund={len(fund)} | meta={len(stocks_meta)}")

    hist_by_code = precompute_hist_by_code(hist)
    meta_by_code = {str(s["code"]).zfill(6): s for s in stocks_meta}

    all_dates_sorted = sorted(hist["date"].unique())
    all_dates = [d for d in all_dates_sorted
                 if START_DATE <= pd.Timestamp(d) <= END_DATE]
    if len(all_dates) < 60:
        print(f"[ABORT] insufficient sessions in range: {len(all_dates)}")
        return
    print(f"[RANGE] {pd.Timestamp(all_dates[0]).date()} ~ "
          f"{pd.Timestamp(all_dates[-1]).date()} | {len(all_dates)} sessions")

    daily_records = []
    regime_buckets = defaultdict(lambda: {"rho": [], "j": []})
    regime_counter = defaultdict(int)

    kp_dates_sorted = sorted(macro.get("KOSPI200", {}).keys())

    for i, d in enumerate(all_dates):
        if i % 100 == 0:
            print(f"  [{i}/{len(all_dates)}] {pd.Timestamp(d).strftime('%Y-%m-%d')}")
        scored = compute_factors_for_date(hist_by_code, flow, fund, meta_by_code, d)
        if len(scored) < TOP_N:
            continue
        asof_str = pd.Timestamp(d).strftime("%Y-%m-%d")
        eligible = [k for k in kp_dates_sorted if k <= asof_str]
        if not eligible:
            regime = "SIDEWAY"
        else:
            regime = engine.compute_regime(macro, eligible[-1])
        regime_counter[regime] += 1
        ctrl = rank_all_with_weights(scored, WEIGHTS_CONTROL, R_VALUE_CONTROL)
        treat = rank_all_with_weights(scored, engine.WEIGHTS[regime], engine.R_VALUE[regime])
        m = compute_daily_metrics(ctrl, treat)
        if m is None:
            continue
        universe_codes = [r["code"] for r in ctrl]
        rec = {"date": d, "regime": regime, "universe_codes": universe_codes, **m}
        daily_records.append(rec)
        if m["rho"] is not None:
            regime_buckets[regime]["rho"].append(m["rho"])
        regime_buckets[regime]["j"].append(m["jaccard"])

    print(f"[DAILY] valid days: {len(daily_records)} | "
          f"regime dist: {dict(regime_counter)}")
    if not daily_records:
        print("[ABORT] no valid daily records")
        return

    rho_all = [r["rho"] for r in daily_records if r["rho"] is not None]
    j_all = [r["jaccard"] for r in daily_records]
    rho_mean = float(np.mean(rho_all)) if rho_all else None
    rho_std = float(np.std(rho_all)) if rho_all else None
    j_mean = float(np.mean(j_all)) if j_all else None
    j_std = float(np.std(j_all)) if j_all else None

    regime_summary = {}
    for r, buckets in regime_buckets.items():
        regime_summary[r] = {
            "n_days": int(regime_counter[r]),
            "n_valid": len(buckets["rho"]),
            "rho_mean": float(np.mean(buckets["rho"])) if buckets["rho"] else None,
            "jaccard_mean": float(np.mean(buckets["j"])) if buckets["j"] else None,
        }

    print("[EV] computing forward returns for universe/control/treat...")
    ev = compute_ev_delta(daily_records, hist_by_code)

    delta_ev_t5 = ev.get("T+5", {}).get("delta_ev")
    verdict = classify_a2(rho_mean, j_mean, delta_ev_t5)

    result = {
        "run_at": datetime.now().isoformat(),
        "config": {
            "control_weights": WEIGHTS_CONTROL,
            "control_r_value": R_VALUE_CONTROL,
            "test_period": [str(pd.Timestamp(all_dates[0]).date()),
                            str(pd.Timestamp(all_dates[-1]).date())],
            "horizons": HORIZONS,
            "n_sessions": len(daily_records),
            "top_n": TOP_N,
        },
        "global": {
            "rho_mean": rho_mean, "rho_std": rho_std,
            "jaccard_mean": j_mean, "jaccard_std": j_std,
        },
        "by_regime": regime_summary,
        "ev_delta": ev,
        "verdict": verdict,
        "decision_thresholds": {
            "CASE_1_NOISE": "rho>0.95 AND J>0.85",
            "CASE_3_STRUCTURAL_ENGINE": "rho<0.85 AND ΔEV(T+5)>0",
            "CASE_2_CONDITIONAL_ALPHA": "otherwise",
        },
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8-sig") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    elapsed = (datetime.now() - t0).total_seconds()
    print(f"\n{'='*70}\n[VERDICT] {verdict}\n{'='*70}")
    if rho_mean is not None:
        print(f"  ρ (Spearman) mean = {rho_mean:.4f} ± {rho_std:.4f}")
    if j_mean is not None:
        print(f"  J (Jaccard)  mean = {j_mean:.4f} ± {j_std:.4f}")
    print(f"\nPer regime:")
    for r, s in regime_summary.items():
        rho_str = f"{s['rho_mean']:.4f}" if s['rho_mean'] is not None else "N/A"
        j_str = f"{s['jaccard_mean']:.4f}" if s['jaccard_mean'] is not None else "N/A"
        print(f"  {r:<12} n={s['n_days']:>5} | ρ={rho_str} | J={j_str}")
    print(f"\nΔEV (treat - control) & alpha vs universe:")
    for h_key, h_v in ev.items():
        if h_v.get("delta_ev") is not None:
            mu, mc, mt = h_v.get("mean_universe"), h_v.get("mean_control"), h_v.get("mean_treat")
            ac, at = h_v.get("alpha_control"), h_v.get("alpha_treat")
            print(f"  {h_key}: universe {mu*100:+.3f}% (n={h_v['n_universe']}) | "
                  f"ctrl {mc*100:+.3f}% (α={ac*100:+.3f}%) | "
                  f"treat {mt*100:+.3f}% (α={at*100:+.3f}%) | "
                  f"ΔEV={h_v['delta_ev']*100:+.3f}%")
    print(f"\n[DONE] elapsed={elapsed:.1f}s → {OUTPUT_PATH}")


if __name__ == "__main__":
    run()
