"""Command server. Stdout: RESULT,type,frequency_hz,vpp_v,duty_pct."""
from __future__ import annotations

import argparse
import json
from contextlib import redirect_stderr
from dataclasses import asdict, replace
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
    def __init__(self, api, output=None, stderr=None, config=None, *, workers=1):
        self.api = api
        self.config = config
        self.lock = threading.Lock()  # Serialize output and acquisition records.
        self.output = output or sys.stdout
        self.stderr = stderr or sys.stderr
        if not 1 <= workers <= 8:
            raise ValueError('R workers must be 1..8')
        from tools.parallel_analysis import FrameAnalysisPool
        self.parallel_pool = FrameAnalysisPool(workers) if workers > 1 else None

    def warm(self):
        if self.parallel_pool is not None:
            self.parallel_pool.warm()

    def close(self):
        if self.parallel_pool is not None:
            self.parallel_pool.close()

    def process_frames(self, captures, *, deadline):
        """Fit independent frames concurrently; archive only on the parent."""
        pool = self.parallel_pool
        acquired = self.lock.acquire(timeout=max(0., deadline-time.perf_counter()))
        try:
            if not acquired:
                raise TimeoutError('R deadline exhausted waiting for analysis; no RESULT emitted')
            pool.last_results = []
            # Normally prewarmed before opening the UART. Recovery after a
            # failed request also counts against the next request's deadline.
            pool.warm(timeout=max(0., deadline-time.perf_counter()))
            started = time.perf_counter()
            fitted = pool.analyze([(p, m) for p, m, _ in captures], self.config, deadline)
            elapsed = time.perf_counter()-started
            for (_, _, record), (line, details, frame_elapsed) in zip(captures, fitted):
                record.info['display_output'] = ''
                record.result(line, details, frame_elapsed)
            if time.perf_counter() >= deadline:
                raise TimeoutError('R parallel archival deadline exhausted; no RESULT emitted')
            return fitted, elapsed
        except Exception as error:
            # Preserve any already completed/rejected frames. Children never
            # own records or stdout, including on cancellation or worker loss.
            for index, (_, _, record) in enumerate(captures):
                if acquired and index < len(pool.last_results) and pool.last_results[index] is not None:
                    line, details, frame_elapsed = pool.last_results[index]
                    record.result(line, details, frame_elapsed)
                record.info.update(algorithm_error=str(error), display_output='')
                if isinstance(error, TimeoutError):
                    record.info['expired_request'] = True
                record.save()
            raise
        finally:
            if acquired:
                self.lock.release()

    def process(self, payload, meta, record, *, deadline=None, emit=True):
        def check_deadline(stage):
            if deadline is not None and time.perf_counter() >= deadline:
                raise TimeoutError(f"R deadline exhausted at {stage}; no RESULT emitted")

        acquired = (self.lock.acquire() if deadline is None else
                    self.lock.acquire(timeout=max(0., deadline - time.perf_counter())))
        try:
            if not acquired:
                raise TimeoutError("R deadline exhausted waiting for analysis; no RESULT emitted")
            started = time.perf_counter()
            check_deadline("analyzing ADC frame")
            config = self.config
            if deadline is not None:
                from tools.unified_solver import SolverConfig
                config = config or SolverConfig()
                config = replace(config, max_seconds=min(config.max_seconds,
                                                         deadline - time.perf_counter()))
                check_deadline("analyzing ADC frame")
            line, details = host.analyze(self.api, payload, meta, config=config)
            elapsed = time.perf_counter() - started
            check_deadline("analyzing ADC frame")
            output = measurement_output(line)
            if record:
                record.info["display_output"] = output if emit else ""
                record.result(line, details, elapsed)
            check_deadline("archiving ADC analysis")
            if emit and output:
                print(output, file=self.output, flush=True)
            elif emit:
                print(f"measurement rejected: status={details['status']}", file=self.stderr, flush=True)
            return line, details, elapsed
        except Exception as error:
            if record:
                record.info.update(algorithm_error=str(error), display_output="")
                if deadline is not None and isinstance(error, TimeoutError):
                    record.info["expired_request"] = True
                record.save()
            raise
        finally:
            if acquired:
                self.lock.release()


