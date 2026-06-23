# Transboundary Opera — HPC pipeline

Automated monitoring of US–Mexico border aquifers from OPERA DISP-S1 data on
CSC Mahti. Per aquifer, the pipeline downloads Sentinel-1 frames, processes each
to MintPy format, decomposes ascending/descending LOS into horizontal/vertical
components, and mosaics the result into Cloud-Optimized GeoTIFFs.

## Stages

`pipeline.py` discovers frames for an aquifer and submits a SLURM dependency chain:

```
frame jobs (download + process_frame.py, one per frame)
  └── decompose job (after ALL frames)   → dhorz/dvert pairs
        └── cog job (after decompose)     → velocity_mosaic_cog.tif
```

Run it:

```bash
module load allas && allas-conf
pixi run -e operaapp python hpc/pipeline.py \
    --aquifer N015 --start 20220101 --end 20231231 --dry-run   # preview
pixi run -e operaapp python hpc/pipeline.py \
    --aquifer N015 --start 20220101 --end 20231231             # submit
```

State and outputs live in Allas (`s3cmd` + permanent `~/.s3cfg`). Frame jobs skip
when their `mintpy/velocity.h5` already exists in Allas, so re-runs are cheap.

## Important: timeseries.h5 must be self-contained

`process_frame.py` writes `mintpy/timeseries.h5`. It used to be an HDF5 **virtual
dataset** (a pointer to its displacement source). Uploaded to Allas without that
source, reads returned NaN/0 on the decompose node while reference-point
attributes survived — a "complete" file with empty data.

Fix: it is now **materialized** (`virtual=False`, contiguous data) by default so
it survives the upload/download round-trip. Toggle with `MATERIALIZE_TS=0` for
purely local, in-place runs. A real `timeseries.h5` is hundreds of MB; a broken
virtual one is ~10 KB — a quick size check tells them apart.

## Graceful no-data frames

Some frames are degenerate (no spatial overlap, or all-NaN coherence over a thin
sliver). These now **skip gracefully** instead of crashing:

- `download_frame.py` and `process_frame.py` exit **2** for no-data frames.
- `frame_job.sh` treats exit 2 as a skip (job `COMPLETED`), so the `afterok`
  decompose/cog dependency is not cancelled.
- `decomposer.py` skips pairs with no overlapping valid pixels and reports them
  separately from failures.

## COG mosaic (Phase 4 — velocity only)

`run_cog_mosaic.py` reads each frame's `velocity.h5`, reprojects to a common CRS,
merges, clips to the aquifer polygon, and writes `velocity_mosaic_cog.tif`.

It's structured around a `PRODUCTS` registry; only `velocity` is wired. Planned
follow-ups: vertical/horizontal products from the dhorz/dvert series, and a
coherence-weighted merge with per-frame reference reconciliation to remove seams
(overlaps currently use first-wins). Requires the shapefile in Allas at
`s3://<bucket>/shapefiles/TBA_full.*` for the clip step.

## Troubleshooting tools

```bash
# Is a stored frame's timeseries real, or a broken virtual link?
pixi run -e operaapp python hpc/check_allas_frame.py --aquifer N015 --frames 40297 --list

# Delete only broken frames so they reprocess (dry-run by default)
pixi run -e operaapp python hpc/reset_broken_frames.py --aquifer N015 --frames 40297 --confirm

# Wipe an aquifer entirely for a clean rebuild (dry-run by default)
pixi run -e operaapp python hpc/reset_aquifer.py --aquifer N015 --state --confirm
```

`ts_integrity.py` (in `src/transboundary_opera/`) backs these checks and also runs
inside the frame and decompose jobs to flag empty timeseries before they're used.

## Layout

```
hpc/
  pipeline.py                  orchestrator
  download_frame.py            per-frame download (host)
  slurm_scripts/
    frame_job.sh               download + process, per frame
    cog_job.sh                 velocity COG mosaic
  check_allas_frame.py         inspect a stored frame
  reset_broken_frames.py       delete broken frames
  reset_aquifer.py             wipe an aquifer
code/process_data/
  process_frame.py             reformat → static → mintpy → reference point
src/transboundary_opera/
  decomposer.py                asc/desc → horizontal/vertical
  run_cog_mosaic.py            COG mosaic (velocity)
  ts_integrity.py              timeseries integrity checker
```