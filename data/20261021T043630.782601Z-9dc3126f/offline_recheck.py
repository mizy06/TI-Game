"""Reanalyse archived ADC frames only; never opens serial or VISA devices."""
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1]))


def analyse(capture):
    from tools.host_waveform import Metadata, analyze
    from threadpoolctl import threadpool_limits
    directory = ROOT / capture
    saved = json.loads((directory / 'capture.json').read_text(encoding='utf-8'))
    meta = Metadata(**saved['analysis_metadata'])
    with threadpool_limits(limits=1):
        _, result = analyze(None, (directory / 'samples.adc.bin').read_bytes(), meta)
    return dict(capture=capture, ok=result['ok'], vpp_v=result['vpp_v'],
                frequency_hz=result['frequency_hz'], type=result['type'],
                spectral_vpp=result.get('spectral_vpp'), vpp_quality=result['vpp_quality'])


def main():
    report = next(ROOT.glob('ro-*'))
    entries = [json.loads(x) for x in (report / 'results.jsonl').read_text().splitlines()]
    captures = [capture for entry in entries for capture in entry['captures']]
    results = {}
    with ProcessPoolExecutor(max_workers=3) as pool:
        for result in pool.map(analyse, captures):
            results[result['capture']] = result
            print('Reanalysed', len(results), '/', len(captures), flush=True)
    cases = []
    for entry in entries:
        frames = [results[c] for c in entry['captures']]
        mean = sum(f['vpp_v'] for f in frames) / len(frames)
        error = 100 * (mean / entry['result']['truth']['vpp_v'] - 1)
        cases.append(dict(case_id=entry['case_id'], original_vpp_bias_pct=entry['result']['vpp_bias_pct'],
                          reanalysed_vpp_v=mean, reanalysed_vpp_bias_pct=error,
                          vpp_within_limit=abs(error) < .1, frames=frames))
        print(entry['case_id'], 'original bias%', round(entry['result']['vpp_bias_pct'], 4),
              'reanalysed bias%', round(error, 4), flush=True)
    summary = dict(note='Offline reanalysis only; no fresh hardware measurements. '
                        'Reject the spectral override when its background-separation check fails. '
                        'Clean-source truth and the strict 0.1% limit are unchanged.',
                   cases=len(cases), frames=len(captures),
                   vpp_within_limit=sum(c['vpp_within_limit'] for c in cases), results=cases)
    path = report / 'offline_recheck.json'
    path.write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print('Saved', path, flush=True)


if __name__ == '__main__':
    main()
