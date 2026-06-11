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

# ── Pull velocity.h5 per frame ────────────────────────────────
for FRAME in "${FRAMES[@]}"; do
    mkdir -p "$SCRATCH/$AQUIFER/$FRAME/mintpy"
    s3cmd get \
        "s3://$BUCKET/$AQUIFER/$FRAME/mintpy/velocity.h5" \
        "$SCRATCH/$AQUIFER/$FRAME/mintpy/velocity.h5"
done

# ── Pull decomposition files ──────────────────────────────────
s3cmd ls "s3://$BUCKET/$AQUIFER/" 2>/dev/null \
    | grep -E "_dhorz\.h5|_dvert\.h5" \
    | awk '{print $4}' \
    | while read -r obj; do
        FNAME=$(basename "$obj")
        s3cmd get "$obj" "$SCRATCH/$AQUIFER/$FNAME"
    done || true

# ── Pull shapefile ────────────────────────────────────────────
mkdir -p "$SCRATCH/shapefiles"
for EXT in shp dbf shx prj cpg; do
    s3cmd get "s3://$BUCKET/shapefiles/TBA_full.$EXT" \
        "$SCRATCH/shapefiles/TBA_full.$EXT" 2>/dev/null || true
done

# ── Run COG mosaic (Phase 4 — to be implemented) ─────────────
apptainer exec \
    --bind "$REPO:/repo" \
    --bind "$SCRATCH:/work" \
    "$SIF" \
    $PYTHON /repo/src/transboundary_opera/run_cog_mosaic.py \
        --aquifer-code "$AQUIFER" \
        --aquifer-dir  "/work/$AQUIFER" \
        --shapefile    "/work/shapefiles/TBA_full.shp" \
        --outdir       "/work/$AQUIFER/cog"

# ── Upload COG outputs ────────────────────────────────────────
s3cmd put --recursive \
    "$SCRATCH/$AQUIFER/cog/" \
    "s3://$BUCKET/$AQUIFER/cog/"

rm -rf "$SCRATCH"
echo "COG mosaic for $AQUIFER complete: $(date -u)"