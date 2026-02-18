#!/usr/bin/env python3
import argparse
import csv
import enum
import math
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import optuna
except ImportError as exc:
    print("ERROR: optuna is not installed. Install with: python -m pip install optuna")
    raise SystemExit(2) from exc


RESULT_RE = re.compile(
    r"RESULT\s+iters=(?P<iters>\d+)\s+out_wl=(?P<out_wl>\d+)\s+out_iwl=(?P<out_iwl>\d+)\s+N=(?P<N>\d+)\s+mse=(?P<mse>[-+0-9.eE]+)\s+ucb95=(?P<ucb95>[-+0-9.eE]+)"
)


class SaveRunsMode(str, enum.Enum):
    NONE = "none"
    ERRORS = "errors"
    ALL = "all"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run full-grid CSim sweep using Optuna GridSampler.")
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="Project root path.")
    p.add_argument("--iters-min", type=int, default=16)
    p.add_argument("--iters-max", type=int, default=20)
    p.add_argument("--out-wl-min", type=int, default=19)
    p.add_argument("--out-wl-max", type=int, default=26)
    p.add_argument("--out-iwl", type=int, default=1)
    p.add_argument("--int-wl-values", type=str, default="19,20,21,22,23,24,25,26")
    p.add_argument("--int-iwl-values", type=str, default="3,4,5,6")
    p.add_argument("--samples", type=int, default=200000)
    p.add_argument("--threshold", type=float, default=2.4e-11)
    p.add_argument(
        "--sampler",
        type=str,
        choices=["grid", "tpe"],
        default="tpe",
        help="Search sampler: exhaustive grid or TPE.",
    )
    p.add_argument("--tpe-startup-trials", type=int, default=192)
    p.add_argument("--tpe-candidates", type=int, default=128)
    p.add_argument(
        "--objective",
        type=str,
        choices=["ucb95", "latency"],
        default="latency",
        help="Primary optimization objective.",
    )
    p.add_argument(
        "--run-syn",
        action="store_true",
        help="Run csynth (via HLS Tcl) after csim pass to collect latency metrics.",
    )
    p.add_argument(
        "--run-impl",
        action="store_true",
        help="Run implementation after csynth pass to collect post-route timing/resource reports.",
    )
    p.add_argument("--part", type=str, default="xc7z020clg484-1")
    p.add_argument("--clock-period-ns", type=float, default=10.0)
    p.add_argument(
        "--latency-weight",
        type=float,
        default=0.9,
        help="Normalized latency weight in latency objective.",
    )
    p.add_argument(
        "--area-weight",
        type=float,
        default=0.1,
        help="Normalized area weight in latency objective.",
    )
    p.add_argument(
        "--latency-norm-ref",
        type=float,
        default=30.0,
        help="Reference latency (cycles) for normalization.",
    )
    p.add_argument(
        "--area-norm-ref",
        type=float,
        default=0.05,
        help="Reference area score for normalization.",
    )
    p.add_argument("--area-lut-w", type=float, default=1.0)
    p.add_argument("--area-ff-w", type=float, default=1.0)
    p.add_argument("--area-dsp-w", type=float, default=1.0)
    p.add_argument("--area-bram-w", type=float, default=1.0)
    p.add_argument("--area-uram-w", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--timeout-sec", type=int, default=1800)
    p.add_argument("--syn-timeout-sec", type=int, default=3600)
    p.add_argument("--impl-timeout-sec", type=int, default=3600)
    p.add_argument("--retry", type=int, default=1)
    p.add_argument("--limit-trials", type=int, default=0, help="0 means run all trials.")
    p.add_argument("--jobs", type=int, default=24, help="Parallel trial workers for Optuna.")
    p.add_argument("--study-name", type=str, default="exp_cordic_latency_grid")
    p.add_argument(
        "--pareto-x",
        type=str,
        default="lat_worst",
        help="Metric for Pareto X axis (minimize).",
    )
    p.add_argument(
        "--pareto-y",
        type=str,
        default="area_score",
        help="Metric for Pareto Y axis (minimize).",
    )
    p.add_argument(
        "--pareto-include-fail",
        action="store_true",
        help="Include FAIL trials in Pareto point candidates.",
    )
    p.add_argument(
        "--save-runs",
        type=str,
        choices=[m.value for m in SaveRunsMode],
        default=SaveRunsMode.ERRORS.value,
        help="Keep per-trial run directories: none|errors|all",
    )
    p.add_argument(
        "--fail-penalty-base",
        type=float,
        default=1000.0,
        help="Base penalty for FAIL trials when objective=latency (finite for TPE).",
    )
    p.add_argument(
        "--error-penalty",
        type=float,
        default=1.0e9,
        help="Penalty value for ERROR_* trials when objective=latency.",
    )
    p.add_argument(
        "--clean-runs-on-start",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Clean sweep_optuna/runs contents before starting a new run.",
    )
    p.add_argument(
        "--reset-study-on-start",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Delete existing Optuna study with same name before running.",
    )
    return p.parse_args()


def parse_int_values(s: str) -> list[int]:
    vals = []
    for tok in s.split(","):
        tok = tok.strip()
        if not tok:
            continue
        vals.append(int(tok))
    vals = sorted(set(vals))
    if not vals:
        raise ValueError("Empty integer value list.")
    return vals


def force_define(header_text: str, macro: str, value: int) -> str:
    pattern = re.compile(rf"(^\s*#define\s+{re.escape(macro)}\s+).*$", re.MULTILINE)
    if pattern.search(header_text):
        return pattern.sub(rf"\g<1>{value}", header_text)
    return f"#define {macro} {value}\n" + header_text


def prepare_trial_dir(
    root: Path,
    trial_dir: Path,
    iters: int,
    out_wl: int,
    out_iwl: int,
    int_wl: int,
    int_iwl: int,
    n_samples: int,
) -> None:
    if trial_dir.exists():
        shutil.rmtree(trial_dir)
    trial_dir.mkdir(parents=True, exist_ok=True)

    copy_files = ["exp_cordic.h", "exp_cordic.cpp", "tb_exp_cordic.cpp", "hls_config.cfg"]
    for fname in copy_files:
        shutil.copy2(root / fname, trial_dir / fname)

    h_text = (trial_dir / "exp_cordic.h").read_text(encoding="ascii")
    h_text = force_define(h_text, "EXP_CORDIC_ITERS", iters)
    h_text = force_define(h_text, "EXP_OUT_WL", out_wl)
    h_text = force_define(h_text, "EXP_OUT_IWL", out_iwl)
    h_text = force_define(h_text, "EXP_INT_WL", int_wl)
    h_text = force_define(h_text, "EXP_INT_IWL", int_iwl)
    (trial_dir / "exp_cordic.h").write_text(h_text, encoding="ascii")

    tb_path = trial_dir / "tb_exp_cordic.cpp"
    tb_text = tb_path.read_text(encoding="ascii")
    tb_text = re.sub(
        r"(^\s*#define\s+TB_N_SAMPLES\s+).*$",
        rf"\g<1>{n_samples}",
        tb_text,
        flags=re.MULTILINE,
    )
    tb_path.write_text(tb_text, encoding="ascii")


def run_csim_once(trial_dir: Path, timeout_sec: int) -> tuple[int, str]:
    vitis_run = r"D:\Apps\Xilinx\2025.2\Vitis\bin\vitis-run.bat"
    cmd = [vitis_run, "--mode", "hls", "--config", "hls_config.cfg", "--work_dir", ".", "--csim"]
    proc = subprocess.run(
        cmd,
        cwd=str(trial_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout_sec,
        shell=False,
    )
    return proc.returncode, proc.stdout


def run_syn_once(trial_dir: Path, part: str, clock_period_ns: float, timeout_sec: int) -> tuple[int, str]:
    tcl_path = trial_dir / "run_syn.tcl"
    tcl_text = (
        "open_component .\n"
        "add_files exp_cordic.h\n"
        "add_files exp_cordic.cpp\n"
        "add_files -tb tb_exp_cordic.cpp\n"
        "set_top exp_cordic_ip\n"
        f"set_part {part}\n"
        "open_solution -flow_target vivado hls\n"
        f"create_clock -period {clock_period_ns:.3f} -name default\n"
        "csynth_design\n"
        "exit\n"
    )
    tcl_path.write_text(tcl_text, encoding="ascii")

    vitis_run = r"D:\Apps\Xilinx\2025.2\Vitis\bin\vitis-run.bat"
    cmd = [vitis_run, "--mode", "hls", "--tcl", str(tcl_path.name), "--work_dir", "."]
    proc = subprocess.run(
        cmd,
        cwd=str(trial_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout_sec,
        shell=False,
    )
    return proc.returncode, proc.stdout


def run_impl_once(trial_dir: Path, timeout_sec: int) -> tuple[int, str]:
    vitis_run = r"D:\Apps\Xilinx\2025.2\Vitis\bin\vitis-run.bat"
    cmd = [vitis_run, "--mode", "hls", "--config", "hls_config.cfg", "--work_dir", ".", "--impl"]
    proc = subprocess.run(
        cmd,
        cwd=str(trial_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout_sec,
        shell=False,
    )
    return proc.returncode, proc.stdout


def extract_result(output: str) -> dict | None:
    m = RESULT_RE.search(output)
    if not m:
        return None
    d = m.groupdict()
    return {
        "iters": int(d["iters"]),
        "out_wl": int(d["out_wl"]),
        "out_iwl": int(d["out_iwl"]),
        "N": int(d["N"]),
        "mse": float(d["mse"]),
        "ucb95": float(d["ucb95"]),
    }


def _get_text(root: ET.Element, path: str) -> str:
    n = root.find(path)
    if n is None or n.text is None:
        return ""
    return n.text.strip()


def _to_int(s: str) -> int | None:
    try:
        return int(s)
    except Exception:
        return None


def _to_float(s: str) -> float | None:
    try:
        return float(s)
    except Exception:
        return None


def extract_latency_metrics(trial_dir: Path) -> dict:
    metrics = {
        "lat_best": None,
        "lat_avg": None,
        "lat_worst": None,
        "lat_worst_ns_nominal": None,
        "ii_min": None,
        "ii_max": None,
        "cp_synth_ns": None,
        "cp_route_ns": None,
        "est_slack_ns": None,
        "est_violation_ns": None,
        "est_timing_violation": None,
        "lut": None,
        "ff": None,
        "dsp": None,
        "bram": None,
        "uram": None,
        "avail_lut": None,
        "avail_ff": None,
        "avail_dsp": None,
        "avail_bram": None,
        "avail_uram": None,
        "area_score": None,
        "area_source": "",
    }

    csynth_xml = trial_dir / "hls" / "syn" / "report" / "exp_cordic_ip_csynth.xml"
    if csynth_xml.exists():
        root = ET.parse(csynth_xml).getroot()
        metrics["lat_best"] = _to_int(_get_text(root, ".//SummaryOfOverallLatency/Best-caseLatency"))
        metrics["lat_avg"] = _to_int(_get_text(root, ".//SummaryOfOverallLatency/Average-caseLatency"))
        metrics["lat_worst"] = _to_int(_get_text(root, ".//SummaryOfOverallLatency/Worst-caseLatency"))
        metrics["ii_min"] = _to_int(_get_text(root, ".//SummaryOfOverallLatency/Interval-min"))
        metrics["ii_max"] = _to_int(_get_text(root, ".//SummaryOfOverallLatency/Interval-max"))
        metrics["cp_synth_ns"] = _to_float(
            _get_text(root, ".//PerformanceEstimates/SummaryOfTimingAnalysis/EstimatedClockPeriod")
        )
        metrics["lut"] = _to_int(_get_text(root, ".//AreaEstimates/Resources/LUT"))
        metrics["ff"] = _to_int(_get_text(root, ".//AreaEstimates/Resources/FF"))
        metrics["dsp"] = _to_int(_get_text(root, ".//AreaEstimates/Resources/DSP"))
        metrics["bram"] = _to_int(_get_text(root, ".//AreaEstimates/Resources/BRAM_18K"))
        metrics["uram"] = _to_int(_get_text(root, ".//AreaEstimates/Resources/URAM"))
        metrics["avail_lut"] = _to_int(_get_text(root, ".//AreaEstimates/AvailableResources/LUT"))
        metrics["avail_ff"] = _to_int(_get_text(root, ".//AreaEstimates/AvailableResources/FF"))
        metrics["avail_dsp"] = _to_int(_get_text(root, ".//AreaEstimates/AvailableResources/DSP"))
        metrics["avail_bram"] = _to_int(_get_text(root, ".//AreaEstimates/AvailableResources/BRAM_18K"))
        metrics["avail_uram"] = _to_int(_get_text(root, ".//AreaEstimates/AvailableResources/URAM"))
        metrics["area_source"] = "syn"

    impl_xml = trial_dir / "hls" / "impl" / "verilog" / "report" / "vivado_impl.xml"
    if impl_xml.exists():
        root = ET.parse(impl_xml).getroot()
        metrics["cp_synth_ns"] = _to_float(_get_text(root, ".//TimingReport/CP_SYNTH"))
        metrics["cp_route_ns"] = _to_float(_get_text(root, ".//TimingReport/CP_ROUTE"))
        metrics["lut"] = _to_int(_get_text(root, ".//AreaReport/Resources/LUT"))
        metrics["ff"] = _to_int(_get_text(root, ".//AreaReport/Resources/FF"))
        metrics["dsp"] = _to_int(_get_text(root, ".//AreaReport/Resources/DSP"))
        metrics["bram"] = _to_int(_get_text(root, ".//AreaReport/Resources/BRAM"))
        metrics["uram"] = _to_int(_get_text(root, ".//AreaReport/Resources/URAM"))
        metrics["avail_lut"] = _to_int(_get_text(root, ".//AreaReport/AvailableResources/LUT"))
        metrics["avail_ff"] = _to_int(_get_text(root, ".//AreaReport/AvailableResources/FF"))
        metrics["avail_dsp"] = _to_int(_get_text(root, ".//AreaReport/AvailableResources/DSP"))
        metrics["avail_bram"] = _to_int(_get_text(root, ".//AreaReport/AvailableResources/BRAM"))
        metrics["avail_uram"] = _to_int(_get_text(root, ".//AreaReport/AvailableResources/URAM"))
        metrics["area_source"] = "impl"

    return metrics


def compute_area_score(metrics: dict, args: argparse.Namespace) -> float | None:
    req = [
        "lut",
        "ff",
        "dsp",
        "bram",
        "uram",
        "avail_lut",
        "avail_ff",
        "avail_dsp",
        "avail_bram",
        "avail_uram",
    ]
    for k in req:
        if metrics.get(k) is None:
            return None
    if (
        metrics["avail_lut"] <= 0
        or metrics["avail_ff"] <= 0
        or metrics["avail_dsp"] <= 0
        or metrics["avail_bram"] <= 0
        or metrics["avail_uram"] < 0
    ):
        return None
    lut_u = metrics["lut"] / metrics["avail_lut"]
    ff_u = metrics["ff"] / metrics["avail_ff"]
    dsp_u = metrics["dsp"] / metrics["avail_dsp"]
    bram_u = metrics["bram"] / metrics["avail_bram"]
    uram_u = 0.0 if metrics["avail_uram"] == 0 else (metrics["uram"] / metrics["avail_uram"])
    return (
        args.area_lut_w * lut_u
        + args.area_ff_w * ff_u
        + args.area_dsp_w * dsp_u
        + args.area_bram_w * bram_u
        + args.area_uram_w * uram_u
    )


def compute_latency_area_cost(lat_worst: int, area_score: float | None, args: argparse.Namespace) -> float:
    # Use nominal target clock to express latency in ns (not estimated clock).
    clk_ns = args.clock_period_ns if args.clock_period_ns > 0.0 else 1.0
    lat_ns = float(lat_worst) * clk_ns
    lat_ref_cycles = args.latency_norm_ref if args.latency_norm_ref > 0.0 else 1.0
    lat_ref = lat_ref_cycles * clk_ns
    area_ref = args.area_norm_ref if args.area_norm_ref > 0.0 else 1.0
    lw = args.latency_weight if args.latency_weight >= 0.0 else 0.0
    aw = args.area_weight if args.area_weight >= 0.0 else 0.0
    wsum = lw + aw
    if wsum <= 0.0:
        lw = 0.7
        aw = 0.3
        wsum = 1.0
    lat_norm = lat_ns / lat_ref
    # If area is missing due report parsing issue, give a mild penalty.
    area_norm = 1.0 if area_score is None else (float(area_score) / area_ref)
    return (lw * lat_norm + aw * area_norm) / wsum


def trial_metric_value(trial: optuna.trial.FrozenTrial, key: str, args: argparse.Namespace) -> float | None:
    if key == "value":
        raw = trial.value
    elif key == "out_iwl":
        raw = args.out_iwl
    elif key in trial.params:
        raw = trial.params[key]
    else:
        raw = trial.user_attrs.get(key)
    if raw is None:
        return None
    try:
        v = float(raw)
    except Exception:
        return None
    if not math.isfinite(v):
        return None
    return v


def metric_has_any_value(
    trials: list[optuna.trial.FrozenTrial],
    metric: str,
    args: argparse.Namespace,
    include_fail: bool,
) -> bool:
    for t in trials:
        status = t.user_attrs.get("status", "")
        if status == "PASS" or (include_fail and status == "FAIL"):
            if trial_metric_value(t, metric, args) is not None:
                return True
    return False


def build_pareto_front(points: list[dict]) -> list[dict]:
    if not points:
        return []
    points_sorted = sorted(points, key=lambda p: (p["x"], p["y"], p["trial_number"]))
    front = []
    best_y = float("inf")
    for p in points_sorted:
        if p["y"] < best_y:
            front.append(p)
            best_y = p["y"]
    return front


def format_hhmmss(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        return "--:--:--"
    s = int(seconds + 0.5)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    sweep_root = Path(__file__).resolve().parent
    runs_dir = sweep_root / "runs"
    results_dir = sweep_root / "results"
    logs_dir = results_dir / "trial_logs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    if args.clean_runs_on_start and runs_dir.exists():
        for child in runs_dir.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except Exception:
                pass
    try:
        int_wl_values = parse_int_values(args.int_wl_values)
        int_iwl_values = parse_int_values(args.int_iwl_values)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    save_mode = SaveRunsMode(args.save_runs)
    if args.iters_min > args.iters_max:
        print("ERROR: --iters-min must be <= --iters-max.", file=sys.stderr)
        return 2
    if args.out_wl_min > args.out_wl_max:
        print("ERROR: --out-wl-min must be <= --out-wl-max.", file=sys.stderr)
        return 2
    if args.jobs <= 0:
        print("ERROR: --jobs must be positive.", file=sys.stderr)
        return 2
    if args.timeout_sec <= 0 or args.syn_timeout_sec <= 0 or args.impl_timeout_sec <= 0:
        print("ERROR: timeout values must be positive.", file=sys.stderr)
        return 2
    if args.objective == "latency" and (not args.run_syn):
        print("ERROR: objective=latency requires --run-syn to collect latency metrics.", file=sys.stderr)
        return 2

    search_space = {
        "iters": list(range(args.iters_min, args.iters_max + 1)),
        "out_wl": list(range(args.out_wl_min, args.out_wl_max + 1)),
        "int_wl": int_wl_values,
        "int_iwl": int_iwl_values,
    }

    if args.sampler == "grid":
        sampler = optuna.samplers.GridSampler(search_space=search_space, seed=args.seed)
    else:
        sampler = optuna.samplers.TPESampler(
            seed=args.seed,
            n_startup_trials=max(1, args.tpe_startup_trials),
            n_ei_candidates=max(1, args.tpe_candidates),
            multivariate=True,
            group=True,
            constant_liar=True,
        )
    storage = f"sqlite:///{(results_dir / 'optuna_study.db').resolve().as_posix()}"
    if args.reset_study_on_start:
        try:
            optuna.delete_study(study_name=args.study_name, storage=storage)
        except Exception:
            pass
    study = optuna.create_study(
        study_name=args.study_name,
        direction="minimize",
        sampler=sampler,
        storage=storage,
        load_if_exists=True,
    )

    search_space_size = (
        len(search_space["iters"])
        * len(search_space["out_wl"])
        * len(search_space["int_wl"])
        * len(search_space["int_iwl"])
    )
    if args.sampler == "grid":
        total_trials = search_space_size
        target_trials = total_trials if args.limit_trials <= 0 else min(args.limit_trials, total_trials)
    else:
        total_trials = search_space_size
        target_trials = total_trials if args.limit_trials <= 0 else min(args.limit_trials, total_trials)

    completed_before = len([t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE])
    planned_new_trials = max(0, target_trials - completed_before)
    progress_lock = threading.Lock()
    io_lock = threading.Lock()
    progress_state = {
        "done_new_trials": 0,
        "start_time": time.time(),
    }

    def print_progress(last_trial_num: int, status: str, value: float) -> None:
        with progress_lock:
            progress_state["done_new_trials"] += 1
            done_new = progress_state["done_new_trials"]
            elapsed = time.time() - progress_state["start_time"]
            if planned_new_trials <= 0:
                pct = 100.0
                eta_sec = 0.0
            else:
                pct = 100.0 * float(done_new) / float(planned_new_trials)
                rate = float(done_new) / elapsed if elapsed > 0 else 0.0
                remaining = max(0, planned_new_trials - done_new)
                eta_sec = (float(remaining) / rate) if rate > 0 else float("inf")
            bar_len = 24
            filled = int((min(max(pct, 0.0), 100.0) / 100.0) * bar_len)
            bar = "[" + ("#" * filled) + ("-" * (bar_len - filled)) + "]"
            if math.isfinite(eta_sec):
                eta_wall = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + eta_sec))
            else:
                eta_wall = "N/A"
            print(
                f"PROGRESS {bar} {done_new}/{planned_new_trials} "
                f"({pct:5.1f}%) elapsed={format_hhmmss(elapsed)} "
                f"eta={format_hhmmss(eta_sec)} eta_at={eta_wall} "
                f"last_trial={last_trial_num} status={status} value={value}"
            )

    live_csv_path = results_dir / "sweep_results.csv"
    live_summary_path = results_dir / "sweep_summary.txt"

    def write_live_csv_and_summary() -> None:
        # Keep these files updated after each completed trial.
        with io_lock:
            trials_live = [t for t in study.trials if t.state.is_finished()]
            trials_sorted_live = sorted(
                trials_live,
                key=lambda t: (
                    t.params.get("iters", 10**9),
                    t.params.get("out_wl", 10**9),
                ),
            )
            with live_csv_path.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(
                    [
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
                )
                for t in trials_sorted_live:
                    w.writerow(
                        [
                            t.number,
                            t.params.get("iters"),
                            t.params.get("out_wl"),
                            args.out_iwl,
                            t.params.get("int_wl"),
                            t.params.get("int_iwl"),
                            t.user_attrs.get("status", "UNKNOWN"),
                            t.user_attrs.get("mse", ""),
                            t.user_attrs.get("ucb95", ""),
                            t.user_attrs.get("N", ""),
                            t.user_attrs.get("lat_best", ""),
                            t.user_attrs.get("lat_avg", ""),
                            t.user_attrs.get("lat_worst", ""),
                            t.user_attrs.get("lat_worst_ns_nominal", ""),
                            t.user_attrs.get("ii_min", ""),
                            t.user_attrs.get("ii_max", ""),
                            t.user_attrs.get("cp_synth_ns", ""),
                            t.user_attrs.get("cp_route_ns", ""),
                            t.user_attrs.get("est_slack_ns", ""),
                            t.user_attrs.get("est_violation_ns", ""),
                            t.user_attrs.get("est_timing_violation", ""),
                            t.user_attrs.get("area_score", ""),
                            t.user_attrs.get("area_source", ""),
                            t.user_attrs.get("lut", ""),
                            t.user_attrs.get("ff", ""),
                            t.user_attrs.get("dsp", ""),
                            t.user_attrs.get("bram", ""),
                            t.user_attrs.get("uram", ""),
                            t.user_attrs.get("elapsed_sec", ""),
                            t.value,
                            t.user_attrs.get("trial_dir", ""),
                            t.user_attrs.get("trial_log", ""),
                            t.user_attrs.get("syn_log", ""),
                            t.user_attrs.get("impl_log", ""),
                        ]
                    )
            passed_live = [t for t in trials_sorted_live if t.user_attrs.get("status") == "PASS"]
            with live_summary_path.open("w", encoding="utf-8") as f:
                f.write(f"total_trials={total_trials}\n")
                f.write(f"search_space_size={search_space_size}\n")
                f.write(f"finished_trials={len(trials_sorted_live)}\n")
                f.write(f"pass_trials={len(passed_live)}\n")
                f.write(f"sampler={args.sampler}\n")
                f.write(f"target_trials={target_trials}\n")
                f.write(f"live_update=1\n")
                f.write(f"threshold={args.threshold}\n")
                f.write(f"objective={args.objective}\n")
                if passed_live:
                    best_live = min(
                        passed_live,
                        key=lambda t: (
                            float(t.value) if t.value is not None else float("inf"),
                            t.params.get("iters", 10**9),
                            t.params.get("out_wl", 10**9),
                            t.params.get("int_wl", 10**9),
                            t.params.get("int_iwl", 10**9),
                        ),
                    )
                    f.write(
                        "best_pass="
                        f"iters={best_live.params.get('iters')} "
                        f"out_wl={best_live.params.get('out_wl')} "
                        f"out_iwl={args.out_iwl} "
                        f"int_wl={best_live.params.get('int_wl')} "
                        f"int_iwl={best_live.params.get('int_iwl')} "
                        f"value={best_live.value} "
                        f"ucb95={best_live.user_attrs.get('ucb95')} "
                        f"lat_worst={best_live.user_attrs.get('lat_worst')} "
                        f"area_score={best_live.user_attrs.get('area_score')} "
                        f"area_source={best_live.user_attrs.get('area_source')} "
                        f"lut={best_live.user_attrs.get('lut')} "
                        f"ff={best_live.user_attrs.get('ff')} "
                        f"dsp={best_live.user_attrs.get('dsp')} "
                        f"bram={best_live.user_attrs.get('bram')} "
                        f"uram={best_live.user_attrs.get('uram')}\n"
                    )
                else:
                    f.write("best_pass=NONE\n")

    def on_trial_complete(_study: optuna.Study, _trial: optuna.trial.FrozenTrial) -> None:
        write_live_csv_and_summary()

    def objective(trial: optuna.Trial) -> float:
        iters = int(trial.suggest_categorical("iters", search_space["iters"]))
        out_wl = int(trial.suggest_categorical("out_wl", search_space["out_wl"]))
        int_wl = int(trial.suggest_categorical("int_wl", search_space["int_wl"]))
        int_iwl = int(trial.suggest_categorical("int_iwl", search_space["int_iwl"]))
        out_iwl = args.out_iwl

        if int_wl <= int_iwl:
            trial.set_user_attr("status", "INVALID")
            trial.set_user_attr("reason", "int_wl must be greater than int_iwl")
            value = float(args.error_penalty)
            print_progress(trial.number, "INVALID", value)
            return value

        tag = (
            f"trial{trial.number:04d}_it{iters:02d}_owl{out_wl:02d}_oi{out_iwl}_"
            f"iw{int_wl:02d}_ii{int_iwl:02d}"
        )
        trial_dir = runs_dir / tag
        prepare_trial_dir(root, trial_dir, iters, out_wl, out_iwl, int_wl, int_iwl, args.samples)

        output = ""
        rc = 1
        start = time.time()
        for attempt in range(args.retry + 1):
            rc, output = run_csim_once(trial_dir, args.timeout_sec)
            if rc == 0:
                break
            if attempt < args.retry:
                time.sleep(1.0)

        elapsed = time.time() - start
        trial_log_path = logs_dir / f"trial{trial.number:04d}.log"
        trial_log_path.write_text(output, encoding="utf-8", errors="ignore")

        parsed = extract_result(output)
        status = "ERROR"
        syn_output = ""
        syn_rc = 0
        impl_output = ""
        impl_rc = 0
        lat_metrics = {
            "lat_best": None,
            "lat_avg": None,
            "lat_worst": None,
            "lat_worst_ns_nominal": None,
            "ii_min": None,
            "ii_max": None,
            "cp_synth_ns": None,
            "cp_route_ns": None,
            "est_slack_ns": None,
            "est_violation_ns": None,
            "est_timing_violation": None,
            "lut": None,
            "ff": None,
            "dsp": None,
            "bram": None,
            "uram": None,
            "avail_lut": None,
            "avail_ff": None,
            "avail_dsp": None,
            "avail_bram": None,
            "avail_uram": None,
            "area_score": None,
            "area_source": "",
        }
        if rc != 0 or parsed is None:
            status = "ERROR"
            trial.set_user_attr("status", status)
            trial.set_user_attr("return_code", rc)
            trial.set_user_attr("elapsed_sec", elapsed)
            value = float(args.error_penalty)
        else:
            pass_acc = parsed["ucb95"] < args.threshold
            status = "PASS" if pass_acc else "FAIL"
            if pass_acc and args.run_syn:
                syn_start = time.time()
                syn_rc = 1
                for attempt in range(args.retry + 1):
                    syn_rc, syn_output = run_syn_once(
                        trial_dir,
                        args.part,
                        args.clock_period_ns,
                        args.syn_timeout_sec,
                    )
                    if syn_rc == 0:
                        break
                    if attempt < args.retry:
                        time.sleep(1.0)
                elapsed += time.time() - syn_start
                syn_log_path = logs_dir / f"trial{trial.number:04d}_syn.log"
                syn_log_path.write_text(syn_output, encoding="utf-8", errors="ignore")
                trial.set_user_attr("syn_log", str(syn_log_path))
                if syn_rc != 0:
                    status = "ERROR_SYN"
                    trial.set_user_attr("syn_return_code", syn_rc)
                else:
                    lat_metrics = extract_latency_metrics(trial_dir)
                    if args.run_impl:
                        impl_start = time.time()
                        impl_rc = 1
                        for attempt in range(args.retry + 1):
                            impl_rc, impl_output = run_impl_once(trial_dir, args.impl_timeout_sec)
                            if impl_rc == 0:
                                break
                            if attempt < args.retry:
                                time.sleep(1.0)
                        elapsed += time.time() - impl_start
                        impl_log_path = logs_dir / f"trial{trial.number:04d}_impl.log"
                        impl_log_path.write_text(impl_output, encoding="utf-8", errors="ignore")
                        trial.set_user_attr("impl_log", str(impl_log_path))
                        if impl_rc != 0:
                            status = "ERROR_IMPL"
                            trial.set_user_attr("impl_return_code", impl_rc)
                        else:
                            lat_metrics = extract_latency_metrics(trial_dir)

            trial.set_user_attr("status", status)
            trial.set_user_attr("elapsed_sec", elapsed)
            trial.set_user_attr("mse", parsed["mse"])
            trial.set_user_attr("ucb95", parsed["ucb95"])
            trial.set_user_attr("N", parsed["N"])
            if lat_metrics["lat_worst"] is not None:
                lat_metrics["lat_worst_ns_nominal"] = float(lat_metrics["lat_worst"]) * float(args.clock_period_ns)
            if lat_metrics["cp_synth_ns"] is not None:
                est_slack_ns = float(args.clock_period_ns) - float(lat_metrics["cp_synth_ns"])
                lat_metrics["est_slack_ns"] = est_slack_ns
                lat_metrics["est_violation_ns"] = max(0.0, -est_slack_ns)
                lat_metrics["est_timing_violation"] = int(est_slack_ns < 0.0)
            trial.set_user_attr("lat_best", lat_metrics["lat_best"])
            trial.set_user_attr("lat_avg", lat_metrics["lat_avg"])
            trial.set_user_attr("lat_worst", lat_metrics["lat_worst"])
            trial.set_user_attr("lat_worst_ns_nominal", lat_metrics["lat_worst_ns_nominal"])
            trial.set_user_attr("ii_min", lat_metrics["ii_min"])
            trial.set_user_attr("ii_max", lat_metrics["ii_max"])
            trial.set_user_attr("cp_synth_ns", lat_metrics["cp_synth_ns"])
            trial.set_user_attr("cp_route_ns", lat_metrics["cp_route_ns"])
            trial.set_user_attr("est_slack_ns", lat_metrics["est_slack_ns"])
            trial.set_user_attr("est_violation_ns", lat_metrics["est_violation_ns"])
            trial.set_user_attr("est_timing_violation", lat_metrics["est_timing_violation"])
            lat_metrics["area_score"] = compute_area_score(lat_metrics, args)
            trial.set_user_attr("area_score", lat_metrics["area_score"])
            trial.set_user_attr("area_source", lat_metrics["area_source"])
            trial.set_user_attr("lut", lat_metrics["lut"])
            trial.set_user_attr("ff", lat_metrics["ff"])
            trial.set_user_attr("dsp", lat_metrics["dsp"])
            trial.set_user_attr("bram", lat_metrics["bram"])
            trial.set_user_attr("uram", lat_metrics["uram"])

            if args.objective == "latency":
                if status == "PASS" and lat_metrics["lat_worst"] is not None:
                    value = compute_latency_area_cost(
                        lat_metrics["lat_worst"],
                        lat_metrics["area_score"],
                        args,
                    )
                elif status == "FAIL":
                    thr = args.threshold if args.threshold > 0.0 else 1.0
                    gap = (parsed["ucb95"] / thr) - 1.0
                    if gap < 0.0:
                        gap = 0.0
                    value = float(args.fail_penalty_base) + float(gap)
                else:
                    value = float(args.error_penalty)
            else:
                if status == "PASS":
                    value = parsed["ucb95"]
                elif status == "FAIL":
                    value = parsed["ucb95"] + 1.0
                else:
                    value = float("inf")

        keep_trial_dir = (
            (save_mode == SaveRunsMode.ALL)
            or (save_mode == SaveRunsMode.ERRORS and status.startswith("ERROR"))
        )
        if keep_trial_dir:
            trial.set_user_attr("trial_dir", str(trial_dir))
        else:
            trial.set_user_attr("trial_dir", "")
            if trial_dir.exists():
                shutil.rmtree(trial_dir)

        trial.set_user_attr("trial_log", str(trial_log_path))
        print_progress(trial.number, status, value)
        return value

    study.optimize(
        objective,
        n_trials=max(0, target_trials - completed_before),
        n_jobs=max(1, args.jobs),
        callbacks=[on_trial_complete],
    )

    trials = [t for t in study.trials if t.state.is_finished()]
    trials_sorted = sorted(
        trials,
        key=lambda t: (
            t.params.get("iters", 10**9),
            t.params.get("out_wl", 10**9),
        ),
    )

    csv_path = results_dir / "sweep_results.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
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
        )
        for t in trials_sorted:
            w.writerow(
                [
                    t.number,
                    t.params.get("iters"),
                    t.params.get("out_wl"),
                    args.out_iwl,
                    t.params.get("int_wl"),
                    t.params.get("int_iwl"),
                    t.user_attrs.get("status", "UNKNOWN"),
                    t.user_attrs.get("mse", ""),
                    t.user_attrs.get("ucb95", ""),
                    t.user_attrs.get("N", ""),
                    t.user_attrs.get("lat_best", ""),
                    t.user_attrs.get("lat_avg", ""),
                    t.user_attrs.get("lat_worst", ""),
                    t.user_attrs.get("lat_worst_ns_nominal", ""),
                    t.user_attrs.get("ii_min", ""),
                    t.user_attrs.get("ii_max", ""),
                    t.user_attrs.get("cp_synth_ns", ""),
                    t.user_attrs.get("cp_route_ns", ""),
                    t.user_attrs.get("est_slack_ns", ""),
                    t.user_attrs.get("est_violation_ns", ""),
                    t.user_attrs.get("est_timing_violation", ""),
                    t.user_attrs.get("area_score", ""),
                    t.user_attrs.get("area_source", ""),
                    t.user_attrs.get("lut", ""),
                    t.user_attrs.get("ff", ""),
                    t.user_attrs.get("dsp", ""),
                    t.user_attrs.get("bram", ""),
                    t.user_attrs.get("uram", ""),
                    t.user_attrs.get("elapsed_sec", ""),
                    t.value,
                    t.user_attrs.get("trial_dir", ""),
                    t.user_attrs.get("trial_log", ""),
                    t.user_attrs.get("syn_log", ""),
                    t.user_attrs.get("impl_log", ""),
                ]
            )

    passed = [t for t in trials_sorted if t.user_attrs.get("status") == "PASS"]
    est_violation_count = sum(
        1
        for t in trials_sorted
        if t.user_attrs.get("est_timing_violation") in (1, "1", True)
    )

    pareto_x_used = args.pareto_x
    pareto_y_used = args.pareto_y
    if not metric_has_any_value(trials_sorted, pareto_x_used, args, args.pareto_include_fail):
        if metric_has_any_value(trials_sorted, "value", args, args.pareto_include_fail):
            pareto_x_used = "value"
    if not metric_has_any_value(trials_sorted, pareto_y_used, args, args.pareto_include_fail):
        if metric_has_any_value(trials_sorted, "ucb95", args, args.pareto_include_fail):
            pareto_y_used = "ucb95"
        elif metric_has_any_value(trials_sorted, "mse", args, args.pareto_include_fail):
            pareto_y_used = "mse"

    pareto_candidates = []
    for t in trials_sorted:
        status = t.user_attrs.get("status", "")
        if status == "PASS" or (args.pareto_include_fail and status == "FAIL"):
            x = trial_metric_value(t, pareto_x_used, args)
            y = trial_metric_value(t, pareto_y_used, args)
            if x is not None and y is not None:
                pareto_candidates.append(
                    {
                        "trial_number": t.number,
                        "status": status,
                        "x": x,
                        "y": y,
                        "iters": t.params.get("iters"),
                        "out_wl": t.params.get("out_wl"),
                        "out_iwl": args.out_iwl,
                        "int_wl": t.params.get("int_wl"),
                        "int_iwl": t.params.get("int_iwl"),
                        "mse": t.user_attrs.get("mse", ""),
                        "ucb95": t.user_attrs.get("ucb95", ""),
                        "N": t.user_attrs.get("N", ""),
                        "lat_best": t.user_attrs.get("lat_best", ""),
                        "lat_avg": t.user_attrs.get("lat_avg", ""),
                        "lat_worst": t.user_attrs.get("lat_worst", ""),
                        "lat_worst_ns_nominal": t.user_attrs.get("lat_worst_ns_nominal", ""),
                        "ii_min": t.user_attrs.get("ii_min", ""),
                        "ii_max": t.user_attrs.get("ii_max", ""),
                        "cp_synth_ns": t.user_attrs.get("cp_synth_ns", ""),
                        "cp_route_ns": t.user_attrs.get("cp_route_ns", ""),
                        "est_slack_ns": t.user_attrs.get("est_slack_ns", ""),
                        "est_violation_ns": t.user_attrs.get("est_violation_ns", ""),
                        "est_timing_violation": t.user_attrs.get("est_timing_violation", ""),
                        "area_score": t.user_attrs.get("area_score", ""),
                        "area_source": t.user_attrs.get("area_source", ""),
                        "lut": t.user_attrs.get("lut", ""),
                        "ff": t.user_attrs.get("ff", ""),
                        "dsp": t.user_attrs.get("dsp", ""),
                        "bram": t.user_attrs.get("bram", ""),
                        "uram": t.user_attrs.get("uram", ""),
                        "elapsed_sec": t.user_attrs.get("elapsed_sec", ""),
                        "value": t.value,
                    }
                )
    pareto_front = build_pareto_front(pareto_candidates)
    pareto_csv_path = results_dir / "pareto_points.csv"
    with pareto_csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
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
            ]
        )
        for p in pareto_front:
            w.writerow(
                [
                    p["trial_number"],
                    p["status"],
                    pareto_x_used,
                    p["x"],
                    pareto_y_used,
                    p["y"],
                    p["iters"],
                    p["out_wl"],
                    p["out_iwl"],
                    p["int_wl"],
                    p["int_iwl"],
                    p["mse"],
                    p["ucb95"],
                    p["N"],
                    p["lat_best"],
                    p["lat_avg"],
                    p["lat_worst"],
                    p["lat_worst_ns_nominal"],
                    p["ii_min"],
                    p["ii_max"],
                    p["cp_synth_ns"],
                    p["cp_route_ns"],
                    p["est_slack_ns"],
                    p["est_violation_ns"],
                    p["est_timing_violation"],
                    p["area_score"],
                    p["area_source"],
                    p["lut"],
                    p["ff"],
                    p["dsp"],
                    p["bram"],
                    p["uram"],
                    p["elapsed_sec"],
                    p["value"],
                ]
            )

    pareto_png_path = results_dir / "pareto.png"
    pareto_plot_status = "not_generated"
    if pareto_candidates:
        try:
            import matplotlib.pyplot as plt  # type: ignore

            xs = [p["x"] for p in pareto_candidates]
            ys = [p["y"] for p in pareto_candidates]
            fx = [p["x"] for p in pareto_front]
            fy = [p["y"] for p in pareto_front]

            plt.figure(figsize=(6.5, 4.8))
            plt.scatter(xs, ys, s=18, alpha=0.35, label="Candidates")
            if fx and fy:
                order = sorted(range(len(fx)), key=lambda i: fx[i])
                fx_sorted = [fx[i] for i in order]
                fy_sorted = [fy[i] for i in order]
                plt.plot(fx_sorted, fy_sorted, "-o", markersize=3.5, linewidth=1.0, label="Pareto front")
            plt.xlabel(pareto_x_used)
            plt.ylabel(pareto_y_used)
            plt.title("Pareto Front")
            plt.grid(True, linestyle="--", alpha=0.3)
            plt.legend()
            plt.tight_layout()
            plt.savefig(pareto_png_path, dpi=150)
            plt.close()
            pareto_plot_status = "ok"
        except Exception:
            pareto_plot_status = "matplotlib_unavailable_or_failed"

    summary_path = results_dir / "sweep_summary.txt"
    with summary_path.open("w", encoding="utf-8") as f:
        f.write(f"total_trials={total_trials}\n")
        f.write(f"search_space_size={search_space_size}\n")
        f.write(f"finished_trials={len(trials_sorted)}\n")
        f.write(f"pass_trials={len(passed)}\n")
        f.write(f"sampler={args.sampler}\n")
        f.write(f"target_trials={target_trials}\n")
        f.write(f"tpe_startup_trials={args.tpe_startup_trials}\n")
        f.write(f"tpe_candidates={args.tpe_candidates}\n")
        f.write(f"threshold={args.threshold}\n")
        f.write(f"objective={args.objective}\n")
        f.write(f"target_clock_ns={args.clock_period_ns}\n")
        f.write(f"latency_weight={args.latency_weight}\n")
        f.write(f"area_weight={args.area_weight}\n")
        f.write(f"latency_norm_ref={args.latency_norm_ref}\n")
        f.write(f"area_norm_ref={args.area_norm_ref}\n")
        f.write(
            f"area_weights=[lut:{args.area_lut_w},ff:{args.area_ff_w},dsp:{args.area_dsp_w},bram:{args.area_bram_w},uram:{args.area_uram_w}]\n"
        )
        f.write(f"run_syn={args.run_syn}\n")
        f.write(f"syn_timeout_sec={args.syn_timeout_sec}\n")
        f.write(f"run_impl={args.run_impl}\n")
        f.write(f"samples={args.samples}\n")
        f.write(f"study_name={args.study_name}\n")
        f.write(f"save_runs={args.save_runs}\n")
        f.write(f"search_iters=[{args.iters_min},{args.iters_max}]\n")
        f.write(f"search_out_wl=[{args.out_wl_min},{args.out_wl_max}]\n")
        f.write(f"search_int_wl={int_wl_values}\n")
        f.write(f"search_int_iwl={int_iwl_values}\n")
        f.write(f"pareto_x_requested={args.pareto_x}\n")
        f.write(f"pareto_y_requested={args.pareto_y}\n")
        f.write(f"pareto_x={pareto_x_used}\n")
        f.write(f"pareto_y={pareto_y_used}\n")
        f.write(f"pareto_include_fail={args.pareto_include_fail}\n")
        f.write(f"pareto_candidates={len(pareto_candidates)}\n")
        f.write(f"pareto_points={len(pareto_front)}\n")
        f.write(f"est_timing_violation_trials={est_violation_count}\n")
        f.write(f"pareto_csv={pareto_csv_path}\n")
        f.write(f"pareto_plot={pareto_png_path if pareto_plot_status == 'ok' else pareto_plot_status}\n")
        if passed:
            best = min(
                passed,
                key=lambda t: (
                    float(t.value) if t.value is not None else float("inf"),
                    t.params["iters"],
                    t.params["out_wl"],
                    t.params["int_wl"],
                    t.params["int_iwl"],
                ),
            )
            f.write(
                "best_pass="
                f"iters={best.params['iters']} "
                f"out_wl={best.params['out_wl']} "
                f"out_iwl={args.out_iwl} "
                f"int_wl={best.params['int_wl']} "
                f"int_iwl={best.params['int_iwl']} "
                f"mse={best.user_attrs.get('mse')} "
                f"ucb95={best.user_attrs.get('ucb95')} "
                f"lat_worst={best.user_attrs.get('lat_worst')} "
                f"lat_worst_ns_nominal={best.user_attrs.get('lat_worst_ns_nominal')} "
                f"est_slack_ns={best.user_attrs.get('est_slack_ns')} "
                f"est_violation_ns={best.user_attrs.get('est_violation_ns')} "
                f"est_timing_violation={best.user_attrs.get('est_timing_violation')} "
                f"area_score={best.user_attrs.get('area_score')} "
                f"area_source={best.user_attrs.get('area_source')} "
                f"lut={best.user_attrs.get('lut')} "
                f"ff={best.user_attrs.get('ff')} "
                f"dsp={best.user_attrs.get('dsp')} "
                f"bram={best.user_attrs.get('bram')} "
                f"uram={best.user_attrs.get('uram')} "
                f"ii=[{best.user_attrs.get('ii_min')},{best.user_attrs.get('ii_max')}] "
                f"cp_route_ns={best.user_attrs.get('cp_route_ns')}\n"
            )
        else:
            f.write("best_pass=NONE\n")

    print(f"Saved CSV: {csv_path}")
    print(f"Saved summary: {summary_path}")
    print(f"Saved Pareto CSV: {pareto_csv_path}")
    if pareto_plot_status == "ok":
        print(f"Saved Pareto plot: {pareto_png_path}")
    else:
        print(f"Pareto plot status: {pareto_plot_status}")
    if passed:
        best = min(
            passed,
            key=lambda t: (
                float(t.value) if t.value is not None else float("inf"),
                t.params["iters"],
                t.params["out_wl"],
                t.params["int_wl"],
                t.params["int_iwl"],
            ),
        )
        print(
            "BEST PASS "
            f"iters={best.params['iters']} out_wl={best.params['out_wl']} "
            f"out_iwl={args.out_iwl} int_wl={best.params['int_wl']} "
            f"int_iwl={best.params['int_iwl']} mse={best.user_attrs.get('mse')} "
            f"ucb95={best.user_attrs.get('ucb95')} lat_worst={best.user_attrs.get('lat_worst')} "
            f"lat_worst_ns_nominal={best.user_attrs.get('lat_worst_ns_nominal')} "
            f"est_slack_ns={best.user_attrs.get('est_slack_ns')} "
            f"est_violation_ns={best.user_attrs.get('est_violation_ns')} "
            f"est_timing_violation={best.user_attrs.get('est_timing_violation')} "
            f"area_score={best.user_attrs.get('area_score')} "
            f"area_source={best.user_attrs.get('area_source')} "
            f"lut={best.user_attrs.get('lut')} "
            f"ff={best.user_attrs.get('ff')} "
            f"dsp={best.user_attrs.get('dsp')} "
            f"bram={best.user_attrs.get('bram')} "
            f"uram={best.user_attrs.get('uram')} "
            f"ii=[{best.user_attrs.get('ii_min')},{best.user_attrs.get('ii_max')}] "
            f"cp_route_ns={best.user_attrs.get('cp_route_ns')}"
        )
    else:
        print("No passing configuration found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