class SerialService:
    """Read unsolicited PB13 frames too, including while RT is running."""
    RESPONSE_GRACE_S = 3.
    FRAME_READ_TIMEOUT_S = .5
    RX_BUFFER_BYTES = 64 * 1024

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
        self.request_lock = threading.RLock()
        self.pending = None
        self.pending_error = None
        self.pending_result = None
        self.pending_started = None
        self.pending_stage = None
        self.drop_until_ready = False
        self.resynchronizing = False
        self.parameters_path = Path(parameters_path if parameters_path is not None else
                                    store.path.parent / "algorithm_parameters.json").resolve()
        self.calibration_profile = None
        self.use_saved_calibration = True
        try:
            self.refresh_calibration()
        except (ValueError, OSError) as error:
            # Raw RO reference captures can repair a broken parameters file.
            # Ordinary R must report the error instead of silently going nominal.
            print(f"R calibration: {error}; run RS to replace the parameters", file=self.stderr, flush=True)
        self._configure_buffers()
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

    def _configure_buffers(self):
        # Windows' default driver queue can be smaller than one 16 KiB ADC
        # frame. Allow headroom for USB bursts and occasional filesystem stalls.
        configure = getattr(self.raw_port, "set_buffer_size", None)
        if callable(configure):
            try:
                configure(rx_size=self.RX_BUFFER_BYTES)
                self._connection_event("rx_buffer_configured", bytes=self.RX_BUFFER_BYTES)
            except (OSError, NotImplementedError) as error:
                self._connection_event("rx_buffer_unavailable", error=str(error))
                print(f"UART: receive buffer sizing unavailable: {error}", file=self.stderr, flush=True)

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
            self._configure_buffers()
            self._start_reader()
            self.reconnect_needed = False
            self._connection_event("reconnect_complete", queued_bytes_archived=drained)
            print("UART: reopened the connection for receive recovery", file=self.stderr, flush=True)
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
                print(f"R calibration loaded: {self.parameters_path}; {profile.source['command']} at {profile.source['calibrated_utc']}",
                      file=self.stderr, flush=True)
            else:
                print("R calibration: no saved parameters; using nominal board metadata",
                      file=self.stderr, flush=True)
        return profile

    def publish_calibration(self, report):
        profile = save_latest_calibration(self.parameters_path, report)
        self.calibration_profile = profile
        return profile

    def _signal(self, request, error=None, result=None):
        with self.pending_lock:
            if request is not None and self.pending is request and not request.is_set():
                self.pending_error = error
                self.pending_result = result
                request.set()
                return True
        return False

    def _stage(self, stage):
        with self.pending_lock:
            if self.pending is not None:
                self.pending_stage = stage

    def _reader(self):
        while not self.stop.is_set() and not self.receiver_stop.is_set():
            frame_started = False
            try:
                raw = self.port.read_until(b"\n", 256)
                with self.pending_lock:
                    request, requested = self.pending, self.pending_started
                if not raw:
                    continue
                if self.resynchronizing:
                    # The old frame can leave binary bytes or READY in flight.
                    # Only the next acquisition's CAPTURING boundary permits
                    # interpreting another frame as this request's response.
                    if raw.rstrip(b"\r\n") == b"CAPTURING" and request is not None:
                        self.resynchronizing = self.drop_until_ready = False
                        self._stage("board ADC/DMA capture")
                        self._connection_event("frame_resynchronized")
                    continue
                if not raw.endswith(b"\n"):
                    raise TimeoutError("serial line incomplete; received bytes saved in uart_rx.bin")
                line = raw.decode("ascii").rstrip("\r\n")
                if line.startswith("DUMP,"):
                    frame_started = True
                    self._stage("receiving ADC frame")
                    # A short timeout applies only to an active binary frame.
                    # read_exact can span several reads, but a missing tail must
                    # not consume two Windows 3 s reads before recovery starts.
                    old_timeout = getattr(self.raw_port, "timeout", None)
                    has_timeout = hasattr(self.raw_port, "timeout")
                    try:
                        if has_timeout:
                            self.raw_port.timeout = (self.FRAME_READ_TIMEOUT_S if old_timeout is None else
                                min(old_timeout, self.FRAME_READ_TIMEOUT_S))
                        payload, meta, record = host.receive_frame(self.port, first_line=line, store=self.store)
                    finally:
                        if has_timeout:
                            self.raw_port.timeout = old_timeout
                    if requested is not None:
                        record.info["request_to_received_s"] = time.perf_counter() - requested
                        record.save()
                    with self.pending_lock:
                        expired = request is not None and self.pending is not request
                    if self.drop_until_ready or expired:
                        record.info["expired_request"] = True
                        record.save()
                        # receive_frame already consumed this block's READY.
                        self.drop_until_ready = False
                        continue
                    if request is not None:
                        # The requesting thread owns analysis and its deadline;
                        # the sole UART reader remains available for reception.
                        self._signal(request, result=(payload, meta, record))
                    else:
                        try:
                            profile = self.refresh_calibration() if self.use_saved_calibration else None
                            from tools.precision_batch import prepare_analysis
                            payload, meta = prepare_analysis(payload, meta, record, profile, self.parameters_path)
                        except Exception as error:
                            record.info["calibration_error"] = str(error)
                            record.save()
                            raise
                        self.engine.process(payload, meta, record)
                elif line.startswith("ERR_"):
                    raise RuntimeError(f"board capture failed: {line}")
                elif line == "CAPTURING":
                    self._stage("board ADC/DMA capture")
                elif line in ("READY", "") or line.startswith(("TI-CUP ", "ADC=", "READY:")):
                    if line == "READY":
                        self.drop_until_ready = False
                    continue
                else:
                    raise ValueError(f"unexpected board response: {line}")
            except Exception as error:
                if self.stop.is_set() or self.receiver_stop.is_set():
                    break
                if isinstance(error, host.FrameReceiveError):
                    # Stop parsing immediately: a late binary tail must not be
                    # treated as ASCII or satisfy a later request.
                    self.resynchronizing = True
                    self.receiver_error = str(error)
                    self.reconnect_needed = True
                self._connection_event("receiver_error", error=f"{type(error).__name__}: {error}")
                if not frame_started:
                    with self.pending_lock:
                        request = self.pending
                if not self._signal(request, error=error):
                    print(f"UART: {error}", file=self.stderr, flush=True)
                if isinstance(error, host.FrameReceiveError):
                    break
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
                self.pending_started = started
                self.pending_stage = "waiting for board response"
            try:
                self._connection_event("request_started", timeout_s=timeout, raw_reference=raw)
                attempt = 1
                while True:
                    if time.perf_counter() >= deadline:
                        raise TimeoutError("serial recovery exhausted the R deadline")
                    if self.port.write(b"R") != 1:
                        raise OSError("serial command write incomplete")
                    self._connection_event("command_sent", attempt=attempt)
                    # A driver-level flush/drain can block without write_timeout.
                    event.wait(max(0., min(self.RESPONSE_GRACE_S, deadline - time.perf_counter())))
                    with self.pending_lock:
                        silent = not event.is_set() and self.pending_stage == "waiting for board response"
                    if (silent and attempt == 1 and deadline - time.perf_counter() > .25 and
                            callable(getattr(self.raw_port, "open", None))):
                        self._connection_event("no_response", stage="waiting for board response")
                        self._reconnect(deadline)
                        # A late complete frame can already satisfy this request.
                        if not event.is_set():
                            attempt += 1
                            continue
                    if not event.wait(max(0., deadline - time.perf_counter())):
                        with self.pending_lock:
                            stage = self.pending_stage
                            self.drop_until_ready = True
                        self.reconnect_needed = callable(getattr(self.raw_port, "open", None))
                        self.resynchronizing = True
                        self._connection_event("request_timeout", stage=stage,
                                               receiver_alive=self.thread.is_alive())
                        raise TimeoutError(f"R exceeded {timeout:g} s at {stage}; "
                                           "received bytes remain archived; this request has ended")
                    error = self.pending_error
                    if (isinstance(error, host.FrameReceiveError) and attempt == 1 and
                            deadline - time.perf_counter() > .25 and
                            callable(getattr(self.raw_port, "open", None))):
                        self._connection_event("frame_retry", error=str(error))
                        print(f"UART: {error}; retrying this acquisition once within the R deadline",
                              file=self.stderr, flush=True)
                        self._reconnect(deadline)
                        with self.pending_lock:
                            event.clear()
                            self.pending_error = self.pending_result = None
                            self.pending_stage = "waiting for board response"
                        attempt += 1
                        continue
                    if error:
                        raise error
                    break
                result = self.pending_result
                if not raw:
                    payload, meta, record = result
                    self._stage("analyzing ADC frame")
                    try:
                        profile = self.refresh_calibration() if self.use_saved_calibration else None
                        from tools.precision_batch import prepare_analysis
                        payload, meta = prepare_analysis(payload, meta, record, profile, self.parameters_path)
                    except Exception as error:
                        record.info["calibration_error"] = str(error)
                        record.save()
                        raise
                    result = (*self.engine.process(payload, meta, record, deadline=deadline), meta, record)
                self._connection_event("request_complete", elapsed_s=time.perf_counter() - started)
                if return_result:
                    return result
            except Exception as error:
                if isinstance(error, OSError) and not isinstance(error, TimeoutError):
                    self.reconnect_needed = True
                self._connection_event("request_failed", error=f"{type(error).__name__}: {error}")
                raise
            finally:
                with self.pending_lock:
                    self.pending = self.pending_started = None
                    self.pending_stage = None

    def close(self):
        self.stop.set()
        self.raw_port.close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("serial receiver did not stop; UART archive left open")
        self.port.close()


