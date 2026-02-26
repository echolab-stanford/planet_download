"""Check for corrupted/unreadable TIF files across Planet mosaic directories."""

import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import rasterio


def check_tif(filepath: str) -> dict | None:
    """Try to open a TIF file with rasterio and return error info if it fails."""
    try:
        with rasterio.open(filepath) as src:
            # Try to actually read a small window to catch read errors
            src.read(1, window=rasterio.windows.Window(0, 0, 1, 1))
        return None
    except Exception as e:
        return {"file": filepath, "error": str(e)}


def main():
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "/mnt/sherlock/scratch/planet_data_sa_dhs"
    )
    nworkers = int(sys.argv[2]) if len(sys.argv) > 2 else 16

    # Collect all tif files from mosaic directories
    mosaic_dirs = sorted(d for d in data_dir.iterdir() if d.is_dir() and "mosaic" in d.name)
    print(f"Found {len(mosaic_dirs)} mosaic directories")

    all_tifs = []
    for d in mosaic_dirs:
        tifs = sorted(d.glob("*.tif"))
        all_tifs.extend(tifs)
    print(f"Found {len(all_tifs)} total TIF files to check")

    # Check files in parallel
    corrupted = []
    checked = 0
    with ProcessPoolExecutor(max_workers=nworkers) as executor:
        futures = {executor.submit(check_tif, str(f)): f for f in all_tifs}
        for future in as_completed(futures):
            checked += 1
            if checked % 5000 == 0:
                print(f"  Checked {checked}/{len(all_tifs)} files, {len(corrupted)} corrupted so far...")
            result = future.result()
            if result is not None:
                corrupted.append(result)

    # Summary
    print(f"\n{'='*80}")
    print(f"RESULTS: Checked {len(all_tifs)} files, found {len(corrupted)} corrupted/unreadable")
    print(f"{'='*80}")

    if corrupted:
        # Group by mosaic directory
        by_dir = {}
        for entry in corrupted:
            parent = os.path.basename(os.path.dirname(entry["file"]))
            by_dir.setdefault(parent, []).append(entry)

        print(f"\nCorrupted files by mosaic directory:")
        for dirname in sorted(by_dir):
            print(f"\n  {dirname}: {len(by_dir[dirname])} file(s)")
            for entry in sorted(by_dir[dirname], key=lambda x: x["file"]):
                print(f"    {os.path.basename(entry['file'])}: {entry['error'][:120]}")

        # Save full report
        report_path = data_dir / "corrupted_files_report.txt"
        with open(report_path, "w") as f:
            f.write(f"Corrupted/unreadable TIF files report\n")
            f.write(f"Total files checked: {len(all_tifs)}\n")
            f.write(f"Corrupted files: {len(corrupted)}\n\n")
            for entry in sorted(corrupted, key=lambda x: x["file"]):
                f.write(f"{entry['file']}\n  Error: {entry['error']}\n\n")
        print(f"\nFull report saved to: {report_path}")


if __name__ == "__main__":
    main()
