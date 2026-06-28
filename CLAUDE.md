# CLAUDE.md

Guidance for working in this repository.

## What this project is

`planet_download` is a Python library + CLI for downloading **Planet Basemaps**
(Global Quarterly mosaics), building composites, and extracting image patches for
ML. The active use case is generating **512×512 RGB patches** from a sub-Saharan
Africa dataset (DHS survey clusters, 2017–2024) for **unsupervised embedding
training**.

## Layout

- `planet_download/` — the package
  - `client.py` — standalone Planet Basemaps API client (`BasemapsClient`,
    `MosaicSeries`, `Mosaic`, `MosaicQuad`). Adapted from Planet's official
    `basemaps_client.py`. Uses `https://api.planet.com/basemaps/v1`, auth = `(api_key, "")`.
  - `cli.py` — `planet-download download-all` CLI.
  - `composites.py` — mean/median temporal composites (COG output), resampling,
    merging.
- `main/download_and_process.py` — `download` + `composite` CLI over a label CSV.
- `scripts/`
  - `patchify.py` — extract 512×512 RGB patches from the **raw per-quarter**
    quads → uniform shard folders + `manifest.csv` (see below).
  - `patchify_composite.py` — same, but from the **per-year composites**
    (cloud-reduced); preferred input. See below.
  - `compute_band_stats.py` — exact per-band mean/std/min/max over the composites
    (RGB by default) for input normalization; writes JSON + a sibling `.npz`.
    rasterio-based (the container has no `osgeo`). Composite stats == patch stats
    (verified bit-exact), so no need to read the 1.2M PNGs.
  - `check_corrupted_tifs.py`, `redownload_corrupted.py` — TIF QA utilities.
- `infrastructure/` — Apptainer/Singularity container (`geospatial.def`) + Slurm
  jobs (`run_patchify.sbatch` raw, `run_patchify_composite.sbatch` composites,
  `run_band_stats.sbatch` normalization stats) for running on Sherlock. See its
  README.
- `data/` — cached dataset-audit outputs (size/band inventory, coverage stats,
  maps). See `data/README.md` for provenance. Don't recompute these; reuse them.
- `reports/` — self-contained HTML coverage report.
- `examples/` — notebooks + AOIs + `dhs_sa_africa_cluster.csv` (the label file).

## The dataset (`planet_dhs_sa_africa`)

- Location: `/mnt/sherlock/oak/embed_develop/data/raw/planet/planet_dhs_sa_africa`
  (copied to `$SCRATCH` for fast HPC runs).
- Planet **Global Quarterly** mosaics, years **2017–2024**, quarters **q1/q2/q3**.
- **58,379** quad GeoTIFFs, all **4-band RGBA `uint8`, EPSG:3857, ~4.78 m/px**.
- Two pixel sizes: **4096×4096** (most) and **2048×2048** (2017 q1 & q2 only).
- **5 corrupt tiles** in `global_quarterly_2022q3_mosaic` (listed in
  `scripts/redownload_corrupted.py`); they cannot be re-fetched (see API note).
- Per-year `crosswalk_quads_planet_<year>.geojson` map each quad → DHS clusters.
- Gross 512² patch count ≈ **3.4M** (before `--min-valid` filtering).

## The composites (`planet_composite_sa_dhs`)

- Location: `/mnt/sherlock/oak/embed_develop/data/intermediate/planet/planet_composite_sa_dhs`
  (copied to `$SCRATCH/planet_composite_sa_dhs` for HPC runs).
- **Per-year** temporal composites (q1/q2/q3 collapsed into one image), named
  `<YYYY>/<x>_<y>_composite.tif`. Same quad grid/ids as the raw dataset.
- **21,701** quads, **4-band RGBA `float64`** (values ~5–255), EPSG:3857,
  ~4.78 m/px, 512² internal blocks. Sizes: 4096² (18,067) + 2048² (3,634, mostly
  2017). Alpha is **255 everywhere** (full coverage) — so `--min-valid` is a
  near-no-op here.
- **Effectively cloud-free**: random sampling shows a median cloud-like-pixel
  fraction of ~0% (max ~0.03%). This is the cloud fix — prefer composites over
  raw quads for patching.
- Gross 512² patch count = **1,214,432** (4096²→64 patches, 2048²→16; ≈ the kept
  count since coverage is full). Reuses the raw dataset's crosswalks.

