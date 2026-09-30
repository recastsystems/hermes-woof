# Campaigns: several runs that answer one question

A campaign is a base plan (the same fields as `woof_plan`) swept along one
axis. `woof_campaign action=plan` prices every arm with WOOF's own sizing and
the plugin's time estimate, then totals them. Arms run one after another on
one card, so wall times add up; arms with the same start data share one
download.

## Axes

| axis | values | how arms differ | comparison |
|---|---|---|---|
| `physics` | suite ids or mixes (`{"microphysics":"morrison-mp10"}`) | physics only; same grid and data | difference maps (A minus B) |
| `resolution` | `root_dx_km` numbers or ladders | grid spacing | side-by-side sheets (grids differ) |
| `source` | source ids | start data | difference maps when grids match |
| `members` | member numbers (`source` `gefs` or `aigefs`) | ensemble member | ensemble products (mean, spread, probability, paintball) |
| `dates` | cycles | start time | scores per date |
| `lead` | forecast start hours | how far ahead | scores per lead |
| `cases` | case ids | historical event | scores per case |

The first arm's `latest` cycle is resolved once and pinned for every arm (except
on the `dates` axis), so arms compare like with like.

## Recipes by question type

- **"Which microphysics handles these storms best?"** `physics` over three to
  five suites at 3 km, 6 to 12 h, `score: true`. Report reflectivity FSS per
  arm with its status, and show the difference maps.
- **"Is 1 km worth it over 3 km?"** `resolution` over `[3, 1.5]` or ladders
  `12-3` and `12-3-1`; compare side by side; check wall time per arm first.
- **"How sure is this forecast?"** `members` over five to ten GEFS or AIGEFS
  members; the campaign draws ensemble products at the end.
- **"Does the setup work in general, not just today?"** `dates` over five to
  ten recent cycles, `score: true`.
- **Sea-breeze or fog studies**: pick a recipe from `woof_catalog kind=recipes`
  first; it names the method, diagnostics and limits; then sweep `dates` or
  `physics` around it. For "next week", plan now on the latest cycle to price
  it, and start each arm when its cycle exists.

## Budgets

Pass `budget: {max_wall_minutes, max_disk_gib}`. The plan says
`within_budget` and why not. To fit a budget: fewer arms, shorter `hours`,
smaller `extent_km`, coarser grid, or fewer products. Say the total before
starting; `action=start` needs the `campaign_id` from the plan.

## One grid for every arm

WOOF sizes each arm to the free memory on its own, so a costlier arm (MYNN
needs about twice the memory of YSU) can land on a smaller box. When the axis
is not `resolution`, the plan does this for you: it plans every arm again at
the smallest arm's box until all share one grid, and says so in
`grid_matched`. State the matched grid to the user.

A refusal that names a suite's fixed memory envelope (the part that does not
shrink with the grid) cannot be fixed by a smaller box or a coarser grid; the
only fixes are freeing card memory or a bigger card. Say that instead of
planning smaller boxes.

## While it runs

A message arrives as each arm finishes (`arm_done`) and when the campaign is
done (`campaign_done`, after comparisons are drawn at the hour the arms differ
most). `woof_campaign
action=status` shows progress; `woof_report campaign_id=...` gives the table.
