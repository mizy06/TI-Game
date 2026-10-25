"""Persistent, isolated ADC-frame fits; acquisition and archival stay in the parent."""
from __future__ import annotations

import multiprocessing as mp
from multiprocessing.connection import wait
import os
import time


def _worker(connection):
    # Limit both imported and subsequently loaded numerical libraries. Each
    # process owns its BLAS state, so independent fits cannot change each other.
    for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
        os.environ[name] = '1'
    from tools import host_waveform as host
    from tools.unified_solver import SolverConfig
    from tools import vpp_spectral  # Preload the optional calibrated Vpp path.
    from dataclasses import replace
    from threadpoolctl import threadpool_limits
    try:
        with threadpool_limits(limits=1, user_api='blas'):
            connection.send(('ready', os.getpid()))
            while True:
                task = connection.recv()
                if task is None:
                    return
                index, payload, meta, config, deadline = task
                started = time.perf_counter()
                try:
                    remaining = deadline - started
                    if remaining <= 0:
                        raise TimeoutError('R deadline exhausted before parallel frame analysis')
                    cfg = config or SolverConfig()
                    cfg = replace(cfg, max_seconds=min(cfg.max_seconds, remaining))
                    line, details = host.analyze(None, payload, meta, config=cfg)
                    elapsed = time.perf_counter() - started
                    if time.perf_counter() >= deadline:
                        raise TimeoutError('R deadline exhausted during parallel frame analysis')
                    connection.send(('result', index, line, details, elapsed))
                except Exception as error:
                    connection.send(('error', index, type(error).__name__, str(error)))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


class FrameAnalysisPool:
    """A bounded spawn pool with a hard parent deadline and explicit cancellation.

    Only immutable sample bytes, metadata and solver config cross the pipes.
    Returned fits are restored to acquisition order before any averaging.
    """
    def __init__(self, workers):
        if not 2 <= workers <= 8:
            raise ValueError('parallel frame workers must be 2..8')
        self.workers = workers
        self._processes = []
        self._connections = []
        self.last_results = []

    def warm(self, timeout=30.):
        if self._processes and all(p.is_alive() for p in self._processes):
            return
        self.close()
        if timeout <= 0:
            raise TimeoutError('R deadline exhausted before parallel worker startup')
        deadline = time.perf_counter() + timeout
        context = mp.get_context('spawn')
        try:
            for _ in range(self.workers):
                parent, child = context.Pipe()
                process = context.Process(target=_worker, args=(child,),
                                          name='adc-frame-fit', daemon=True)
                try:
                    process.start()
                except BaseException:
                    parent.close()
                    child.close()
                    raise
                child.close()
                self._processes.append(process)
                self._connections.append(parent)
            pending = set(self._connections)
            while pending:
                ready = wait(pending, timeout=max(0., deadline-time.perf_counter()))
                if not ready:
                    raise TimeoutError('parallel ADC workers did not finish preloading')
                for connection in ready:
                    if connection.recv()[0] != 'ready':
                        raise RuntimeError('invalid parallel ADC worker startup')
                    pending.remove(connection)
        except BaseException:
            self.close()
            raise

    def analyze(self, captures, config, deadline, *, allow_rejected=False):
        """Caller serializes requests and prewarms outside the measurement deadline."""
        self.last_results = [None] * len(captures)
        if not self._processes or not all(p.is_alive() for p in self._processes):
            self.close()
            raise RuntimeError('parallel ADC worker unavailable; no RESULT emitted')
        next_index = 0
        active = {}

        def submit(connection):
            nonlocal next_index
            index = next_index
            payload, meta = captures[index]
            connection.send((index, payload, meta, config, deadline))
            active[connection] = index
            next_index += 1

        try:
            for connection in self._connections[:len(captures)]:
                submit(connection)
            while active:
                remaining = deadline-time.perf_counter()
                if remaining <= 0:
                    raise TimeoutError('R parallel analysis deadline exhausted; no RESULT emitted')
                ready = wait(active, timeout=remaining)
                if not ready:
                    raise TimeoutError('R parallel analysis deadline exhausted; no RESULT emitted')
                for connection in ready:
                    try:
                        message = connection.recv()
                    except EOFError as error:
                        raise RuntimeError('parallel ADC worker exited before result; no RESULT emitted') from error
                    index = active.pop(connection)
                    if message[1] != index:
                        raise RuntimeError('parallel ADC frame order mismatch')
                    if message[0] == 'error':
                        error_type = {'TimeoutError': TimeoutError, 'ValueError': ValueError}.get(message[2], RuntimeError)
                        raise error_type(f'R block {index+1}: {message[3]}; no RESULT emitted')
                    if message[0] != 'result':
                        raise RuntimeError('invalid parallel ADC worker response')
                    result = tuple(message[2:])
                    self.last_results[index] = result
                    if not result[1]['ok'] and not allow_rejected:
                        raise ValueError(f"R block {index+1} rejected: status={result[1]['status']}; no RESULT emitted")
                    if next_index < len(captures):
                        submit(connection)
            return self.last_results
        except BaseException:
            # No job may outlive an expired/rejected request, produce a late
            # result or consume a later request's CPU budget.
            self.close()
            raise

    def close(self):
        processes, self._processes = self._processes, []
        connections, self._connections = self._connections, []
        for process in processes:
            if process.is_alive():
                process.terminate()
        for connection in connections:
            connection.close()
        for process in processes:
            process.join(timeout=1.)
            if process.is_alive():
                process.kill()
                process.join(timeout=1.)
            process.close()
