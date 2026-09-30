# Changelog

## 0.1.0 (2026-09-30)

First release.

- Twelve tools: `woof_doctor`, `woof_catalog`, `woof_docs`, `woof_plan`, `woof_launch`,
  `woof_status`, `woof_stop`, `woof_show`, `woof_compare`, `woof_campaign`, `woof_score`,
  `woof_report`.
- The skill `woof:woof` with ten references, and a knowledge pack built from WOOF 1.0's docs
  that backs `woof_docs`.
- `hermes woof install` installs WOOF 1.0.x from PyPI (`recast-woof>=1.0.0,<1.1`, the `all-cu13`
  extra by default, `--cuda 12` for `all-cu12`), fetches the Thompson tables and geography, and
  runs WOOF's doctor. `--wheel-dir` installs from a folder of release files. Also
  `hermes woof doctor`, `runs` and `lint`.
- WOOF runs on this machine, on a Linux GPU box over SSH, or in WSL2 (untested). A runner on the
  WOOF host keeps one queue per GPU, and run events arrive in the chat as messages.
- Plans are sized to the GPU memory a local model server on the same card leaves free.
- Campaigns sweep physics, resolution, start data, dates or ensemble members against a time
  budget. Their difference maps are drawn at the hour the runs differ most.
- Scores against MRMS radar, ASOS stations and Stage-IV precipitation, queued until the
  observations are archived.
- `serve/` and `docs/LOCAL-MODEL.md` cover serving Qwen3.8-27B with llama.cpp, including a tool
  shim for nested tool arguments.
- Checked with Hermes Agent v0.21.5 and WOOF 1.0.0.
