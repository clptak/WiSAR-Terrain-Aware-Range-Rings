"""Subject profile datasets (profiles/<id>.json) and TARR threshold resolution.

Resolution mirrors app.js applyLPB exactly: exact eco_region+terrain, then
eco_region with default terrain, then the category default, then the last
variant. Calibration is per band (m25/m50/m75), as in app.js runAnalysis.

p90 (custom subjects only) is an API addition: the Arizona table's 90%
distance. It is never calibrated (no dataset has a 90% multiplier). When
calibration pushes p75 to or past it, the 90% ring is dropped: it is left
out of final_distances_km and the job reports a warning (runner.py).
"""
import glob
import json
import os

from .problems import unprocessable

MI_TO_KM = 1.609344
BANDS = (('p25', 'm25'), ('p50', 'm50'), ('p75', 'm75'))


class ProfileStore:
    def __init__(self, directory, default_dataset):
        self.default_dataset = default_dataset
        self.datasets = {}
        for path in sorted(glob.glob(os.path.join(directory, '*.json'))):
            with open(path) as f:
                ds = json.load(f)
            self.datasets[ds['id']] = ds
        if default_dataset not in self.datasets:
            raise RuntimeError(f'Default profile dataset {default_dataset!r} not found in {directory}')

    def listing(self):
        return {'default_dataset': self.default_dataset, 'datasets': list(self.datasets.values())}

    def resolve(self, body):
        """Turn a validated TarrJobRequest into the ResolvedTarr block."""
        subject = body['subject']
        mode = body.get('calibration', 'auto')
        ds_id = body.get('dataset', self.default_dataset)
        ds = self.datasets.get(ds_id)
        if ds is None:
            raise unprocessable([{'pointer': '/dataset', 'detail': f'unknown dataset {ds_id!r}; see GET /profiles'}])

        if subject['kind'] == 'listed':
            cat = next((c for c in ds['categories'] if c['name'] == subject['category']), None)
            if cat is None:
                raise unprocessable([{'pointer': '/subject/category',
                                      'detail': f"unknown category {subject['category']!r} in dataset {ds_id!r}"}])
            variant = _pick_variant(cat['variants'], subject.get('eco_region'), subject.get('terrain'))
            source = dict(variant['distances_km'])
            label = cat['name'] + _variant_suffix(variant)
            variant_out = {'eco_region': variant['eco_region'], 'terrain': variant['terrain']}
        else:
            cat, variant_out = None, None
            unit = subject['distances'].get('unit', 'km')
            factor = MI_TO_KM if unit == 'mi' else 1.0
            source = {p: subject['distances'][p] * factor for p, _ in BANDS}
            if subject['distances'].get('p90') is not None:
                source['p90'] = subject['distances']['p90'] * factor
            label = subject['name']

        if not source['p25'] < source['p50'] < source['p75']:
            raise unprocessable([{'pointer': '/subject/distances',
                                  'detail': 'p25, p50 and p75 must be strictly increasing'}])
        if 'p90' in source and not source['p90'] > source['p75']:
            raise unprocessable([{'pointer': '/subject/distances/p90',
                                  'detail': 'p90 must be greater than p75'}])

        if mode == 'none' or (mode == 'auto' and cat is None):
            applied, mult = 'none', {'m25': 1.0, 'm50': 1.0, 'm75': 1.0}
        elif mode == 'auto' and cat.get('calibration'):
            applied, mult = 'category', cat['calibration']
        else:
            if not ds.get('default_calibration'):
                raise unprocessable([{'pointer': '/calibration',
                                      'detail': f'dataset {ds_id!r} has no default calibration'}])
            applied = 'global' if mode == 'global' else 'dataset_default'
            mult = ds['default_calibration']

        final = {p: round(source[p] * mult[m], 4) for p, m in BANDS}
        if not final['p25'] < final['p50'] < final['p75']:
            raise unprocessable([{'pointer': '/calibration',
                                  'detail': f'calibrated distances are not increasing: {final}'}])
        if 'p90' in source and source['p90'] > final['p75']:
            final['p90'] = round(source['p90'], 4)  # never calibrated
        source = {p: round(v, 4) for p, v in source.items()}
        return {
            'dataset': ds_id if (cat is not None or applied != 'none') else body.get('dataset'),
            'subject_label': label,
            'variant': variant_out,
            'source_distances_km': {**source, 'unit': 'km'},
            'calibration_applied': applied,
            'multipliers': {m: mult[m] for _, m in BANDS},
            'final_distances_km': {**final, 'unit': 'km'},
            'radius_km': round(final.get('p90', final['p75']) + 2.0, 4),
        }


def _pick_variant(variants, eco, terrain):
    for want in ((eco, terrain), (eco, None), (None, None)):
        for v in variants:
            if (v['eco_region'], v['terrain']) == want:
                return v
    return variants[-1]


def _variant_suffix(v):
    parts = [p for p in (v['eco_region'], v['terrain']) if p]
    return f" ({', '.join(parts)})" if parts else ''
