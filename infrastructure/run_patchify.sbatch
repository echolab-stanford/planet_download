#!/bin/bash
# Slurm job: run patchify.py inside the geospatial Apptainer image on Sherlock.
#
# Submit (fresh run):
#   sbatch infrastructure/run_patchify.sbatch
# Resume after a timeout/crash (reuse the SAME settings):
#   sbatch --export=ALL,RESUME=1 infrastructure/run_patchify.sbatch
# Sample 10% instead of the full ~3.4M patches:
#   sbatch --export=ALL,SAMPLE_FRAC=0.1 infrastructure/run_patchify.sbatch
#
# -c must match WORKERS. Bump -t/--mem for the full (sample_frac=1.0) run.

#SBATCH --job-name=patchify
#SBATCH --partition=serc
#SBATCH --cpus-per-task=48
#SBATCH --mem=64G
#SBATCH --time=12:00:00
#SBATCH --output=patchify_%j.log
#SBATCH --open-mode=append
#SBATCH --mail-user=ihigueme@stanford.edu
#SBATCH --mail-type=ALL

set -euo pipefail
ml system apptainer

# --- config (override on the sbatch command line via --export=ALL,VAR=...) ---
SIF=${SIF:-$SCRATCH/geospatial.sif}
SRC=${SRC:-$SCRATCH/planet_dhs_sa_africa}
OUT=${OUT:-$SCRATCH/planet_patches}
REPO=${REPO:-$SCRATCH/planet_download}
WORKERS=${WORKERS:-48}
GRID_DEG=${GRID_DEG:-1}
SHARD_SIZE=${SHARD_SIZE:-512}
MIN_VALID=${MIN_VALID:-0.5}
SAMPLE_FRAC=${SAMPLE_FRAC:-1.0}
SEED=${SEED:-0}
RESUME=${RESUME:-0}

args=(--src "$SRC" --out "$OUT" --workers "$WORKERS" --grid-deg "$GRID_DEG"
      --shard-size "$SHARD_SIZE" --min-valid "$MIN_VALID"
      --sample-frac "$SAMPLE_FRAC" --seed "$SEED")
[ "$RESUME" = "1" ] && args+=(--resume)

echo "host=$(hostname) workers=$WORKERS sample_frac=$SAMPLE_FRAC resume=$RESUME"
echo "src=$SRC out=$OUT"

apptainer exec --cleanenv --bind "$SCRATCH" "$SIF" \
  python "$REPO/scripts/patchify.py" "${args[@]}"
