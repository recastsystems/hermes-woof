"""`hermes woof install`: PyPI by default within the WOOF 1.0.x line, a wheel folder on request.

The host is a stand-in that records every command, so these run anywhere and install nothing.
"""
import argparse
from pathlib import PurePosixPath

import pytest

REL = "1.0.0"
LISTING = [f"recast_woof-{REL}-py3-none-any.whl", f"recast_woof-{REL}-py3-none-manylinux_2_28_x86_64.whl",
           f"recast_woof_data-{REL}-py3-none-any.whl", f"recast_woof-{REL}.tar.gz"]


class FakeX:
    remote = False
    kind = "local"

    def __init__(self, listing):
        self.calls = []
        self.listing = listing

    def run(self, argv, timeout=60, env=None, input_bytes=None, cwd=None):
        from hermes_woof_plugin.hermes_woof.executor import Result
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[:1] == ["uname"]:
            return Result(0, b"Linux x86_64\n", "", 0.0)
        if argv[:1] == ["ls"]:
            return Result(0, ("\n".join(self.listing) + "\n").encode(), "", 0.0)
        if argv[-1:] == ["version"]:
            return Result(0, f"woof {REL}\n".encode(), "", 0.0)
        return Result(0, b"", "", 0.0)

    def expand(self, path):
        return str(path).replace("~", "/srv/u", 1) if str(path).startswith("~") else str(path)

    def join(self, *parts):
        return str(PurePosixPath(*parts))

    def exists(self, path):
        return True

    def mkdirs(self, path):
        pass


class FakeHost:
    label = "local"
    runs_dir = "/srv/u/woof-runs"

    def __init__(self, x):
        self.x = x


def _install(monkeypatch, *, cuda_major=13, listing=(), **argv):
    from hermes_woof_plugin.hermes_woof import cli, tools
    x = FakeX(list(listing))
    monkeypatch.setattr(cli, "Host", lambda s: FakeHost(x))
    monkeypatch.setattr(tools, "_probe", lambda host: {
        "devices": [{"name": "Test GPU", "memory_total_gib": 24.0}], "cuda_major": cuda_major})
    monkeypatch.setattr(tools, "gpu_command", lambda *a, **k: {"busy": True})
    args = argparse.Namespace(wheel_dir="", cuda="", venv="~/woof-env", geog_root="", no_geog=True, yes=False)
    for k, v in argv.items():
        setattr(args, k, v)
    assert cli.cmd_install(args) == 0
    return x.calls


def _pip(calls):
    return next(c for c in calls if "install" in c and ("pip" in c or "-m" in c))


def test_default_is_pypi_cu13_within_the_1_0_line(pkg, plugin_env, monkeypatch):
    calls = _install(monkeypatch)
    assert "recast-woof[all-cu13]>=1.0.0,<1.1" in _pip(calls)
    assert any(c[-1:] == ["fetch-tables"] for c in calls), "the Thompson tables are staged after a PyPI install"


def test_cuda_12_by_flag(pkg, plugin_env, monkeypatch):
    calls = _install(monkeypatch, cuda="12")
    assert "recast-woof[all-cu12]>=1.0.0,<1.1" in _pip(calls)


def test_cuda_12_driver_gets_the_cu12_extra(pkg, plugin_env, monkeypatch):
    calls = _install(monkeypatch, cuda_major=12)
    assert "recast-woof[all-cu12]>=1.0.0,<1.1" in _pip(calls)


def test_wheel_dir_installs_the_platform_wheel_and_its_data(pkg, plugin_env, monkeypatch):
    calls = _install(monkeypatch, wheel_dir="/w", listing=LISTING)
    pip = _pip(calls)
    assert f"/w/recast_woof-{REL}-py3-none-manylinux_2_28_x86_64.whl[all-cu13]" in pip
    assert f"/w/recast_woof_data-{REL}-py3-none-any.whl" in pip
    assert not any("recast-woof[" in a for a in pip)
    assert any(c[-1:] == ["fetch-tables"] for c in calls)


def test_requirement_line():
    from hermes_woof_plugin.hermes_woof import requirement
    assert requirement.pip_spec("all-cu13") == "recast-woof[all-cu13]>=1.0.0,<1.1"
    assert requirement.supported("1.0.0") and requirement.supported("1.0.7")
    assert requirement.supported("1.1.0") is False and requirement.supported("0.9.9") is False
    assert requirement.supported(None) is None
