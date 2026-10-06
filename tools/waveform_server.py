"""Command server. Stdout contains only the historical five CSV fields."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr
import subprocess
import sys
import threading
import time
from pathlib import Path

from tools import host_waveform as host
from tools.capture_store import CaptureStore, RecordingSerial

PROJECT = Path(__file__).resolve().parents[1]


class DiagnosticTee:
    def __init__(self, log, console):
        self.log, self.console = log, console

    def write(self, value):
        self.log.write(value)
        self.console.write(value)
        return len(value)

    def flush(self):
        self.log.flush()
        self.console.flush()

    def fileno(self):
        # Child compiler processes write directly into the persistent log.
        return self.log.fileno()


def build_api(path=None):
    target = Path(path) if path else host.ROOT / host.LIBRARY_NAME
    if path is not None:
        return host.load_api(str(target.resolve()))

    sources = [PROJECT / name for name in (
        "waveform_analyzer.c", "waveform_analyzer.h", "waveform_cascade.c", "tools/host_api.c")]
    command = (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(host.ROOT / "build_host_waveform.ps1")] if sys.platform == "win32"
               else ["bash", str(host.ROOT / "build_host_waveform.sh")])

    def rebuild():
        subprocess.run(command, cwd=PROJECT, stdout=sys.stderr, stderr=sys.stderr, check=True)

    # A DLL can be newer than the sources while still being an older build
    # (for example, when a branch switch leaves generated files in place).
    # Try the normal timestamp path first, then rebuild once when the ABI
    # probe rejects the library.  The probe runs in a short-lived process so
    # Windows releases the old DLL before the rebuild replaces it.
    if not target.exists() or any(p.stat().st_mtime_ns > target.stat().st_mtime_ns for p in sources):
        rebuild()
    else:
        probe = subprocess.run(
            [sys.executable, "-c",
             "import sys; from tools import host_waveform as h; "
             "\ntry: h.load_api(sys.argv[1])\n"
             "except Exception as e: print(f'{type(e).__name__}: {e}', file=sys.stderr); raise SystemExit(1)",
             str(target.resolve())],
            cwd=PROJECT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if probe.returncode:
            detail = probe.stderr.strip() or "unknown ABI error"
            print(f"host library ABI check failed ({detail}); rebuilding", file=sys.stderr, flush=True)
            rebuild()
    return host.load_api(str(target.resolve()))


class Engine:
    def __init__(self, api, output=None):
        self.api = api
        self.lock = threading.Lock()  # C cascade owns shared, non-reentrant workspaces.
        self.output = output or sys.stdout

    def process(self, payload, meta, record):
        with self.lock:
            started = time.perf_counter()
            try:
                line, details = host.analyze(self.api, payload, meta)
            except Exception as error:
                if record:
                    record.info["algorithm_error"] = str(error)
                    record.save()
                raise
            elapsed = time.perf_counter() - started
            if record:
                record.result(line, details, elapsed)
            print(line, file=self.output, flush=True)
        return line, details, elapsed


class SerialService:
    """Read unsolicited PB13 frames too, including while RT is running."""
    def __init__(self, port, store, engine, stderr=None):
        self.store, self.engine = store, engine
        self.stderr = stderr or sys.stderr
        self.raw_port = port
        self.port = RecordingSerial(port, store.path)
        self.stop = threading.Event()
        self.pending_lock = threading.Lock()
        self.request_lock = threading.Lock()
        self.pending = None
        self.pending_error = None
        self.pending_started = None
        self.thread = threading.Thread(target=self._reader, name="adc-uart-receiver", daemon=True)
        self.thread.start()

    def _signal(self, error=None):
        with self.pending_lock:
            if self.pending is not None:
                self.pending_error = error
                self.pending.set()
                return True
        return False

    def _reader(self):
        while not self.stop.is_set():
            try:
                raw = self.port.read_until(b"\n", 256)
                if not raw:
                    continue
                if not raw.endswith(b"\n"):
                    raise TimeoutError("serial line incomplete; received bytes saved in uart_rx.bin")
                line = raw.decode("ascii").rstrip("\r\n")
                if line.startswith("DUMP,"):
                    payload, meta, record = host.receive_frame(self.port, first_line=line, store=self.store)
                    with self.pending_lock:
                        requested = self.pending_started if self.pending is not None else None
                    if requested is not None:
                        record.info["request_to_received_s"] = time.perf_counter() - requested
                        record.save()
                    self.engine.process(payload, meta, record)
                    self._signal()
                elif line.startswith("ERR_"):
                    raise RuntimeError(f"board capture failed: {line}")
                elif line in ("CAPTURING", "READY", "") or line.startswith(("TI-CUP ", "ADC=", "READY:")):
                    continue
                else:
                    raise ValueError(f"unexpected board response: {line}")
            except Exception as error:
                if self.stop.is_set():
                    break
                if not self._signal(error):
                    print(f"UART: {error}", file=self.stderr, flush=True)
                # A closed/device-disconnected port cannot be recovered by parsing.
                if hasattr(self.raw_port, "is_open") and not self.raw_port.is_open:
                    break
                if isinstance(error, OSError):
                    break

    def measure(self, timeout=10.):
        with self.request_lock:
            if not self.thread.is_alive():
                raise RuntimeError("serial receiver is stopped; restart the server")
            event = threading.Event()
            with self.pending_lock:
                self.pending, self.pending_error = event, None
                self.pending_started = time.perf_counter()
            try:
                if self.port.write(b"R") != 1:
                    raise OSError("serial command write incomplete")
                self.port.flush()
                if not event.wait(timeout):
                    raise TimeoutError("R exceeded 10 s; received bytes remain archived")
                if self.pending_error:
                    raise self.pending_error
            finally:
                with self.pending_lock:
                    self.pending = self.pending_started = None

    def close(self):
        self.stop.set()
        self.raw_port.close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("serial receiver did not stop; UART archive left open")
        self.port.close()


def parser():
    p = argparse.ArgumentParser(description="R: external ADC measurement; RT: complete host self-test matrix.")
    p.add_argument("command", nargs="?", type=str.upper, choices=("R", "RT"))
    p.add_argument("--port", help="XDS110 UART, e.g. COM6 or /dev/ttyACM0; RT works without hardware")
    p.add_argument("--dll", help="explicit existing host library; default builds automatically when needed")
    p.add_argument("--data-dir", type=Path, default=PROJECT / "data")
    p.add_argument("--sample-csv", action="store_true", help="also export all raw sample voltages to CSV (large)")
    p.add_argument("--verbose", action="store_true", help="also show diagnostic messages on stderr; default IDE output is only five fields")
    p.add_argument("--suite", choices=("all", "competition"), default="all",
                   help="board-era matrix: 200 preliminary cases plus 96 evaluated challenges")
    p.add_argument("--seed", type=int, default=20261005)
    p.add_argument("--random-count", type=int, default=512,
                   help="accepted for CLI compatibility; the restored board-era matrix is fixed")
    p.add_argument("--case-id", action="append", default=[], help="repeat a deterministic case ID")
    p.add_argument("--max-cases", type=int, help="explicit short smoke run; does not constitute full coverage")
    p.add_argument("--list-matrix", action="store_true", help="save selected matrix JSON without running")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.seed < 0 or args.random_count < 0 or (args.max_cases is not None and args.max_cases < 1):
        parser().error("seed/random-count must be nonnegative; max-cases must be positive")
    try:
        store = CaptureStore(args.data_dir, args.sample_csv)
        with (store.path / "server.log").open("w", encoding="utf-8", buffering=1) as diagnostics:
            sink = DiagnosticTee(diagnostics, sys.stderr) if args.verbose else diagnostics
            with redirect_stderr(sink):
                return run_server(args, store)
    except Exception as error:
        print(f"server: {error}", file=sys.stderr)
        return 2


def run_server(args, store):
    service = None
    try:
        print(f"Data: {store.path}", file=sys.stderr, flush=True)
        if args.list_matrix:
            from dataclasses import asdict
            from tools.capture_store import atomic_json
            from tools.self_test import matrix
            cases = [c for c in matrix(args.seed, args.random_count)
                     if (args.suite == "all" or c.suite == args.suite) and
                     (not args.case_id or c.id in args.case_id)]
            if args.max_cases is not None:
                cases = cases[:args.max_cases]
            atomic_json(store.path / "matrix.json", {"seed": args.seed, "cases": [asdict(c) for c in cases]})
            print(f"Matrix: {len(cases)} cases; {store.path/'matrix.json'}", file=sys.stderr)
            return 0
        engine = Engine(build_api(args.dll))
        if args.port:
            import serial
            port = serial.Serial(args.port, 115200, timeout=3, write_timeout=3)
            service = SerialService(port, store, engine)

        def dispatch(command):
            if command == "R":
                if service is None:
                    raise ValueError("R requires --port COM6 (or your XDS110 UART)")
                service.measure()
                return 0
            if command == "RT":
                from tools.self_test import run
                return run(engine.api, store, engine.process, suite=args.suite, seed=args.seed,
                           random_count=args.random_count, case_ids=args.case_id,
                           max_cases=args.max_cases)
            if command in ("Q", "QUIT", "EXIT"):
                return None
            raise ValueError("enter R, RT or Q")

        if args.command:
            return dispatch(args.command)
        print("R = external measurement; RT = host self-test; Q = quit", file=sys.stderr, flush=True)
        for line in sys.stdin:
            command = line.strip().upper()
            if not command:
                continue
            try:
                code = dispatch(command)
                if code is None:
                    break
                if code:
                    print("RT regression failures: see summary.json", file=sys.stderr, flush=True)
            except Exception as error:
                print(f"server: {error}", file=sys.stderr, flush=True)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"server: {error}", file=sys.stderr, flush=True)
        return 2
    finally:
        if service:
            service.close()
