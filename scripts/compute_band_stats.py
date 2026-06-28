"""Compute dataset-level per-band mean/std for normalizing inputs at train /
inference time.

rasterio-based (runs in the geospatial Apptainer container, which bundles GDAL
via the rasterio wheel but does NOT ship the `osgeo` Python bindings). Stats are
exact: each worker streams a file's pixels into float64 sum / sum-of-squares /
count accumulators, and the partials are combined.

The Planet composites are 4-band RGBA; band 4 is an all-255 coverage mask (NOT a
cloud mask), so the default `--bands 1,2,3` computes RGB-only stats. With
`--round-uint8` (default) the float64 composite values are rounded/clipped to
uint8 first, so the result is byte-for-byte the stats of the extracted PNG
patches -- verified identical on a full quad. There is no per-pixel alpha
masking: patchify keeps every pixel of a kept patch, so do we.

Writes the per-band descriptive stats (mean, std, min, max, count) to
`--output-json` and to a sibling `.npz` (same stem) for direct loading at
train / inference time.

Usage (inside the container):
    python compute_band_stats.py \\
        --input-path $SCRATCH/planet_composite_sa_dhs \\
        --output-json $SCRATCH/planet_composite_sa_dhs/band_stats.json \\
        --pattern '*_composite.tif' --bands 1,2,3 --workers 32
"""

import json
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

import click
import numpy as np
import rasterio
from tqdm import tqdm


def _file_partials(args):
    """Return (sum, sumsq, count, min, max) per band for one raster (float64)."""
    path, bands, round_uint8 = args
    with rasterio.open(path) as ds:
        a = ds.read(bands)  # (B, H, W) in native dtype
    a = a.astype(np.float64, copy=False)
    if round_uint8:
        a = np.clip(np.rint(a), 0, 255)
    flat = a.reshape(len(bands), -1)
    s = flat.sum(axis=1)
    sq = np.square(flat).sum(axis=1)
    n = float(flat.shape[1])
    return s, sq, n, flat.min(axis=1), flat.max(axis=1)


def compute_band_stats(paths, bands, round_uint8, workers):
    """Exact per-band descriptive stats over all pixels of all `paths` (parallel)."""
    nb = len(bands)
    total_sum = np.zeros(nb, dtype=np.float64)
    total_sq = np.zeros(nb, dtype=np.float64)
    total_n = 0.0
    total_min = np.full(nb, np.inf, dtype=np.float64)
    total_max = np.full(nb, -np.inf, dtype=np.float64)

    work = [(p, bands, round_uint8) for p in paths]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_file_partials, w) for w in work]
        for f in tqdm(as_completed(futs), total=len(futs), desc="band stats"):
            s, sq, n, mn, mx = f.result()
            total_sum += s
            total_sq += sq
            total_n += n
            total_min = np.minimum(total_min, mn)
            total_max = np.maximum(total_max, mx)

    means = total_sum / total_n
    variances = np.maximum(total_sq / total_n - np.square(means), 0.0)
    stds = np.sqrt(variances)
    return {
        "mean": means.tolist(),
        "std": stds.tolist(),
        "min": total_min.tolist(),
        "max": total_max.tolist(),
        "bands": list(bands),
        "n_files": len(paths),
        "n_pixels": int(total_n),
        "round_uint8": round_uint8,
    }


@click.command()
@click.option(
    "--input-path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Directory containing raster files (searched recursively).",
)
@click.option(
    "--output-json",
    type=click.Path(file_okay=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to write output JSON with mean/std per band.",
)
@click.option(
    "--pattern",
    type=str,
    default="*.tif",
    show_default=True,
    help="Glob pattern for rasters inside input-path.",
)
@click.option(
    "--bands",
    type=str,
    default="1,2,3",
    show_default=True,
    help="Comma-separated 1-based band indices (default RGB; skips the alpha mask).",
)
@click.option(
    "--round-uint8/--no-round-uint8",
    default=True,
    show_default=True,
    help="Round/clip to uint8 before accumulating (matches the PNG patches exactly).",
)
@click.option(
    "--workers",
    type=int,
    default=0,
    show_default=True,
    help="Parallel workers (0 = os.cpu_count()).",
)
def main(input_path, output_json, pattern, bands, round_uint8, workers):
    """Compute dataset-level per-band mean/std and save as JSON."""
    import os

    band_idx = tuple(int(b) for b in bands.split(","))
    paths = sorted(input_path.rglob(pattern))
    if not paths:
        raise click.UsageError(
            f"No files matching pattern '{pattern}' found in {input_path}."
        )
    workers = workers or os.cpu_count() or 1

    stats = compute_band_stats(paths, band_idx, round_uint8, workers)

    output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(output_json, "w") as f:
        json.dump(stats, f, indent=2)

    # Same descriptive stats as arrays, for direct loading at train/inference time.
    output_npz = output_json.with_suffix(".npz")
    np.savez(
        output_npz,
        mean=np.asarray(stats["mean"], dtype=np.float64),
        std=np.asarray(stats["std"], dtype=np.float64),
        min=np.asarray(stats["min"], dtype=np.float64),
        max=np.asarray(stats["max"], dtype=np.float64),
        bands=np.asarray(stats["bands"], dtype=np.int64),
        n_files=np.int64(stats["n_files"]),
        n_pixels=np.int64(stats["n_pixels"]),
        round_uint8=np.bool_(stats["round_uint8"]),
    )

    click.echo(f"Processed {stats['n_files']} files, {stats['n_pixels']:,} px/band")
    click.echo(f"bands: {stats['bands']}  round_uint8: {round_uint8}")
    click.echo(f"Saved stats to: {output_json} and {output_npz}")
    click.echo(f"mean: {stats['mean']}")
    click.echo(f"std: {stats['std']}")


if __name__ == "__main__":
    main()
