"""Small file API: component-by-component no-follow access and atomic durable writes."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
import uuid
from pathlib import Path

_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class SafeFiles:
    def __init__(self, root: Path):
        root = root.absolute()
        if ".." in root.parts:
            raise ValueError("parent traversal in data root")
        for part in (root, *root.parents):
            if part.is_symlink():
                raise ValueError("symlink in data root")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = root

    @contextlib.contextmanager
    def parent(self, relative: str, *, create: bool = False):
        parts = relative.split("/")
        if any(not _COMPONENT.fullmatch(p) or p in (".", "..") for p in parts):
            raise ValueError("unsafe path")
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                if create:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def create(self, relative: str):
        with self.parent(relative, create=True) as (fd, name):
            out = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode=0o600, dir_fd=fd
            )
        return os.fdopen(out, "wb")

    def read(self, relative: str, limit: int = 16_000_000) -> bytes:
        with self.parent(relative) as (fd, name):
            src = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            if not stat.S_ISREG(os.fstat(src).st_mode):
                os.close(src)
                raise ValueError("regular input file required")
        with os.fdopen(src, "rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("file too large")
        return data

    def atomic(self, relative: str, data: bytes):
        with self.parent(relative, create=True) as (fd, name):
            temp = f"tmp-{uuid.uuid4().hex}"
            out = os.open(
                temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode=0o600, dir_fd=fd
            )
            try:
                with os.fdopen(out, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temp, dir_fd=fd)

    def json(self, relative: str, value):
        self.atomic(relative, json.dumps(value, ensure_ascii=False, sort_keys=True).encode())

    def rename(self, src: str, dst: str):
        with self.parent(src) as (sfd, sname), self.parent(dst) as (dfd, dname):
            os.rename(sname, dname, src_dir_fd=sfd, dst_dir_fd=dfd)
            os.fsync(dfd)

    def remove(self, relative: str):
        with self.parent(relative) as (fd, name):
            os.unlink(name, dir_fd=fd)
            os.fsync(fd)

    @contextlib.contextmanager
    def lock(self, relative: str):
        with self.parent(relative, create=True) as (fd, name):
            lock_fd = os.open(
                name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, mode=0o600, dir_fd=fd
            )
        try:
            metadata = os.fstat(lock_fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("regular single-link lock file required")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                try:
                    yield True
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    def directory(self, relative: str):
        """Create one new directory; never adopt an attacker-created attempt directory."""
        with self.parent(relative, create=True) as (fd, name):
            os.mkdir(name, mode=0o700, dir_fd=fd)
            os.fsync(fd)
        return self.root / relative

    def copy_verified(
        self,
        source: SafeFiles,
        src: str,
        dst: str,
        expected: str,
        *,
        limit: int,
        prefix: bytes = b"",
    ) -> int:
        """Stream a regular, single-link input into an exclusively created bounded file."""
        import hashlib

        with source.parent(src) as (fd, name):
            inp = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
        try:
            before = os.fstat(inp)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
                raise ValueError("unsafe snapshot source")
            digest, total = hashlib.sha256(), 0
            with os.fdopen(inp, "rb", closefd=False) as reader, self.create(dst) as output:
                while block := reader.read(65536):
                    if total == 0 and not block.startswith(prefix):
                        raise ValueError("snapshot codec mismatch")
                    total += len(block)
                    if total > limit:
                        raise ValueError("snapshot size limit")
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            after = os.fstat(inp)
            if digest.hexdigest() != expected or (
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("snapshot source changed or hash mismatch")
            return total
        finally:
            os.close(inp)

    def seal(self):
        """Seal a newly broker-created input tree, without following links.

        Separate worker identities and a read-only bind mount must enforce this on the host.
        Only invoke on an exclusive input directory, never a worker-writable output tree.
        """
        for base, dirs, files in os.walk(self.root, followlinks=False, topdown=False):
            for name in files:
                path = Path(base) / name
                if path.is_symlink() or not path.is_file():
                    raise ValueError("unsafe input snapshot")
                os.chmod(path, 0o400, follow_symlinks=False)
            for name in dirs:
                path = Path(base) / name
                if path.is_symlink():
                    raise ValueError("symlink input directory")
                os.chmod(path, 0o500, follow_symlinks=False)
        os.chmod(self.root, 0o500)

    def sha256(self, relative: str, *, limit: int) -> str:
        import hashlib

        with self.parent(relative) as (fd, name):
            inp = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
        with os.fdopen(inp, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("regular input file required")
            digest, count = hashlib.sha256(), 0
            while block := stream.read(65536):
                count += len(block)
                if count > limit:
                    raise ValueError("hash input exceeds limit")
                digest.update(block)
            return digest.hexdigest()
