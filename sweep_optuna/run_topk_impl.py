#!/usr/bin/env python3
import argparse
import csv
import math
import subprocess
import sys
from pathlib import Path


BASE_SWEEP_FIELDS = [
    "trial_number",
    "iters",
    "out_wl",
    "out_iwl",
    "int_wl",
    "int_iwl",
    "status",
    "mse",
    "ucb95",
    "N",
    "lat_best",
    "lat_avg",
    "lat_worst",
    "lat_worst_ns_nominal",
    "ii_min",
    "ii_max",
    "cp_synth_ns",
    "cp_route_ns",
    "est_slack_ns",
    "est_violation_ns",
    "est_timing_violation",
    "area_score",
    "area_source",
    "lut",
    "ff",
    "dsp",
    "bram",
    "uram",
    "elapsed_sec",
    "value",
    "trial_dir",
    "trial_log",
    "syn_log",
    "impl_log",
]


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    results_dir = script_dir / "results"
    p = argparse.ArgumentParser(description="Run impl-only phase on top-K PASS configs from a prior sweep CSV.")
    p.add_argument("--results-csv", type=Path, default=results_dir / "sweep_results.csv")
    p.add_argument("--k", type=int, default=10, help="Top-K PASS configs ranked by value.")
    p.add_argument("--samples", type=int, default=200000)
    p.add_argument("--threshold", type=float, default=2.4e-11)
    p.add_argument("--out-iwl", type=int, default=1)
    p.add_argument("--part", type=str, default="xc7z020clg484-1")
    p.add_argument("--clock-period-ns", type=float, default=10.0)
    p.add_argument("--syn-timeout-sec", type=int, default=3600)
    p.add_argument("--impl-timeout-sec", type=int, default=3600)
    p.add_argument("--jobs", type=int, default=1, help="Keep this low for impl stage.")
    p.add_argument("--save-runs", type=str, default="none", choices=["none", "errors", "all"])
    p.add_argument("--study-prefix", type=str, default="topk_impl")
    p.add_argument("--objective", type=str, default="latency", choices=["latency", "ucb95"])

    p.add_argument("--pareto-x", type=str, default="lat_worst")
    p.add_argument("--pareto-y", type=str, default="area_score")
    p.add_argument("--pareto-include-fail", action="store_true")

    p.add_argument("--aggregate-csv", type=Path, default=results_dir / "topk_impl_results.csv")
    p.add_argument("--pareto-csv", type=Path, default=results_dir / "topk_impl_pareto_points.csv")
    p.add_argument("--pareto-png", type=Path, default=results_dir / "topk_impl_pareto.png")
    p.add_argument("--summary", type=Path, default=results_dir / "topk_impl_summary.txt")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def _to_float(v: str) -> float:
    try:
        return float(v)
    except Exception:
        return float("inf")


def _to_float_or_none(v: str) -> float | None:
    try:
        x = float(v)
    except Exception:
        return None
    if not math.isfinite(x):
        return None
    return x


