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
DL_WORKERS="{DL_WORKERS}"
REPO="{REPO}"
BUCKET="{BUCKET}"
PYTHON="/transboundary_opera/.pixi/envs/operaapp/bin/python"

echo "============================================================"
echo "Job: $SLURM_JOB_ID  |  Aquifer: $AQUIFER  |  Frame: $FRAME_ID"
echo "Date range: $START_DATE → $END_DATE"
echo "Node: $(hostname) | Started: $(date -u)"
echo "============================================================"

module load allas

# ── Skip if already done ──────────────────────────────────────
if s3cmd ls "s3://$BUCKET/$AQUIFER/$FRAME_ID/mintpy/velocity.h5" 2>/dev/null | grep -q "velocity.h5"; then
    echo "Frame $FRAME_ID already in Allas — skipping."
    exit 0
fi

SCRATCH="$LOCAL_SCRATCH/$SLURM_JOB_ID"
FRAME_DIR="$SCRATCH/data/$AQUIFER/$FRAME_ID"
mkdir -p "$FRAME_DIR" "$SCRATCH/cache"

echo "Scratch: $SCRATCH"
echo "Available disk: $(df -h $LOCAL_SCRATCH | tail -1)"

# ── Pull container ────────────────────────────────────────────
echo "Pulling container..."
s3cmd get "s3://$BUCKET/container/transboundary_opera.sif" \
    "$SCRATCH/transboundary_opera.sif"
SIF="$SCRATCH/transboundary_opera.sif"

# ── Read Earthdata credentials from ~/.netrc ──────────────────
# Pass as env vars so ProcessPoolExecutor child processes can find them
# opera_utils checks EARTHDATA_USERNAME + EARTHDATA_PASSWORD
EARTHDATA_USER=$(grep -A3 'urs.earthdata.nasa.gov' ~/.netrc | grep 'login' | awk '{print $2}')
EARTHDATA_PASS=$(grep -A3 'urs.earthdata.nasa.gov' ~/.netrc | grep 'password' | awk '{print $2}')

if [ -z "$EARTHDATA_USER" ] || [ -z "$EARTHDATA_PASS" ]; then
    echo "ERROR: Earthdata credentials not found in ~/.netrc"
    echo "Run: echo 'machine urs.earthdata.nasa.gov login USER password PASS' >> ~/.netrc"
    exit 1
fi
echo "Earthdata credentials loaded for user: $EARTHDATA_USER"

# ── Apptainer command with all required env vars ──────────────
APPTAINER="apptainer exec
    --bind $REPO:/repo
    --bind $SCRATCH:/work
    --env XDG_CACHE_HOME=/work/cache
    --env EARTHDATA_USERNAME=$EARTHDATA_USER
    --env EARTHDATA_PASSWORD=$EARTHDATA_PASS
    $SIF"

# ── Step 1: Download .nc files ────────────────────────────────
echo ""
echo "--- Step 1/2: Downloading DISP-S1 .nc files ---"

set +e
$APPTAINER \
    $PYTHON /repo/hpc/download_frame.py \
        --aquifer    "$AQUIFER" \
        --frame      "$FRAME_ID" \
        --output-dir "/work/data/$AQUIFER/$FRAME_ID" \
        --shapefile  "/repo/raw_data/TBA_full.shp" \
        --start      "$START_DATE" \
        --end        "$END_DATE" \
        --workers    "$DL_WORKERS"
DOWNLOAD_EXIT=$?
set -e

if [ "$DOWNLOAD_EXIT" -eq 2 ]; then
    echo "Frame $FRAME_ID has no spatial overlap with $AQUIFER — skipping gracefully."
    rm -rf "$SCRATCH"
    exit 0
elif [ "$DOWNLOAD_EXIT" -ne 0 ]; then
    echo "ERROR: download failed with exit code $DOWNLOAD_EXIT"
    exit 1
fi

NC_COUNT=$(find "$FRAME_DIR/subset-ncs" -name "*.nc" 2>/dev/null | wc -l)
echo "Downloaded $NC_COUNT .nc files"
echo "Disk after download: $(du -sh $SCRATCH/data | cut -f1)"

# ── Step 2: Process with process_frame.py ────────────────────
echo ""
echo "--- Step 2/2: Processing with process_frame.py ---"
$APPTAINER \
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