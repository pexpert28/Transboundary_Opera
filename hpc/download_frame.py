#!/usr/bin/env python3
"""
download_frame.py — Download OPERA DISP-S1 .nc files for a single frame.

Reads Earthdata credentials from ~/.netrc in the main process and sets
EARTHDATA_USERNAME / EARTHDATA_PASSWORD as environment variables before
calling run_download(). This ensures ProcessPoolExecutor child processes
inherit them regardless of spawn/fork mode or filesystem mount differences.

Exit codes:
  0 = success (files downloaded, or already done)
  2 = no spatial overlap between aquifer and frame (skip gracefully)
  1 = download error
"""

import argparse
import netrc as netrc_module
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


def ensure_earthdata_credentials():
    """Read ~/.netrc and set env vars so all child processes can find them."""
    if os.environ.get('EARTHDATA_USERNAME') and os.environ.get('EARTHDATA_PASSWORD'):
        print(f"Earthdata credentials from env vars: {os.environ['EARTHDATA_USERNAME']}")
        return

    try:
        n = netrc_module.netrc()
        auth = n.authenticators('urs.earthdata.nasa.gov')
        if auth and auth[0] and auth[2]:
            os.environ['EARTHDATA_USERNAME'] = auth[0]
            os.environ['EARTHDATA_PASSWORD'] = auth[2]
            print(f"Earthdata credentials from ~/.netrc: {auth[0]}")
            return
    except Exception as e:
        print(f"Warning: could not read ~/.netrc: {e}")

    print("ERROR: No Earthdata credentials found.")
    print("Add to ~/.netrc:  machine urs.earthdata.nasa.gov login USER password PASS")
    sys.exit(1)


def create_parser():
    p = argparse.ArgumentParser(
        description="Download OPERA DISP-S1 .nc files for one frame"
    )
    p.add_argument("--aquifer",     required=True)
    p.add_argument("--frame",       required=True, type=int)
    p.add_argument("--output-dir",  required=True, type=Path)
    p.add_argument("--shapefile",   required=True, type=Path)
    p.add_argument("--start",       required=True, help="YYYYMMDD")
    p.add_argument("--end",         required=True, help="YYYYMMDD")
    p.add_argument("--workers",     default=4, type=int)
    return p


def main():
    args = create_parser().parse_args()

    # ── Set credentials FIRST in main process ─────────────────
    # ProcessPoolExecutor workers inherit env vars from parent via fork.
    # Reading .netrc here and exporting to env vars guarantees all workers
    # can authenticate without needing to read .netrc themselves.
    ensure_earthdata_credentials()

    from opera_utils.disp import _download
    from transboundary_opera import displacement_tools as dt
    import geopandas as gpd

    # ── Load shapefile ─────────────────────────────────────────
    gdf = gpd.read_file(args.shapefile)
    gdf_aq = gdf[gdf["CODE_2021"] == args.aquifer]
    if gdf_aq.empty:
        print(f"ERROR: aquifer {args.aquifer} not found in {args.shapefile}")
        sys.exit(1)

    aquifer_geom = gdf_aq.geometry.iloc[0]

    # ── Get frame geometry ────────────────────────────────────
    print(f"Fetching geometry for frame {args.frame}...")
    geom_frames = dt.get_frame_geometries(
        [args.frame],
        gdf_bounds=aquifer_geom.bounds
    )

    if geom_frames.empty:
        print(f"No geometry found for frame {args.frame} — skipping")
        sys.exit(2)

    frame_geom = geom_frames[geom_frames["frame_id"] == args.frame]
    clipped = gpd.clip(
        gpd.GeoSeries([aquifer_geom], crs=gdf.crs),
        frame_geom
    )

    if clipped.empty:
        print(f"No spatial overlap between {args.aquifer} and frame {args.frame} — skipping")
        sys.exit(2)

    clipped_bbox = clipped.geometry.iloc[0].bounds
    print(f"Clipped bbox: {[round(x, 4) for x in clipped_bbox]}")

    # ── Check if already done ─────────────────────────────────
    output_dir  = args.output_dir
    subset_dir  = output_dir / "subset-ncs"
    done_marker = output_dir / ".download_complete"

    if done_marker.exists():
        nc_count = len(list(subset_dir.glob("*.nc")))
        print(f"Already downloaded ({nc_count} files) — skipping")
        sys.exit(0)

    if subset_dir.exists():
        print(f"Cleaning up partial download...")
        shutil.rmtree(subset_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Download ──────────────────────────────────────────────
    start_dt = datetime.strptime(args.start, "%Y%m%d")
    end_dt   = datetime.strptime(args.end,   "%Y%m%d")

    print(f"Downloading frame {args.frame}  {args.start} → {args.end}...")
    _download.run_download(
        args.frame,
        start_datetime=start_dt,
        end_datetime=end_dt,
        num_workers=args.workers,
        output_dir=subset_dir,
        bbox=clipped_bbox,
    )

    nc_files = list(subset_dir.glob("*.nc"))
    print(f"Downloaded {len(nc_files)} .nc files")

    if not nc_files:
        print("ERROR: no .nc files downloaded")
        sys.exit(1)

    done_marker.touch()
    print("Done.")
    sys.exit(0)


if __name__ == "__main__":
    main()