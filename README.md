# WOOF for Hermes Agent

`hermes-woof` is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for
[WOOF](https://pypi.org/project/recast-woof/), an open-source weather model that runs on one
NVIDIA GPU. Ask for a forecast in plain words. The agent plans the run with WOOF's own sizing
code and tells you the grid, the memory fit, the download and the estimated time. It then
launches the run on your card and puts the maps in the chat while the forecast runs. It can also
compare runs, score them against radar and surface observations, and plan multi-run research
campaigns.

The agent never guesses: every grid, memory fit, download size and option name comes from WOOF,
and every picture from WOOF's own renderer. Wall times are estimates from measured throughput,
always shown with their basis.

## Install

```bash
hermes plugins install https://github.com/recastsystems/hermes-woof   # answer y to "Enable 'woof' now?"
hermes woof install
hermes
```

`hermes woof install` sets up WOOF where it will run:

- makes `~/woof-env` and installs `recast-woof[all-cu13]>=1.0.0,<1.1` from PyPI (`--cuda 12`
  for `all-cu12`; a CUDA 12 driver gets `all-cu12` on its own);
- fetches WOOF's two Thompson microphysics tables (about 314 MiB);
- downloads geography after asking (about 1.3 GB, 17 GB unpacked), or uses a WPS_GEOG folder
  you already have with `--geog-root`;
- runs WOOF's own doctor on the GPU and records the `woof` command in the plugin settings.

`hermes woof doctor` then shows what this machine can run. For a scripted install, add
`--enable` to the first line and `--yes` (or `--no-geog`) to the second. `--wheel-dir DIR`
installs from a folder of WOOF release files instead of PyPI.

This release is built for WOOF 1.0.x (`recast-woof>=1.0.0,<1.1`) and checked with Hermes Agent
v0.21.5.

## Requirements

To run forecasts, the machine where WOOF runs needs:

- Linux on x86_64 and Python 3.11 or newer;
- an NVIDIA GPU with a CUDA 12 (R525 or newer) or CUDA 13 (R580 or newer) driver. A quick 3 km
  run peaks under 4 GiB of GPU memory. Bigger boxes, finer grids and nests need more, and every
  plan states its peak against the memory the card has free before anything starts;
- about 20 GB of disk for WOOF and its geography, plus each run's download and output (the
  plan states both).

Without an NVIDIA GPU the plugin works in planning mode: it sizes runs for a card you name, and
it can drive a Linux GPU box over SSH (`executor: ssh`). On Windows, WOOF runs in WSL2 Ubuntu
with the host's NVIDIA driver (`executor: wsl`); that route is untested. Hermes itself needs
Python 3.11 to 3.13.

## Example session

Condensed from a real session on an RTX 4090 that WOOF shared with a local model server
(Qwen3.8-27B on llama.cpp), with WOOF 1.0.0 and Hermes Agent v0.21.5.

> **You:** Can my GPU run WOOF? What can I run?
>
> **Agent** (`woof_doctor`): Yes. RTX 4090, 24 GB, CUDA 13 driver; WOOF 1.0.0 is installed with
> its tables and geography. The local model server holds 16.6 GiB of the card, so runs are sized
> for the roughly 6 GiB left. A 3 km run over a 300 km box fits comfortably.
>
> **You:** Run a 12 hour forecast for San Francisco at 1 km from this morning's HRRR. If that
> will not finish in about 15 minutes on this card, pick the closest setup that does and tell me
> what you changed. Show me the plots while it runs.
>
> **Agent** (`woof_plan`): It fits as asked. 86 x 68 points at 1 km with 49 levels (WOOF
> shrank the 150 km box to fit the memory left, and it still covers the Bay Area and the coast).
> HRRR 15Z, 12 hours. Peak 4.49 GiB against a 4.75 GiB budget. About 13 minutes (fetch 7.5,
> prepare 1.8, forecast 3.6), estimated from 5 earlier runs on this machine. Physics
> `thompson-mp8-ysu-mm5-noah-rte-rrtmgp-v1`, status implemented-unverified. Launching.
>
> **Agent** (`woof_launch`): Launched. I will post the maps when the first pictures are ready.
>
> `[woof] first_products_ready: the first pictures are on disk while the forecast runs on.`
>
> **Agent** (`woof_show`): The starting state, 15Z: dry everywhere, a cool marine layer on the
> coast and the bay, warmer inland valleys, onshore wind. *(reflectivity, 2 m temperature, 10 m
> wind and precipitation maps)*
>
> `[woof] completed: finished in 633 s, 104 pictures.`
>
> **Agent** (`woof_report`, `woof_show`): Done in 10.5 minutes against the 13 minute estimate,
> and the timing is recorded so later estimates on this card get sharper. It stayed dry for all
> 12 hours. Every frame is in the run's gallery page. *(the final hour's maps)*

