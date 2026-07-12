"""Backward-compatibility shim - the CLI now lives in ``planet_download.cli``.

All commands (download, download-aoi, composite, resample, merge, grid-mosaic,
ps-search, ps-download, ps-composite, download-all) are available both here and
through the installed ``planet-download`` entry point (e.g. via ``uvx``).
"""

from planet_download.cli import cli

if __name__ == "__main__":
    cli()
