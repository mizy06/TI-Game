"""Replay every archived RO case through the public multi-frame path."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import threading
import time

# Spawned analysis workers must import the same reviewed project as the parent.
if os.environ.get('TI_REPLAY_PROJECT'):
    sys.path.insert(0, os.environ['TI_REPLAY_PROJECT'])


class Record:
    def __init__(self, path):
        self.path, self.info = path, {}

    def save(self):
        pass

    def result(self, line, details, elapsed):
        self.info.update(output=line, algorithm=details, algorithm_elapsed_s=elapsed)


class Replay:
    use_saved_calibration = False
    parameters_path = Path('algorithm_parameters.json')

    def __init__(self, engine, root, captures):
        self.engine = engine
        self.store = type('Store', (), {'path': root})()
        self.request_lock = threading.RLock()
        self.captures = iter(captures)

    def capture(self, timeout):
        payload, meta, path = next(self.captures)
        return payload, meta, Record(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path, help='Completed RO directory containing results.json')
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case-id', action='append', default=[])
    parser.add_argument('--follow', action='store_true', help='Replay a running 296-case RO journal as captures finish')
    args = parser.parse_args()
    os.environ['TI_REPLAY_PROJECT'] = str(args.project.resolve())
    sys.path.insert(0, str(args.project.resolve()))
    from tools.capture_store import atomic_json
    from tools.host_waveform import Metadata, fnv1a
    from tools.precision_batch import measure_batch
    from tools.unified_solver import SolverConfig
    from tools.waveform_server import Engine

    if args.follow:
        if args.case_id:
            raise ValueError('--follow requires the complete matrix')
        matrix = json.loads((args.report/'matrix.json').read_text())
        selected_ids = [r['id'] for r in matrix['cases']]
        if len(selected_ids) != 296 or len(set(selected_ids)) != 296:
            raise ValueError('Full matrix requires all 296 distinct cases')

        def follow_rows():
            index = 0
            last_progress = time.monotonic()
            while index < 296:
                lines = (args.report/'results.jsonl').read_text().splitlines()
                if index < len(lines):
                    try:
                        row = json.loads(lines[index])
                    except json.JSONDecodeError:
                        time.sleep(.5)
                        continue
                    if row['case_id'] != selected_ids[index]:
                        raise ValueError('Live journal order differs from saved matrix')
                    yield row
                    index += 1
                    last_progress = time.monotonic()
                    continue
                final = args.report/'results.json'
                if final.exists():
                    source = json.loads(final.read_text())
                    if not source['complete'] and time.monotonic()-last_progress > 120:
                        raise ValueError('Live RO stopped before completing the matrix')
                time.sleep(.5)

        rows = follow_rows()
        total = 296
        identity = args.report/'matrix.json'
    else:
        original = json.loads((args.report/'results.json').read_text())
        if not original['complete']:
            raise ValueError('Source matrix is incomplete')
        rows = original['results']
        if args.case_id:
            rows = [r for r in rows if r['case_id'] in args.case_id]
        elif len(rows) != 296 or len({r['case_id'] for r in rows}) != 296:
            raise ValueError('Full matrix requires all 296 distinct cases')
        total = len(rows)
        identity = args.report/'results.json'
    args.output.mkdir(parents=True, exist_ok=False)
    root = args.report.resolve().parent
    files = ['tools/unified_solver.py', 'tools/host_waveform.py', 'tools/precision_batch.py',
             'tools/square_duty.py', 'tools/acquisition_calibration.py', 'tools/waveform_server.py']
    files = [f for f in files if (args.project/f).is_file()]
    atomic_json(args.output/'provenance.json', dict(source_report=str(args.report.resolve()),
        source_identity_file=identity.name, source_sha256=hashlib.sha256(identity.read_bytes()).hexdigest(),
        project=str(args.project.resolve()),
        files={f: hashlib.sha256((args.project/f).read_bytes()).hexdigest() for f in files}))
    engine = Engine(None, output=io.StringIO(), config=SolverConfig(max_seconds=10.), workers=3)
    results = []
    try:
        engine.warm()
        with (args.output/'results.jsonl').open('w', encoding='utf-8', buffering=1) as journal:
            for row in rows:
                captures, hashes = [], []
                for path in row['captures']:
                    directory = (root/path).resolve()
                    if not directory.is_relative_to(root):
                        raise ValueError('Capture escapes source session')
                    info = json.loads((directory/'capture.json').read_text())
                    payload = (directory/'samples.adc.bin').read_bytes()
                    meta = Metadata(**info.get('analysis_metadata', info['metadata']))
                    sha = hashlib.sha256(payload).hexdigest()
                    if fnv1a(payload) != meta.checksum or len(payload) != meta.payload_bytes:
                        raise ValueError('Original ADC identity failed')
                    if info.get('analysis_sha256', sha) != sha:
                        raise ValueError('Original analysis SHA-256 differs')
                    captures.append((payload, meta, directory))
                    hashes.append(sha)
                result = dict(case_id=row['case_id'], wave=row['case']['wave'], sha256=hashes,
                    original_measured=row['r_batch']['measured'])
                try:
                    service = Replay(engine, root, captures)
                    line, details, elapsed, meta, record = measure_batch(
                        service, len(captures), 15., return_result=True)
                    truth = row['result']['truth']
                    result.update(measured=record.info['r_batch']['measured'], output=line,
                        frequency_error_pct=100*abs(details['frequency_hz']/truth['frequency_hz']-1),
                        vpp_error_pct=100*abs(details['vpp_v']/truth['vpp_v']-1),
                        duty_error_pp=(abs(details['duty_fraction']*100-truth['duty_pct'])
                                       if row['case']['wave']=='square' else 0.),
                        algorithm_elapsed_s=elapsed,
                        classification_correct=line.split(',')[0]==row['case']['wave'])
                except Exception as error:
                    result['error'] = f'{type(error).__name__}: {error}'
                results.append(result)
                journal.write(json.dumps(result)+'\n')
                print(f'{len(results)}/{total} {row["case_id"]} '
                      f'{result.get("output", result.get("error"))}', flush=True)
    finally:
        engine.close()
        atomic_json(args.output/'summary.json', dict(selected=total, completed=len(results),
            complete=len(results)==total,
            errors=[r['case_id'] for r in results if 'error' in r],
            classification_correct=sum(r.get('classification_correct', False) for r in results)))
    return int(any('error' in r for r in results))


if __name__ == '__main__':
    sys.exit(main())
