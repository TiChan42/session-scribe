"""Host-side fixed dispatcher contract; never installed in a recorder container."""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .store import Store


@dataclass(frozen=True)
class Resources:
    available_memory: int
    free_disk: int
    load1: float
    cpus: int

    def admits(self, phase: str = "stt") -> bool:
        if phase == "analysis":
            return (
                self.available_memory >= 768 * 1024**2
                and self.free_disk >= 128 * 1024**2
                and self.cpus >= 1
                and self.load1 <= max(0.5, self.cpus - 1)
            )
        if phase != "stt":
            raise ValueError("unknown worker phase")
        return (
            self.available_memory >= 3584 * 1024**2
            and self.free_disk >= 2 * 1024**3
            and self.cpus >= 2
            and self.load1 <= max(0.5, self.cpus - 2)
        )


def host_resources(path: Path) -> Resources:
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    available = int(memory["MemAvailable"].split()[0]) * 1024
    return Resources(
        available, shutil.disk_usage(path).free, os.getloadavg()[0], os.cpu_count() or 1
    )


@contextlib.contextmanager
def global_stt_lock(path: Path):
    """All tenant dispatchers must use one operator-owned host lock, held until worker exit."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("regular single-link global lock required")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class Dispatcher:
    def __init__(
        self,
        store: Store,
        lock: Path,
        runner: Callable[[dict], None],
        resources: Callable[[], Resources],
    ):
        self.store, self.lock, self.runner, self.resources = store, lock, runner, resources

    def once(self) -> bool:
        with global_stt_lock(self.lock) as acquired:
            if not acquired or not self.resources().admits():
                return False
            job = self.store.claim("stt", lease_seconds=3660)
            if job is None:
                return False
            try:
                self.runner(job)  # runner must return only after the entire cgroup has exited
            except (OSError, ValueError, subprocess.SubprocessError):
                self.store.finish(job, error="worker_failed")  # no transcript/exception in logs
            else:
                self.store.finish(job)
            return True


def worker_argv(
    *, image: str, input_path: Path, output_path: Path, model_path: Path, seccomp_path: Path
) -> list[str]:
    """Reviewable argv for a broker under a dedicated STT service user.

    Identity, mount ownership, AppArmor hook and handoff creation belong to the
    infrastructure role. Paths come from administrator configuration, never a job body.
    This function does not grant the recorder host execution or launch a container.
    """
    if not re.fullmatch(r"[a-z0-9./:_-]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("immutable image required")
    for path in (input_path, output_path, model_path, seccomp_path):
        if (
            not path.is_absolute()
            or ".." in path.parts
            or any(c in str(path) for c in ",\r\n\0")
            or path.is_symlink()
            or any(p.is_symlink() for p in path.parents)
        ):
            raise ValueError("unsafe operator path")
    return [
        "/usr/bin/podman",
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--user=65532:65532",
        "--cap-drop=all",
        "--security-opt=no-new-privileges",
        f"--security-opt=seccomp={seccomp_path}",
        "--annotation=io.isobox.managed=true",
        "--memory=2560m",
        "--memory-swap=2560m",
        "--cpus=2",
        "--pids-limit=96",
        "--timeout=3600",
        "--stop-timeout=10",
        "--log-driver=none",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--mount",
        f"type=bind,src={input_path},dst=/input,ro=true",
        "--mount",
        f"type=bind,src={output_path},dst=/output,rw=true",
        "--mount",
        f"type=bind,src={model_path},dst=/model,ro=true",
        image,
    ]
