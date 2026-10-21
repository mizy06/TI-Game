"""Check archived stimulus recipes and the current encoder; no instrument I/O."""
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1]))

from tools.identifiable_finals import noise_samples
from tools.ro_test import _finals_waveform, encode, ro_matrix


def main():
    report = ROOT / 'ro-1792557414809228700'
    entries = {e['case_id']: e for e in
               map(json.loads, (report / 'results.jsonl').read_text().splitlines())}
    results = []
    for case in ro_matrix():
        row = json.loads(case.source)
        size = row['samples'] * 32
        noise = np.fft.irfft(np.fft.rfft(noise_samples(row, case.seed)[0]), n=size) * 32
        sigma = row['noise'] * row['vpp'] / 6
        historical_noise = noise * (row['noise'] * row['vpp'] / np.ptp(noise))
        phase = np.arange(size) * row['frequency'] / (row['fs'] * 32) + row['phase']
        clean = row['dc_v'] + .5 * row['vpp'] * _finals_waveform(
            row['kind'], phase, row['duty'], row['rise'], row['fall'])
        historical = np.rint((2 * (clean + historical_noise) / 3.3 - 1) * 32767).astype('<i2')
        historical_hash = hashlib.sha256(historical.tobytes()).hexdigest()
        _, dac, config = encode(case, 3.3, 1., oversample=32)
        restored = 1.65 * (np.frombuffer(dac, dtype='<i2') / 32767 + 1)
        actual_sigma = float(np.std(restored - clean))
        saved = entries.get(case.id)
        results.append(dict(
            case_id=case.id, requested_noise_std_v=sigma,
            historical_noise_std_v=float(np.std(historical_noise)),
            historical_sigma_ratio=float(np.std(historical_noise) / sigma),
            historical_dac_sha256=historical_hash,
            matches_archived_dac_hash=(historical_hash == saved['generator']['uncompensated_dac_sha256']
                                      if saved else None),
            current_noise_std_v=actual_sigma, current_sigma_ratio=actual_sigma / sigma,
            current_dac_sha256=hashlib.sha256(dac).hexdigest()))
    archived = [r for r in results if r['matches_archived_dac_hash'] is not None]
    audit = dict(
        note='Offline stimulus fidelity only, not an accuracy or hardware result. '
             'Historical recipe explicitly reproduces the former peak-to-peak renormalization; '
             'current recipe must retain the published Gaussian sigma = noise_ratio * vpp / 6.',
        cases=len(results), archived_cases=len(archived),
        archived_hashes_matched=sum(r['matches_archived_dac_hash'] for r in archived),
        historical_sigma_ratio_range=[min(r['historical_sigma_ratio'] for r in results),
                                      max(r['historical_sigma_ratio'] for r in results)],
        current_sigma_ratio_range=[min(r['current_sigma_ratio'] for r in results),
                                   max(r['current_sigma_ratio'] for r in results)],
        results=results)
    path = report / 'offline_stimulus_audit.json'
    path.write_text(json.dumps(audit, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k:v for k,v in audit.items() if k != 'results'}, indent=2))
    print('Saved', path)


if __name__ == '__main__':
    main()