def load_topk_configs(csv_path: Path, k: int) -> list[dict]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Results CSV not found: {csv_path}")

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    pass_rows = [r for r in rows if r.get("status") == "PASS"]
    pass_rows.sort(key=lambda r: _to_float(r.get("value", "")))

    uniq = []
    seen = set()
    for r in pass_rows:
        key = (r.get("iters"), r.get("out_wl"), r.get("int_wl"), r.get("int_iwl"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
        if len(uniq) >= k:
            break
    return uniq


def build_cmd(run_sweep_py: Path, cfg: dict, args: argparse.Namespace, idx: int) -> tuple[list[str], str]:
    iters = int(cfg["iters"])
    out_wl = int(cfg["out_wl"])
    int_wl = int(cfg["int_wl"])
    int_iwl = int(cfg["int_iwl"])
    study_name = f"{args.study_prefix}_k{idx:02d}_it{iters}_owl{out_wl}_iw{int_wl}_ii{int_iwl}"

    cmd = [
        sys.executable,
        str(run_sweep_py),
        "--iters-min",
        str(iters),
        "--iters-max",
        str(iters),
        "--out-wl-min",
        str(out_wl),
        "--out-wl-max",
        str(out_wl),
        "--out-iwl",
        str(args.out_iwl),
        "--int-wl-values",
        str(int_wl),
        "--int-iwl-values",
        str(int_iwl),
        "--samples",
        str(args.samples),
        "--threshold",
        str(args.threshold),
        "--objective",
        args.objective,
        "--run-syn",
        "--run-impl",
        "--part",
        args.part,
        "--clock-period-ns",
        str(args.clock_period_ns),
        "--syn-timeout-sec",
        str(args.syn_timeout_sec),
        "--impl-timeout-sec",
        str(args.impl_timeout_sec),
        "--limit-trials",
        "1",
        "--jobs",
        str(args.jobs),
        "--study-name",
        study_name,
        "--save-runs",
        args.save_runs,
    ]
    return cmd, study_name


def load_single_run_result(run_csv: Path, cfg: dict) -> dict | None:
    if not run_csv.exists():
        return None
    with run_csv.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None
    matches = [
        r
        for r in rows
        if r.get("iters") == str(cfg["iters"])
        and r.get("out_wl") == str(cfg["out_wl"])
        and r.get("int_wl") == str(cfg["int_wl"])
        and r.get("int_iwl") == str(cfg["int_iwl"])
    ]
    if matches:
        return matches[-1]
    return rows[-1]


def metric_value(row: dict, key: str) -> float | None:
    if key not in row:
        return None
    return _to_float_or_none(row.get(key, ""))


def has_metric(rows: list[dict], key: str, include_fail: bool) -> bool:
    for r in rows:
        st = r.get("status", "")
        if st == "PASS" or (include_fail and st == "FAIL"):
            if metric_value(r, key) is not None:
                return True
    return False


def build_pareto(rows: list[dict], x_key: str, y_key: str, include_fail: bool) -> tuple[list[dict], list[dict]]:
    candidates = []
    for r in rows:
        st = r.get("status", "")
        if not (st == "PASS" or (include_fail and st == "FAIL")):
            continue
        x = metric_value(r, x_key)
        y = metric_value(r, y_key)
        if x is None or y is None:
            continue
        candidates.append({"row": r, "x": x, "y": y})

    candidates.sort(key=lambda p: (p["x"], p["y"], int(p["row"].get("topk_rank", "9999"))))
    front = []
    best_y = float("inf")
    for p in candidates:
        if p["y"] < best_y:
            front.append(p)
            best_y = p["y"]
    return candidates, front


def main() -> int:
    args = parse_args()
    if args.k <= 0:
        print("ERROR: --k must be positive.", file=sys.stderr)
        return 2
    if args.jobs <= 0:
        print("ERROR: --jobs must be positive.", file=sys.stderr)
        return 2

    script_dir = Path(__file__).resolve().parent
    results_dir = script_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    run_sweep_py = script_dir / "run_optuna_sweep.py"
    run_csv = results_dir / "sweep_results.csv"

    topk = load_topk_configs(args.results_csv, args.k)
    if not topk:
        print("ERROR: No PASS rows found in results CSV.", file=sys.stderr)
        return 1

    print(f"Loaded {len(topk)} top configs from {args.results_csv}")
    for i, cfg in enumerate(topk):
        print(
            f"TOPK[{i}] iters={cfg['iters']} out_wl={cfg['out_wl']} int_wl={cfg['int_wl']} "
            f"int_iwl={cfg['int_iwl']} source_value={cfg.get('value')}"
        )

    agg_rows: list[dict] = []
    for i, cfg in enumerate(topk):
        cmd, study_name = build_cmd(run_sweep_py, cfg, args, i)
        print("\n=== RUN", i, "===")
        print("CMD:", " ".join(cmd))
        if args.dry_run:
            continue
        proc = subprocess.run(cmd, cwd=str(script_dir.parent))
        if proc.returncode != 0:
            print(f"ERROR: top-k impl run failed at index {i}, return code {proc.returncode}", file=sys.stderr)
            return proc.returncode
        row = load_single_run_result(run_csv, cfg)
        if row is None:
            print("ERROR: Cannot parse per-run sweep_results.csv after impl run.", file=sys.stderr)
            return 1
        row["topk_rank"] = str(i)
        row["source_value"] = str(cfg.get("value", ""))
        row["source_status"] = str(cfg.get("status", ""))
        row["source_ucb95"] = str(cfg.get("ucb95", ""))
        row["source_mse"] = str(cfg.get("mse", ""))
        row["source_lat_worst"] = str(cfg.get("lat_worst", ""))
        row["source_area_score"] = str(cfg.get("area_score", ""))
        row["impl_study_name"] = study_name
        agg_rows.append(row)

    if args.dry_run:
        print("Dry-run done.")
        return 0
    if not agg_rows:
        print("ERROR: No top-k impl rows collected.", file=sys.stderr)
        return 1

    agg_fields = ["topk_rank", "impl_study_name"] + BASE_SWEEP_FIELDS + [
        "source_value",
        "source_status",
        "source_ucb95",
        "source_mse",
        "source_lat_worst",
        "source_area_score",
    ]
    with args.aggregate_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=agg_fields, extrasaction="ignore")
        w.writeheader()
        for r in agg_rows:
            w.writerow(r)

    x_req = args.pareto_x
    y_req = args.pareto_y
    x_used = x_req if has_metric(agg_rows, x_req, args.pareto_include_fail) else "value"
    if has_metric(agg_rows, y_req, args.pareto_include_fail):
        y_used = y_req
    elif has_metric(agg_rows, "area_score", args.pareto_include_fail):
        y_used = "area_score"
    elif has_metric(agg_rows, "ucb95", args.pareto_include_fail):
        y_used = "ucb95"
    else:
        y_used = "mse"

    candidates, front = build_pareto(agg_rows, x_used, y_used, args.pareto_include_fail)
    with args.pareto_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "topk_rank",
                "trial_number",
                "status",
                "x_metric",
                "x",
                "y_metric",
                "y",
                "iters",
                "out_wl",
                "out_iwl",
                "int_wl",
                "int_iwl",
                "value",
                "ucb95",
                "lat_worst",
                "area_score",
            ]
        )
        for p in front:
            r = p["row"]
            w.writerow(
                [
                    r.get("topk_rank", ""),
                    r.get("trial_number", ""),
                    r.get("status", ""),
                    x_used,
                    p["x"],
                    y_used,
                    p["y"],
                    r.get("iters", ""),
                    r.get("out_wl", ""),
                    r.get("out_iwl", ""),
                    r.get("int_wl", ""),
                    r.get("int_iwl", ""),
                    r.get("value", ""),
                    r.get("ucb95", ""),
                    r.get("lat_worst", ""),
                    r.get("area_score", ""),
                ]
            )

    plot_status = "not_generated"
    if candidates:
        try:
            import matplotlib.pyplot as plt  # type: ignore

            xs = [p["x"] for p in candidates]
            ys = [p["y"] for p in candidates]
            fx = [p["x"] for p in front]
            fy = [p["y"] for p in front]
            plt.figure(figsize=(6.5, 4.8))
            plt.scatter(xs, ys, s=24, alpha=0.45, label="Top-K candidates")
            if fx and fy:
                plt.plot(fx, fy, "-o", markersize=4, linewidth=1.1, label="Pareto front")
            plt.xlabel(x_used)
            plt.ylabel(y_used)
            plt.title("Top-K Impl Pareto")
            plt.grid(True, linestyle="--", alpha=0.3)
            plt.legend()
            plt.tight_layout()
            plt.savefig(args.pareto_png, dpi=150)
            plt.close()
            plot_status = "ok"
        except Exception:
            plot_status = "matplotlib_unavailable_or_failed"

    pass_count = sum(1 for r in agg_rows if r.get("status") == "PASS")
    with args.summary.open("w", encoding="utf-8") as f:
        f.write(f"input_results_csv={args.results_csv}\n")
        f.write(f"topk_requested={args.k}\n")
        f.write(f"topk_ran={len(agg_rows)}\n")
        f.write(f"pass_count={pass_count}\n")
        f.write(f"objective={args.objective}\n")
        f.write(f"samples={args.samples}\n")
        f.write(f"jobs={args.jobs}\n")
        f.write(f"run_syn=True\n")
        f.write(f"run_impl=True\n")
        f.write(f"pareto_x_requested={x_req}\n")
        f.write(f"pareto_y_requested={y_req}\n")
        f.write(f"pareto_x={x_used}\n")
        f.write(f"pareto_y={y_used}\n")
        f.write(f"pareto_include_fail={args.pareto_include_fail}\n")
        f.write(f"pareto_candidates={len(candidates)}\n")
        f.write(f"pareto_points={len(front)}\n")
        f.write(f"aggregate_csv={args.aggregate_csv}\n")
        f.write(f"pareto_csv={args.pareto_csv}\n")
        f.write(f"pareto_plot={args.pareto_png if plot_status == 'ok' else plot_status}\n")

    print(f"Saved Top-K CSV: {args.aggregate_csv}")
    print(f"Saved Top-K summary: {args.summary}")
    print(f"Saved Top-K Pareto CSV: {args.pareto_csv}")
    if plot_status == "ok":
        print(f"Saved Top-K Pareto plot: {args.pareto_png}")
    else:
        print(f"Top-K Pareto plot status: {plot_status}")
    print("Top-K impl flow completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
