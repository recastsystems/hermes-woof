# Running WOOF for Hermes on a local model

The WOOF plugin works with any model Hermes can reach. This page covers the open model it was
tested with, Qwen3.8-27B, served on your own NVIDIA GPU. No account or API key is involved.

## The model

- **Qwen3.8-27B** (`Qwen/Qwen3.8-27B`, Apache-2.0). A 27B dense model that sees images, calls
  tools natively and keeps long context fast.
- GGUF files for llama.cpp: `unsloth/Qwen3.8-27B-GGUF`. Official FP8: `Qwen/Qwen3.8-27B-FP8`.
  NVFP4 builds: `unsloth/Qwen3.8-27B-NVFP4`, `Inferact/Qwen3.8-27B-NVFP4`.

## Pick a stack

| Your hardware | Stack | What to run |
|---|---|---|
| One 32 GB card (RTX 5090) | llama.cpp | `UD-Q5_K_XL` (19.4 GiB) with the vision projector, 131,072 context, MTP drafting |
| One 24 GB card (RTX 4090, 3090) | llama.cpp | `UD-Q4_K_XL` with 131,072 context if the model has the card to itself; `UD-Q4_K_S` with 65,536 context if WOOF runs on the same card |
| 48 GB or more, or several GPUs | vLLM | official FP8 or BF16 with tensor parallel |
| One 32 GB card with vLLM | vLLM | NVFP4 only, with `--enforce-eager --max-model-len 131072` |
| Cards with FP8 support | SGLang | official FP8 |

## Tool-call parsers: get this right or nothing runs

Qwen3.8 writes tool calls in an XML form. A server must be told to read that form, or the tool
calls arrive as plain text and the agent never runs a tool.

- **llama.cpp:** `--jinja --reasoning-format deepseek`
- **vLLM:** `--enable-auto-tool-choice --tool-call-parser qwen3_xml` (or `qwen3_coder`) and
  `--reasoning-parser qwen3`
- **SGLang:** `--tool-call-parser qwen3_coder --reasoning-parser qwen3`

The `hermes` and `qwen` parsers, which some guides list for Qwen models, expect an older JSON
form and return this model's tool calls as plain text. Do not use them with Qwen3.8.

## llama.cpp: nested tool arguments need the tool shim

llama.cpp turns each tool's JSON schema into a grammar. Inside a nested object (for example the
`intent` of `woof_plan`) that grammar only allows the listed keys in their listed order. The model
often writes `name` first, which is listed last, and is then blocked from writing any other key:
the rest of the plan ends up inside the `name` string, or the text repeats until the token limit
and the server answers HTTP 500 ("Failed to parse tool call arguments as JSON").

Measured on llama.cpp b11246 with Qwen3.8-27B `UD-Q5_K_XL`, 16 attempts at "Plan a 3 hour run near
Norman, Oklahoma" through the full tool loop:

| Tool schemas | Loop completed | Plan carried `hours` and `point` | HTTP 500 |
|---|---|---|---|
| As written (nested objects) | 13 of 16 | 7 of 16 | 2 |
| Nested objects loosened | 16 of 16 | 16 of 16 | 0 |

`serve/tool_shim.py` sits in front of `llama-server` and rewrites every nested object in the
request's tools into a plain object whose description lists the keys, types and allowed values.
The model still sees every key; only the fixed order goes away. `serve/start-server.sh` runs the
shim by default (`TOOL_SHIM=1`). vLLM and SGLang read tool calls after generation by default
(no grammar on the arguments); they were not measured for this.

## Start and stop (llama.cpp on Linux)

The `serve/` folder has the scripts:

```bash
serve/fetch-model.sh demo              # llama.cpp release + Q5_K_XL + vision projector, checksums verified
LLAMA_HOST=127.0.0.1,<lan-address> serve/start-server.sh demo
serve/check_server.py --base-url http://<gpu-host>:8080/v1 --image some-woof-map.png --expect reflectivity
serve/stop-server.sh
```