Launches return at once; the `[woof]` messages arrive in the chat on their own. Pictures show
inline: media cards in Hermes Desktop, file links in the terminal, attachments on chat
platforms. Every run also gets a `gallery.html` page with a frame slider per product.

More to try:

> Now compare Thompson and Morrison microphysics on the same setup.

> Score both runs against radar and surface observations.

> Plan me a research campaign on Florida sea-breeze storms next week that fits in two hours on
> this card.

## Tools

| Tool | What it does |
|---|---|
| `woof_doctor` | GPU, memory, WOOF version, whether runs can launch here, one fix per gap |
| `woof_catalog` | live choices: sources, physics, profiles, products, presets, cases, recipes, global configs, observations |
| `woof_docs` | search WOOF's documentation |
| `woof_plan` | size and price one run before anything starts |
| `woof_launch` | queue a planned run; returns at once |
| `woof_status` | stage, progress, speed, time left, errors with remedies |
| `woof_stop` | stop a run this plugin started (needs confirmation) |
| `woof_show` | the run's maps, to the model and to the user |
| `woof_compare` | difference maps or side-by-side sheets for two runs |
| `woof_campaign` | plan, start, watch and stop a sweep of runs against a budget |
| `woof_score` | score a finished run against ASOS, MRMS and Stage-IV |
| `woof_report` | summary: setup, estimated vs actual time, health, scores, key maps |

The bundled skill `woof:woof` teaches the agent the habits (plan first, say the estimate's
basis, say physics and score status, end picture replies with the `MEDIA:` lines). A knowledge
pack built from WOOF's own docs backs `woof_docs`. Hex model runs are planning-only in this
version.

## Settings

`plugins.entries.woof.settings` in `config.yaml` (or the Desktop settings form):

| key | default | meaning |
|---|---|---|
| `executor` | `local` | `local`, `ssh` (a Linux GPU box) or `wsl` (WSL2 on Windows) |
| `ssh_host` | | `user@host` for `ssh` |
| `wsl_distro` | | distribution for `wsl` |
| `woof_bin` | | the `woof` command on the WOOF host; empty searches `~/woof-env/bin` then `PATH` |
| `runs_dir` | `~/woof-runs` | the run store on the WOOF host |
| `images_to_model` | `on` | `off` for models that cannot read images |
| `lock_file` | | optional shared GPU lock file (one owner line while a run uses the card) |
| `viewer_url` | | optional 3D viewer base URL |
| `geog_root` | | an existing WPS_GEOG folder on the WOOF host |
| `vram_reserve_gib` | | GiB of the card to keep free for other work; empty measures local model servers (llama.cpp, Ollama, vLLM, SGLang and similar) on the card and sizes runs for what they leave |

```yaml
plugins:
  enabled: [woof]
  entries:
    woof:
      settings:
        executor: local
        runs_dir: ~/woof-runs
tools:
  tool_search:
    enabled: "off"     # recommended with local models: offer the twelve woof_* tools directly
```

With Hermes' default tool search, plugin tools sit behind `tool_search` and the model has to
look them up first; small local models do better with them offered directly.

Push messages into gateway chats (Telegram, Discord and so on) stay off unless you set
`plugins.entries.woof.allow_gateway_injection: true`.

### Scoring needs observed weather

A forecast is scored once its valid times have been observed and archived (radar within
minutes, surface stations within about 20 minutes, Stage-IV precipitation about 90 minutes
after each hour). `woof_score` on a fresh forecast of the coming hours queues the score and says
when it will start; runs from an earlier cycle score at once.

## Models

The plugin works with any model Hermes can reach. A model that reads images can describe the
maps; for one that cannot, set `images_to_model: off`. [docs/LOCAL-MODEL.md](docs/LOCAL-MODEL.md)
covers serving the open model it was tested with, Qwen3.8-27B, on your own GPU, including the
tool-call parser settings that matter; the `serve/` folder has the scripts.

## How it works

- WOOF is driven only as a subprocess through `woof run-plan` (JSON plan in, JSON events out)
  and its JSON catalog commands. Hermes and WOOF keep separate Python environments.
- A small standard-library runner on the WOOF host keeps a queue per GPU, runs one forecast at a
  time, and records everything on disk, so restarting Hermes loses nothing.
- A watcher turns WOOF's events into chat messages, once each.

## Development

```bash
python -m pytest tests                         # unit tests; set WOOF_BIN to also run against a real WOOF
python scripts/lint_text.py                    # shipped-text rules
python scripts/sync_knowledge.py --woof-src <tree-or-tar> --commit <sha> --woof-bin <woof>   # rebuild the pack
python scripts/sync_knowledge.py ... --check   # drift test
```

The tests and the text rules run on every push (`.github/workflows/tests.yml`).

License: Apache-2.0.
