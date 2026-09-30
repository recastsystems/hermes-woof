# Outputs and pictures

Every picture comes from WOOF's own renderer and carries "source: WOOF" on the
map. Pictures are drawn while the forecast runs, frame by frame.

## Where things are (on the WOOF host)

```
<runs_dir>/runs/<run_id>/
  plan.json, plan-summary.json, record.json   what was asked, priced and done
  woof-out/                                   WOOF's run folder: events.jsonl, wrfout files, png/
  media/                                      every picture, named woof_<product>_<domain>_<valid>_f<NNN>.png
  model-view/                                 896 x 672 copies the model looks at
  gallery.html                                one card per product with a frame slider
  score/                                      observation scores, when scored
```

`woof_show` returns pictures for the model to look at and `MEDIA:` lines for
the user. In Hermes Desktop they show as media cards, in the terminal as file
links, and on chat platforms as attachments. Copy the `MEDIA:` lines into the
reply unchanged.

## Choosing what to show

- `products`: names or sets (`headline`, `severe`, `surface`, `upper`).
- `valid_time`: `latest` (default), `all`, `f002` (forecast hour 2) or an ISO time.
- `domain`: `d01` or a nest such as `d02`.
- `section`: "lat,lon,lat,lon" draws a vertical cross-section (cloud water and
  vertical motion) along that line.

## Product sets

<!-- generated:products -->
- **headline**: `composite_reflectivity`, `2m_temperature`, `10m_wind_speed_and_direction`, `total_qpf`
- **severe**: `composite_reflectivity`, `composite_reflectivity_uh`, `uh_2to5km`, `mlcape`, `mucape`, `sbcape`, `mlcin`, `srh_0_1km`, `srh_0_3km`, `bulk_shear_0_6km`, `stp_fixed`
- **surface**: `2m_temperature`, `2m_dewpoint`, `2m_relative_humidity`, `10m_wind_speed_and_direction`, `mslp_10m_winds`, `total_qpf`, `low_cloud_cover`
- **upper**: `250mb_height_winds`, `500mb_height_winds`, `700mb_temperature_height_winds`, `850mb_temperature_height_winds`, `precipitable_water`

All allowed products: `composite_reflectivity`, `composite_reflectivity_uh`, `1km_reflectivity`, `2m_temperature`, `2m_dewpoint`, `2m_relative_humidity`, `10m_wind_speed_and_direction`, `mslp_10m_winds`, `total_qpf`, `low_cloud_cover`, `precipitable_water`, `mlcape`, `mucape`, `sbcape`, `mlcin`, `srh_0_1km`, `srh_0_3km`, `bulk_shear_0_6km`, `stp_fixed`, `500mb_height_winds`, `850mb_temperature_height_winds`, `250mb_height_winds`, `2m_temperature_10m_winds`, `2m_dewpoint_10m_winds`, `uh_2to5km`, `700mb_temperature_height_winds`
<!-- /generated -->

## Reading the maps

- Composite reflectivity: radar-like echo in dBZ; 40+ dBZ is heavy rain or
  hail cores, 50+ strong storms.
- 2 m temperature and 10 m wind: fronts, outflow boundaries and sea breezes
  show as temperature gradients and wind shifts.
- Total precipitation: accumulation since the start.
- Updraft helicity and the instability and shear set flag rotating storms.

Difference maps (`woof_compare`) use a zero-centred colour bar: A minus B.
