# Start data (sources)

WOOF fetches its own start data; none of the sources below needs a key except
ERA5 ensemble members through the Copernicus service.

<!-- generated:sources -->
| source | name | coverage | longest lead (h) |
|---|---|---|---|
| `hrrr` | HRRR (native hybrid levels) | regional window | 48 |
| `hrrr-prs` | HRRR (pressure levels) | regional window | 48 |
| `gem-gdps` | GEM GDPS (Canadian global) | global | 240 |
| `icon-global` | ICON global 13 km (DWD) | global | 180 |
| `icon-eu` | ICON-EU (DWD regional) | regional window | 120 |
| `icon-d2` | ICON-D2 2.2 km (DWD, Germany) | regional window | 48 |
| `gfs` | GFS (global, 0.25 degree) | global | 384 |
| `gdas` | GDAS analysis (0.25 degree) | global | 9 |
| `gefs` | GEFS (global ensemble member) | global | 384 |
| `aigfs` | AIGFS (NOAA AI global forecast) | global | 384 |
| `aigefs` | AIGEFS (NOAA AI global ensemble member) | global | 384 |
| `ecmwf-open-data` | ECMWF IFS (open data, 0.25 degree) | global | 360 |
| `aifs` | ECMWF AIFS single (open data) | global | 360 |
| `rap` | RAP (32 km North America) | regional window | 51 |
| `rrfs` | RRFS (operational 3 km CONUS) | regional window | 84 |
| `era5` | ERA5 reanalysis (ECMWF) | global |  |
| `era5-l137` | ERA5 reanalysis (native 137 model levels) | global |  |
| `20crv3` | 20CRv3 reanalysis (member, GRIB2) | global |  |
| `20crv3-cf` | 20CRv3 reanalysis (ensemble mean, NetCDF) | global |  |
<!-- /generated -->

- `cycle: latest` resolves to the newest complete cycle (GFS six-hourly, HRRR
  hourly) and the plan records the concrete cycle.
- HRRR covers the lower 48 (a regional window); the plugin picks `hrrr` for a
  point inside it and `gfs` elsewhere unless the user names a source.
- `woof_catalog kind=sources source=hrrr cycle=2024-05-21T00` checks whether a
  past cycle can be fetched.
- ERA5 reaches back to 1940 (reanalysis, published days late, no `latest`).
- Ensembles: `gefs`, `aigefs` with `member`.
- AI global models: `aifs`, `aigfs` as start data for WOOF's own physics.
- Any GRIB or NetCDF source can be mapped with `woof adapt` (advanced).
