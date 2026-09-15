"""Detectors. Each one reduces to "how many new events since I last looked".

A detector runs outside the thing it watches. None of these has a handle on the
agent VM; they read what the host already exports about it. A detector that raises
is reported as down, which the watchdog treats exactly as severely as one that
fires (D11).
"""

from __future__ import annotations

import re
import socket
import threading
from enum import IntEnum
from pathlib import Path
from typing import Callable


class DetectorId(IntEnum):
    """Spec §5.1, numbered as `sandbox.first_detector_id` reports them."""

    D1_DENIED_FLOW = 1
    D2_NON_VSOCK = 2
    D3_CANARY_HOST = 3
    D4_CANARY_CREDENTIAL = 4
    D5_VMM_SECCOMP = 5
    D6_MAC_DENIAL = 6
    D7_VMM_ANOMALY = 7
    D8_HOST_INTEGRITY = 8
    D9_BROKER_SCHEMA = 9
    D10_STATE_BYPASS = 10
    D11_LIVENESS = 11


class DetectorSeverity(IntEnum):
    SUSPICIOUS = 1
    ESCAPE = 2


SEVERITY = {
    DetectorId.D9_BROKER_SCHEMA: DetectorSeverity.SUSPICIOUS,
    DetectorId.D10_STATE_BYPASS: DetectorSeverity.SUSPICIOUS,
}

# Which egress metric each detector's events roll into (spec §7).
METRIC_FOR = {
    DetectorId.D1_DENIED_FLOW: "sandbox.denied_flows",
    DetectorId.D2_NON_VSOCK: "sandbox.denied_flows",
    DetectorId.D3_CANARY_HOST: "sandbox.canary_hits",
    DetectorId.D4_CANARY_CREDENTIAL: "sandbox.canary_hits",
    DetectorId.D5_VMM_SECCOMP: "sandbox.vmm_violations",
    DetectorId.D6_MAC_DENIAL: "sandbox.vmm_violations",
    DetectorId.D7_VMM_ANOMALY: "sandbox.vmm_violations",
}


class Detector:
    """Base: a cumulative counter the watchdog diffs between polls."""

    def __init__(self, detector_id: DetectorId) -> None:
        self.id = DetectorId(detector_id)
        self.severity = SEVERITY.get(self.id, DetectorSeverity.ESCAPE)

    def total(self) -> int:
        """Cumulative event count. Raise if the source cannot be read."""
        raise NotImplementedError


class CounterSource(Detector):
    """A detector over any callable that returns a cumulative count.

    This is how an eBPF map, a VMM's per-interface packet counter, or a test feeds
    the watchdog: the callable is the source, and the watchdog never learns more
    than an integer from it.
    """

    def __init__(self, detector_id: DetectorId, read: Callable[[], int]) -> None:
        super().__init__(detector_id)
        self._read = read

    def total(self) -> int:
        value = self._read()
        assert isinstance(value, int) and not isinstance(value, bool), value
        return value


class FileCounter(Detector):
    """A cumulative count exported as a decimal integer in a file.

    The shape `nft list counter` or an eBPF exporter writes on a timer. A missing
    or unparseable file raises, which the watchdog reports as the detector being
    down rather than as zero events.
    """

    def __init__(self, detector_id: DetectorId, path: Path) -> None:
        super().__init__(detector_id)
        self.path = Path(path)

    def total(self) -> int:
        return int(self.path.read_text().strip())


class LogPatternCounter(Detector):
    """Count lines matching a pattern in an append-only log.

    D5 reads `SIGSYS` events for the VMM process out of the audit log this way.
    The file is read from the last offset, so a rotated or truncated log raises
    rather than silently restarting from zero.
    """

    def __init__(self, detector_id: DetectorId, path: Path, pattern: str) -> None:
        super().__init__(detector_id)
        self.path = Path(path)
        self._pattern = re.compile(pattern)
        self._offset = 0
        self._count = 0

    def total(self) -> int:
        size = self.path.stat().st_size
        assert size >= self._offset, f"{self.path} shrank; log was rotated under the detector"
        with self.path.open("rb") as fh:
            fh.seek(self._offset)
            for line in fh:
                if self._pattern.search(line.decode("utf-8", "replace")):
                    self._count += 1
            self._offset = fh.tell()
        return self._count


class CanaryListener(Detector):
    """D3: a listener on an address nothing legitimate ever connects to.

    Reachable only by something that has left the agent zone. Any connection is a
    hit; the payload is never read, because there is nothing a connecting party
    could say that would change the answer.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        super().__init__(DetectorId.D3_CANARY_HOST)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(16)
        self.address = self._sock.getsockname()
        self._hits = 0
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            conn.close()
            with self._lock:
                self._hits += 1

    def total(self) -> int:
        assert not self._closed, "canary listener closed"
        with self._lock:
            return self._hits

    def close(self) -> None:
        self._closed = True
        self._sock.close()


class FileIntegrityDetector(Detector):
    """D8: the VMM binary, guest kernel, base rootfs, and broker code must not change.

    Hashes each path at construction. `total` is the number of paths whose hash
    has ever differed or that have gone missing; it never decreases, because a
    file restored after tampering is still a file that was tampered with.
    """

    def __init__(self, paths: list[Path]) -> None:
        super().__init__(DetectorId.D8_HOST_INTEGRITY)
        from blake3 import blake3

        self._hash = lambda p: blake3(Path(p).read_bytes()).hexdigest()
        self.paths = [Path(p) for p in paths]
        assert self.paths, "nothing to watch"
        self._expected = {p: self._hash(p) for p in self.paths}
        self._tripped: set[Path] = set()

    def total(self) -> int:
        for path, expected in self._expected.items():
            if not path.is_file() or self._hash(path) != expected:
                self._tripped.add(path)
        return len(self._tripped)


class StateBypassDetector(Detector):
    """D10: a change to a site's backing DB not attributable to an env-broker request.

    The site attributes every write to a writer. The env broker counts the writes
    it performed as `agent`. If the site holds more agent-written rows than the
    broker performed writes, something reached the database around the broker,
    which is either a broker client bug or an escape. Either halts the run.
    """

    def __init__(self, env_factory) -> None:
        super().__init__(DetectorId.D10_STATE_BYPASS)
        self._factory = env_factory
        self._peak = 0

    def total(self) -> int:
        import sqlite3

        from tools.compose_fastapi_sqlite_v1 import WRITER_COLUMN

        excess = 0
        for episode in list(self._factory.open_episodes):
            rows = 0
            for db_path in list(episode.db_paths.values()):
                db = sqlite3.connect(db_path)
                try:
                    tables = [r[0] for r in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                    for table in tables:
                        assert table.isidentifier(), table
                        if WRITER_COLUMN in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
                            rows += db.execute(
                                f"SELECT COUNT(*) FROM {table} WHERE {WRITER_COLUMN} = 'agent'"
                            ).fetchone()[0]
                finally:
                    db.close()
            excess += max(0, rows - episode.env.counters.writes)
        self._peak = max(self._peak, excess)
        return self._peak
