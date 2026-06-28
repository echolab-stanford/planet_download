#!/bin/bash
# Slurm job: run compute_band_stats.py inside the geospatial Apptainer image on
# Sherlock. Computes exact per-band mean/std/min/max over the composites (RGB by
# default) for input normalization -- see scripts/compute_band_stats.py.
#
# Submit:
#   sbatch infrastructure/run_band_stats.sbatch
# Stats over the raw quads instead:
#   sbatch --export=ALL,SRC=$SCRATCH/planet_dhs_sa_africa,PATTERN=*.tif \
#          infrastructure/run_band_stats.sbatch
# NOTE: --export values cannot contain commas, so to change BANDS (default 1,2,3)
# edit the default below rather than passing it via --export.
#
# -c must match WORKERS. Composites are float64 (~4x the raw uint8 RAM).

#SBATCH --job-name=band_stats
#SBATCH --partition=serc
#SBATCH --cpus-per-task=32
#SBATCH --mem=96G
#SBATCH --time=04:00:00
#SBATCH --output=band_stats_%j.log
#SBATCH --open-mode=append
#SBATCH --mail-user=ihigueme@stanford.edu
#SBATCH --mail-type=ALL

set -euo pipefail
ml system apptainer

# --- config (override on the sbatch command line via --export=ALL,VAR=...) ---
SIF=${SIF:-$SCRATCH/geospatial.sif}
SRC=${SRC:-$SCRATCH/planet_composite_sa_dhs}
OUT=${OUT:-$SCRATCH/planet_composite_sa_dhs/band_stats.json}
REPO=${REPO:-$SCRATCH/planet_download}
WORKERS=${WORKERS:-32}
PATTERN=${PATTERN:-*_composite.tif}
BANDS=${BANDS:-1,2,3}
ROUND_UINT8=${ROUND_UINT8:-1}

args=(--input-path "$SRC" --output-json "$OUT" --pattern "$PATTERN"
      --bands "$BANDS" --workers "$WORKERS")
[ "$ROUND_UINT8" = "1" ] && args+=(--round-uint8) || args+=(--no-round-uint8)

echo "host=$(hostname) workers=$WORKERS pattern=$PATTERN bands=$BANDS round_uint8=$ROUND_UINT8"
echo "src=$SRC out=$OUT (+ sibling .npz)"

apptainer exec --cleanenv --bind "$SCRATCH" "$SIF" \
  python "$REPO/scripts/compute_band_stats.py" "${args[@]}"
