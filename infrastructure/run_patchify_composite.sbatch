#!/bin/bash
# Slurm job: run patchify_composite.py inside the geospatial Apptainer image on
# Sherlock. Patches the per-year *composite* quads (cloud-reduced) instead of the
# raw per-quarter quads -- see scripts/patchify_composite.py.
#
# Submit (fresh run):
#   sbatch infrastructure/run_patchify_composite.sbatch
# Resume after a timeout/crash (reuse the SAME settings):
#   sbatch --export=ALL,RESUME=1 infrastructure/run_patchify_composite.sbatch
# Sample 10% instead of the full ~1.2M patches:
#   sbatch --export=ALL,SAMPLE_FRAC=0.1 infrastructure/run_patchify_composite.sbatch
# Drop the worst residual-cloud patches:
#   sbatch --export=ALL,MAX_CLOUD=0.2 infrastructure/run_patchify_composite.sbatch
#
# -c must match WORKERS. Composites are float64 (~4x the raw uint8 RAM), hence the
# higher --mem than run_patchify.sbatch.

#SBATCH --job-name=patchify_composite
#SBATCH --partition=serc
#SBATCH --cpus-per-task=48
#SBATCH --mem=96G
#SBATCH --time=12:00:00
#SBATCH --output=patchify_composite_%j.log
#SBATCH --open-mode=append
#SBATCH --mail-user=ihigueme@stanford.edu
#SBATCH --mail-type=ALL

set -euo pipefail
ml system apptainer

# --- config (override on the sbatch command line via --export=ALL,VAR=...) ---
SIF=${SIF:-$SCRATCH/geospatial.sif}
SRC=${SRC:-$SCRATCH/planet_composite_sa_dhs}
CROSSWALKS=${CROSSWALKS:-$SCRATCH/planet_dhs_sa_africa}
OUT=${OUT:-$SCRATCH/planet_patches_composite}
REPO=${REPO:-$SCRATCH/planet_download}
WORKERS=${WORKERS:-48}
GRID_DEG=${GRID_DEG:-1}
SHARD_SIZE=${SHARD_SIZE:-512}
MIN_VALID=${MIN_VALID:-0.5}
MAX_CLOUD=${MAX_CLOUD:-1.0}
SAMPLE_FRAC=${SAMPLE_FRAC:-1.0}
SEED=${SEED:-0}
RESUME=${RESUME:-0}

args=(--src "$SRC" --crosswalks "$CROSSWALKS" --out "$OUT" --workers "$WORKERS"
      --grid-deg "$GRID_DEG" --shard-size "$SHARD_SIZE" --min-valid "$MIN_VALID"
      --max-cloud "$MAX_CLOUD" --sample-frac "$SAMPLE_FRAC" --seed "$SEED")
[ "$RESUME" = "1" ] && args+=(--resume)

echo "host=$(hostname) workers=$WORKERS sample_frac=$SAMPLE_FRAC max_cloud=$MAX_CLOUD resume=$RESUME"
echo "src=$SRC crosswalks=$CROSSWALKS out=$OUT"

apptainer exec --cleanenv --bind "$SCRATCH" "$SIF" \
  python "$REPO/scripts/patchify_composite.py" "${args[@]}"
