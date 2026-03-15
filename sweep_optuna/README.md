# Optuna Sweep for `exp_cordic_ip`

This folder contains the design-space exploration (DSE) flow for `exp_cordic_ip`.

## Files
- `run_optuna_sweep.py`: main sweep driver
- `run_full.ps1`: one-shot full run launcher
- `run_topk_impl.py`: optional top-k implementation flow
- `runs/`: per-trial working directories (can be auto-cleaned)
- `results/`: generated sweep outputs (ignored by git)

## Install
```powershell
python -m pip install optuna
```

## Current default search space (`run_optuna_sweep.py`)
- `ITERS`: `16..20`
- `OUT_WL`: `19..26`
- `OUT_IWL`: `1` (fixed)
- `INT_WL`: `19,20,21,22,23,24,25,26`
- `INT_IWL`: `3,4,5,6`
- Total combinations: `1280`

## Objective and constraints (defaults)
- `objective`: `latency`
- `run-syn`: enabled by default in the full flow
- `samples` (`N`): `200000`
- accuracy threshold: `ucb95 < 2.4e-11`
- weighted latency-area cost: `0.9 : 0.1`

## Sampler and trial control
- Supported samplers: `grid`, `tpe`
- `--limit-trials 0` means **run all points** (both grid and tpe modes)
- TPE defaults:
  - `--tpe-startup-trials 192`
  - `--tpe-candidates 128`

## Full run (recommended)
```powershell
powershell -ExecutionPolicy Bypass -File .\sweep_optuna\run_full.ps1
```

Current `run_full.ps1` defaults:
- `Sampler=grid`
- `LimitTrials=0` (full space)
- `Jobs=20`
- auto cleanup enabled:
  - `--reset-study-on-start`
  - `--clean-runs-on-start`
  - `--save-runs none`

## Common custom runs

Run with TPE:
```powershell
python .\sweep_optuna\run_optuna_sweep.py --sampler tpe --limit-trials 800 --jobs 20
```

Run with custom search points:
```powershell
python .\sweep_optuna\run_optuna_sweep.py --int-wl-values 21,22,23,24 --int-iwl-values 3,4,5
```

Run with implementation step:
```powershell
python .\sweep_optuna\run_optuna_sweep.py --objective latency --run-syn --run-impl
```
