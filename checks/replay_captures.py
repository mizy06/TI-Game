"""Re-evaluate saved UART captures with their original metadata and RO limits."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import host_waveform as host
from tools.ro_test import evaluate_ro
from tools.self_test import Case
from tools.unified_solver import SolverConfig


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('report', type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--failed-only', action='store_true')
    p.add_argument('--case-id', action='append')
    p.add_argument('--trace', action='store_true')
    p.add_argument('--seconds', type=float, default=10.)
    p.add_argument('--solver', type=Path, help='Diagnostic replay of a saved solver revision')
    p.add_argument('--frequency-min', type=float, default=200., help='Inclusive lower test-frequency limit (default: 200 Hz)')
    p.add_argument('--frequency-below', type=float, default=600., help='Exclusive upper test-frequency limit (default: 600 Hz)')
    p.add_argument('--noise-below', type=float, default=.5, help='Exclusive noise_pp_ratio limit (default: 0.5)')
    a = p.parse_args()
    report = a.report
    session = report.parent
    rows = json.loads((report / 'results.json').read_text(encoding='utf-8'))['results']
    solver_path = a.solver or ROOT/'tools/unified_solver.py'
    solver_sha = hashlib.sha256(solver_path.read_bytes()).hexdigest()
    config_class = SolverConfig
    if a.solver:
        spec = importlib.util.spec_from_file_location('capture_replay_solver', solver_path)
        saved = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = saved
        spec.loader.exec_module(saved)
        import tools.unified_solver as active
        active.estimate = saved.estimate
        config_class = saved.SolverConfig
    a.output.mkdir(parents=True, exist_ok=True)
    results = []
    with (a.output / 'results.jsonl').open('w', encoding='utf-8') as log:
        for row in rows:
            if not (a.frequency_min <= row['case']['frequency_hz'] < a.frequency_below
                    and row['case'].get('noise_pp_ratio', 0.) < a.noise_below):
                continue
            if a.failed_only and row['result']['passed']:
                continue
            if a.case_id and row['case_id'] not in a.case_id:
                continue
            cap = session / row['capture']
            payload = (cap / 'samples.adc.bin').read_bytes()
            info = json.loads((cap / 'capture.json').read_text(encoding='utf-8'))
            meta = host.Metadata(**row['metadata'])
            sha = hashlib.sha256(payload).hexdigest()
            if (info['validation'] != 'valid' or len(payload) != meta.payload_bytes or
                    host.fnv1a(payload) != meta.checksum or sha != info['analysis_sha256']):
                raise ValueError(f"Raw capture integrity failure: {cap}")
            candidates = []

            def trace(frame, event, value):
                if event == 'return' and frame.f_code.co_name == 'score' and value is not None:
                    loc = frame.f_locals
                    if 'fit' not in loc:
                        return
                    fit = loc['fit']
                    cand = {k: fit[k] for k in ('kind', 'order', 'a')}
                    cand.update(q=fit['q'].tolist(), score=float(value) if math.isfinite(value) else None,
                                admitted=math.isfinite(value),
                                residual_rms=float((fit['residual']**2).mean()**.5),
                                innovation_energy=float(fit['innovation_energy']),
                                observation_model=fit.get('cycle') if isinstance(fit.get('cycle'),dict) else None)
                    for key in ('ratio', 'limit', 'stable_ratio', 'aperture', 'local_sigma', 'local_variance'):
                        if key in loc:
                            cand[key] = float(loc[key])
                    if 'valid' in loc:
                        cand['valid_fraction'] = float(loc['valid'].mean()) if loc['valid'] is not None else None
                    candidates.append(cand)

            started = time.perf_counter()
            if a.trace:
                sys.setprofile(trace)
            try:
                line, details = host.analyze(None, payload, meta, config=config_class(max_seconds=a.seconds))
            except (TimeoutError, ValueError) as error:
                line, details = 'ERROR,analysis', dict(ok=False, error=f'{type(error).__name__}: {error}')
            finally:
                sys.setprofile(None)
            elapsed = time.perf_counter() - started
            case = replace(Case(**row['case']), fs_hz=meta.sample_rate_hz_exact or meta.sample_rate_millihz/1000,
                           vref_v=meta.reference_microvolts/1e6, gain=meta.gain_ppm/1e6,
                           vpp_v=row['generator']['truth']['vpp_v'], dc_v=row['generator']['truth']['dc_v'])
            check = evaluate_ro(case, line, details, elapsed, row['generator'])
            result = dict(case_id=row['case_id'], sha256=sha, capture=row['capture'], result=check,
                          algorithm=details, recorded_result=row['result'])
            if a.trace:
                result['candidates'] = candidates
            results.append(result)
            log.write(json.dumps(result, allow_nan=False) + '\n')
            log.flush()
            print(row['case_id'], check['verdict'], line, '; '.join(check['failure_reasons']), flush=True)
    summary = dict(report=str(report), raw_captures=len(results),
                   frequency_min_hz=a.frequency_min,
                   frequency_below_hz=a.frequency_below,
                   noise_below=a.noise_below,
                   excluded_frequency_cases=sum(not (a.frequency_min <= r['case']['frequency_hz'] < a.frequency_below and r['case'].get('noise_pp_ratio', 0.) < a.noise_below) for r in rows),
                   passed=sum(r['result']['passed'] for r in results),
                   restored=[r['case_id'] for r in results if not r['recorded_result']['passed'] and r['result']['passed']],
                   regressed=[r['case_id'] for r in results if r['recorded_result']['passed'] and not r['result']['passed']],
                   failed=[r['case_id'] for r in results if not r['result']['passed']],
                   failures=dict(Counter(k for r in results for k in r['result']['failure_reasons'])),
                   solver_sha256=solver_sha,
                   solver_changed_during_replay=solver_sha != hashlib.sha256(solver_path.read_bytes()).hexdigest())
    (a.output / 'summary.json').write_text(json.dumps(summary, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
