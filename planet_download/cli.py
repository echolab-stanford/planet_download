"""Command line interface for Planet Download."""

import click
from dotenv import load_dotenv

# Load environment variables at module level
load_dotenv()


@click.group()
def cli():
    """Planet Download CLI for basemap data."""
    pass


# --- Simple Basemap List Command ---
@cli.command(
    help="List available Planet basemaps. Example:\n  planet-download list-mosaics"
)
def list_mosaics():
    """List available Planet basemaps using BasemapsClient from client.py."""
    from planet_download.client import BasemapsClient

    client = BasemapsClient()
    mosaics = list(client.list_mosaics())
    click.echo("Available mosaics:")
    for m in mosaics:
        click.echo(f"  - {m.name}")


def main():
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
