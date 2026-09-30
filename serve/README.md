# Serving a local model for WOOF

Scripts to run Qwen3.8-27B with llama.cpp on your own NVIDIA GPU for the WOOF plugin.
See `docs/LOCAL-MODEL.md` for the full guide, measured speeds and the Hermes config.

| File | What it does |
|---|---|
| `fetch-model.sh` | downloads the llama.cpp CUDA release and the GGUF files for a profile, checksums verified |
| `start-server.sh` | starts a profile (`demo`, `shared24`, `shared24v`, `card24`) detached and waits until it answers |
| `stop-server.sh` | stops what `start-server.sh` started and removes only its own GPU lock line |
| `check_server.py` | tool-call round trip, long-context, vision and speed checks against a running server |
| `tool_shim.py` | pass-through in front of `llama-server` that loosens nested tool schemas (on by default) |
| `loosen_schemas.py` | the schema rewrite the shim applies, usable on its own |
