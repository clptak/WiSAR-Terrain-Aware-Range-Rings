"""Runs one v1 job through the unmodified pipeline and builds its outputs."""
from . import outputs as out


def _run_tarr_pipeline(ipp, final_km, radius_km):
    from pipeline import run_analysis
    return run_analysis(ipp_lat=ipp['lat'], ipp_lng=ipp['lon'],
                        pct_25_km=final_km['p25'], pct_50_km=final_km['p50'], pct_75_km=final_km['p75'],
                        radius_km=radius_km)


def _run_isochrone_pipeline(ipp, speed_kmh, intervals, radius_km):
    from pipeline import run_isochrone_analysis
    return run_isochrone_analysis(ipp_lat=ipp['lat'], ipp_lng=ipp['lon'], base_speed_kmh=speed_kmh,
                                  time_intervals_hours=intervals, radius_km=radius_km)


def run_job(job, job_dir):
    """Execute a job. Returns (outputs_meta, result_block). Raises on failure."""
    req, resolved = job['request'], job['resolved']
    if job['type'] == 'tarr':
        result = _run_tarr_pipeline(req['ipp'], resolved['final_distances_km'], resolved['radius_km'])
    else:
        result = _run_isochrone_pipeline(req['ipp'], resolved['speed_kmh'], resolved['intervals_hours'],
                                         resolved['min_radius_km'])
    return out.build_outputs(result, job['type'], job_dir)
