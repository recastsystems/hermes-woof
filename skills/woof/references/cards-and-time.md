# Cards, memory and time

## Memory

WOOF sizes every plan against the card's memory before anything runs:
`fit.peak_gib` is the binding phase's peak, `fit.budget_gib` what the card
allows after a reserve (about 4 GiB, 6 GiB on 32 GB cards). A plan that does
not fit is refused with the numbers. A 16 GB card fits the whole
12-3-1-0.5 km four-domain ladder; a 3 km 300 km box needs under 4 GiB.
Out-of-core streaming (`tiles`) lets domains larger than the card run from host
memory.

## Wall time (always an estimate)

WOOF prints no wall time for an arbitrary setup. The plugin's estimate:

- forecast = cell-steps / measured throughput, plus a per-step floor for small
  grids;
- plus fetch (download bytes at the measured rate), preparation, a one-time
  kernel compile (about 50 s the first time a suite runs), initialisation and
  the last frame's pictures.

Measured throughput, default suite at 49 levels (cell-steps per second):
RTX 5090 about 83 million; RTX PRO 6000 about the same; RTX 4090 0.74 of the
5090; RTX PRO 4500 0.54; L40S 0.45. Other cards use the nearest measured card
by memory bandwidth, with low confidence.

After each finished run, the plugin records the actual times for that card;
later estimates use the last five runs ("this machine, N runs").

Measured end to end: a 3 km HRRR run, 100 x 80 columns, 3 h, on an RTX 4090
took 217 s the first time (download and kernel compile included) and about
62 s with both warm. A 24 h global run at T255 took 439 s.

## Sharing a card

One WOOF run per GPU at a time; the queue holds the rest. A local language
model on the same card needs its own memory: on a 24 GB card a 4-bit 27B model
leaves about 6 GB, enough for a 3 km 300 km box.
