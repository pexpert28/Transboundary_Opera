#!/bin/bash
#SBATCH --job-name=opera_{AQUIFER}_{FRAME_ID}
#SBATCH --account={CSC_PROJECT}
#SBATCH --partition={PARTITION}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={CPUS}
#SBATCH --mem={MEM}
#SBATCH --time={TIME}
#SBATCH --gres=nvme:{NVME_GB}
#SBATCH --output={LOGS_DIR}/{AQUIFER}_{FRAME_ID}_%j.out
#SBATCH --error={LOGS_DIR}/{AQUIFER}_{FRAME_ID}_%j.err

set -euo pipefail

AQUIFER="{AQUIFER}"
FRAME_ID="{FRAME_ID}"
START_DATE="{START_DATE}"
END_DATE="{END_DATE}"
OPERA_VERSION="{OPERA_VERSION}"
DL_WORKERS="{DL_WORKERS}"
REPO="{REPO}"
BUCKET="{BUCKET}"
PYTHON="/transboundary_opera/.pixi/envs/operaapp/bin/python"

echo "============================================================"
echo "Job: $SLURM_JOB_ID  |  Aquifer: $AQUIFER  |  Frame: $FRAME_ID"
echo "Date range: $START_DATE → $END_DATE"
echo "Node: $(hostname) | Started: $(date -u)"
echo "============================================================"

# s3cmd reads permanent credentials from ~/.s3cfg — no token expiry
module load allas

# ── Skip if already done ──────────────────────────────────────
if s3cmd ls "s3://$BUCKET/$AQUIFER/$FRAME_ID/mintpy/velocity.h5" 2>/dev/null | grep -q "velocity.h5"; then
    echo "Frame $FRAME_ID already in Allas — skipping."
    exit 0
fi

SCRATCH="$LOCAL_SCRATCH/$SLURM_JOB_ID"
FRAME_DIR="$SCRATCH/data/$AQUIFER/$FRAME_ID"
mkdir -p "$FRAME_DIR/subset-ncs" "$FRAME_DIR/orbit_data" "$FRAME_DIR/geom_data"

echo "Scratch: $SCRATCH"
echo "Available disk: $(df -h $LOCAL_SCRATCH | tail -1)"

# ── Pull container ────────────────────────────────────────────
echo "Pulling container..."
s3cmd get "s3://$BUCKET/container/transboundary_opera.sif" "$SCRATCH/transboundary_opera.sif"
SIF="$SCRATCH/transboundary_opera.sif"

# ── Step 1: Download .nc files ────────────────────────────────
echo ""
echo "--- Step 1/2: Downloading DISP-S1 .nc files ---"
apptainer exec \
    --bind "$REPO:/repo" \
    --bind "$SCRATCH:/work" \
    "$SIF" \
    $PYTHON /repo/src/transboundary_opera/run1_download_DISP_S1_Static.py \
        --frameID    "$FRAME_ID" \
        --version    "$OPERA_VERSION" \
        --startDate  "$START_DATE" \
        --endDate    "$END_DATE" \
        --dispDir    "/work/data/$AQUIFER/$FRAME_ID/subset-ncs" \
        --staticDir  "/work/data/$AQUIFER/$FRAME_ID/orbit_data" \
        --geomDir    "/work/data/$AQUIFER/$FRAME_ID/geom_data" \
        --nWorkers   "$DL_WORKERS"

NC_COUNT=$(find "$FRAME_DIR/subset-ncs" -name "*.nc" | wc -l)
echo "Downloaded $NC_COUNT .nc files"
if [ "$NC_COUNT" -eq 0 ]; then
    echo "ERROR: No .nc files downloaded."
    exit 1
fi

# ── Step 2: Process with process_frame.py ────────────────────
echo ""
echo "--- Step 2/2: Processing with process_frame.py ---"
apptainer exec \
    --bind "$REPO:/repo" \
    --bind "$SCRATCH:/work" \
    "$SIF" \
    $PYTHON /repo/code/process_data/process_frame.py \
        --data-dir   "/work/data" \
        --aquifer    "$AQUIFER" \
        --frame      "$FRAME_ID" \
        --start-date "$START_DATE" \
        --end-date   "$END_DATE"

# ── Verify outputs ────────────────────────────────────────────
echo ""
echo "--- Verifying outputs ---"
for f in timeseries.h5 velocity.h5 geometryGeo.h5 avgSpatialCoh.h5; do
    FPATH="$FRAME_DIR/mintpy/$f"
    if [ -f "$FPATH" ]; then
        echo "  ✓ $f ($(du -sh $FPATH | cut -f1))"
    else
        echo "  ✗ $f MISSING"
        exit 1
    fi
done

# ── Upload mintpy/ to Allas ───────────────────────────────────
echo ""
echo "--- Uploading to Allas ---"
s3cmd put --recursive "$FRAME_DIR/mintpy/" \
    "s3://$BUCKET/$AQUIFER/$FRAME_ID/mintpy/"

echo "Uploaded: s3://$BUCKET/$AQUIFER/$FRAME_ID/mintpy/"
rm -rf "$SCRATCH"

echo ""
echo "============================================================"
echo "Frame $FRAME_ID complete: $(date -u)"
echo "============================================================"