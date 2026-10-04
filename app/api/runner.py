"""Runs one v1 job through the unmodified pipeline and builds its outputs."""
import os
import shutil

from . import outputs as out

WORK_SUBDIR = 'work'  # pipeline scratch inside the job folder; removed once outputs are built


def _run_tarr_pipeline(ipp, final_km, radius_km, work_dir):
    from pipeline import run_analysis
    return run_analysis(ipp_lat=ipp['lat'], ipp_lng=ipp['lon'],
                        pct_25_km=final_km['p25'], pct_50_km=final_km['p50'], pct_75_km=final_km['p75'],
                        radius_km=radius_km, work_dir=work_dir)


def _run_isochrone_pipeline(ipp, speed_kmh, intervals, radius_km, work_dir):
    from pipeline import run_isochrone_analysis
    return run_isochrone_analysis(ipp_lat=ipp['lat'], ipp_lng=ipp['lon'], base_speed_kmh=speed_kmh,
                                  time_intervals_hours=intervals, radius_km=radius_km, work_dir=work_dir)


def run_job(job, job_dir):
    """Execute a job. Returns (outputs_meta, result_block). Raises on failure.

    The pipeline writes its intermediates (DEM, NLCD, cost rasters, masks)
    into the job's own work folder, so no two analyses ever share files.
    """
    req, resolved = job['request'], job['resolved']
    work_dir = os.path.join(job_dir, WORK_SUBDIR)
    os.makedirs(work_dir, exist_ok=True)
    try:
        if job['type'] == 'tarr':
            result = _run_tarr_pipeline(req['ipp'], resolved['final_distances_km'], resolved['radius_km'], work_dir)
            out.add_p90_ring(result, resolved)
        else:
            result = _run_isochrone_pipeline(req['ipp'], resolved['speed_kmh'], resolved['intervals_hours'],
                                             resolved['min_radius_km'], work_dir)
        return out.build_outputs(result, job['type'], job_dir)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
