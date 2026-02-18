param(
  [ValidateSet('tpe','grid')]
  [string]$Sampler = 'grid',
  [int]$LimitTrials = 0,
  [int]$Jobs = 20
)

python .\sweep_optuna\run_optuna_sweep.py `
  --iters-min 16 --iters-max 20 `
  --out-wl-min 19 --out-wl-max 26 `
  --out-iwl 1 `
  --int-wl-values 19,20,21,22,23,24,25,26 `
  --int-iwl-values 3,4,5,6 `
  --sampler $Sampler `
  --limit-trials $LimitTrials `
  --tpe-startup-trials 192 `
  --tpe-candidates 128 `
  --objective latency `
  --run-syn `
  --latency-weight 0.9 `
  --area-weight 0.1 `
  --samples 200000 `
  --jobs $Jobs `
  --save-runs none `
  --reset-study-on-start `
  --clean-runs-on-start
