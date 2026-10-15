#!/usr/bin/env python3
"""Replay original UART ADC payloads from data.zip. Never generate samples."""
import argparse, hashlib, json, sys, time, zipfile
from pathlib import Path, PurePosixPath


def replay(project, archive, output, session=None):
    project = project.resolve()
    sys.path.insert(0, str(project))
    from tools.host_waveform import Metadata, analyze, fnv1a
    from tools.unified_solver import SolverConfig
    here = Path(__file__).resolve().parent
    expected = json.loads((here/'manifest.json').read_text())['files']['tools/unified_solver.py']
    actual = hashlib.sha256((project/'tools/unified_solver.py').read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError('Install this exact patch before replaying; solver SHA-256 differs.')
    records = []
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
        candidates = []
        for name in names:
            if not name.endswith('/results.json') or '/ro-' not in name:
                continue
            if session and session not in name:
                continue
            obj = json.loads(z.read(name))
            rows = obj.get('results', [])
            if rows:
                candidates.append((len(rows), name, rows))
        if not candidates:
            raise ValueError('No completed RO result rows found. Original data.zip is required.')
        candidates.sort(key=lambda x: (-x[0], x[1]))
        if len(candidates)>1 and candidates[0][0] == candidates[1][0]:
            raise ValueError('Multiple completed sessions: select one with --session.')
        _, result_path, rows = candidates[0]
        base = PurePosixPath(result_path).parent.parent
        output.mkdir(parents=True, exist_ok=True)
        print('Replaying original ADC frames from', result_path, flush=True)
        with (output/'raw_replay.jsonl').open('w', encoding='utf-8') as log:
            for row in rows:
                capture = PurePosixPath(row['capture'])
                if capture.is_absolute() or '..' in capture.parts:
                    raise ValueError('Unsafe capture reference')
                cap = base/capture
                raw_path = str(cap/'samples.adc.bin')
                payload = z.read(raw_path)
                info = json.loads(z.read(str(cap/'capture.json')))
                sha = hashlib.sha256(payload).hexdigest()
                if info.get('analysis_payload_file') != 'samples.adc.bin' or sha != info['analysis_sha256']:
                    raise ValueError('Original analysis payload identity failed: '+raw_path)
                meta = Metadata(**row['metadata'])
                if len(payload) != meta.payload_bytes or fnv1a(payload) != meta.checksum:
                    raise ValueError('ADC length/checksum mismatch: '+raw_path)
                tic = time.perf_counter()
                rec = dict(case_id=row['case_id'], raw_path=raw_path, sha256=sha,
                           recorded_pass=row['result']['passed'], recorded_result=row['result'])
                try:
                    line, details = analyze(None, payload, meta, config=SolverConfig(max_seconds=10.))
                    rec.update(output=line, algorithm=details)
                    c = row['case']
                    freq, vpp, duty = c['frequency_hz'], c['vpp_v'], c.get('duty', .5)
                    correct = details['ok'] and line.split(',')[0] == c['wave']
                    rec['classification_correct'] = bool(correct)
                    fe = abs(details['frequency_hz']/freq-1)
                    ae = abs(details['vpp_v']/vpp-1)
                    de = abs(details['duty_fraction']-duty) if c['wave']=='square' else 0.
                    rec['frequency_vpp_duty_pass'] = bool(correct and fe<.01 and ae<.1 and de<.025)
                    rec['relative_frequency_error'] = fe
                    rec['relative_vpp_error'] = ae
                    rec['duty_fraction_error'] = de
                    # Per-case DC limits belong to the original test driver.
                    # Do not silently invent a full engineering-pass decision.
                    rec['full_original_engineering_pass'] = None
                except Exception as exc:
                    rec.update(error=type(exc).__name__+': '+str(exc), classification_correct=False,
                               frequency_vpp_duty_pass=False, full_original_engineering_pass=None)
                rec['elapsed_s'] = time.perf_counter()-tic
                records.append(rec)
                log.write(json.dumps(rec, ensure_ascii=False)+'\n'); log.flush()
                print(row['case_id'], rec.get('output', rec.get('error')), flush=True)
    summary = dict(input_type='original_uart_adc', session=result_path, cases=len(records),
                   classification_correct=sum(r['classification_correct'] for r in records),
                   frequency_vpp_duty_pass=sum(r['frequency_vpp_duty_pass'] for r in records),
                   full_original_engineering_pass=None,
                   note='DC and full pass must be evaluated by the original per-case thresholds; raw results retained.',
                   solver_sha256=actual)
    (output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', required=True, type=Path)
    p.add_argument('--data', required=True, type=Path)
    p.add_argument('--output', default=Path('raw-replay-8.1'), type=Path)
    p.add_argument('--session')
    a=p.parse_args()
    replay(a.project, a.data, a.output, a.session)
