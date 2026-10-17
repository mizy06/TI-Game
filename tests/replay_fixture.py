"""In-memory ADC replay fixtures for the current parallel-analysis tests."""
import threading
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

class MemoryRecord:
    def __init__(self, path):
        self.path, self.info = path, {}

    def save(self):
        pass

    def result(self, line, diagnostics, elapsed_s):
        self.info.update(output=line, algorithm=diagnostics, algorithm_elapsed_s=elapsed_s)


class ReplayService:
    use_saved_calibration = False
    parameters_path = ROOT/'algorithm_parameters.json'

    def __init__(self, engine, root, captures):
        self.engine = engine
        self.request_lock = threading.RLock()
        self.store = type('Store', (), {'path': root})()
        self.captures = iter(captures)
        self.records = []

    def capture(self, timeout):
        payload, meta, path = next(self.captures)
        record = MemoryRecord(path)
        self.records.append(record)
        return payload, meta, record


