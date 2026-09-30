# Troubleshooting

| Symptom | What it means | What to do |
|---|---|---|
| `error.kind: not_installed` | WOOF is not on the WOOF host | `hermes woof install` in a terminal (WOOF 1.0.x from PyPI) |
| `woof_doctor` says the WOOF version is outside what the plugin is built for | the plugin is checked against WOOF 1.0.x | `hermes woof install` puts a matching WOOF in place |
| `error.kind: no_gpu` | no NVIDIA GPU where WOOF runs | see `without-a-gpu.md`; plan with `card` |
| `woof_plan` returns `refused` with memory numbers | the grid does not fit the card | smaller `extent_km`, coarser `root_dx_km`, fewer nests, or a bigger card |
| refused: source window | the point is outside a regional source (HRRR is lower 48 only) | use `gfs` or another global source |
| refused: cycle not available | that cycle is not published or not archived | `cycle: latest`, or check with `woof_catalog kind=sources source=... cycle=...` |
| refused: physics | the suite does not run with that source or spacing | `woof_catalog kind=profiles source=...` for what runs |
| warning: Thompson tables not staged | coefficient tables missing | `woof fetch-tables` (315 MiB) |
| run `failed` at fetch | the archive did not answer | retry later; `woof_status` shows WOOF's remedy |
| run `failed` at prepare: geography | no WPS_GEOG geography | `woof fetch-geog --datasets wrf`, or set `geog_root` |
| run waits (`waiting_for_gpu`) | another job holds the card's shared lock | it starts on its own when the card frees |
| run is slow | first run compiles kernels (about 50 s); big grids take longer | the estimate says what to expect; `woof warm-kernels` precompiles |
| no pictures yet | frames are drawn as they are written | the `first_products_ready` message will arrive |

## Reporting a problem

`woof report RUN_DIR --dry-run` lists what a diagnostic bundle would hold, then
`woof report RUN_DIR --output diag.zip` writes it. Nothing is uploaded.

## Checking the install

`woof_doctor` (fast, no GPU work) and `woof_doctor deep=true` (WOOF's full
self-check; it compiles a kernel on the GPU, so it is refused while a run is
active).
