# How the woof tools drive WOOF

WOOF has one door written for programs: `woof run-plan`. A JSON plan goes in; a
JSON event stream comes out. The plugin never scrapes human output.

## The plan

`woof_plan` writes a `gpuwm.run-plan.v1` document whose `config.intent` is the
user's request in WOOF's own terms. Intent keys map one to one onto the
`woof domain` wizard, which fits the grid to the card:

| intent key | meaning |
|---|---|
| `point` | centre "lat,lon" (or `polygon`, a GeoJSON polygon, with `buffer_km`) |
| `extent_km` | box size across; without it the plugin uses about 100 grid columns |
| `root_dx_km` | outer grid spacing, 0.05 to 200 km |
| `chain` | nest ratios, for example `[3]` for 2.25 km with a 750 m nest |
| `ladder` | preset nest ladders: `12`, `12-3`, `12-3-1`, `12-3-1-0.5`, `auto` |
| `nz` | vertical levels (default 49) |
| `hours` | forecast length |
| `source`, `cycle` | start data and cycle (`latest` or `YYYY-MM-DDTHH`) |
| `forecast_start_hour` | start from a later lead of the source cycle |
| `member` | ensemble member for `gefs` or `aigefs` |
| `physics_profile` / `physics_choices` | a shipped suite, or a family-to-scheme mix |

There is no `nx`/`ny` to set: the grid size is fitted to the card's memory and
reported back. Anything WOOF chooses on its own is listed in its answer.

## What woof_plan returns

- `domains`: each grid's `nx x ny x nz`, spacing and time step.
- `fit`: `fits`, `peak_gib` of the binding phase against `budget_gib`.
- `download_gib`, `disk_gib` (download, preparation, history, checkpoints, pictures).
- `frames`: history frames that will be written.
- `wall`: `total_s`, its `parts` (fetch, prepare, compile, forecast, render),
  `basis` and `confidence`. Always say it is an estimate.
- `physics`: the suite and its status.
- `refused`: WOOF's own sentence when the plan cannot run (exit 2), with the
  memory numbers for a memory refusal. Nothing starts on a refused plan.
- `toml`: the full experiment file WOOF generated (show it when asked).

The plan pins the start-data cycle it was priced on, so a launch an hour later
runs the same data, and two plans made back to back can be differenced.

## Launch and progress

`woof_launch` queues the plan on the WOOF host; one run per GPU at a time, the
rest wait in order. WOOF streams events: `plan_accepted`, `stage_started` and
`stage_finished` for fetch, prepare, initialize, forecast and finalize,
`model_progress` (model time, speed as a multiple of real time),
`output_committed` per history frame, `first_products_ready` (pictures while
the forecast runs), then `completed` or `failed` (with `error_class`,
`message` and `remedy`). The plugin turns the important ones into chat
messages; `woof_status` reads the rest.

## Routes

- `prepared`: GFS, HRRR and every packaged mapped source (HRRR pressure levels,
  ICON-EU, RAP, RRFS, GEM, AIFS, AIGFS, ECMWF open data and more).
- `experiment`: ERA5 (keyless public store by default).

The plugin picks the route from WOOF's own source registry.
