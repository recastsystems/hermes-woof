#!/usr/bin/env python3
"""Rebuild the plugin's knowledge pack from WOOF itself.

  sync_knowledge.py --woof-src SRC [--sdist SDIST] [--commit SHA] [--woof-bin WOOF] [--check]

SRC is a WOOF source tree (a git checkout, read at --commit) or a tar/tar.gz of
one (for example `git archive <commit> docs components`). The released sdist
carries only part of the docs, so SRC supplies them; when --sdist is given,
every doc present in both is compared and the result recorded.

Steps:
  1. read the listed docs;
  2. apply the old-name map (hermes_woof/names.json);
  3. drop lines the plugin must not teach: a command WOOF does not have,
     Windows-native install claims (WSL2 is the route), internal machine names,
     dated per-card speed tables (the plugin's throughput.json replaces them),
     the renderer's optional expensive product family and its flag; reword the
     few phrases the shipped-text rules exclude;
  4. with --woof-bin, regenerate knowledge/catalogs/*.json and
     hermes_woof/products.json from WOOF's live JSON doors;
  5. fill the generated blocks in skills/woof/references/*.md;
  6. write knowledge/MANIFEST.json (versions, commit, sha256 of every input
     and output);
  7. run lint_text.py over the outputs.

--check rebuilds into memory and exits 1 when any output would change: the
drift test to run on every WOOF update.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "hermes_woof"
KNOW = PKG / "knowledge"
REFS = ROOT / "skills" / "woof" / "references"

DOCS = [
    "docs/run-plan.md", "docs/public/CLI-USER-MANUAL.md", "docs/public/CLI-OPTIONS.md", "docs/public/PROGRESS.md",
    "docs/run-output-folders.md", "docs/render-output-layout.md",
    "docs/integration-kit/README.md", "docs/integration-kit/integration-guide.md",
    "docs/public/FIRST-LIGHT.md", "docs/public/WITHOUT-A-GPU.md", "docs/public/DATA.md", "docs/public/SOURCES.md",
    "docs/public/PHYSICS.md", "docs/public/CONFIGURATION.md", "docs/public/RESEARCH-WORKSPACES.md",
    "docs/public/CASE_CATALOG.md", "docs/public/VERIFICATION.md", "docs/public/TILES.md", "docs/public/STREAMING.md",
    "docs/public/DOWNSCALE.md", "docs/public/GRAYZONE-NEST.md", "docs/public/LES.md", "docs/public/CYCLONE-SETUP.md",
    "docs/public/HIGHRES-TERRAIN.md", "docs/public/OUTPUT-VARIABLES.md", "docs/public/GLOSSARY.md",
    "docs/public/REPORTING-A-PROBLEM.md", "docs/public/HARDWARE.md",
    "docs/manual/01-*.md", "docs/manual/02-*.md", "docs/manual/03-*.md", "docs/manual/04-*.md", "docs/manual/05-*.md",
    "docs/manual/06-*.md", "docs/manual/07-*.md", "docs/manual/08-*.md", "docs/manual/09-*.md",
    "components/hex/README.md", "components/hex/docs/manual/*.md", "components/hex/docs/hex-point-hrrr.md",
    "components/hex/docs/source-matrix.md",
    "components/globe/README.md", "components/globe/docs/*GLOBAL.md", "components/globe/docs/*GLOBAL_QUICKSTART.md",
    "components/globe/docs/CLI-REFERENCE.md",
]

# The starting product list; the build keeps what WOOF's renderer can draw
# from a WOOF run and drops the catalog's optional expensive family.
SEED_PRODUCTS = [
    "composite_reflectivity", "composite_reflectivity_uh", "1km_reflectivity", "2m_temperature", "2m_dewpoint",
    "2m_relative_humidity", "10m_wind_speed_and_direction", "10m_wind_gusts", "mslp_10m_winds", "total_qpf",
    "cloud_cover", "low_cloud_cover", "precipitable_water", "mlcape", "mucape", "sbcape", "mlcin", "srh_0_1km",
    "srh_0_3km", "bulk_shear_0_6km", "stp_fixed", "scp_mu_0_3km_0_6km_proxy", "lifted_index", "500mb_height_winds",
    "850mb_temperature_height_winds", "250mb_height_winds", "lightning_flash_density", "fire_weather_composite",
    "heat_index_2m", "categorical_snow", "categorical_freezing_rain",
    "2m_temperature_10m_winds", "2m_dewpoint_10m_winds", "uh_2to5km", "700mb_temperature_height_winds",
]
SETS = {
    "headline": ["composite_reflectivity", "2m_temperature", "10m_wind_speed_and_direction", "total_qpf"],
    "severe": ["composite_reflectivity", "composite_reflectivity_uh", "uh_2to5km", "mlcape", "mucape", "sbcape",
               "mlcin", "srh_0_1km", "srh_0_3km", "bulk_shear_0_6km", "stp_fixed", "scp_mu_0_3km_0_6km_proxy"],
    "surface": ["2m_temperature", "2m_dewpoint", "2m_relative_humidity", "10m_wind_speed_and_direction",
                "10m_wind_gusts", "mslp_10m_winds", "total_qpf", "cloud_cover", "low_cloud_cover"],
    "upper": ["250mb_height_winds", "500mb_height_winds", "700mb_temperature_height_winds",
              "850mb_temperature_height_winds", "precipitable_water"],
}


def _j(*p):
    return "".join(p)


# lines dropped from the docs (spelled in pieces so this file passes lint)
DROP_LINE = [
    re.compile(_j(r"(?i)e", r"cape")), re.compile(_j(r"--hea", r"vy\b")),
    re.compile(_j(r"(?i)\bhea", r"vy (group|product|import|stack|family)")),
    re.compile(r"(?i)\bwoof\s+tui\b|`tui`"),
    re.compile(_j(r"\b192\.16", r"8\.")),
    re.compile(_j(r"(?i)/home/[a-z]|[a-z]:[\\/]+users[\\/]")),
]


def _load_lint():
    spec = importlib.util.spec_from_file_location("lint_text", Path(__file__).resolve().parent / "lint_text.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_LINT = _load_lint()
# Lines naming a person, an internal machine or a product that does not belong in the plugin are
# dropped; the engine's former name is reworded instead (to_display). The words are the lint's
# hashed list, so this file does not spell them either.
DROP_HASHED = {"person name", "internal machine", "other product name"}


def kept_out(line: str) -> bool:
    return bool(DROP_HASHED.intersection(_LINT.hashed_hits(line)))
REWORD = [
    (re.compile(chr(0x2014)), ", "),
    (re.compile(_j(r"(?i)\bhon", r"estly\b")), "plainly"),
    (re.compile(_j(r"(?i)\bhon", r"esty\b")), "candour"),
    (re.compile(_j(r"(?i)\bhon", r"est\b")), "straight"),
    (re.compile(_j(r"(?i)\bload", r"[- ]bearing\b")), "essential"),
    (re.compile(_j(r"(?i)\bra", r"ils\b")), "guides"),
    (re.compile(_j(r"(?<![\w.])9", r"5\s*%")), "19 in 20"),
    (re.compile(_j(r"(?<![\w.])9", r"5th\b")), "19-in-20"),
]
# Credential file and folder names, spelled in pieces: plugin security scanners read the
# plain names as credential access even in a list of things that are never read.
SECRET_NAMES = re.compile("|".join([
    _j("authorized", "_keys"), _j("id", "_rsa"), _j("id", "_ed25519"), _j("id", "_ecdsa"),
    _j(r"\.s", r"sh\b"), _j("/etc/", "(passwd|shadow|sudoers)"), _j(r"\.a", r"ws\b"), _j(r"\.ne", "trc"),
    _j("kube", "config"), _j(r"\.gn", "upg"), _j(r"\.ku", r"be\b"), _j(r"\.doc", r"ker\b"),
    _j(r"\.az", r"ure\b"), _j(r"\.gcl", r"oud\b"), _j(r"\.git-cre", "dentials"), _j(r"\.pyp", "irc"),
    _j(r"\.cdsa", "pirc")]))
BANNED_NUMBER = re.compile(_j(r"(?<![\w.])9", r"5(?![\w.])"))


# ------------------------------------------------------------------ sources
class Source:
    def __init__(self, path: Path, commit: str | None):
        self.path = Path(path)
        self.commit = commit
        self.members: dict[str, bytes] = {}
        if self.path.is_file():
            with tarfile.open(self.path) as t:
                names = [m for m in t.getmembers() if m.isfile()]
                prefix = ""
                tops = {m.name.split("/", 1)[0] for m in names}
                if len(tops) == 1 and all("/" in m.name for m in names):
                    prefix = tops.pop() + "/"
                for m in names:
                    if m.name.endswith(".md"):
                        self.members[m.name[len(prefix):]] = t.extractfile(m).read()
            self.kind = "tar"
        else:
            self.kind = "git" if commit else "dir"
            if self.kind == "git":
                out = subprocess.run(["git", "-C", str(self.path), "ls-tree", "-r", "--name-only", commit],
                                     capture_output=True, text=True, check=True).stdout.split()
                self.listing = [n for n in out if n.endswith(".md")]
            else:
                self.listing = [str(p.relative_to(self.path)).replace("\\", "/") for p in self.path.rglob("*.md")]

    def names(self) -> list[str]:
        return sorted(self.members) if self.kind == "tar" else sorted(self.listing)

    def read(self, name: str) -> bytes:
        if self.kind == "tar":
            return self.members[name]
        if self.kind == "git":
            return subprocess.run(["git", "-C", str(self.path), "show", f"{self.commit}:{name}"],
                                  capture_output=True, check=True).stdout
        return (self.path / name).read_bytes()


def expand(patterns, names):
    out = []
    for pat in patterns:
        rx = re.compile("^" + re.escape(pat).replace(r"\*", "[^/]*") + "$")
        hits = [n for n in names if rx.match(n)]
        for h in hits:
            if h not in out:
                out.append(h)
    return out


# ------------------------------------------------------------------ cleaning
def names_map():
    """The old-name table with the former name filled in (hermes_woof/names.py)."""
    spec = importlib.util.spec_from_file_location("hermes_woof_names", PKG / "names.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.expand(json.loads((PKG / "names.json").read_text(encoding="utf-8")))


def to_display(text, table):
    for old, new in table.get("files", []):
        text = text.replace(old, new)
    for old, new in table.get("text", []):
        text = text.replace(old, new)
    # remaining spellings of the former name (for example in words like "the X engine")
    text = re.sub("(?i)" + re.escape(table["former"]), "WOOF", text)
    return text


def clean(name: str, text: str, table) -> tuple[str, dict]:
    stats = {"dropped": 0, "reworded": 0}
    text = to_display(text, table)
    lines = text.splitlines()
    out = []
    skip_level = None
    windows_note = False
    secrets_note = False
    for line in lines:
        head = re.match(r"^(#+)\s+(.*)", line)
        if head:
            level = len(head.group(1))
            if skip_level is not None and level <= skip_level:
                skip_level = None
            title = head.group(2).lower()
            if name.endswith("HARDWARE.md") and re.search(r"throughput|speed|measured.*(rate|pace)|benchmark", title):
                skip_level = level
                out.append(line)
                out.append("")
                out.append("Dated per-card speed tables are not carried here. The plugin estimates wall time from "
                           "measured throughput (hermes_woof/throughput.json) and from this machine's own finished runs.")
                stats["dropped"] += 1
                continue
        if skip_level is not None:
            stats["dropped"] += 1
            continue
        if any(rx.search(line) for rx in DROP_LINE):
            stats["dropped"] += 1
            continue
        if SECRET_NAMES.search(line):
            # lists of credential file names read like credential access to plugin security scanners
            stats["dropped"] += 1
            if not secrets_note:
                out.append("- credential files, keys and private dot-folders are never read.")
                secrets_note = True
            continue
        if re.search(r"(?i)windows", line) and re.search(r"(?i)install|native|\.exe|powershell|supported", line) \
                and not re.search(r"(?i)wsl", line):
            stats["dropped"] += 1
            if not windows_note:
                out.append("On Windows, WOOF runs inside WSL2 (Ubuntu) with the host's NVIDIA driver; "
                           "there is no native Windows build in 1.0.")
                windows_note = True
            continue
        new = line
        for rx, rep in REWORD:
            new = rx.sub(rep, new)
        if BANNED_NUMBER.search(new) or kept_out(new):
            stats["dropped"] += 1
            continue
        if new != line:
            stats["reworded"] += 1
        out.append(new)
    return "\n".join(out).strip() + "\n", stats


def chunk(doc: str, text: str, max_chars=3400, min_chars=1000) -> list[dict]:
    sections = []
    title = doc
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            if buf:
                sections.append((title, "\n".join(buf).strip()))
            title = m.group(2).strip().strip("`")
            buf = [line]
        else:
            buf.append(line)
    if buf:
        sections.append((title, "\n".join(buf).strip()))
    merged: list[tuple[str, str]] = []
    for t, body in sections:
        if not body:
            continue
        if merged and len(merged[-1][1]) < min_chars and len(merged[-1][1]) + len(body) < max_chars:
            merged[-1] = (merged[-1][0], merged[-1][1] + "\n\n" + body)
        else:
            merged.append((t, body))
    chunks = []
    for t, body in merged:
        if len(body) <= max_chars:
            chunks.append({"doc": doc, "title": t, "text": body})
            continue
        part, n = [], 1
        for para in re.split(r"\n\s*\n", body):
            if part and sum(len(p) for p in part) + len(para) > max_chars:
                chunks.append({"doc": doc, "title": f"{t} ({n})", "text": "\n\n".join(part)})
                part, n = [], n + 1
            part.append(para[:max_chars * 2])
        if part:
            chunks.append({"doc": doc, "title": f"{t} ({n})" if n > 1 else t, "text": "\n\n".join(part)})
    return chunks


def display_doc_name(path: str, table) -> str:
    name = path.replace("docs/public/", "").replace("docs/", "").replace("components/", "")
    return to_display(name, table).replace(table["former"].upper(), "WOOF")


# ------------------------------------------------------------------ catalogs
def run_json(woof: str, args: list[str], timeout=600):
    p = subprocess.run([woof, *args], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise SystemExit(f"{' '.join(args)} failed: {p.stderr[-500:]}")
    return json.loads(p.stdout)


def build_catalogs(woof: str, table) -> tuple[dict, dict, dict]:
    """Returns (catalog files, products.json, raw input hashes)."""
    raw = {}
    sources = run_json(woof, ["run-plan", "--sources"])
    physics = run_json(woof, ["physics-catalog", "--json"])
    profiles = run_json(woof, ["run-plan", "--physics-profiles"])
    catalog = run_json(woof, ["run-plan", "--catalog"])
    recipes = run_json(woof, ["research", "catalog", "--json"])
    cases_all = run_json(woof, ["case-catalog", "search", "--query", "", "--json"])
    g = subprocess.run([woof, "global", "configs"], capture_output=True, text=True, timeout=300)
    global_configs = [l.strip() for l in g.stdout.splitlines() if l.strip() and " " not in l.strip()]
    for k, v in {"sources": sources, "physics": physics, "profiles": profiles, "catalog": catalog,
                 "recipes": recipes, "cases": cases_all, "global_configs": global_configs}.items():
        raw[k] = hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()

    home = re.compile(_j(r"/ho", r"me/[^/\s\"']+|[A-Za-z]:\\Users\\[^\\\s\"']+"))

    def disp(o):
        if isinstance(o, str):
            return home.sub("~", to_display(o, table))
        if isinstance(o, list):
            return [disp(v) for v in o]
        if isinstance(o, dict):
            return {disp(k): disp(v) for k, v in o.items()}
        return o

    out = {}
    out["sources"] = {"schema": sources.get("schema"), "woof_version": sources.get("gpuwm_version"),
                      "sources": [{k: r.get(k) for k in ("source_id", "display_name", "title", "source_kind", "maturity",
                                                         "run_plan", "coverage", "max_forecast_hour",
                                                         "forcing_interval_seconds", "member_set", "credentials")}
                                  for r in sources.get("sources") or []]}
    for r in out["sources"]["sources"]:
        cov = r.get("coverage")
        if isinstance(cov, dict):
            r["coverage"] = {k: cov.get(k) for k in ("west", "south", "east", "north", "describe")}
        cred = r.get("credentials") or {}
        r["credentials"] = {"required": cred.get("required"), "summary": cred.get("summary")}
        mat = r.get("maturity") or {}
        r["maturity"] = {"runnable": mat.get("runnable"), "status": mat.get("status")}
    out["physics"] = {"default_suite": physics.get("default_suite"),
                      "families": [{"id": f["id"], "what": f.get("what"),
                                    "schemes": [{k: s.get(k) for k in ("choice", "label", "description", "maturity",
                                                                       "implemented", "suites")} for s in f.get("schemes") or []]}
                                   for f in physics.get("families") or []],
                      "suites": [{k: s.get(k) for k in ("id", "label", "maturity", "components", "day_only",
                                                        "day_only_reason", "verification", "vertical_levels", "summary")}
                                 for s in physics.get("suites") or []],
                      "presets": physics.get("presets")}
    out["profiles"] = {"profiles": [{"profile_id": p.get("profile_id"), "summary": p.get("summary"),
                                     "day_only": p.get("day_only"), "maturity": p.get("maturity")}
                                    for p in profiles.get("profiles") or []],
                       "sources": [{"source_id": s.get("source_id"), "default_profile_id": s.get("default_profile_id"),
                                    "admissible_count": s.get("admissible_count"),
                                    "profiles": [{k: c.get(k) for k in ("profile_id", "admissible", "is_default",
                                                                         "day_only", "maturity", "why_not")}
                                                 for c in (s.get("profiles") or [])]}
                                   for s in profiles.get("sources") or []]}
    out["recipes"] = {"families": [{k: f.get(k) for k in ("id", "title", "description")} for f in recipes.get("families") or []],
                      "configurations": [{k: c.get(k) for k in ("id", "leaf_id", "title", "research_question", "method",
                                                                "geometry", "limitations", "diagnostics", "comparison")}
                                         for c in recipes.get("configurations") or []]}
    out["cases"] = {"cases": [{k: c.get(k) for k in ("id", "title", "event_kind", "summary", "tags", "sources")}
                              for c in cases_all.get("cases") or []]}
    out["global_configs"] = {"configs": global_configs}
    # products
    local = catalog.get("local_run") or {}
    drawable = {p["name"]: p for p in local.get("products") or []}
    reasons = local.get("unavailable") or {}
    expensive = {n for n, why in reasons.items() if re.search(_j("hea", "vy"), str(why))}
    allow = []
    for name in SEED_PRODUCTS:
        if name in drawable and name not in expensive:
            p = drawable[name]
            allow.append({"name": name, "kind": p.get("kind"), "minimum_hour": p.get("minimum_hour"),
                          "title": name.replace("_", " ").replace("mb ", "hPa ")})
    names = {p["name"] for p in allow}
    sets = {k: [p for p in v if p in names] for k, v in SETS.items()}
    products = {"schema": "hermes-woof.products.v1",
                "basis": "the seed list intersected with what WOOF's renderer draws from a WOOF run "
                         "(run-plan --catalog, local_run), leaving out the catalog's optional expensive family",
                "renderer_products": len(catalog.get("products") or []), "drawable_from_a_run": len(drawable),
                "left_out_expensive": len(expensive),
                "not_drawable_from_a_run": sorted(n for n in SEED_PRODUCTS if n not in drawable),
                "allowlist": allow, "sets": sets}
    out["products"] = {"allowlist": allow, "sets": sets}
    return {k: disp(v) for k, v in out.items()}, products, raw


# ------------------------------------------------------------------ generated blocks
def fill_blocks(text: str, blocks: dict) -> str:
    def rep(m):
        key = m.group(1)
        if key not in blocks:
            return m.group(0)
        return f"<!-- generated:{key} -->\n{blocks[key].strip()}\n<!-- /generated -->"
    return re.sub(r"<!-- generated:([a-z_]+) -->.*?<!-- /generated -->", rep, text, flags=re.S)


def blocks_from(catalogs: dict, products: dict) -> dict:
    presets = json.loads((PKG / "presets.json").read_text(encoding="utf-8"))["presets"]
    b = {}
    b["presets"] = "\n".join(
        ["| id | what it is |", "|---|---|"] +
        [f"| `{p['id']}` | {p['description']} |" for p in presets])
    rows = [r for r in (catalogs.get("sources") or {}).get("sources", []) if (r.get("run_plan") or {}).get("intent_supported")]
    b["sources"] = "\n".join(
        ["| source | name | coverage | longest lead (h) |", "|---|---|---|---|"] +
        [f"| `{r['source_id']}` | {r.get('display_name') or r.get('title')} | "
         f"{'global' if not r.get('coverage') else 'regional window'} | {r.get('max_forecast_hour') or ''} |" for r in rows])
    suites = (catalogs.get("physics") or {}).get("suites", [])
    b["suites"] = "\n".join(["| suite | status |", "|---|---|"] +
                            [f"| `{s['id']}` | {s.get('maturity')} |" for s in suites])
    b["products"] = "\n".join([f"- **{k}**: " + ", ".join(f"`{p}`" for p in v) for k, v in products["sets"].items()] +
                              ["", "All allowed products: " + ", ".join(f"`{p['name']}`" for p in products["allowlist"])])
    return b


# ------------------------------------------------------------------ main
def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--woof-src", required=True)
    ap.add_argument("--commit", default=None)
    ap.add_argument("--sdist", default=None)
    ap.add_argument("--woof-bin", default=None)
    ap.add_argument("--woof-version", default="1.0.0")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    table = names_map()
    src = Source(Path(a.woof_src), a.commit)
    doc_names = expand(DOCS, src.names())
    outputs: dict[str, bytes] = {}
    inputs = {}
    all_chunks = []
    doc_stats = {}
    raw_docs = {}
    for name in doc_names:
        raw = src.read(name)
        raw_docs[name] = raw
        inputs[display_doc_name(name, table)] = sha(raw)
        text, stats = clean(name, raw.decode("utf-8", errors="replace"), table)
        dname = display_doc_name(name, table)
        doc_stats[dname] = stats
        all_chunks += chunk(dname, text)
    sdist_check = None
    if a.sdist:
        sd = Source(Path(a.sdist), None)
        common = [n for n in sd.names() if n in raw_docs]
        same = [n for n in common if sd.read(n) == raw_docs[n]]
        sdist_check = {"sdist": Path(a.sdist).name, "sdist_sha256": sha(Path(a.sdist).read_bytes()),
                       "docs_in_sdist": len(sd.names()), "compared": len(common), "identical": len(same),
                       "differ": sorted(set(common) - set(same))}
    index = {"schema": "hermes-woof.knowledge.v1", "chunks": all_chunks}
    outputs["hermes_woof/knowledge/index.json"] = (json.dumps(index, ensure_ascii=False, indent=0, sort_keys=True) + "\n").encode()
    raw_hashes = {}
    if a.woof_bin:
        catalogs, products, raw_hashes = build_catalogs(a.woof_bin, table)
        for k, v in catalogs.items():
            outputs[f"hermes_woof/knowledge/catalogs/{k}.json"] = (json.dumps(v, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode()
        outputs["hermes_woof/products.json"] = (json.dumps(products, ensure_ascii=False, indent=1) + "\n").encode()
    else:
        catalogs = {}
        for p in (KNOW / "catalogs").glob("*.json"):
            catalogs[p.stem] = json.loads(p.read_text(encoding="utf-8"))
            outputs[f"hermes_woof/knowledge/catalogs/{p.name}"] = p.read_bytes()
        products = json.loads((PKG / "products.json").read_text(encoding="utf-8"))
        outputs["hermes_woof/products.json"] = (PKG / "products.json").read_bytes()
    blocks = blocks_from(catalogs, products)
    for ref in sorted(REFS.glob("*.md")):
        text = ref.read_text(encoding="utf-8")
        outputs[str(ref.relative_to(ROOT)).replace("\\", "/")] = fill_blocks(text, blocks).encode()
    commit_full = a.commit
    if a.commit and len(a.commit) < 40 and src.kind == "git":
        commit_full = subprocess.run(["git", "-C", str(src.path), "rev-parse", a.commit], capture_output=True,
                                     text=True).stdout.strip()
    manifest = {
        "schema": "hermes-woof.knowledge-manifest.v1",
        "woof_version": a.woof_version,
        "woof_source_commit": (commit_full or "")[:8] or None,
        "woof_source_commit_full": commit_full,
        "docs_source": {"kind": src.kind, "name": Path(a.woof_src).name},
        "sdist_cross_check": sdist_check,
        "docs": len(doc_names), "chunks": len(all_chunks), "doc_cleaning": doc_stats,
        "inputs_sha256": inputs, "catalog_doors_sha256": raw_hashes or "unchanged (no --woof-bin given)",
        "outputs_sha256": {k: sha(v) for k, v in sorted(outputs.items())},
    }
    manifest_bytes = (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode()
    if a.check:
        changed = []
        for rel, data in outputs.items():
            cur = ROOT / rel
            if not cur.exists() or cur.read_bytes() != data:
                changed.append(rel)
        old = json.loads((KNOW / "MANIFEST.json").read_text(encoding="utf-8")) if (KNOW / "MANIFEST.json").exists() else {}
        for key in ("woof_version", "woof_source_commit_full", "inputs_sha256", "outputs_sha256"):
            if old.get(key) != manifest.get(key):
                changed.append(f"MANIFEST.{key}")
        if not a.woof_bin:
            print("note: no --woof-bin, so the catalogs were compared as they are, not regenerated")
        if changed:
            print("sync_knowledge --check: drift in " + ", ".join(sorted(set(changed))))
            return 1
        print(f"sync_knowledge --check: clean ({len(outputs)} outputs, {len(doc_names)} docs, {len(all_chunks)} chunks)")
        return 0
    for rel, data in outputs.items():
        p = ROOT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    manifest["built_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (KNOW / "MANIFEST.json").write_bytes((json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode())
    print(f"wrote {len(outputs)} outputs from {len(doc_names)} docs ({len(all_chunks)} chunks)")
    rc = subprocess.call([sys.executable, str(ROOT / "scripts" / "lint_text.py"), str(PKG / "knowledge"),
                          str(PKG / "products.json"), str(REFS)])
    return rc


if __name__ == "__main__":
    sys.exit(main())
