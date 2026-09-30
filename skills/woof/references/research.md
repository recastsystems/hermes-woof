# Going further: research with WOOF

WOOF ships two libraries for research that the tools expose.

## Research recipes (114)

`woof_catalog kind=recipes query=...` searches 12 families: everyday forecast;
supercells and severe storms; hurricanes and tropical weather; a scenario and
hurricane lab; snow, ice and blizzards; heavy rain and flooding; fire weather;
wind and damaging gusts; heat, cold and frost; clouds, fog and aviation
weather; mountains and local terrain; coasts and sea breezes. Each recipe
carries:

- a research question and method;
- the geometry it needs (spacing, nests, hours, minimum span);
- diagnostics to look at and what to compare against;
- limitations and citations.

Plan one with `woof_plan recipe_id=... intent={point, source, cycle}`; WOOF
builds the study workspace and sizes it to the card.

## Historical cases (300)

`woof_catalog kind=cases query="El Reno"` (120 tornado, 60 convective wind,
70 tropical cyclone, 50 synoptic). `woof_catalog kind=cases id=...` previews
one with its usable start-data options. Plan with `woof_plan case_id=...`
(`case_tier` lower, recommended or upper).

## A research workflow

1. Turn the user's interest into a question that a run can answer ("does
   finer resolution change where the sea-breeze front starts storms?").
2. Find a recipe or case that matches; read its method and limitations.
3. Plan a campaign: one axis, three to eight arms, a budget.
4. Run it, look at the pictures as they arrive, compare arms.
5. Score against observations when the period is in the past.
6. Summarise: what changed, how confident, what the limits are, what to try next.

Be plain about limits: a single case is an anecdote; several dates or members
make a pattern; scores are under a proposed rule.

## Ensembles

Source members (`gefs`, `aigefs` with `member`) run as separate arms; the
campaign then draws mean, spread, probability and paintball products. WOOF also
has an experimental perturbation ensemble tool; offer it only when the user asks
for perturbations, and pass on its printed limits.
