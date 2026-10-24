"""Compare complete raw-ADC matrix replays without hiding existing failures."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.capture_store import atomic_json


def compare(before, after):
    def read(root):
        summary = json.loads((root/'summary.json').read_text())
        rows = [json.loads(line) for line in (root/'results.jsonl').read_text().splitlines()]
        indexed = {r['case_id']: r for r in rows}
        if summary['completed'] != 296 or len(rows) != 296 or len(indexed) != 296:
            raise ValueError('Comparison requires two complete 296-case replays')
        return indexed

    old, new = read(before), read(after)
    if set(old) != set(new):
        raise ValueError('Matrix case IDs differ')
    if any(old[i]['sha256'] != new[i]['sha256'] for i in new):
        raise ValueError('Comparison must use identical ADC bytes')
    report = dict(before=str(before.resolve()), after=str(after.resolve()), cases=296,
                  identical_adc=True, errors_before=[], errors_after=[], metrics={}, numerical_changes={})
    report['errors_before'] = [i for i in old if 'error' in old[i]]
    report['errors_after'] = [i for i in new if 'error' in new[i]]
    valid = [i for i in new if 'error' not in old[i] and 'error' not in new[i]]
    for field in ('frequency_hz', 'vpp_v', 'duty_fraction', 'dc_offset_v'):
        report['numerical_changes'][field] = {
            group: max((abs(new[i]['measured'][field]-old[i]['measured'][field])
                for i in valid if group=='all' or new[i]['wave']!='square'), default=None)
            for group in ('all', 'non_square')}
    for field in ('frequency_error_pct', 'vpp_error_pct', 'duty_error_pp'):
        ids = [i for i in valid if field != 'duty_error_pp' or new[i]['wave']=='square']
        metric = dict(count=len(ids))
        for name, rows in (('before', old), ('after', new)):
            values = [rows[i][field] for i in ids]
            metric[name] = dict(mean=sum(values)/len(values), maximum=max(values))
        if field != 'duty_error_pp':
            old_fail = [i for i in ids if old[i][field] >= .1]
            new_fail = [i for i in ids if new[i][field] >= .1]
            metric.update(failed_before=old_fail, failed_after=new_fail,
                          new_failures=sorted(set(new_fail)-set(old_fail)))
        report['metrics'][field] = metric
    report['classification_correct_before'] = sum(old[i].get('classification_correct', False) for i in old)
    report['classification_correct_after'] = sum(new[i].get('classification_correct', False) for i in new)
    report['no_new_other_parameter_failures'] = (
        not set(report['errors_after'])-set(report['errors_before']) and
        report['classification_correct_after'] >= report['classification_correct_before'] and
        all(not m.get('new_failures') for m in report['metrics'].values()))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = compare(args.before, args.after)
    atomic_json(args.output, report)
    print(json.dumps(report, indent=2))
    return int(not report['no_new_other_parameter_failures'])


if __name__ == '__main__':
    raise SystemExit(main())
