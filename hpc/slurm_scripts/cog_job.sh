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

echo "============================================================"
echo "Job: $SLURM_JOB_ID — COG mosaic for $AQUIFER"
echo "Node: $(hostname) | Started: $(date -u)"
echo "============================================================"

module load allas
allas-conf --silent

# ── Skip if already done ──────────────────────────────────────
if a-check "$BUCKET/$AQUIFER/cog/velocity_mosaic_cog.tif" 2>/dev/null; then
    echo "COG outputs already in Allas — skipping."
    exit 0
fi

SCRATCH="$LOCAL_SCRATCH/$SLURM_JOB_ID"
mkdir -p "$SCRATCH/$AQUIFER/cog"

a-get "$BUCKET/container/transboundary_opera.sif" -C "$SCRATCH/"
SIF="$SCRATCH/transboundary_opera.sif"

# ── Pull velocity.h5 per frame ────────────────────────────────
for FRAME in "${FRAMES[@]}"; do
    mkdir -p "$SCRATCH/$AQUIFER/$FRAME/mintpy"
    a-get "$BUCKET/$AQUIFER/$FRAME/mintpy/velocity.h5" \
        -C "$SCRATCH/$AQUIFER/$FRAME/mintpy/"
done

# ── Pull decomposition files ──────────────────────────────────
a-list "$BUCKET/$AQUIFER/" 2>/dev/null \
    | grep -E "_dhorz\.h5|_dvert\.h5" \
    | while read -r obj; do
        FNAME=$(basename "$obj")
        a-get "$BUCKET/$AQUIFER/$FNAME" -C "$SCRATCH/$AQUIFER/"
    done || echo "  No decomposition files (single-pass aquifer)"

# ── Pull shapefile ────────────────────────────────────────────
mkdir -p "$SCRATCH/shapefiles"
for EXT in shp dbf shx prj cpg; do
    a-get "$BUCKET/shapefiles/TBA_full.$EXT" -C "$SCRATCH/shapefiles/" 2>/dev/null || true
done

# ── Run COG mosaic ────────────────────────────────────────────
# NOTE: run_cog_mosaic.py is Phase 4 — to be implemented
apptainer exec \
    --bind "$REPO:/repo" \
    --bind "$SCRATCH:/work" \
    "$SIF" \
    python /repo/src/transboundary_opera/run_cog_mosaic.py \
        --aquifer-code "$AQUIFER" \
        --aquifer-dir  "/work/$AQUIFER" \
        --shapefile    "/work/shapefiles/TBA_full.shp" \
        --outdir       "/work/$AQUIFER/cog"

# ── Upload COG outputs to Allas ───────────────────────────────
# FIX: slashes go in -b, not --object
a-put "$SCRATCH/$AQUIFER/cog/" \
    -b "$BUCKET/$AQUIFER/cog" \
    --nc

rm -rf "$SCRATCH"
echo "COG mosaic for $AQUIFER complete: $(date -u)"