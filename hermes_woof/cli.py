"""`hermes woof ...` subcommands: install, doctor, runs, lint.

`hermes woof install` sets up WOOF on the WOOF host through the configured
executor (local, ssh or wsl):

  hermes woof install                  from PyPI: recast-woof[all-cu13]>=1.0.0,<1.1 (--cuda 12 for all-cu12)
  hermes woof install --wheel-dir DIR  from a folder of WOOF release files (offline installs)

It makes a venv, installs WOOF with the CUDA extra that matches the driver,
stages the Thompson tables, sets up geography (links an existing folder or
downloads it after asking), runs WOOF's own doctor, and records woof_bin in
the plugin settings. These are the steps of WOOF's own install guide.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath

from . import requirement, settings, tools
from .host import Host

WHEEL_RE = re.compile(r"^recast_woof-(?P<ver>[\w.]+)-py3-none-manylinux_\d+_\d+_x86_64\.whl$")
DATA_RE = re.compile(r"^recast_woof_data-(?P<ver>[\w.]+)-py3-none-any\.whl$")
PURE_RE = re.compile(r"^recast_woof-[\w.]+-py3-none-any\.whl$")


def setup(parser) -> None:
    sub = parser.add_subparsers(dest="woof_command")
    p = sub.add_parser("install", help="install WOOF on the WOOF host")
    p.add_argument("--wheel-dir", default="",
                   help="install from a folder holding the WOOF release wheels instead of PyPI")
    p.add_argument("--cuda", choices=["12", "13"], default="",
                   help="CUDA major for the extra: 13 (all-cu13, the default) or 12 (all-cu12, "
                        "chosen on its own when the driver is CUDA 12)")
    p.add_argument("--venv", default="~/woof-env", help="where the WOOF venv goes (default ~/woof-env)")
    p.add_argument("--geog-root", default="", help="an existing WPS_GEOG folder to use instead of downloading")
    p.add_argument("--no-geog", action="store_true", help="skip geography (runs need it; add it later)")
    p.add_argument("--yes", action="store_true", help="answer yes to the geography download question")
    sub.add_parser("doctor", help="what this machine can run")
    sub.add_parser("runs", help="list runs")
    p = sub.add_parser("lint", help="check shipped text in the plugin")
    p.add_argument("paths", nargs="*")
    parser.set_defaults(func=main)


def _say(msg: str) -> None:
    print(msg, flush=True)


def _run(host: Host, argv, timeout=3600, env=None) -> int:
    _say("  $ " + " ".join(shlex.quote(str(a)) for a in argv))
    res = host.x.run(argv, timeout=timeout, env=env)
    tail = [l for l in (res.text + "\n" + res.err).splitlines() if l.strip()][-8:]
    for line in tail:
        _say("    " + line[:200])
    return res.rc


def _upload(host: Host, local_dir: Path, remote_dir: str) -> None:
    host.x.mkdirs(remote_dir)
    for f in sorted(local_dir.glob("*.whl")):
        target = host.x.join(remote_dir, f.name)
        if host.x.exists(target):
            continue
        _say(f"  copying {f.name} to the WOOF host")
        res = host.x.run(["bash", "-c", f"cat > {shlex.quote(target)}"], timeout=1800, input_bytes=f.read_bytes())
        if res.rc != 0:
            raise SystemExit(f"copy failed: {res.err[-300:]}")


def cmd_install(args) -> int:
    s = settings.load()
    host = Host(s)
    x = host.x
    _say(f"WOOF install on {host.label}")
    uname = x.run(["uname", "-sm"], timeout=30).text.strip()
    if "Linux" not in uname or "x86_64" not in uname:
        _say(f"WOOF runs on Linux x86_64; this host reports {uname or 'no Linux shell'}.")
        _say("On Windows set executor to wsl (WSL2 Ubuntu); on a Mac point executor ssh at a Linux GPU box.")
        return 2
    probe = tools._probe(host)
    cuda = args.cuda or "13"
    driver_major = probe.get("cuda_major") or 0
    if not args.cuda and probe["devices"] and 0 < driver_major < 13:
        # a CUDA 12 driver cannot load the CUDA 13 runtime the all-cu13 extra brings
        cuda = "12"
        _say(f"  the driver supports CUDA {driver_major}, so the all-cu12 extra is used (all-cu13 needs an R580+ driver)")
    if probe["devices"]:
        g = probe["devices"][0]
        _say(f"  GPU: {g['name']}, {g['memory_total_gib']} GiB; driver CUDA {probe.get('cuda_major')}; extra all-cu{cuda}")
    else:
        _say("  no NVIDIA GPU found: WOOF will install for planning and preparing only")
    venv = x.expand(args.venv)
    py = x.join(venv, "bin", "python")
    uv = x.run(["bash", "-lc", "command -v uv || ls ~/.local/bin/uv 2>/dev/null"], timeout=30).text.strip().splitlines()
    uv = uv[-1] if uv else ""
    if not x.exists(py):
        rc = _run(host, [uv, "venv", "-p", "3.12", venv] if uv else ["python3", "-m", "venv", venv], timeout=600)
        if rc != 0:
            _say("could not make the venv; WOOF needs Python 3.11 or newer")
            return 1
    if args.wheel_dir:
        wdir = x.expand(args.wheel_dir)
        local = Path(args.wheel_dir).expanduser()
        if x.remote and local.is_dir():
            wdir = x.join(host.runs_dir, "wheels")
            _upload(host, local, wdir)
        listing = x.run(["ls", "-1", wdir], timeout=30).text.split()
        pure = [n for n in listing if PURE_RE.match(n)]
        main_w = [n for n in listing if WHEEL_RE.match(n)]
        data_w = [n for n in listing if DATA_RE.match(n)]
        if not main_w:
            if pure:
                _say(f"refusing {pure[0]}: the py3-none-any wheel carries no native tools. "
                     "Use the manylinux wheel from the same release.")
            else:
                _say(f"no recast_woof manylinux wheel in {wdir}")
            return 2
        # prefer a wheel inside the release line this plugin is built for
        inside = [n for n in main_w if requirement.supported(WHEEL_RE.match(n).group("ver"))]
        chosen = (inside or main_w)[-1]
        ver = WHEEL_RE.match(chosen).group("ver")
        if not inside:
            _say(f"  note: this plugin is built for {requirement.pip_spec()}; {chosen} is WOOF {ver}")
        data_same = [d for d in data_w if DATA_RE.match(d).group("ver") == ver] or data_w[-1:]
        targets = [f"{x.join(wdir, chosen)}[all-cu{cuda}]"] + [x.join(wdir, d) for d in data_same[-1:]]
        if uv:
            rc = _run(host, [uv, "pip", "install", "--python", py, "--find-links", wdir, *targets], timeout=3600)
        else:
            rc = _run(host, [py, "-m", "pip", "install", "--find-links", wdir, *targets], timeout=3600)
    else:
        spec = requirement.pip_spec(f"all-cu{cuda}")
        _say(f"  installing {spec} from PyPI")
        rc = _run(host, [uv, "pip", "install", "--python", py, spec] if uv else [py, "-m", "pip", "install", spec], timeout=3600)
    if rc != 0:
        _say("WOOF did not install; the lines above say why")
        return 1
    woof = x.join(venv, "bin", "woof")
    _run(host, [woof, "version"], timeout=120)
    settings.save("woof_bin", woof)
    s = settings.load()
    host = Host(s)
    # the Linux wheel carries the native tools; the two large Thompson tables are fetched, as in WOOF's own install
    rc = _run(host, [woof, "fetch-tables"], timeout=1800)
    if rc != 0:
        _say("fetch-tables failed; runs with Thompson microphysics need it (rerun: woof fetch-tables)")
    if args.geog_root:
        root = x.expand(args.geog_root)
        if not x.exists(root):
            _say(f"{root} does not exist on the WOOF host")
            return 2
        settings.save("geog_root", root)
        host = Host(settings.load())
        _say(f"  geography: using {root}")
    elif not args.no_geog:
        try:
            answer = "y" if args.yes else input("  Download geography now (about 1.3 GB, 17 GB on disk)? [y/N] ").strip().lower()
        except EOFError:  # no terminal to answer from: the default, no
            answer = ""
        if answer.startswith("y"):
            _run(host, [woof, "fetch-geog", "--datasets", "wrf"], timeout=7200)
        else:
            _say("  geography skipped; add it later with woof fetch-geog --datasets wrf, or --geog-root")
    if probe["devices"]:
        _say("  running WOOF's own doctor (it compiles one kernel on the GPU)")
        ans = tools.gpu_command(host, "woof-install-doctor", ["doctor"], wait_s=0)
        if ans.get("busy"):
            _say("  the GPU is busy with another job (shared lock held); run `woof doctor` later")
        else:
            for line in [l for l in (ans.get("stdout", "") + ans.get("stderr", "")).splitlines() if l.strip()][-12:]:
                _say("    " + line[:200])
    _say(f"done: woof_bin = {woof}")
    return 0


def cmd_doctor(args) -> int:
    host = Host(settings.load())
    data = tools.doctor_data(host, fresh=True)
    print(json.dumps(data, indent=2, default=str))
    return 0 if data.get("mode") == "full" else 1


def cmd_runs(args) -> int:
    print(tools.woof_status({}))
    return 0


def cmd_lint(args) -> int:
    root = Path(__file__).resolve().parent.parent
    script = root / "scripts" / "lint_text.py"
    return subprocess.call([sys.executable, str(script), *(args.paths or [str(root)])])


def main(args) -> int:
    cmd = getattr(args, "woof_command", None)
    if cmd == "install":
        return cmd_install(args)
    if cmd == "doctor":
        return cmd_doctor(args)
    if cmd == "runs":
        return cmd_runs(args)
    if cmd == "lint":
        return cmd_lint(args)
    print("usage: hermes woof install|doctor|runs|lint")
    return 2
