"""Single-job model for the HackRF radio.

The radio is half-duplex and there is one device, so at most ONE job
(sweep / capture / replay) runs at a time. A second submit while busy is
rejected. Each job wraps a subprocess; stdout/stderr are read line-by-line
by parser callbacks that push events onto the job's event deque, which the
SSE endpoint streams to the browser.
"""
from __future__ import annotations
import collections
import subprocess
import threading
import time
import uuid
from typing import Optional, Callable, Dict, Any, List

# A parser gets (job, line) and typically calls job.emit(...)
Parser = Callable[["Job", str], None]
OnExit = Callable[["Job"], None]

_ACTIVE = ("starting", "running")


class Job:
    def __init__(self, job_id: str, kind: str, argv: List[str]):
        self.id = job_id
        self.kind = kind            # sweep | capture | replay
        self.argv = argv
        self.state = "starting"     # starting|running|done|error|stopped
        self.rc: Optional[int] = None
        self.error: Optional[str] = None
        self.started_at = time.time()
        self.ended_at: Optional[float] = None
        self.proc: Optional[subprocess.Popen] = None
        # For custom (function) jobs like the GPS sequence: the currently-running
        # child subprocess, and a cooperative stop flag the function polls.
        self.child: Optional[subprocess.Popen] = None
        self.stop_event = threading.Event()
        self.meta: Dict[str, Any] = {}
        self.events: "collections.deque[dict]" = collections.deque(maxlen=8192)
        # Monotonic total-emitted counter. The deque drops old events once it
        # hits maxlen, so an absolute index into it would run past the end and
        # silently stop delivering — SSE consumers track `seq` instead.
        self.seq = 0
        self.cond = threading.Condition()

    def emit(self, kind: str, **payload: Any) -> None:
        ev = {"t": time.time(), "kind": kind, **payload}
        with self.cond:
            self.seq += 1
            self.events.append(ev)
            self.cond.notify_all()

    @property
    def active(self) -> bool:
        return self.state in _ACTIVE

    def snapshot(self) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "state": self.state,
                "rc": self.rc, "error": self.error, "argv": self.argv,
                "started_at": self.started_at, "ended_at": self.ended_at,
                "meta": self.meta}


class JobManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current: Optional[Job] = None
        self.jobs: Dict[str, Job] = {}

    def start(self, kind: str, argv: List[str],
              stdout_parser: Optional[Parser] = None,
              stderr_parser: Optional[Parser] = None,
              on_exit: Optional[OnExit] = None) -> Job:
        with self.lock:
            if self.current and self.current.active:
                c = self.current
                raise RuntimeError(f"busy: {c.kind} job {c.id} is {c.state}")
            job = Job(uuid.uuid4().hex[:12], kind, argv)
            self.current = job
            self.jobs[job.id] = job
        threading.Thread(target=self._run, name=f"job-{job.id}", daemon=True,
                         args=(job, stdout_parser, stderr_parser, on_exit)).start()
        return job

    def start_func(self, kind: str, fn) -> Job:
        """Run an arbitrary function as a single job (e.g. the GPS loop that
        spawns many hackrf_transfer children). fn(job) must poll job.stop_event
        and set job.child when it spawns a subprocess so stop() can kill it."""
        with self.lock:
            if self.current and self.current.active:
                c = self.current
                raise RuntimeError(f"busy: {c.kind} job {c.id} is {c.state}")
            job = Job(uuid.uuid4().hex[:12], kind, [])
            self.current = job
            self.jobs[job.id] = job
        threading.Thread(target=self._run_func, name=f"job-{job.id}", daemon=True,
                         args=(job, fn)).start()
        return job

    def _run_func(self, job: Job, fn) -> None:
        try:
            job.state = "running"
            fn(job)
            if job.state != "stopped":
                job.state = "done"
        except Exception as e:  # noqa: BLE001
            if job.state != "stopped":
                job.state = "error"
                job.error = str(e)
        finally:
            job.ended_at = time.time()
            job.emit("done", state=job.state, error=job.error)
            with self.lock:
                if self.current is job:
                    self.current = None

    def _run(self, job: Job, stdout_parser, stderr_parser, on_exit) -> None:
        try:
            job.proc = subprocess.Popen(
                job.argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=1, text=True)
            job.state = "running"
            job.emit("info", msg="started: " + " ".join(job.argv))
            readers = []
            if stdout_parser:
                readers.append(self._reader(job, job.proc.stdout, stdout_parser))
            if stderr_parser:
                readers.append(self._reader(job, job.proc.stderr, stderr_parser))
            rc = job.proc.wait()
            for t in readers:
                t.join(timeout=2)
            job.rc = rc
            if job.state != "stopped":
                job.state = "done" if rc == 0 else "error"
                if rc != 0:
                    job.error = f"exit code {rc}"
        except Exception as e:  # noqa: BLE001
            job.state = "error"
            job.error = str(e)
        finally:
            job.ended_at = time.time()
            if on_exit:
                try:
                    on_exit(job)
                except Exception:  # noqa: BLE001
                    pass
            job.emit("done", state=job.state, rc=job.rc, error=job.error)
            with self.lock:
                if self.current is job:
                    self.current = None

    @staticmethod
    def _reader(job: Job, stream, parser: Parser) -> threading.Thread:
        def run() -> None:
            try:
                for line in iter(stream.readline, ""):
                    if not line:
                        break
                    try:
                        parser(job, line.rstrip("\n"))
                    except Exception:  # noqa: BLE001 — one bad line shouldn't kill the reader
                        pass
            except Exception:  # noqa: BLE001
                pass
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def stop(self, job_id: Optional[str] = None) -> bool:
        with self.lock:
            job = self.jobs.get(job_id) if job_id else self.current
        if not job:
            return False
        job.state = "stopped"
        job.stop_event.set()          # custom (GPS) jobs poll this
        for p in (job.proc, job.child):
            if not p:
                continue
            try:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
            except Exception:  # noqa: BLE001
                pass
        return True
