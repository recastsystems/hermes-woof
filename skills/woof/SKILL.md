---
name: woof
description: Plan, launch, watch, compare and score WOOF weather simulations on the user's own NVIDIA GPU, and design multi-run research campaigns. Use for any request to run, size, price, show, compare or verify a weather simulation or forecast with WOOF.
version: 0.1.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [weather, simulation, gpu, science, research, forecasting]
    category: science
    requires_toolsets: [woof]
    requires_tools: [woof_doctor, woof_plan, woof_launch, woof_show]
---

# WOOF weather simulations

WOOF is an open-source GPU weather model: a WRF-faithful regional core (3 km
storm-scale runs in minutes on one card), a preview global model and a preview
hex (variable-resolution) model. It pulls its own start data (HRRR, GFS, ERA5,
ECMWF open data, ICON and more), prepares it, forecasts on the GPU and draws
maps with its own renderer while the forecast runs. These tools drive it.

## When to Use

- The user wants a forecast or simulation somewhere ("run 3 km over Norman").
- The user asks what their machine can run, how long a run takes, or whether it fits.
- The user wants to compare physics, resolution, start data, dates or ensemble members.
- The user wants maps from a run, a difference map, or scores against observations.
- The user has a research question that needs several runs (a campaign).
- The user has no NVIDIA GPU and wants to know what they can still do.

## Prerequisites

- `woof_doctor` answers whether WOOF is installed and whether runs can launch here.
- Runs need Linux x86_64 and an NVIDIA GPU (CUDA 12 or 13 driver). Without one the
  tools work in planning mode (see Pitfalls).
- If WOOF is missing, the fix is `hermes woof install` in a terminal (it installs
  WOOF 1.0.x from PyPI, `recast-woof>=1.0.0,<1.1`).

## How to Run

The habit that matters most: **plan, say the plan, then launch.**

1. `woof_doctor` once per conversation: card, free memory, WOOF version, mode.
2. `woof_plan` with a place (`intent.point` as "lat,lon") and settings, or a
   `preset`, a historical `case_id`, or a research `recipe_id`.
3. Tell the user, from the tool's numbers: the grid, whether it fits (peak vs
   budget GiB), download and disk size, and the estimated wall time **with its
   basis** (it is an estimate; say so). Name the physics suite and its status.
4. `woof_launch` with the `plan_id`. It returns at once. Do not poll: a message
   starting `[woof]` arrives when the first pictures are ready and when the run
   ends.
5. On `first_products_ready` or `completed`: call `woof_show`, look at the maps,
   describe what they show in plain words, and end your reply with the `MEDIA:`
   lines exactly as the tool returned them so the user sees the pictures.
6. On completion: `woof_report` for timings and health, `woof_score` if the user
   wants verification.

## Quick Reference

| The user says | Call |
|---|---|
| "what can I run here?", "is WOOF ready?" | `woof_doctor` |
| "run / forecast / simulate X" | `woof_plan` then (after stating the plan) `woof_launch` |
| "how long would ... take?", "will it fit on 16 GB?" | `woof_plan` (with `card` for another card) |
| "what data / physics / products / cases / recipes are there?" | `woof_catalog` with the right `kind` |
| "does Thompson with MYNN work?" | `woof_catalog kind=physics check={...}` |
| "how does X work in WOOF?", option meanings, limits | `woof_docs` |
| "how is it going?" | `woof_status` (only when asked; updates arrive by themselves) |
| "show me reflectivity", "show hour 2" | `woof_show` (`products`, `valid_time`) |
| "compare A and B", "what did the physics change?" | `woof_compare` |
| "how good was it?", "score against radar/stations" | `woof_score`, then `woof_report` |
| "plan a study / sweep / ensemble" | `woof_campaign action=plan`, then `action=start` |
| "stop it" | ask to confirm, then `woof_stop confirm=true` |

Presets (`woof_catalog kind=presets`): `quick-3km` (needs a point; the demo
default: 300 km box, HRRR, 3 h), `southern-plains-12km`, `conus-3km`,
`sf-bay-750m`, `la-basin-750m`, `nyc-500m`, `toronto-500m`.

Product sets for `products`: `headline` (composite reflectivity, 2 m
temperature, 10 m wind, total precipitation), `severe`, `surface`, `upper`.
Single names come from `woof_catalog kind=products`; nothing else reaches the
renderer. Pick products that show the question: fog, marine layer and low
cloud are `surface` (it holds `low_cloud_cover`, `2m_relative_humidity` and
`2m_dewpoint`); storms are `severe`; the default `headline` set has no cloud
field. In a campaign, put `products` in `spec.base` and the same names in
`spec.compare_products`.

