#!/usr/bin/env python3
"""Strict end-to-end gate over all 12 noise families, including challenges."""
import argparse
import json
import sys
from pathlib import Path

from evaluate_cascade import library
from noise_cascade_matrix import GAUSSIAN, CHALLENGE, evaluate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--library', required=True)
    parser.add_argument('--seed', type=int, default=20260925)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    report = evaluate(library(args.library), GAUSSIAN + CHALLENGE, args.seed)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    failures = report['failures']
    passes = report['cases'] - len(failures)
    print(f'seed={args.seed} complete_noise_matrix={passes}/{report["cases"]} '
          f'failures={len(failures)} report={output}')
    for row in failures[:12]:
        print(f'FAIL #{row["index"]} {row["profile"]} {row["kind"]} '
              f'{row["freq"]}Hz {row["vpp_counts"]}counts: '
              f'{row["result"]["cascade"]}', file=sys.stderr)
    return 0 if report['cases'] == 1296 and not failures else 1


if __name__ == '__main__':
    raise SystemExit(main())
