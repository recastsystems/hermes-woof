# Scoring runs against observations

`woof_score` runs WOOF's battery scorer on a finished run, in the background,
and posts a message when it is done. It fetches exactly what the run needs from
keyless public archives, for the run's own box and hours:

- **MRMS** composite reflectivity (CONUS radar mosaic), always: it carries the
  primary score, the fractions skill score (FSS) at 30 dBZ with a 4-cell
  neighbourhood, plus other thresholds and neighbourhoods.
- **Stage-IV** precipitation (1 h, and 6 h for runs of 6 h or longer): FSS by
  threshold.
- **ASOS** surface stations in the box: 2 m temperature, dewpoint and 10 m wind,
  reported as the median station RMSE. Stations are admitted only on land,
  inside the interior and where model terrain is within 100 m of the station.

## Reading a score

- FSS runs from 0 to 1; above 0.5 + f/2 (f the observed event fraction) is
  "useful" at that scale. Compare arms under the same registration, not across
  different setups.
- Lower RMSE is better; units are those of the variable (K, m/s).
- The first hour is spin-up: runs under 6 h score hours 1 to H; longer runs
  score hours 2 to 18.

## Status

Every score carries the scorer's registration status. A one-off score is
`proposed-unratified`: the scoring rules are the proposed defaults. Report the
numbers and the status together, never the numbers alone.

MRMS and Stage-IV cover the lower 48 only. Outside it, only ASOS-style station
scores (where networks exist) are meaningful.
