"""Re-download specific corrupted quads by mosaic name and quad ID."""

from planet_download.client import BasemapsClient

# Corrupted files: mosaic_name -> list of (x, y) quad coordinates
CORRUPTED = {
    "global_quarterly_2022q3_mosaic": [
        (1208, 883),
        (1209, 883),
        (1211, 883),
        (1212, 883),
        (1222, 883),
    ],
    "global_quarterly_2024q1_mosaic": [
        (1051, 1065),
    ],
}

SAVE_DIR = "/mnt/sherlock/scratch/planet_data_sa_dhs"


def main():
    client = BasemapsClient()

    for mosaic_name, quads in CORRUPTED.items():
        print(f"\nProcessing mosaic: {mosaic_name}")
        mosaic = client.mosaic(name=mosaic_name)

        for x, y in quads:
            quad_id = f"{x}-{y}"
            print(f"  Downloading quad {quad_id}...", end=" ", flush=True)
            try:
                quad_info = client._item(f"mosaics/{mosaic.id}/quads/{quad_id}")
                from planet_download.client import MosaicQuad
                quad = MosaicQuad(quad_info, mosaic, client)
                path = quad.download(
                    filename=f"{x}_{y}.tif",
                    output_dir=f"{SAVE_DIR}/{mosaic_name}",
                )
                print(f"OK -> {path}")
            except Exception as e:
                print(f"FAILED: {e}")


if __name__ == "__main__":
    main()
