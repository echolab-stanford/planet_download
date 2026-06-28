# `infrastructure/` — container definitions

Apptainer/Singularity definitions for running this project on HPC (Sherlock)
without hand-building a Python environment on the cluster.

## `geospatial.def`

A self-contained image with a **uv-managed** Python 3.12 environment holding the
full geospatial stack (`rasterio`, `geopandas`, `shapely`, `pyproj`, `rio-cogeo`,
`numpy`, `pandas`, `pillow`, …). Installed from PyPI manylinux wheels, which
bundle GDAL/GEOS/PROJ — **no system GDAL needed**. The env lives at `/opt/venv`
and is on `PATH`, so plain `python` resolves to it (no conda-style activation).

The repo code is **not** baked in — bind-mount your checkout at runtime so you
can `git pull`/edit without rebuilding.

### Build (once)

Run on a compute/`sdev` node with internet — not a login node — and put the
Apptainer cache/tmp on `$SCRATCH` so they don't fill `$HOME`:

```bash
ml system apptainer
export APPTAINER_CACHEDIR=$SCRATCH/.apptainer/cache
export APPTAINER_TMPDIR=$SCRATCH/.apptainer/tmp
mkdir -p "$APPTAINER_CACHEDIR" "$APPTAINER_TMPDIR"

apptainer build --fakeroot $SCRATCH/geospatial.sif infrastructure/geospatial.def
```

If `--fakeroot` is disabled on the cluster, build the `.sif` on a machine where
you have Docker/root and `scp` it to `$SCRATCH`.

### Verify

```bash
apptainer exec $SCRATCH/geospatial.sif \
  python -c "import rasterio, geopandas, numpy, PIL; print('ok')"
```

### Run patch extraction

```bash
apptainer exec --cleanenv --bind $SCRATCH $SCRATCH/geospatial.sif \
  python $SCRATCH/planet_download/scripts/patchify.py \
    --src $SCRATCH/planet_dhs_sa_africa --out $SCRATCH/planet_patches \
    --workers 48 --grid-deg 1 --shard-size 512 --min-valid 0.5
```

Wrap it in `sbatch` for the real run (`-c 48 --mem=64G -t 08:00:00`), and run on
a **compute node** — login nodes have low per-process limits (a too-low
open-file limit there shows up as `OSError: [Errno 24] Too many open files`
during heavy geospatial imports).

To patch the **cloud-reduced per-year composites** instead, use
`scripts/patchify_composite.py` (`--src $SCRATCH/planet_composite_sa_dhs
--crosswalks $SCRATCH/planet_dhs_sa_africa`). The submit jobs are
`run_patchify.sbatch` (raw) and `run_patchify_composite.sbatch` (composites);
the latter uses `--mem=96G` since composites are float64 (~4× the raw RAM):

```bash
sbatch infrastructure/run_patchify_composite.sbatch                       # full run (~1.2M patches)
sbatch --export=ALL,SAMPLE_FRAC=0.02 infrastructure/run_patchify_composite.sbatch  # smoke test
sbatch --export=ALL,RESUME=1 infrastructure/run_patchify_composite.sbatch          # resume
```

To compute the per-band normalization stats (mean/std/min/max over the
composites, RGB by default), use `run_band_stats.sbatch` → writes
`band_stats.json` + `band_stats.npz` next to the composites. Composite stats
equal the patch stats (verified bit-exact), so this avoids reading the ~1.2M
PNGs:

```bash
sbatch infrastructure/run_band_stats.sbatch
```

### Notes

- The dependency list in `geospatial.def` mirrors `pyproject.toml` (plus
  `pillow`). For byte-for-byte reproducibility, pin versions in the def or feed
  `uv pip install` a constraints file / lockfile.
- The build smoke-tests every import, so a broken environment fails the build
  rather than a job hours into a run.
