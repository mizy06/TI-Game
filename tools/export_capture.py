"""Export an existing capture without changing its binary data or metadata."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.capture_store import export_samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, help="directory containing capture.json and samples.adc.bin")
    args = parser.parse_args()
    try:
        path = args.capture.resolve()
        info = json.loads((path / "capture.json").read_text(encoding="utf-8"))
        output = path / "samples.csv"
        export_samples(path / "samples.adc.bin", info.get("metadata", {}), output)
        print(output)
        return 0
    except Exception as error:
        print(f"export_capture: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
