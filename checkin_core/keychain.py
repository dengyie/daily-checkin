"""Credential storage backend. Secret values never enter SQLite or logs, and
never fall back to plain env vars / task files.

Two backends share a common ``CredentialStore`` protocol:

* :class:`KeychainCredentials` — macOS Keychain via the ``security`` CLI
  (the historical default on macOS hosts).
* :class:`FileCredentials` — a single 0600 JSON file on Linux (the historical
  macOS Keychain is unavailable there). The whole file is kept mode 0600 and
  the ref is the on-disk key; secrets never print.

The backend is chosen by ``DAILY_CHECKIN_CREDENTIAL_BACKEND`` (``keychain`` or
``file``), defaulting to ``keychain`` on macOS and ``file`` elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

SERVICE = "com.mango.daily-checkin"
Runner = Callable[..., Any]


class CredentialStore(Protocol):
    def put(self, ref: str, secret: str) -> None: ...
    def get(self, ref: str) -> str | None: ...
    def delete(self, ref: str) -> None: ...


@dataclass(frozen=True)
class CredentialRef:
    ref: str
    kind: str
    label: str = ""


def make_ref(site_name: str, kind: str) -> str:
    digest = hashlib.sha256(f"{site_name}\0{kind}".encode()).hexdigest()[:24]
    return f"daily-checkin:{digest}"


class KeychainCredentials:
    def __init__(self, runner: Runner = subprocess.run):
        self._runner = runner

    def put(self, ref: str, secret: str) -> None:
        if not ref or not secret:
            raise ValueError("credential ref and secret are required")
        # Keep the secret out of argv: `-w` without a value reads the prompt
        # from stdin, keeping process listings and application logs clean.
        completed = self._runner(
            ["security", "add-generic-password", "-U", "-a", ref, "-s", SERVICE, "-w"],
            input=secret + "\n" + secret + "\n", text=True, capture_output=True,
        )
        if completed.returncode != 0:
            raise RuntimeError("Keychain write failed")

    def get(self, ref: str) -> str | None:
        completed = self._runner(
            ["security", "find-generic-password", "-a", ref, "-s", SERVICE, "-w"],
            text=True, capture_output=True,
        )
        if completed.returncode == 44:
            return None
        if completed.returncode != 0:
            raise RuntimeError("Keychain read failed")
        return completed.stdout.rstrip("\n")

    def delete(self, ref: str) -> None:
        completed = self._runner(
            ["security", "delete-generic-password", "-a", ref, "-s", SERVICE],
            text=True, capture_output=True,
        )
        if completed.returncode not in (0, 44):
            raise RuntimeError("Keychain delete failed")


class FileCredentials:
    """0600-credential-file backend for hosts without macOS Keychain.

    Stores ref→secret in a single JSON file. The containing directory and file
    are both forced to mode 0700/0600, matching the secrecy of the Keychain on
    macOS. Secrets are never printed; errors raise without echoing the value.
    """

    def __init__(self, path: str | os.PathLike[str], mkdir: bool = True):
        self.path = Path(path).expanduser()
        if mkdir:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # Fail closed: if we can't make the immediate parent 0700 and
        # self-owned, refuse rather than silently write into a looser dir.
        try:
            self.path.parent.chmod(0o700)
        except OSError as exc:
            raise RuntimeError("credential directory not writable with mode 0700") from exc
        if self.path.exists():
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    @staticmethod
    def _parent_is_private(dir: Path) -> bool:
        try:
            st = dir.stat()
        except OSError:
            return False
        return stat.S_ISDIR(st.st_mode) and (st.st_mode & 0o777) == 0o700 and st.st_uid == os.getuid()

    def _load(self, *, strict: bool = False) -> dict[str, str]:
        """Read refs. With strict=True an unreadable/corrupt file raises (used
        by mutating ops so we never truncate away every stored secret). get()
        stays loss-tolerant on a read-only path: missing/absent j=={}."""
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            if strict:
                raise RuntimeError("credential file unreadable; refusing to overwrite") from exc
            return {}
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            if strict:
                raise RuntimeError("credential file corrupt; refusing to overwrite") from exc
            return {}
        if not isinstance(payload, dict):
            if strict:
                raise RuntimeError("credential file corrupt; refusing to overwrite")
            return {}
        return payload

    def _write(self, data: dict[str, str]) -> None:
        fd = os.open(
            self.path,
            os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(dict(sorted(data.items())), fh, ensure_ascii=False)
        finally:
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

    def put(self, ref: str, secret: str) -> None:
        if not ref or not secret:
            raise ValueError("credential ref and secret are required")
        if not self._parent_is_private(self.path.parent):
            raise RuntimeError("credential directory not private (mode/owner)")
        data = self._load(strict=True)
        data[ref] = secret
        self._write(data)

    def get(self, ref: str) -> str | None:
        if not ref:
            return None
        return self._load().get(ref)

    def delete(self, ref: str) -> None:
        if not self._parent_is_private(self.path.parent):
            raise RuntimeError("credential directory not private (mode/owner)")
        data = self._load(strict=True)
        if ref in data:
            data.pop(ref, None)
            self._write(data)


def credential_store_from_env() -> CredentialStore:
    """Pick the credential backend from config, defaulting keychain on macOS.

    Explicit ``DAILY_CHECKIN_CREDENTIAL_BACKEND`` wins. Unknown values raise
    so an operator typo never silently degrades to a plaintext fallback.
    """
    explicit = os.environ.get("DAILY_CHECKIN_CREDENTIAL_BACKEND", "").strip().lower()
    is_macos = sys.platform == "darwin"
    if not explicit:
        backend = "keychain" if is_macos else "file"
    else:
        backend = explicit
    if backend == "keychain":
        if not is_macos:
            raise RuntimeError("keychain backend unavailable on this platform")
        return KeychainCredentials()
    if backend == "file":
        dir_env = (os.environ.get("DAILY_CHECKIN_CREDENTIALS_DIR") or "").strip()
        default = Path(dir_env or Path.home() / ".config" / "daily-checkin")
        file_env = (os.environ.get("DAILY_CHECKIN_CREDENTIALS_FILE") or "").strip()
        path = file_env or str(default / "credentials.json")
        return FileCredentials(path)
    raise RuntimeError(f"unknown DAILY_CHECKIN_CREDENTIAL_BACKEND: {backend!r}")
