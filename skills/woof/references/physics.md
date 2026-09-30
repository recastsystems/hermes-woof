# Physics in WOOF

Seven families: microphysics, cumulus, boundary layer (pbl), surface layer,
land surface, radiation and turbulent mixing. A shipped **suite** fixes one
scheme per family; a **mix** (`physics_choices`) overrides some families on
top of the source's default suite.

- `woof_catalog kind=physics` lists families, schemes, suites and presets
  (`convection-3km`, `tropical-cyclone`, `winter-precipitation-type`,
  `fast-look`, `les`, `coastal-fog-stratus`).
- `woof_catalog kind=physics check={"microphysics":"thompson-mp8","pbl":"mynn"}`
  asks WOOF whether a mix runs and, if not, the nearest mixes that do.
- `woof_catalog kind=profiles source=hrrr` lists what each source can run and
  its default. HRRR at 3 km refuses cumulus suites (the grid resolves storms).

## Status words (say them when you pick a suite)

- `wrf-matched-run`, `supported`: verified against the reference model.
- `wrf-matched-run-candidate`: close to verified.
- `implemented-unverified`, `expert-only`, `experimental-runtime`: they run;
  their receipts say they are not verified.

## Shipped suites

<!-- generated:suites -->
| suite | status |
|---|---|
| `20crv3-wsm6-ysu-mm5-noah-kf-rte-rrtmgp-implemented-unverified-v1` | implemented-unverified |
| `kessler-mp1-ysu-mm5-noah-dudhia-v1` | implemented-unverified |
| `milbrandt2mom-mp9-ysu-mm5-noah-ntiedtke-rrtmg-legacy-v1` | implemented-unverified |
| `morrison-mp10-ysu-mm5-noah-kf-rte-rrtmgp-v1` | wrf-matched-run |
| `nssl2-mp18-ysu-mm5-noah-kf-rrtmg-legacy-validation-candidate-v1` | wrf-matched-run-candidate |
| `nssl2-mp18-ysu-mm5-noah-kf-rte-rrtmgp-validation-candidate-v1` | wrf-matched-run-candidate |
| `p3-mp50-ysu-mm5-noah-rrtmg-legacy-v1` | implemented-unverified |
| `thompson-aerosol-mp28-myj-eta-noah-rte-rrtmgp-v1` | implemented-unverified |
| `thompson-mp8-mynn-mynn-ruc-dudhia-implemented-unverified-v1` | implemented-unverified |
| `thompson-mp8-mynn-mynn-ruc-rte-rrtmgp-implemented-unverified-v1` | implemented-unverified |
| `thompson-mp8-shinhong-mm5-noah-rrtmg-legacy-v1` | wrf-matched-run-candidate |
| `thompson-mp8-ysu-mm5-noah-kf-rte-rrtmgp-v1` | wrf-matched-run |
| `thompson-mp8-ysu-mm5-noah-rrtmg-legacy-v1` | wrf-matched-run-candidate |
| `thompson-mp8-ysu-mm5-noah-rte-rrtmgp-v1` | implemented-unverified |
| `thompson-mp8-ysu-mm5-noah-validation-v1` | experimental-runtime |
| `wdm6-mp16-ysu-mm5-noah-grell-freitas-rte-rrtmgp-v1` | implemented-unverified |
| `wsm6-mynn-mynn-noah-no-radiation-implemented-unverified-v1` | implemented-unverified |
| `wsm6-mynn-mynn-noah-rte-rrtmgp-implemented-unverified-v1` | implemented-unverified |
| `wsm6-mynn-mynn-noahmp-no-radiation-expert-only-v1` | implemented-unverified |
| `wsm6-mynn-mynn-noahmp-rte-rrtmgp-expert-only-v1` | implemented-unverified |
| `wsm6-mynn-mynn-ruc-no-radiation-implemented-unverified-v1` | implemented-unverified |
| `wsm6-mynn-mynn-ruc-rte-rrtmgp-implemented-unverified-v1` | implemented-unverified |
| `wsm6-pbl-off-mm5-noah-constant-k-v1` | implemented-unverified |
| `wsm6-pbl-off-mm5-noah-smagorinsky-3d-v1` | implemented-unverified |
| `wsm6-pbl-off-mm5-noah-tke-1-5-order-v1` | implemented-unverified |
| `wsm6-sase-revised-mm5-noah-closure-supplied-v1` | implemented-unverified |
| `wsm6-ysu-mm5-noah-no-radiation-v1` | supported |
| `wsm6-ysu-mm5-noahmp-no-radiation-expert-only-v1` | implemented-unverified |
| `wsm6-ysu-mm5-ruc-no-radiation-implemented-unverified-v1` | implemented-unverified |
<!-- /generated -->

## Notes

- Suites with `no-radiation` or `validation` in the id turn longwave off and are
  refused over local night unless acknowledged; avoid them for real forecasts.
- Cumulus: below about 4 km most setups turn it off; naming a suite keeps its
  cumulus at any spacing.
- A common microphysics comparison at 3 km: `thompson-mp8-ysu-mm5-noah-rte-rrtmgp-v1`
  (the HRRR default) against a Morrison mix
  `{"microphysics":"morrison-mp10"}` on the same grid and data.
- The full city-style stack (Thompson, MYNN, RUC at 59 levels) costs about
  three times the default suite per cell-step; the estimate accounts for it.
