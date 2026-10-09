"""Command server. Stdout: RESULT,type,frequency_hz,vpp_v,duty_pct."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from contextlib import redirect_stderr
from dataclasses import asdict
from datetime import datetime, timezone
import subprocess
import sys
import threading
import time
from pathlib import Path

from tools import host_waveform as host
from tools.acquisition_calibration import load_latest_calibration, save_latest_calibration
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


def measurement_output(line):
    """Format successful recognition for the contest's IDE result protocol."""
    fields = line.split(",")
    if fields[0] not in host.NAMES[:-1]:
        return ""
    duty = fields[3] if fields[0] == "square" else "0"
    return f"RESULT,{','.join(fields[:3])},{duty}"


class Engine:
    def __init__(self, api, output=None, stderr=None):
        self.api = api
        self.lock = threading.Lock()  # C cascade owns shared, non-reentrant workspaces.
        self.output = output or sys.stdout
        self.stderr = stderr or sys.stderr

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
            output = measurement_output(line)
            if record:
                record.info["display_output"] = output
                record.result(line, details, elapsed)
            if output:
                print(output, file=self.output, flush=True)
            else:
                print(f"measurement rejected: status={details['status']}", file=self.stderr, flush=True)
        return line, details, elapsed


class SerialService:
    """Read unsolicited PB13 frames too, including while RT is running."""
    RESPONSE_GRACE_S = 3.

    def __init__(self, port, store, engine, stderr=None, *, parameters_path=None):
        self.store, self.engine = store, engine
        self.stderr = stderr or sys.stderr
        self.raw_port = port
        self.port = RecordingSerial(port, store.path)
        self.stop = threading.Event()
        self.receiver_stop = threading.Event()
        self.reconnect_needed = False
        self.receiver_error = None
        self.request_sequence = 0
        self.connection_log_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.request_lock = threading.Lock()
        self.pending = None
        self.pending_error = None
        self.pending_result = None
        self.pending_started = None
        self.pending_stage = None
        self.pending_raw = False
        self.parameters_path = Path(parameters_path if parameters_path is not None else
                                    store.path.parent / "algorithm_parameters.json").resolve()
        self.calibration_profile = None
        self.use_saved_calibration = True
        try:
            self.refresh_calibration()
        except (ValueError, OSError) as error:
            # Raw RO reference captures can repair a broken parameters file.
            # Ordinary R must report the error instead of silently going nominal.
            print(f"R calibration: {error}; run RO to replace the parameters", file=self.stderr, flush=True)
        self._connection_event("opened", port=getattr(port, "port", None))
        self._start_reader()

    def _connection_event(self, event, **details):
        value = {"utc": datetime.now(timezone.utc).isoformat(), "event": event,
                 "request": self.request_sequence, "received_bytes": self.port.wire.tell(), **details}
        with self.connection_log_lock:
            with (self.store.path / "connection.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(value, ensure_ascii=False) + "\n")
                stream.flush()

    def _start_reader(self):
        self.receiver_stop.clear()
        self.receiver_error = None
        self.thread = threading.Thread(target=self._reader, name="adc-uart-receiver", daemon=True)
        self.thread.start()

    def _reconnect(self, deadline):
        """Stop the sole reader, archive queued RX bytes, then reopen this port."""
        if not callable(getattr(self.raw_port, "open", None)):
            raise RuntimeError("serial port cannot reopen; restart the server")
        self.reconnect_needed = True
        self._connection_event("reconnect_started")
        self.receiver_stop.set()
        try:
            cancel = getattr(self.raw_port, "cancel_read", None)
            if callable(cancel) and getattr(self.raw_port, "is_open", True):
                cancel()
            self.thread.join(timeout=max(0., min(1., deadline - time.perf_counter())))
            if self.thread.is_alive():
                # Closing releases a stalled driver read when cancellation did not.
                self.raw_port.close()
                self.thread.join(timeout=max(0., min(1., deadline - time.perf_counter())))
            if self.thread.is_alive():
                raise RuntimeError("serial receiver did not stop during recovery; no second reader was started")
            drained = 0
            if getattr(self.raw_port, "is_open", False):
                # Preserve bytes still queued in the old handle before open()
                # initializes the driver. Never reset or clear the RX buffer.
                while time.perf_counter() < deadline:
                    waiting = getattr(self.raw_port, "in_waiting", 0)
                    if not waiting:
                        break
                    data = self.port.read(min(waiting, 8192))
                    drained += len(data)
                    if not data:
                        break
            self.raw_port.close()
            if time.perf_counter() >= deadline:
                raise TimeoutError("serial recovery exhausted the R deadline")
            self.raw_port.open()
            self._start_reader()
            self.reconnect_needed = False
            self._connection_event("reconnect_complete", queued_bytes_archived=drained)
            print("UART: reopened the connection after no board response", file=self.stderr, flush=True)
        except Exception as error:
            self._connection_event("reconnect_failed", error=f"{type(error).__name__}: {error}")
            raise

    @property
    def calibration(self):
        profile = self.calibration_profile
        return profile.calibration if self.use_saved_calibration and profile is not None else None

    def refresh_calibration(self):
        profile = load_latest_calibration(self.parameters_path)
        if profile != self.calibration_profile:
            self.calibration_profile = profile
            if profile is not None:
                print(f"R calibration loaded: {self.parameters_path}; RO at {profile.source['calibrated_utc']}",
                      file=self.stderr, flush=True)
            else:
                print("R calibration: no saved parameters; using nominal board metadata",
                      file=self.stderr, flush=True)
        return profile

    def publish_calibration(self, report):
        profile = save_latest_calibration(self.parameters_path, report)
        self.calibration_profile = profile
        return profile

    def _signal(self, error=None, result=None):
        with self.pending_lock:
            if self.pending is not None and not self.pending.is_set():
                self.pending_error = error
                self.pending_result = result
                self.pending.set()
                return True
        return False

    def _stage(self, stage):
        with self.pending_lock:
            if self.pending is not None:
                self.pending_stage = stage

    def _reader(self):
        while not self.stop.is_set() and not self.receiver_stop.is_set():
            try:
                raw = self.port.read_until(b"\n", 256)
                if not raw:
                    continue
                if not raw.endswith(b"\n"):
                    raise TimeoutError("serial line incomplete; received bytes saved in uart_rx.bin")
                line = raw.decode("ascii").rstrip("\r\n")
                if line.startswith("DUMP,"):
                    self._stage("receiving ADC frame")
                    payload, meta, record = host.receive_frame(self.port, first_line=line, store=self.store)
                    with self.pending_lock:
                        requested = self.pending_started if self.pending is not None else None
                        raw_only = self.pending_raw if self.pending is not None else False
                    if requested is not None:
                        record.info["request_to_received_s"] = time.perf_counter() - requested
                        record.save()
                    if raw_only:
                        self._signal(result=(payload, meta, record))
                    else:
                        try:
                            profile = self.refresh_calibration() if self.use_saved_calibration else None
                            if profile is not None:
                                record.info.update(acquisition_calibration=profile.calibration.to_dict(),
                                    acquisition_calibration_source=profile.source,
                                    algorithm_parameters_file=str(self.parameters_path))
                                record.save()
                                payload, meta = profile.calibration.apply(payload, meta)
                        except Exception as error:
                            record.info["calibration_error"] = str(error)
                            record.save()
                            raise
                        if profile is not None:
                            with (record.path / "samples.calibrated.adc.bin").open("wb") as stream:
                                stream.write(payload)
                                stream.flush()
                                os.fsync(stream.fileno())
                            record.info.update(analysis_metadata=asdict(meta),
                                analysis_payload_file="samples.calibrated.adc.bin",
                                analysis_sha256=hashlib.sha256(payload).hexdigest())
                            record.save()
                        self._stage("analyzing ADC frame")
                        result = self.engine.process(payload, meta, record)
                        self._signal(result=(*result, meta, record))
                elif line.startswith("ERR_"):
                    raise RuntimeError(f"board capture failed: {line}")
                elif line == "CAPTURING":
                    self._stage("board ADC/DMA capture")
                elif line in ("READY", "") or line.startswith(("TI-CUP ", "ADC=", "READY:")):
                    continue
                else:
                    raise ValueError(f"unexpected board response: {line}")
            except Exception as error:
                if self.stop.is_set() or self.receiver_stop.is_set():
                    break
                self._connection_event("receiver_error", error=f"{type(error).__name__}: {error}")
                if not self._signal(error):
                    print(f"UART: {error}", file=self.stderr, flush=True)
                # A closed/device-disconnected port cannot be recovered by parsing.
                if ((hasattr(self.raw_port, "is_open") and not self.raw_port.is_open) or
                        (isinstance(error, OSError) and not isinstance(error, TimeoutError))):
                    self.receiver_error = str(error)
                    self.reconnect_needed = True
                    break

    def capture(self, timeout=10.):
        """Archive a dedicated reference without analysis or measurement stdout."""
        return self.measure(timeout, return_result=True, raw=True)

    def measure(self, timeout=10., *, return_result=False, raw=False):
        with self.request_lock:
            self.request_sequence += 1
            started = time.perf_counter()
            deadline = started + timeout
            if self.reconnect_needed or not self.thread.is_alive():
                self._reconnect(deadline)
            event = threading.Event()
            with self.pending_lock:
                self.pending, self.pending_error = event, None
                self.pending_result = None
                self.pending_raw = raw
                self.pending_started = started
                self.pending_stage = "waiting for board response"
            try:
                self._connection_event("request_started", timeout_s=timeout, raw_reference=raw)
                if self.port.write(b"R") != 1:
                    raise OSError("serial command write incomplete")
                self._connection_event("command_sent", attempt=1)
                # Serial.write already submits the byte and has write_timeout.
                # A driver-level flush/drain can block without that timeout.
                event.wait(max(0., min(self.RESPONSE_GRACE_S, deadline - time.perf_counter())))
                with self.pending_lock:
                    silent = not event.is_set() and self.pending_stage == "waiting for board response"
                if (silent and deadline - time.perf_counter() > .25 and
                        callable(getattr(self.raw_port, "open", None))):
                    self._connection_event("no_response", stage="waiting for board response")
                    self._reconnect(deadline)
                    # A late complete frame received while stopping the old
                    # reader already satisfies this request; do not send twice.
                    if not event.is_set():
                        if self.port.write(b"R") != 1:
                            raise OSError("serial recovery command write incomplete")
                        self._connection_event("command_sent", attempt=2)
                if not event.wait(max(0., deadline - time.perf_counter())):
                    with self.pending_lock:
                        stage = self.pending_stage
                    if stage == "waiting for board response":
                        self.reconnect_needed = callable(getattr(self.raw_port, "open", None))
                    self._connection_event("request_timeout", stage=stage,
                                           receiver_alive=self.thread.is_alive())
                    raise TimeoutError(f"R exceeded {timeout:g} s at {stage}; "
                                       "received bytes remain archived; this request has ended")
                if self.pending_error:
                    raise self.pending_error
                self._connection_event("request_complete", elapsed_s=time.perf_counter() - started)
                if return_result:
                    return self.pending_result
            except Exception as error:
                if isinstance(error, OSError) and not isinstance(error, TimeoutError):
                    self.reconnect_needed = True
                self._connection_event("request_failed", error=f"{type(error).__name__}: {error}")
                raise
            finally:
                with self.pending_lock:
                    self.pending = self.pending_started = None
                    self.pending_stage = None
                    self.pending_raw = False

    def close(self):
        self.stop.set()
        self.raw_port.close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("serial receiver did not stop; UART archive left open")
        self.port.close()


def parser():
    p = argparse.ArgumentParser(description="R: external ADC measurement; RT: complete host self-test matrix.")
    p.add_argument("command", nargs="?", type=str.upper, choices=("R", "RT", "RO"))
    p.add_argument("--port", help="XDS110 UART, e.g. COM6 or /dev/ttyACM0; RT and the RO "
                                  "encoding run without hardware")
    p.add_argument("--dll", help="explicit existing host library; default builds automatically when needed")
    p.add_argument("--data-dir", type=Path, default=PROJECT / "data")
    p.add_argument("--algorithm-parameters", type=Path, default=PROJECT / "algorithm_parameters.json",
                   help="persistent parameters from the latest successful RO calibration; R loads them automatically")
    p.add_argument("--sample-csv", action="store_true", help="also export all raw sample voltages to CSV (large)")
    p.add_argument("--verbose", action="store_true", help="also show progress on stderr; stdout is RESULT,type,frequency,vpp,duty (0 for non-square)")
    p.add_argument("--suite", choices=("all", "competition"), default="all",
                   help="board-era matrix: 200 preliminary cases plus 96 evaluated challenges")
    p.add_argument("--seed", type=int, default=20261005)
    p.add_argument("--random-count", type=int, default=512,
                   help="accepted for CLI compatibility; the restored board-era matrix is fixed")
    p.add_argument("--case-id", action="append", default=[], help="repeat a deterministic case ID")
    p.add_argument("--max-cases", type=int, help="explicit short smoke run; does not constitute full coverage")
    p.add_argument("--list-matrix", action="store_true", help="save selected matrix JSON without running")
    p.add_argument("--ro-host", default="10.11.13.220", help="RO: Siglent SDG6022X-E address")
    p.add_argument("--ro-port", type=int, default=111, help="RO: VXI-11 portmapper port (111, not the 5025 SCPI socket)")
    p.add_argument("--ro-channel", type=int, choices=(1, 2), default=1, help="RO: connected output channel")
    p.add_argument("--ro-timeout", type=float, default=10., help="RO: SCPI/R timeout in seconds")
    p.add_argument("--ro-settle", type=float, default=.15, help="RO: output settling time in seconds")
    p.add_argument("--ro-level-mode", choices=("adc", "source"), default="adc",
                   help="RO: map original ADC codes to this board; source keeps original volts if safe")
    p.add_argument("--ro-calibrate", choices=("auto", "off"), default="auto",
                   help="RO: calibrate the connected ADC with independent generator references before the matrix; off uses nominal board metadata")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.seed < 0 or args.random_count < 0 or (args.max_cases is not None and args.max_cases < 1):
        parser().error("seed/random-count must be nonnegative; max-cases must be positive")
    try:
        store = CaptureStore(args.data_dir, args.sample_csv)
        with (store.path / "server.log").open("w", encoding="utf-8", buffering=1) as diagnostics:
            errors = DiagnosticTee(diagnostics, sys.stderr)
            sink = errors if args.verbose else diagnostics
            with redirect_stderr(sink):
                return run_server(args, store, error_output=errors)
    except Exception as error:
        print(f"server: {error}", file=sys.stderr)
        return 2


def run_server(args, store, error_output=None):
    service = None
    errors = error_output or sys.stderr
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
        engine = Engine(build_api(args.dll), stderr=errors)
        # Connection layer: the board link is optional.  A missing or busy UART
        # must not take the whole server down, because RO only needs the board
        # for the R leg; its matrix encoding and generator upload run without it.
        if args.port:
            import serial
            try:
                port = serial.Serial(args.port, 115200, timeout=3, write_timeout=3)
            except Exception as error:
                print(f"UART {args.port} unavailable: {error}; continuing without the board "
                      "(R needs the port, RT is host-only, RO will encode and upload only)",
                      file=errors, flush=True)
            else:
                service = SerialService(port, store, engine, stderr=errors,
                                        parameters_path=args.algorithm_parameters)

        def dispatch(command):
            if command == "R":
                if service is None:
                    raise ValueError("R requires --port COM6 (your XDS110 UART port): "
                                     "restart with python server.py --port COM6; "
                                     "no ADC frame was acquired")
                service.measure()
                return 0
            if command == "RT":
                from tools.self_test import run
                return run(engine.api, store, engine.process, suite=args.suite, seed=args.seed,
                           random_count=args.random_count, case_ids=args.case_id,
                           max_cases=args.max_cases)
            if command == "RO":
                # service may be None: then RO runs the generator encoding without the board.
                from tools.ro_test import run
                return run(service, store, args, stderr=errors)
            if command in ("Q", "QUIT", "EXIT"):
                return None
            raise ValueError("enter R, RT or Q; RO = generator matrix test")

        if args.command:
            return dispatch(args.command)
        print("R = external measurement; RT = host self-test; "
              "RO = generator matrix test (board optional); Q = quit", file=sys.stderr, flush=True)
        for line in sys.stdin:
            command = line.strip().upper()
            if not command:
                continue
            try:
                code = dispatch(command)
                if code is None:
                    break
                if code:
                    print(f"{command} regression failures: see summary.json", file=errors, flush=True)
            except Exception as error:
                print(f"server: {error}", file=errors, flush=True)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"server: {error}", file=errors, flush=True)
        return 2
    finally:
        if service:
            service.close()
