# Without an NVIDIA GPU

WOOF forecasts need an NVIDIA GPU on Linux. AMD, Apple and integrated GPUs
count as none. On such a machine `woof_doctor` reports `mode: plan_only` and
says why.

## What still works

- `woof_plan` with a named card: `card: "24gb"`, `"16gb"`, `"RTX 4090"`. The
  answer is WOOF's own sizing, labelled for hardware not present. Without a
  card the plan is sized for 24 GB and says so.
- `woof_catalog`, `woof_docs`: everything.
- `woof_campaign action=plan`: price a whole study before renting anything.
- `woof_show` and `woof_compare` on output made elsewhere.

## Ways to run

1. **A Linux NVIDIA box over SSH**: set the plugin's `executor` to `ssh` and
   `ssh_host` to `user@host`. Every tool then drives WOOF on that box; pictures
   are copied back for the chat.
2. **Windows with an NVIDIA GPU**: set `executor` to `wsl` and use WSL2 Ubuntu
   with the host NVIDIA driver (CUDA works inside WSL2). This route has no
   recorded WOOF run yet; treat it as untested.
3. **Prepare here, run there**: `woof prep` builds a portable prepared bundle
   on the CPU that a GPU machine can run.
4. **Rented GPUs**: any Linux box with a recent NVIDIA card and driver (CUDA 12
   R525+ or CUDA 13 R580+). A 24 GB card runs everything the presets offer
   except the 3 km CONUS preset, which wants 24 GB free or more.

## On a Mac

Run Hermes on the Mac, set `executor: ssh`, and point it at a GPU box. The
model can plan, price and explain locally, and launch remotely.
