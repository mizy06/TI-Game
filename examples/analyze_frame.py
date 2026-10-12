"""Run the complete server algorithm on one synthetic 200 Hz ADC frame."""
import math
import struct
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import host_waveform as host
from tools.waveform_server import build_api, measurement_output


def main():
    values = [round(2048 + 1000 * math.sin(2 * math.pi * 200 * i / 20000))
              for i in range(host.SAMPLES)]
    payload = struct.pack(f"<{host.SAMPLES}H", *values)
    metadata = host.Metadata(
        sequence=1, sample_rate_millihz=20000000,
        reference_microvolts=3300000, gain_ppm=1000000,
        adc_full_scale=4095, flags=1, checksum=host.fnv1a(payload))
    api = build_api()
    # A shared application must use one common lock for every C invocation.
    algorithm_lock = threading.Lock()
    with algorithm_lock:
        internal_line, details = host.analyze(api, payload, metadata)
    if not details["ok"]:
        raise RuntimeError(f"measurement rejected: status={details['status']}")
    print(measurement_output(internal_line))
    print(f"ABI=0x{details['algorithm_version']:08x}, "
          f"sequence={details['sequence']}, quality_flags={details['quality_flags']}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
