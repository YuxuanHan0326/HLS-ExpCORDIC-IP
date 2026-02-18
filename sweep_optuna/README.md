# Optuna Sweep for `exp_cordic_ip`

This folder contains the Optuna-based full-grid `csim` sweep flow and outputs.

## Files
- `run_optuna_sweep.py`: main driver
- `runs/`: per-trial copied sources and logs
- `results/sweep_results.csv`: per-trial metrics
- `results/sweep_summary.txt`: compact summary and best passing config
- `results/optuna_study.db`: Optuna study database

## Install
```powershell
python -m pip install optuna
```

## Run full sweep
```powershell
python sweep_optuna/run_optuna_sweep.py
```
or
```powershell
.\sweep_optuna\run_full.ps1
```

Use parallel workers:
```powershell
python sweep_optuna/run_optuna_sweep.py --jobs 16
```

Defaults:
- `ITERS`: 10..20
- `OUT_WL`: 13..24
- `OUT_IWL`: 1
- `INT_WL`: 24,26,28,30,32,34,36,38,40,42
- `INT_IWL`: 4,5,6,7
- `objective`: latency
- `run-syn`: enabled for latency extraction
- `N`: 200000
- threshold: `ucb95 < 2.4e-11`
- `save-runs`: `errors` (only keep trial directories for failed/error runs)

Override internal-width search points:
```powershell
python sweep_optuna/run_optuna_sweep.py --int-wl-values 32,36,40 --int-iwl-values 5,6
```

Control disk usage:
```powershell
python sweep_optuna/run_optuna_sweep.py --save-runs none
```
This keeps only `results/*.csv`, `results/*.txt`, `results/optuna_study.db`, and `results/trial_logs/*.log`.

Latency-oriented run:
```powershell
python sweep_optuna/run_optuna_sweep.py --objective latency --run-syn --save-runs none
```
The script extracts latency and II from `hls/syn/report/exp_cordic_ip_csynth.xml`.

Optional post-route timing:
```powershell
python sweep_optuna/run_optuna_sweep.py --objective latency --run-syn --run-impl
```

## Smoke test (quick)
```powershell
python sweep_optuna/run_optuna_sweep.py --iters-min 20 --iters-max 20 --out-wl-min 32 --out-wl-max 32 --samples 20000 --limit-trials 1
```