## Procedure

**One run.** Doctor, plan, state the plan, launch, wait for the `[woof]`
message, show, describe, offer next steps (compare, score, a longer or finer run).

**Comparing two setups** (for example Thompson vs Morrison microphysics):
plan the second run from the first plan's settings with one change, and pass
`intent.cycle` = the first plan's `start.cycle`, so the grid and start data
match (the second run then reuses the first run's download). A microphysics
change is `intent.physics_choices: {"microphysics": "morrison-mp10"}`. Launch
it; when both are done, `woof_compare` gives difference maps (A minus B) when
grids match, and side-by-side sheets otherwise. Its result carries
`difference_by_hour` (how far apart the runs are at every shared hour) and
says the hour they differ most; `valid_time=peak` draws that hour directly.
Things that come and go (morning fog, a passing storm) are often gone by the
last hour, so describe the peak hour, not the last one. A campaign draws its
comparisons at the peak hour by itself.

The `woof` skill is part of the plugin and is read-only; do not try to patch it.

**Campaigns** (`woof_campaign`): pick the axis that answers the question.
Read `references/campaigns.md` before planning one.

| Question | axis | values |
|---|---|---|
| Which physics works best here? | `physics` | suite ids, or mixes like `{"microphysics":"morrison-mp10"}` |
| Does finer resolution help? | `resolution` | `root_dx_km` numbers or ladders (`12-3`, `12-3-1`) |
| Which start data is best? | `source` | `hrrr`, `gfs`, `ecmwf-open-data`, `aifs`, ... |
| How uncertain is it? | `members` | member numbers with `source: gefs` or `aigefs` |
| Does it hold across days? | `dates` | cycles `YYYY-MM-DDTHH` |
| How far ahead is it useful? | `lead` | forecast start hours |
| Historical events | `cases` | case ids from `woof_catalog kind=cases` |

`action=plan` prices every arm and totals wall time (arms run one after another
on one card), disk and download against the user's budget. Say the total and
whether it fits the budget before `action=start`. A message arrives as each
arm finishes and when the campaign is done.

**Research.** For a science question, look for a recipe first
(`woof_catalog kind=recipes query=...`): each carries a research question,
method, diagnostics and limitations. Historical events live in
`kind=cases` (300 cases). Read `references/research.md`.

## Pitfalls

- Never promise a wall time without the estimate's basis. WOOF itself prints no
  wall time; the plugin's estimate comes from measured throughput and improves
  after the machine's own runs.
- A refusal from `woof_plan` is a valid answer, not an error. Explain it in the
  user's words and offer the nearest plan that fits (smaller `extent_km`,
  coarser `root_dx_km`, fewer `hours`, or a bigger card).
- Say the physics suite's status when you pick one: `wrf-matched-run` and
  `supported` are verified; `implemented-unverified` and `expert-only` run, and
  their receipts say so.
- Scores carry the scorer's status verbatim. A one-off score is
  `proposed-unratified`: report the numbers and the status together.
- Always end a pictures reply with the `MEDIA:` lines from the tool result.
- Only allowed product names work; the tool returns the nearest allowed names
  when one is wrong.
- The hex model is planning-only in this version (no ready start path from GFS).
  The global model can be planned and run (24 h at T255 in about 7 minutes on a
  24 GB card).
- **No NVIDIA GPU** (Mac, AMD, integrated, or none): `woof_doctor` reports
  `mode: plan_only`. Plans still work for a card the user names (`card: "24gb"`
  or `"RTX 4090"`) and are labelled for hardware not present. To run, the user
  can point the plugin at a Linux NVIDIA box (`executor: ssh`), rent one, or
  prepare a portable bundle with `woof prep`. Read `references/without-a-gpu.md`.
- `woof_stop` needs the user's go-ahead and `confirm: true`.

## Verification

- A launched run shows `completed` in `woof_status`, with pictures in
  `woof_show` and a `gallery.html` page (frame slider per product).
- `woof_report` compares the estimate with the actual time and lists health
  results; large misses are recorded so the next estimate on this machine is
  better.
- Scores: `woof_score` then `woof_report` (ASOS surface RMSE, MRMS reflectivity
  fractions skill score, Stage-IV precipitation skill), each with its status.

References (read with `skill_view` when the task needs them):
`references/run-plan.md`, `references/campaigns.md`, `references/physics.md`,
`references/sources.md`, `references/cards-and-time.md`,
`references/outputs.md`, `references/verification.md`,
`references/research.md`, `references/without-a-gpu.md`,
`references/troubleshooting.md`.