def parser():
    p = argparse.ArgumentParser(description="R: acquisition; RO: finals matrix; RT: self-test; RS: calibration only.")
    p.add_argument("command", nargs="?", type=str.upper, choices=("R", "RT", "RO", "RS"))
    p.add_argument("--port", help="XDS110 UART, e.g. COM6 or /dev/ttyACM0; RT and the RO "
                                  "encoding run without hardware")
    p.add_argument("--dll", help="explicit existing host library; default builds automatically when needed")
    p.add_argument("--data-dir", type=Path, default=PROJECT / "data")
    p.add_argument("--algorithm-parameters", type=Path, default=PROJECT / "algorithm_parameters.json",
                   help="persistent parameters from the latest successful RS calibration; loaded automatically")
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
    p.add_argument("--ro-transport", choices=("vxi11", "visa"), default="visa",
                   help="RO: generator transport; visa uses USBTMC")
    p.add_argument("--ro-resource",
                   default="USB0::0xF4EC::0x1101::SDG6XFCQ900843::INSTR",
                   help="RO: VISA USBTMC resource (used with --ro-transport visa)")
    p.add_argument("--ro-visa-backend", default=None,
                   help="RO: optional VISA backend, e.g. @py or @ivi")
    p.add_argument("--ro-channel", type=int, choices=(1, 2), default=1, help="RO: connected output channel")
    p.add_argument("--ro-timeout", type=float, default=14., help="RO: SCPI timeout and total per-case R deadline in seconds")
    p.add_argument("--ro-settle", type=float, default=.15, help="RO: output settling time in seconds")
    p.add_argument("--ro-stop-on-failure", action="store_true",
                   help="RO: save the first failed verdict and shut down the generator immediately")
    p.add_argument("--ro-vpp-calibrate", action=argparse.BooleanOptionalAction, default=True,
                   help="RO: independently measure dense ADC and ARB voltage transfers for Vpp")
    p.add_argument("--ro-oversample",type=int,choices=(1,8,16,32),default=32,
                   help="RO: replay identifiable finals source at a finer time/voltage resolution")
    p.add_argument('--ro-reuse-reference-report',help='RO: reuse archived independent ADC/ARB references')
    p.add_argument("--ro-level-mode", choices=("adc", "source"), default="adc",
                   help="RO: map original ADC codes to this board; source keeps original volts if safe")
    p.add_argument("--ro-calibrate", choices=("saved", "auto", "off"), default="saved",
                   help="RO: saved loads the latest independent references; auto recalibrates; off uses nominal metadata. RS always recalibrates")
    p.add_argument("--r-frames", type=int, default=3,
                   help="R/RO: acquire 1 to 8 successive frames before fitting and averaging; default 3")
    p.add_argument("--r-workers", type=int, default=3,
                   help="R/RO: persistent independent frame-fit processes, 1..8; 1 selects serial; default 3")
    p.add_argument("--r-timeout", type=float, default=15.,
                   help="R total capture/analysis/archive deadline, 0 < value <= 15 s")
    p.add_argument("--solver-fmin", type=float, default=0., help="search lower Hz; 0 uses 4 cycles/frame")
    p.add_argument("--solver-fmax", type=float, default=0., help="search upper Hz; 0 uses fs/12")
    p.add_argument("--solver-starts", type=int, default=2, help="independent frequency starting points")
    p.add_argument("--solver-seconds", type=float, default=10., help="per-frame solver budget, at most 12 s")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.seed < 0 or args.random_count < 0 or (args.max_cases is not None and args.max_cases < 1):
        parser().error("seed/random-count must be nonnegative; max-cases must be positive")
    if not 1 <= args.r_frames <= 8 or not 1 <= args.r_workers <= 8 or not 0 < args.r_timeout <= 15:
        parser().error("r-frames/r-workers must be 1..8 and r-timeout must be (0, 15] seconds")
    if not 0 < args.ro_timeout <= 14:
        parser().error("ro-timeout must be (0, 14] seconds")
    import math
    if (not all(math.isfinite(x) for x in (args.solver_fmin,args.solver_fmax,args.solver_seconds)) or
            min(args.solver_fmin,args.solver_fmax)<0 or args.solver_starts<1 or
            not 0 < args.solver_seconds <= 12 or
            (args.solver_fmax and args.solver_fmin >= args.solver_fmax)):
        parser().error("invalid unified solver range or budget")
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
    engine = None
    errors = error_output or sys.stderr
    try:
        print(f"Data: {store.path}", file=sys.stderr, flush=True)
        try:
            profile = load_latest_calibration(args.algorithm_parameters)
        except ValueError as error:
            profile = None
            print(f"Calibration: {error}; RS = repair", file=errors, flush=True)
        print(f"Calibration: {args.algorithm_parameters}" if profile is not None else
              "Calibration: no saved parameters; RS = calibrate", file=errors, flush=True)
        if args.list_matrix:
            from dataclasses import asdict
            from tools.capture_store import atomic_json
            if args.command == "RO":
                from tools.ro_test import select_ro_cases
                from tools.identifiable_finals import DESIGN_SEED
                cases = select_ro_cases(args)
                matrix_seed = DESIGN_SEED
            else:
                from tools.self_test import matrix
                matrix_seed = args.seed
                cases = [c for c in matrix(args.seed, args.random_count)
                         if (args.suite == "all" or c.suite == args.suite) and
                         (not args.case_id or c.id in args.case_id)]
                if args.max_cases is not None:
                    cases = cases[:args.max_cases]
            atomic_json(store.path / "matrix.json", {"seed": matrix_seed, "cases": [asdict(c) for c in cases]})
            print(f"Matrix: {len(cases)} cases; {store.path/'matrix.json'}", file=sys.stderr)
            return 0
        from tools.unified_solver import SolverConfig
        engine = Engine(build_api(args.dll) if args.dll else None, stderr=errors,
            config=SolverConfig(frequency_min_hz=args.solver_fmin,frequency_max_hz=args.solver_fmax,
                starts=args.solver_starts,max_seconds=args.solver_seconds),
            workers=(min(args.r_workers, args.r_frames) if args.command != 'RT' else 1))
        engine.warm()
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
                from tools.precision_batch import measure_batch
                measure_batch(service, args.r_frames, args.r_timeout)
                return 0
            if command == "RT":
                from tools.self_test import run
                return run(engine.api, store, engine.process, suite=args.suite, seed=args.seed,
                           random_count=args.random_count, case_ids=args.case_id,
                           max_cases=args.max_cases)
            if command in ("RO", "RS"):
                # service may be None: then RO runs the generator encoding without the board.
                from tools.ro_test import run
                return run(service, store, args, stderr=errors, calibration_only=command == "RS")
            if command in ("Q", "QUIT", "EXIT"):
                return None
            raise ValueError("enter R, RO, RT, RS or Q")

        if args.command:
            return dispatch(args.command)
        print("R = external measurement; RT = host self-test; "
              "RO = 296-case finals matrix; RS = calibration only; Q = quit", file=errors, flush=True)
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
        try:
            if service:
                service.close()
        finally:
            if engine:
                engine.close()
