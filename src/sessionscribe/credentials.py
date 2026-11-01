"""Read only the application's own private credential, without shell evaluation."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from pydantic import SecretStr


class CredentialError(ValueError):
    pass


def discord_token(path: Path) -> SecretStr:
    """Accept a small private DISCORD_TOKEN dotenv file; never echo its contents.

    A non-root container may receive a root-owned read-only secret mount. Group
    and world access, symlinks, hard links and non-regular inputs are rejected.
    """
    fd = None
    try:
        if any(parent.is_symlink() for parent in path.parents):
            raise CredentialError("symlink in credential directory")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_uid not in (0, os.geteuid())
            or info.st_size > 4096
        ):
            raise CredentialError("credential must be a private, regular file (0400/0600)")
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise CredentialError("credential file too large")
        entries = [line.strip() for line in raw.decode().splitlines()]
        entries = [line for line in entries if line and not line.startswith("#")]
        if len(entries) != 1 or not entries[0].startswith("DISCORD_TOKEN="):
            raise CredentialError("expected one DISCORD_TOKEN assignment")
        token = entries[0].removeprefix("DISCORD_TOKEN=").strip()
        if token[:1] in ("'", '"') and token[-1:] == token[:1]:
            token = token[1:-1]
        if not re.fullmatch(r"[A-Za-z0-9_.-]{20,1024}", token):
            raise CredentialError("DISCORD_TOKEN is empty or invalid")
        return SecretStr(token)
    except (OSError, UnicodeError):
        raise CredentialError("credential file cannot be securely read") from None
    finally:
        if fd is not None:
            os.close(fd)
