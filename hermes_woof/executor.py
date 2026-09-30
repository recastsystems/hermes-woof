"""Where WOOF runs: this machine (local), a Linux GPU box over SSH (ssh), or a
WSL2 distribution on a Windows host (wsl). All three run the same commands.

The local kind runs argv lists directly. The ssh and wsl kinds build one
quoted shell line per call and run it through ``ssh`` or ``wsl.exe``.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Optional, Sequence

from .settings import Settings


@dataclass
class Result:
    rc: int
    out: bytes
    err: str
    seconds: float

    @property
    def text(self) -> str:
        return self.out.decode("utf-8", errors="replace")


class ExecutorError(RuntimeError):
    pass


class Executor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.kind = (settings.executor or "local").strip().lower()
        if self.kind not in ("local", "ssh", "wsl"):
            raise ExecutorError(f"unknown executor {self.kind!r}; use local, ssh or wsl")
        if self.kind == "ssh" and not settings.ssh_host.strip():
            raise ExecutorError("executor ssh needs ssh_host (user@host)")
        self._home: Optional[str] = None
        self._woof: Optional[str] = None

    # -- identity --------------------------------------------------------
    @property
    def label(self) -> str:
        if self.kind == "ssh":
            return f"ssh:{self.settings.ssh_host}"
        if self.kind == "wsl":
            return f"wsl:{self.settings.wsl_distro or 'default'}"
        return "local"

    @property
    def remote(self) -> bool:
        return self.kind != "local"

    # -- low level -------------------------------------------------------
    def _wrap(self, line: str) -> list[str]:
        if self.kind == "ssh":
            return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                    self.settings.ssh_host, line]
        wsl = shutil.which("wsl.exe") or shutil.which("wsl") or "wsl.exe"
        argv = [wsl]
        if self.settings.wsl_distro.strip():
            argv += ["-d", self.settings.wsl_distro.strip()]
        return argv + ["--", "bash", "-lc", line]

    @staticmethod
    def _line(argv: Sequence[str], env: Mapping[str, str] | None, cwd: str | None) -> str:
        parts = []
        if cwd:
            parts.append(f"cd {shlex.quote(cwd)} &&")
        if env:
            parts.append("env " + " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items()))
        parts.append(" ".join(shlex.quote(str(a)) for a in argv))
        return " ".join(parts)

    def run(self, argv: Sequence[str], *, timeout: float = 60, input_bytes: bytes | None = None,
            env: Mapping[str, str] | None = None, cwd: str | None = None) -> Result:
        t0 = time.monotonic()
        try:
            if self.kind == "local":
                full_env = dict(os.environ)
                if env:
                    full_env.update({k: str(v) for k, v in env.items()})
                proc = subprocess.run([str(a) for a in argv], input=input_bytes, capture_output=True,
                                      timeout=timeout, env=full_env, cwd=cwd)
            else:
                proc = subprocess.run(self._wrap(self._line(argv, env, cwd)), input=input_bytes,
                                      capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            return Result(124, exc.stdout or b"", f"timed out after {timeout:.0f} s", time.monotonic() - t0)
        except FileNotFoundError as exc:
            return Result(127, b"", str(exc), time.monotonic() - t0)
        return Result(proc.returncode, proc.stdout or b"",
                      (proc.stderr or b"").decode("utf-8", errors="replace"), time.monotonic() - t0)

    def shell(self, line: str, *, timeout: float = 60, input_bytes: bytes | None = None) -> Result:
        """Run a shell line on the WOOF host (bash)."""
        return self.run(["bash", "-c", line], timeout=timeout, input_bytes=input_bytes)

    def spawn_detached(self, argv: Sequence[str], *, log: str, env: Mapping[str, str] | None = None,
                       cwd: str | None = None) -> int:
        """Start a process that outlives this call; returns its pid (0 if unknown)."""
        if self.kind == "local":
            full_env = dict(os.environ)
            if env:
                full_env.update({k: str(v) for k, v in env.items()})
            Path(log).parent.mkdir(parents=True, exist_ok=True)
            with open(log, "ab") as fh:
                proc = subprocess.Popen([str(a) for a in argv], stdout=fh, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, env=full_env, cwd=cwd,
                                        start_new_session=True, close_fds=True)
            return proc.pid
        line = self._line(argv, env, cwd)
        wrapped = (f"mkdir -p {shlex.quote(str(PurePosixPath(log).parent))} && "
                   f"setsid nohup bash -c {shlex.quote(line)} >> {shlex.quote(log)} 2>&1 < /dev/null & echo $!")
        res = self.run(["bash", "-c", wrapped], timeout=30)
        try:
            return int(res.text.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return 0

    # -- paths -----------------------------------------------------------
    def home(self) -> str:
        if self._home is None:
            if self.kind == "local":
                self._home = str(Path.home())
            else:
                res = self.run(["bash", "-c", "echo $HOME"], timeout=30)
                if res.rc != 0 or not res.text.strip():
                    raise ExecutorError(f"cannot reach the WOOF host ({self.label}): {res.err.strip()[:300]}")
                self._home = res.text.strip().splitlines()[-1]
        return self._home

    def expand(self, path: str) -> str:
        path = str(path).strip()
        if path == "~":
            return self.home()
        if path.startswith("~/"):
            return self.home().rstrip("/") + "/" + path[2:]
        return path

    def join(self, *parts: str) -> str:
        if self.kind == "local":
            return str(Path(*parts))
        return str(PurePosixPath(*parts))

    # -- files -----------------------------------------------------------
    def exists(self, path: str) -> bool:
        if self.kind == "local":
            return Path(path).exists()
        return self.run(["test", "-e", path], timeout=30).rc == 0

    def read_bytes(self, path: str) -> Optional[bytes]:
        if self.kind == "local":
            try:
                return Path(path).read_bytes()
            except OSError:
                return None
        res = self.run(["cat", path], timeout=120)
        return res.out if res.rc == 0 else None

    def read_text(self, path: str) -> Optional[str]:
        raw = self.read_bytes(path)
        return None if raw is None else raw.decode("utf-8", errors="replace")

    def write_text(self, path: str, text: str) -> None:
        if self.kind == "local":
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, p)
            return
        parent = str(PurePosixPath(path).parent)
        line = (f"mkdir -p {shlex.quote(parent)} && cat > {shlex.quote(path + '.tmp')} && "
                f"mv -f {shlex.quote(path + '.tmp')} {shlex.quote(path)}")
        res = self.run(["bash", "-c", line], timeout=60, input_bytes=text.encode("utf-8"))
        if res.rc != 0:
            raise ExecutorError(f"could not write {path} on {self.label}: {res.err.strip()[:300]}")

    def mkdirs(self, path: str) -> None:
        if self.kind == "local":
            Path(path).mkdir(parents=True, exist_ok=True)
        else:
            self.run(["mkdir", "-p", path], timeout=30)

    def fetch(self, path: str, local_dir: Path) -> Optional[Path]:
        """A local copy of a file on the WOOF host (the same path when local)."""
        if self.kind == "local":
            return Path(path) if Path(path).exists() else None
        local_dir.mkdir(parents=True, exist_ok=True)
        target = local_dir / PurePosixPath(path).name
        if target.exists() and target.stat().st_size > 0:
            return target
        raw = self.read_bytes(path)
        if raw is None:
            return None
        target.write_bytes(raw)
        return target

    # -- WOOF ------------------------------------------------------------
    def woof(self) -> Optional[str]:
        """The woof command on the WOOF host, or None when WOOF is absent."""
        if self._woof is not None:
            return self._woof or None
        candidates = []
        if self.settings.woof_bin.strip():
            candidates.append(self.expand(self.settings.woof_bin.strip()))
        candidates.append(self.expand("~/woof-env/bin/woof"))
        found = ""
        for cand in candidates:
            if self.exists(cand):
                found = cand
                break
        if not found:
            if self.kind == "local":
                found = shutil.which("woof") or ""
            else:
                res = self.run(["bash", "-lc", "command -v woof"], timeout=30)
                found = res.text.strip().splitlines()[-1] if res.rc == 0 and res.text.strip() else ""
        self._woof = found
        return found or None

    def python(self) -> str:
        """The interpreter for the host-side helpers: the WOOF venv's own when
        WOOF is installed (always 3.11 or newer), else python3."""
        woof = self.woof()
        if woof:
            cand = str(PurePosixPath(woof).parent / "python3") if self.remote else str(Path(woof).parent / "python3")
            if self.exists(cand):
                return cand
        if self.kind == "local":
            return sys.executable if os.name != "nt" else (shutil.which("python3") or sys.executable)
        return "python3"

    def woof_env(self) -> dict:
        env = {}
        if self.settings.geog_root.strip():
            env["WOOF_WPS_GEOG"] = self.expand(self.settings.geog_root.strip())
        return env