Profiles: `demo` (one stream, 131,072 context, vision, MTP), `shared24` (Q4_K_S, 65,536 context,
text only, leaves room for a WOOF run on a 24 GB card), `shared24v` (`shared24`
plus vision; the projector runs on the CPU so the card keeps the same room for WOOF, `MMPROJ_GPU=1`
puts it on the card), `card24` (Q4_K_XL, 131,072 context, MTP). If your machine shares its GPU through a
lock file, set `GPU_LOCK_FILE`; the start script then refuses to start while another line is in it
and removes only its own line on stop.

The server command behind the `demo` profile:

```bash
llama-server -m Qwen3.8-27B-UD-Q5_K_XL.gguf --mmproj mmproj-BF16.gguf \
  --host 127.0.0.1 --port 8081 -ngl 999 -fa on -c 131072 -np 1 \
  --cache-type-k q8_0 --cache-type-v q8_0 --spec-type draft-mtp --spec-draft-n-max 3 \
  --jinja --reasoning-format deepseek --chat-template-kwargs '{"reasoning_effort":"medium"}' \
  --temp 1.0 --top-k 20 --alias qwen3.8-27b --metrics
```

## What to expect (measured, llama.cpp b11246)

| Profile, card | VRAM | Decode, 2K context | Decode, 60K context | Prefill |
|---|---|---|---|---|
| `demo`, RTX 5090 | 26.5 GiB peak | 81 to 83 tok/s | 67 tok/s | 2,400 to 2,700 tok/s |
| `shared24`, measured on an RTX 5090 | 16.7 GiB | 81 tok/s | 65 tok/s (55K) | 2,900 to 3,600 tok/s |

MTP drafting adds about a quarter to single-stream decode (81 vs 65 tok/s) and is on in `demo`.
A 70,000-token prompt returns in about 30 s on the `demo` profile. A Hermes turn that plans a run
(hardware check, catalog lookups, plan, answer) takes 15 to 30 s; a campaign plan with a dozen
tool calls took about two minutes.

## Hermes

- Hermes refuses a model whose context window is under 64,000 tokens. With llama.cpp, `-np`
  splits the window between slots: `-c 131072 -np 4` gives 32,768 per slot, which Hermes refuses
  for chat. Use `-np 1` for Hermes.
- Running the model and WOOF on the same card: `shared24` or `shared24v` leaves about 7 GiB of a
  24 GB card, enough for a quick 3 km WOOF run (about 3.8 GiB peak). On a 32 GB card the `demo`
  profile leaves about 5 GiB; use `shared24` there too if WOOF runs on the same card.
- The plugin sees a model server on the card (llama.cpp, Ollama, vLLM, SGLang and similar, from
  `nvidia-smi`) and sizes every plan for the memory it leaves, and says so in the plan. When the
  model runs somewhere `nvidia-smi` cannot see it, set the plugin's `vram_reserve_gib`.

`~/.hermes/config.yaml`:

```yaml
model:
  provider: custom
  base_url: http://<gpu-host>:8080/v1
  default: qwen3.8-27b
  context_length: 131072
  api_key: ""
  supports_vision: true     # the server loads the vision projector; leave out for text-only profiles
plugins:
  enabled: [woof]
  entries:
    woof:
      settings:
        executor: local
        images_to_model: "on"     # "off" when the server runs without the vision projector
tools:
  tool_search:
    enabled: "off"                # offer the twelve woof tools directly
```

For `shared24` and `shared24v` set `context_length: 65536`.

Hermes passes images only to a model it knows can see them. A local endpoint is not in its model
catalog, so without `supports_vision: true` every WOOF picture reaches the model as text and the
model cannot describe the maps (`woof_doctor` warns about this). For the text-only profiles
(`shared24`, `card24`) leave the key out and set the plugin's `images_to_model` to
`off`.

Offer the model only the tools it needs: `hermes chat -t woof,skills,clarify` starts a session
with the twelve WOOF tools, the skill reader and clarifying questions, about 16 tools instead of
nearly 40. The system prompt shrinks by about 8,000 tokens, which matters in a 65,536-token window.