## Key gotchas (read before debugging)

- **The 4th band is a no-data/coverage mask, NOT a cloud mask** — it's ~255
  everywhere covered. It cannot be used to remove clouds.
- **Basemaps are not perfectly cloud-free.** Residual clouds/haze remain. Reduce
  them with a temporal **median composite** across q1/q2/q3 (`composites.py`),
  not the alpha band.
- **The Planet API key no longer has a Basemaps/Mosaics entitlement.** The
  Basemaps API still authenticates (HTTP 200) but returns empty series/mosaics.
  Data API (scenes) still works. The data is already downloaded; re-downloads
  (incl. the 5 corrupt tiles) are not currently possible.
- **Sherlock login nodes have a low open-file limit** → `OSError: [Errno 24] Too
  many open files` on heavy geospatial imports. Run on a **compute node**
  (`sh_dev`/sbatch), not the login node.

## Running on Sherlock (Apptainer)

The geospatial stack is delivered as a uv-built container — no conda, no system
GDAL (wheels bundle GDAL/GEOS/PROJ; the def adds `libexpat1`):

```bash
ml system apptainer
export APPTAINER_CACHEDIR=$SCRATCH/.apptainer/cache APPTAINER_TMPDIR=$SCRATCH/.apptainer/tmp
apptainer build --fakeroot $SCRATCH/geospatial.sif infrastructure/geospatial.def
sbatch infrastructure/run_patchify.sbatch          # full run
sbatch --export=ALL,SAMPLE_FRAC=0.02 infrastructure/run_patchify.sbatch  # smoke test
sbatch --export=ALL,RESUME=1 infrastructure/run_patchify.sbatch          # resume
```

Inside the container, plain `python` is the uv venv (`/opt/venv/bin`, on PATH via
`%environment`) — no `uv run` needed.

## patchify.py

- Output: `<out>/shard_<WW>_<NNNNN>/<BLOCK>_<YYYYqQ>_<x>_<y>_<rowcol>.png` (uniform
  `--shard-size` folders) + `manifest.csv` (provenance + contained DHS clusters).
- RGB only (alpha used solely to drop near-empty patches via `--min-valid`).
- `--sample-frac`/`--seed`: deterministic, run-stable subsampling.
- `--resume`: per-worker checkpoints + manifest fragments; skips finished quads,
  reproduces a clean run byte-for-byte, dedupes manifest by path. A resumed run
  must reuse the SAME `--workers/--grid-deg/--shard-size/--sample-frac/--seed`.

## patchify_composite.py

Composite-input sibling of `patchify.py` (preferred — cloud-reduced). Same shard
scheme, deterministic sampling, and resume semantics. Differences:

- Source layout `<src>/<YYYY>/<x>_<y>_composite.tif`; crosswalks live separately
  (`--crosswalks`, defaults to `--src`), keyed by `(year, x-y)` with clusters
  aggregated across quarters.
- Composites are float64 → rounded/clipped to uint8 before PNG.
- No quarter dimension: keys/filenames are `<BLOCK>_<YYYY>_<x>_<y>_<rowcol>`;
  manifest has `year` (no `quarter`) plus a `cloud_frac` column.
- `--max-cloud` drops patches whose cloud-like-pixel fraction exceeds the
  threshold (1.0 = keep all). Cheap safety net; drops ~nothing on these
  composites. A resumed run must also reuse the SAME `--max-cloud`.

## Local dev environment (uv)

On Sherlock, prefer prebuilt wheels and don't load `gdal`/`proj` modules:
```bash
module purge
uv python install 3.12
uv sync --no-install-project --no-build   # wheels only; skip building the local pkg
```
The package itself is standalone for scripts — `patchify.py` imports only
`rasterio`, `numpy`, `PIL`, `geopandas`.

## Conventions

- **Git:** work on a branch, open a PR, merge to `main` (the user pulls `main` on
  Sherlock). Don't push directly to `main`. Commit trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Lint:** `ruff` with default rules (`ruff check` must pass; `ruff format`).
- **Packaging:** `uv` + `pyproject.toml` (hatchling). Runtime deps live there.
- The user has unrelated work-in-progress in `examples/` and `planet_download/`;
  stage only the files relevant to the task at hand.
