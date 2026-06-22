from pathlib import Path
import warnings
import numpy as np
from pyproj import Transformer
from mintpy.asc_desc2horz_vert import asc_desc2horz_vert
from mintpy.utils import ptime, readfile, utils as ut, writefile
import h5py

class InSARDecomposer:
    """Process overlapping InSAR pairs and decompose LOS to horizontal/vertical components."""

    def __init__(self, overlapping_pairs, ds_name='timeseries', angle=-90):
        self.overlapping_pairs = overlapping_pairs
        self.ds_name = ds_name
        self.angle = angle
        self.failed_pairs = []
        self.successful_pairs = []
        self.skipped_pairs = []

    def _get_file_paths(self, timeseries_files):
        """Generate all related file paths from timeseries files."""
        return {
            'timeseries': timeseries_files,
            'velocity': [Path(str(f).replace('timeseries', 'velocity')) for f in timeseries_files],
            'geometry': [Path(str(f).replace('timeseries', 'geometryGeo')) for f in timeseries_files],
            'coherence': [f.parent / 'avgSpatialCoh.h5' for f in timeseries_files],
        }

    def _compute_grid_params(self, atr, bbox):
        """Compute grid dimensions and steps from attributes and bounding box."""
        S, N, W, E = bbox
        lat_step = float(atr['Y_STEP'])
        lon_step = float(atr['X_STEP'])
        length = int(round((S - N) / lat_step))
        width = int(round((E - W) / lon_step))
        return {'length': length, 'width': width, 'lat_step': lat_step, 'lon_step': lon_step}

    def _find_max_coherence_location(self, coh_data, bbox, grid):
        """Find the location of maximum coherence within the processed region.

        Args:
            coh_data: 2D coherence array (already subsetted to bbox),
                      should be pre-masked to valid timeseries pixels
            bbox: Bounding box (S, N, W, E)
            grid: Grid parameters dict with lat_step, lon_step

        Returns:
            (lat, lon) of max coherence location in geographic coordinates

        Raises:
            ValueError: If no valid (non-NaN) pixels exist in the coherence data
        """
        # Check if we have any valid data
        if np.all(np.isnan(coh_data)):
            raise ValueError(
                "No valid pixels found with both coherence and timeseries data. "
                "Check that bounding box overlaps with valid data in both tracks."
            )

        max_idx = np.unravel_index(np.nanargmax(coh_data), coh_data.shape)
        row, col = max_idx

        S, N, W, E = bbox

        ref_lat = N + row * grid['lat_step']
        ref_lon = W + col * grid['lon_step']

        return ref_lat, ref_lon

    def _build_output_metadata(self, atr, grid, bbox, ref_coords, dates=None):
        """Build metadata dictionary for output files."""
        _, N, W, _ = bbox
        ref_lat, ref_lon = ref_coords

        out_atr = atr.copy()
        out_atr.update({
            'FILE_TYPE': 'timeseries',
            'WIDTH': str(grid['width']),
            'LENGTH': str(grid['length']),
            'X_STEP': str(grid['lon_step']),
            'Y_STEP': str(grid['lat_step']),
            'X_FIRST': str(W),
            'Y_FIRST': str(N),
        })

        ref_y, ref_x = ut.coordinate(out_atr).geo2radar(ref_lat, ref_lon)[0:2]
        out_atr['REF_Y'] = int(ref_y)
        out_atr['REF_X'] = int(ref_x)

        if dates:
            out_atr['DATE12'] = f"{dates[0]}-{dates[-1]}"

        return out_atr

    def _read_pair_data(self, paths, atr_list, grid, bbox, common_dates=None):
        """Read all data arrays for a pair of files - common time slices only.

        Parameters
        ----------
        paths : dict
            Dictionary with file paths for timeseries, geometry, coherence
        atr_list : list
            List of attribute dictionaries for each file
        grid : dict
            Grid parameters (length, width, lat_step, lon_step)
        bbox : tuple
            Bounding box (S, N, W, E)
        common_dates : list, optional
            Pre-computed list of common dates. If None, will compute from files.
        """
        N, W = bbox[1], bbox[2]

        # Get date lists from each file (needed for index lookup)
        date_lists = []
        for ts_file in paths['timeseries']:
            with h5py.File(ts_file, 'r') as f:
                dates = [d.decode() for d in f['date'][:]]
                date_lists.append(dates)

        # Use pre-computed common_dates if provided, otherwise compute
        if common_dates is None:
            common_dates = sorted(set(date_lists[0]) & set(date_lists[1]))

        if len(common_dates) == 0:
            raise ValueError("No common dates between the two timeseries")

        print(f"  Using {len(common_dates)} common dates out of {len(date_lists[0])} and {len(date_lists[1])}")

        n_times = len(common_dates)
        shape_3d = (len(paths['timeseries']), n_times, grid['length'], grid['width'])
        shape_2d = (len(paths['timeseries']), grid['length'], grid['width'])

        dlos = np.zeros(shape_3d, dtype=np.float32)
        los_inc_angle = np.zeros(shape_2d, dtype=np.float32)
        los_az_angle = np.zeros(shape_2d, dtype=np.float32)
        coh = np.zeros(shape_2d, dtype=np.float32)

        for i, (atr, ts_file) in enumerate(zip(atr_list, paths['timeseries'])):
            coord = ut.coordinate(atr)
            y0, x0 = coord.lalo2yx(N, W)
            box = (x0, y0, x0 + grid['width'], y0 + grid['length'])

            coh[i] = readfile.read(paths['coherence'][i], box=box, datasetName='avgSpatialCoh')[0]
            los_inc_angle[i] = readfile.read(paths['geometry'][i], box=box, datasetName='incidenceAngle')[0]
            los_az_angle[i] = readfile.read(paths['geometry'][i], box=box, datasetName='azimuthAngle')[0]

            # Read only common dates
            full_data, _ = readfile.read(ts_file, box=box, datasetName=self.ds_name)
            for t, date in enumerate(common_dates):
                date_idx = date_lists[i].index(date)
                dlos[i, t] = full_data[date_idx]

        # Report how much real data each track contributes in the overlap. A
        # track reading ~0% finite is the broken-virtual-link signature -- the
        # timeseries.h5 was uploaded without its displacement source, so reads
        # come back NaN/0. Run ts_integrity.py on the named frame to confirm.
        for i in range(len(paths['timeseries'])):
            frac = float(np.isfinite(dlos[i]).mean())
            track = paths['timeseries'][i].parts[-3]
            print(f"    track {track}: {frac:.1%} finite timeseries pixels in overlap")

        return {
            'dlos': dlos,
            'inc': los_inc_angle,
            'az': los_az_angle,
            'coh': coh,
            'n_times': n_times,
            'dates': common_dates
        }

    def process_pair(self, pair):
        """Process a single overlapping pair and return output paths.

        Returns None if the pair has no overlapping valid pixels (skipped),
        which the runner treats as a skip rather than a failure.
        """
        timeseries_files = list(pair.values())[:2]
        paths = self._get_file_paths(timeseries_files)
        tracks = [timeseries_files[0].parts[-3], timeseries_files[1].parts[-3]]
        ref_file = timeseries_files[0]

        atr_list = [readfile.read_attribute(f) for f in timeseries_files]
        grid = self._compute_grid_params(atr_list[0], pair['bbox'])

        # Read all data - pass pre-computed common_dates if available
        common_dates = pair.get('common_dates', None)
        data = self._read_pair_data(paths, atr_list, grid, pair['bbox'], common_dates=common_dates)

        n_times = data['n_times']
        length, width = grid['length'], grid['width']

        # Allocate output arrays for all time slices
        dhorz_all = np.zeros((n_times, length, width), dtype=np.float32)
        dvert_all = np.zeros((n_times, length, width), dtype=np.float32)

        # Decompose each time slice
        print(f"  Decomposing {n_times} time slices...")
        for t in range(n_times):
            dlos_t = data['dlos'][:, t, :, :]

            # Create mask where BOTH tracks have valid data for this time slice
            valid_mask = ~np.isnan(dlos_t[0]) & ~np.isnan(dlos_t[1])

            dlos_clean = np.nan_to_num(dlos_t, nan=0.0)

            dhorz_t, dvert_t = asc_desc2horz_vert(
                dlos_clean, data['inc'], data['az'], self.angle
            )

            # Apply mask - only keep values where both tracks had valid data
            dhorz_all[t] = np.where(valid_mask, dhorz_t, np.nan)
            dvert_all[t] = np.where(valid_mask, dvert_t, np.nan)

        # Find reference point from max coherence within valid data region
        # Use first time slice's valid mask
        valid_mask_t0 = ~np.isnan(data['dlos'][0, 0, :, :]) & ~np.isnan(data['dlos'][1, 0, :, :])

        # nanmean over an all-NaN column emits "Mean of empty slice" -- silence
        # it; we handle the empty case explicitly right after.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            mean_coh = np.nanmean(data['coh'], axis=0)
        masked_coh = np.where(valid_mask_t0, mean_coh, np.nan)

        # Guard: if nothing overlaps with valid data in BOTH tracks, skip this
        # pair gracefully instead of raising. This is the expected outcome for
        # sparse overlaps, and also what an empty (broken-link) timeseries
        # produces -- in which case the per-track finite report above will read
        # ~0%, pointing at the real cause.
        if not np.isfinite(masked_coh).any():
            print(
                f"  Skipping pair {tracks[0]} x {tracks[1]}: no overlapping valid "
                f"pixels (empty timeseries/coherence in overlap). If unexpected, "
                f"run ts_integrity.py on these frames."
            )
            return None

        ref_coords = self._find_max_coherence_location(masked_coh, pair['bbox'], grid)

        # Build metadata and write
        out_atr = self._build_output_metadata(atr_list[0], grid, pair['bbox'], ref_coords, data['dates'])
        return self._write_outputs(dhorz_all, dvert_all, out_atr, ref_file, tracks, data['dates'])

    def _write_outputs(self, dhorz, dvert, out_atr, ref_file, tracks, dates):
        """Write horizontal and vertical components to HDF5 files."""
        out_dir = ref_file.parent.parent.parent
        outfiles = {
            'horz': out_dir / f'{tracks[0]}_{tracks[1]}_dhorz.h5',
            'vert': out_dir / f'{tracks[0]}_{tracks[1]}_dvert.h5',
        }

        # MintPy timeseries format expects:
        # - 'timeseries': 3D array (n_times, length, width)
        # - 'date': 1D array of dates as bytes
        date_array = np.array([d.encode('utf-8') for d in dates], dtype='S8')

        horz_dict = {
            'timeseries': dhorz,
            'date': date_array,
        }

        vert_dict = {
            'timeseries': dvert,
            'date': date_array,
        }

        print(f'  Writing horizontal: {outfiles["horz"]}')
        writefile.write(horz_dict, out_file=str(outfiles['horz']), metadata=out_atr)

        print(f'  Writing vertical:   {outfiles["vert"]}')
        writefile.write(vert_dict, out_file=str(outfiles['vert']), metadata=out_atr)

        return outfiles

    def run(self, verbose=True):
        """Process all overlapping pairs."""
        self.failed_pairs = []
        self.successful_pairs = []
        self.skipped_pairs = []

        for idx, pair in enumerate(self.overlapping_pairs):
            if verbose:
                print(f'Processing pair {idx + 1}/{len(self.overlapping_pairs)}')
            try:
                outfiles = self.process_pair(pair)
                if outfiles is None:
                    # process_pair returned None == intentionally skipped (no overlap)
                    self.skipped_pairs.append({'index': idx, 'pair': pair})
                else:
                    self.successful_pairs.append(outfiles)
            except Exception as e:
                if verbose:
                    print(f'  Failed: {e}')
                self.failed_pairs.append({'index': idx, 'pair': pair, 'error': str(e)})

        if verbose:
            self._print_summary()

        return self

    def _print_summary(self):
        """Print processing summary."""
        print(f'\n{"="*50}')
        print(f'Complete: {len(self.successful_pairs)} succeeded, '
              f'{len(self.skipped_pairs)} skipped, {len(self.failed_pairs)} failed')
        if self.skipped_pairs:
            print('Skipped pairs (no overlapping valid pixels):')
            for item in self.skipped_pairs:
                ts = list(item['pair'].values())[:2]
                print(f'  Pair {item["index"]}: {ts[0].parts[-3]} x {ts[1].parts[-3]}')
        if self.failed_pairs:
            print('Failed pairs:')
            for item in self.failed_pairs:
                print(f'  Pair {item["index"]}: {item["error"]}')