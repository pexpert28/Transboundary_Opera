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

PYTHON_CONTAINER="/transboundary_opera/.pixi/envs/operaapp/bin/python"
PYTHON_HOST="$REPO/.pixi/envs/operaapp/bin/python"

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
mkdir -p "$FRAME_DIR" "$SCRATCH/cache" "$SCRATCH/netrc_dir"

echo "Scratch: $SCRATCH"
echo "Available disk: $(df -h $LOCAL_SCRATCH | tail -1)"

# ── Pull container ────────────────────────────────────────────
echo "Pulling container..."
s3cmd get "s3://$BUCKET/container/transboundary_opera.sif" \
    "$SCRATCH/transboundary_opera.sif"
SIF="$SCRATCH/transboundary_opera.sif"

# ── Prepare .netrc for container use ─────────────────────────
# asf_search inside the container reads ~/.netrc directly.
# We write it to scratch and bind-mount it over the home path
# so both the main process and multiprocessing workers can read it.
EARTHDATA_USER=$(awk '/urs\.earthdata\.nasa\.gov/{
    for(i=1;i<=NF;i++) if($i=="login") print $(i+1)
}' ~/.netrc)
EARTHDATA_PASS=$(awk '/urs\.earthdata\.nasa\.gov/{
    for(i=1;i<=NF;i++) if($i=="password") print $(i+1)
}' ~/.netrc)

if [ -z "$EARTHDATA_USER" ] || [ -z "$EARTHDATA_PASS" ]; then
    echo "ERROR: Earthdata credentials not found in ~/.netrc"
    exit 1
fi

NETRC_SCRATCH="$SCRATCH/netrc_dir/netrc"
printf 'machine urs.earthdata.nasa.gov login %s password %s\n' \
    "$EARTHDATA_USER" "$EARTHDATA_PASS" > "$NETRC_SCRATCH"
chmod 600 "$NETRC_SCRATCH"
echo "Earthdata credentials staged for: $EARTHDATA_USER"

# Bind-mount the staged .netrc over the home directory's .netrc inside container
# This makes it visible to asf_search (process_frame.py static download)
HOME_NETRC="/users/$USER/.netrc"
APPTAINER_ARGS="--bind $REPO:/repo \
    --bind $SCRATCH:/work \
    --bind $NETRC_SCRATCH:$HOME_NETRC:ro \
    --env XDG_CACHE_HOME=/work/cache \
    --env PATH=/transboundary_opera/.pixi/envs/operaapp/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


# ── Step 1: Download .nc files (host Python, outside container) ──
echo ""
echo "--- Step 1/2: Downloading DISP-S1 .nc files (host Python) ---"

set +e
"$PYTHON_HOST" "$REPO/hpc/download_frame.py" \
    --aquifer    "$AQUIFER" \
    --frame      "$FRAME_ID" \
    --output-dir "$FRAME_DIR" \
    --shapefile  "$REPO/raw_data/TBA_full.shp" \
    --start      "$START_DATE" \
    --end        "$END_DATE" \
    --workers    "$DL_WORKERS"
DOWNLOAD_EXIT=$?
set -e

if [ "$DOWNLOAD_EXIT" -eq 2 ]; then
    echo "Frame $FRAME_ID: no data or no overlap — skipping gracefully."
    rm -rf "$SCRATCH"
    exit 0
elif [ "$DOWNLOAD_EXIT" -ne 0 ]; then
    echo "ERROR: download failed with exit code $DOWNLOAD_EXIT"
    exit 1
fi

NC_COUNT=$(find "$FRAME_DIR/subset-ncs" -name "*.nc" 2>/dev/null | wc -l)
echo "Downloaded $NC_COUNT .nc files"
echo "Disk after download: $(du -sh $SCRATCH/data | cut -f1)"

# ── Step 2: Process with process_frame.py (inside container) ──
echo ""
echo "--- Step 2/2: Processing with process_frame.py (container) ---"
apptainer exec $APPTAINER_ARGS "$SIF" \
    $PYTHON_CONTAINER /repo/code/process_data/process_frame.py \
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