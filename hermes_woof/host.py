"""The WOOF host as the plugin sees it: an executor plus the run store, with
the host-side helpers (runner.py, gallery.py, score_run.py, names.json)
deployed into the store and version-stamped."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional, Sequence

from . import names
from .executor import Executor, ExecutorError, Result
from .settings import Settings

_HERE = Path(__file__).parent
_HELPERS = ("runner.py", "gallery.py", "score_run.py", "names.json")


def _bundle_stamp() -> str:
    h = hashlib.sha256()
    for name in _HELPERS:
        h.update(name.encode())
        h.update((_HERE / name).read_bytes())
    return h.hexdigest()[:16]


class HostError(RuntimeError):
    pass


class Host:
    _deployed: dict = {}

    def __init__(self, settings: Settings):
        self.settings = settings
        self.x = Executor(settings)
        self._runs_dir: Optional[str] = None

    @property
    def label(self) -> str:
        return self.x.label

    @property
    def runs_dir(self) -> str:
        if self._runs_dir is None:
            self._runs_dir = self.x.expand(self.settings.runs_dir or "~/woof-runs")
        return self._runs_dir

    def deploy(self) -> None:
        key = f"{self.label}|{self.runs_dir}"
        stamp = _bundle_stamp()
        if Host._deployed.get(key) == stamp:
            return
        marker = self.x.join(self.runs_dir, ".hermes-woof-helpers")
        current = (self.x.read_text(marker) or "").strip()
        if current != stamp:
            self.x.mkdirs(self.runs_dir)
            for name in _HELPERS:
                if name == "names.json":
                    # the runner reads the table as plain pairs, so it gets the filled-in table
                    text = json.dumps(names.table(), ensure_ascii=False) + "\n"
                else:
                    text = (_HERE / name).read_text(encoding="utf-8")
                self.x.write_text(self.x.join(self.runs_dir, name), text)
            self.x.write_text(marker, stamp + "\n")
        Host._deployed[key] = stamp

    def runner(self, args: Sequence[str], *, stdin: Optional[dict] = None, timeout: float = 120) -> dict:
        self.deploy()
        argv = [self.x.python(), self.x.join(self.runs_dir, "runner.py"), *[str(a) for a in args]]
        res = self.x.run(argv, timeout=timeout, env=self.x.woof_env(),
                         input_bytes=json.dumps(stdin).encode() if stdin is not None else None)
        if res.rc == 124:
            raise HostError(f"the WOOF host did not answer within {timeout:.0f} s")
        text = res.text.strip()
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    break
        raise HostError(f"runner {args[0] if args else ''} failed (exit {res.rc}): {(res.err or text)[-600:]}")

    def woof(self, args: Sequence[str], *, timeout: float = 120, cwd: str | None = None) -> Result:
        woof = self.x.woof()
        if not woof:
            return Result(127, b"", "woof is not installed on the WOOF host", 0.0)
        return self.x.run([woof, *[str(a) for a in args]], timeout=timeout, env=self.x.woof_env(), cwd=cwd)

    def woof_json(self, args: Sequence[str], *, timeout: float = 120) -> tuple[Optional[object], Result]:
        res = self.woof(args, timeout=timeout)
        text = res.text.strip()
        if not text:
            return None, res
        try:
            return json.loads(text), res
        except ValueError:
            for line in reversed(text.splitlines()):
                if line.strip().startswith(("{", "[")):
                    try:
                        return json.loads(line), res
                    except ValueError:
                        continue
        return None, res
