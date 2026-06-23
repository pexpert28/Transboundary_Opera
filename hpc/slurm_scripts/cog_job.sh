#!/bin/bash
#SBATCH --job-name=opera_{AQUIFER}_cog
#SBATCH --account={CSC_PROJECT}
#SBATCH --partition={PARTITION}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={CPUS}
#SBATCH --mem={MEM}
#SBATCH --time={TIME}
#SBATCH --gres=nvme:{NVME_GB}
#SBATCH --output={LOGS_DIR}/{AQUIFER}_cog_%j.out
#SBATCH --error={LOGS_DIR}/{AQUIFER}_cog_%j.err

set -euo pipefail

AQUIFER="{AQUIFER}"
FRAMES=({FRAMES})
REPO="{REPO}"
BUCKET="{BUCKET}"
PYTHON="/transboundary_opera/.pixi/envs/operaapp/bin/python"

echo "============================================================"
echo "Job: $SLURM_JOB_ID — COG mosaic for $AQUIFER"
echo "Node: $(hostname) | Started: $(date -u)"
echo "============================================================"

module load allas

# ── Skip if already done ──────────────────────────────────────
if s3cmd ls "s3://$BUCKET/$AQUIFER/cog/velocity_mosaic_cog.tif" 2>/dev/null | grep -q "velocity_mosaic_cog.tif"; then
    echo "Already done — skipping."
    exit 0
fi

SCRATCH="$LOCAL_SCRATCH/$SLURM_JOB_ID"
mkdir -p "$SCRATCH/$AQUIFER/cog"

s3cmd get "s3://$BUCKET/container/transboundary_opera.sif" "$SCRATCH/transboundary_opera.sif"
SIF="$SCRATCH/transboundary_opera.sif"

# ── Pull velocity.h5 per frame (skip frames with no Allas output) ──
# Some frames are legitimately skipped during processing (no spatial overlap,
# no valid coherence) and have no velocity.h5 in Allas. Check existence first
# and skip on miss, so a 404 doesn't abort the whole job under `set -e`.
VELOCITY_COUNT=0
for FRAME in "${FRAMES[@]}"; do
    if ! s3cmd ls "s3://$BUCKET/$AQUIFER/$FRAME/mintpy/velocity.h5" 2>/dev/null | grep -q "velocity.h5"; then
        echo "  Frame $FRAME: no velocity.h5 in Allas (skipped during processing) — skipping"
        continue
    fi
    mkdir -p "$SCRATCH/$AQUIFER/$FRAME/mintpy"
    s3cmd get \
        "s3://$BUCKET/$AQUIFER/$FRAME/mintpy/velocity.h5" \
        "$SCRATCH/$AQUIFER/$FRAME/mintpy/velocity.h5"
    VELOCITY_COUNT=$((VELOCITY_COUNT + 1))
done
echo "Pulled $VELOCITY_COUNT velocity.h5 file(s)."

# ── Pull decomposition files ──────────────────────────────────
DECOMP_COUNT=0
while read -r obj; do
    [ -z "$obj" ] && continue
    FNAME=$(basename "$obj")
    if s3cmd get "$obj" "$SCRATCH/$AQUIFER/$FNAME"; then
        DECOMP_COUNT=$((DECOMP_COUNT + 1))
    fi
done < <(s3cmd ls "s3://$BUCKET/$AQUIFER/" 2>/dev/null \
            | grep -E "_dhorz\.h5|_dvert\.h5" \
            | awk '{print $4}')
echo "Pulled $DECOMP_COUNT decomposition file(s)."

# ── Pull shapefile ────────────────────────────────────────────
mkdir -p "$SCRATCH/shapefiles"
for EXT in shp dbf shx prj cpg; do
    s3cmd get "s3://$BUCKET/shapefiles/TBA_full.$EXT" \
        "$SCRATCH/shapefiles/TBA_full.$EXT" 2>/dev/null || true
done

# ── Run COG mosaic (Phase 4 — to be implemented) ─────────────
# run_cog_mosaic.py is still a placeholder; tolerate a non-zero exit so the
# job reports the state clearly instead of dying under `set -e`.
set +e
apptainer exec \
    --bind "$REPO:/repo" \
    --bind "$SCRATCH:/work" \
    "$SIF" \
    $PYTHON /repo/src/transboundary_opera/run_cog_mosaic.py \
        --aquifer-code "$AQUIFER" \
        --aquifer-dir  "/work/$AQUIFER" \
        --shapefile    "/work/shapefiles/TBA_full.shp" \
        --outdir       "/work/$AQUIFER/cog"
MOSAIC_EXIT=$?
set -e

if [ "$MOSAIC_EXIT" -ne 0 ]; then
    echo "WARNING: run_cog_mosaic.py exited $MOSAIC_EXIT (Phase 4 not yet implemented?)."
    echo "Skipping upload; decomposition outputs are already in Allas."
    rm -rf "$SCRATCH"
    exit 0
fi

# ── Upload COG outputs (only if the mosaic produced something) ──
if [ -z "$(ls -A "$SCRATCH/$AQUIFER/cog" 2>/dev/null)" ]; then
    echo "No COG outputs were produced — nothing to upload."
    rm -rf "$SCRATCH"
    exit 0
fi

s3cmd put --recursive \
    "$SCRATCH/$AQUIFER/cog/" \
    "s3://$BUCKET/$AQUIFER/cog/"

rm -rf "$SCRATCH"
echo "COG mosaic for $AQUIFER complete: $(date -u)"