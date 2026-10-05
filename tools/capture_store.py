"""Durable LE16 captures and exact received UART bytes, including failed frames."""
from __future__ import annotations

import csv
import json
import os
import struct
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path


def atomic_json(path, value):
    # A unique temporary name avoids collisions with scanners or another
    # record update; Windows may briefly deny the final replace while it scans.
    temp = path.with_suffix(path.suffix + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(8):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.025 * (attempt + 1))
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def export_samples(payload_path, meta, output_path):
    raw = Path(payload_path).read_bytes()
    values = struct.unpack(f"<{len(raw)//2}H", raw[:len(raw)//2*2])
    fs = meta.get("sample_rate_millihz", 0) / 1000
    adc_max = meta.get("adc_full_scale", 0)
    vref = meta.get("reference_microvolts", 0) / 1e6
    gain = meta.get("gain_ppm", 0) / 1e6
    with Path(output_path).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["index", "time_s", "adc_counts", "adc_voltage_v", "input_voltage_v"])
        for i, value in enumerate(values):
            voltage = value * vref / adc_max if adc_max else None
            writer.writerow([i, i / fs if fs else "", value,
                             voltage if voltage is not None else "",
                             voltage / gain if voltage is not None and gain else ""])
        stream.flush()
        os.fsync(stream.fileno())


class CaptureStore:
    def __init__(self, root, sample_csv=False):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self.path = Path(root).resolve() / (stamp + "-" + uuid.uuid4().hex[:8])
        self.path.mkdir(parents=True, exist_ok=False)
        self.sample_csv = sample_csv
        self.lock = threading.Lock()
        self.counter = 0
        atomic_json(self.path / "session.json", {
            "schema": 1, "created_utc": stamp, "encoding": "uint16 little-endian",
            "sample_csv": sample_csv, "uart_rx": "uart_rx.bin",
            "note": "Raw UART bytes are saved before protocol checks; frame payloads before analysis."})

    def begin(self, source, context=None):
        with self.lock:
            self.counter += 1
            directory = self.path / source / f"{self.counter:06d}"
            directory.mkdir(parents=True, exist_ok=False)
        return CaptureRecord(directory, source, context or {}, self.sample_csv)


class CaptureRecord:
    def __init__(self, path, source, context, sample_csv):
        self.path = path
        self.sample_csv = sample_csv
        self.info = {"schema": 1, "source": source, "context": context,
                     "created_utc": datetime.now(timezone.utc).isoformat(),
                     "validation": "receiving", "payload_bytes": 0,
                     "payload_file": "samples.adc.bin", "encoding": "<u2"}
        self.payload = path / "samples.adc.bin"
        self.payload.touch(exist_ok=False)
        self.save()

    def save(self):
        atomic_json(self.path / "capture.json", self.info)

    def set_metadata(self, meta):
        self.info["metadata"] = asdict(meta)
        self.save()

    def append(self, data):
        with self.payload.open("ab") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        self.info["payload_bytes"] += len(data)

    def complete(self, payload=None):
        if payload is not None and self.info["payload_bytes"] == 0:
            self.append(payload)
        self.info["validation"] = "valid"
        self.info["sample_count"] = self.info["payload_bytes"] // 2
        if self.sample_csv:
            self.export_csv()
        self.save()

    def fail(self, error):
        self.info["validation"] = "invalid"
        self.info["error"] = str(error)
        self.info["sample_count"] = self.payload.stat().st_size // 2
        self.info["payload_bytes"] = self.payload.stat().st_size
        if self.sample_csv:
            self.export_csv()
        self.save()

    def result(self, line, diagnostics, elapsed_s):
        self.info.update(output=line, algorithm=diagnostics, algorithm_elapsed_s=elapsed_s)
        self.save()

    def export_csv(self):
        export_samples(self.payload, self.info.get("metadata", {}), self.path / "samples.csv")


class RecordingSerial:
    """Every received byte reaches disk before the caller sees it. Never reset RX."""
    def __init__(self, port, path):
        self.port = port
        self.wire = (Path(path) / "uart_rx.bin").open("ab", buffering=0)

    def _record(self, data):
        if data:
            self.wire.write(data)
            os.fsync(self.wire.fileno())
        return data

    def read(self, count):
        return self._record(self.port.read(count))

    def read_until(self, *args):
        return self._record(self.port.read_until(*args))

    def write(self, data):
        return self.port.write(data)

    def flush(self):
        self.port.flush()

    def close(self):
        self.wire.close()
