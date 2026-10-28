# RAG with LunarShroud

llama.cpp has no "enable RAG" switch, and this launcher deliberately does not
invent one. What llama.cpp actually provides is a set of **server-side
building blocks**; a RAG pipeline is those blocks plus a client that does the
chunking, indexing and retrieval. This document says which blocks exist, how
the launcher exposes them, and where the boundary is.

Verified against llama.cpp `0.7.0-dev (build 11698)` on an 8 GB laptop GPU.

## The building blocks

| Block | Flag | Endpoint | Notes |
| --- | --- | --- | --- |
| Embeddings | `--embeddings`, `--pooling`, `--embd-normalize` | `/v1/embeddings` | Use a dedicated embedding model. Since v0.7.0 pooling skips the output layer, so embedding VRAM is roughly a tenth of what it was. |
| Reranking | `--rerank` | `/rerank` | Needs a reranker model; rescoring a retrieved candidate list is what makes small retrievals usable. |
| Prompt caching | `--cache-prompt`, `--cache-reuse` | — | RAG repeats the same system prompt and retrieved chunks every turn; caching is the cheap win. On by default upstream. |
| Agentic retrieval | `--tools`, `--agent`, `--tools-runtime` | `/tools` | Built-in tools: `read_file`, `file_glob_search`, `grep_search`, `get_info`, plus `exec_shell_command`, `write_file`, `edit_file`. |
| External tools | `--mcp-servers-config`, `--mcp-servers-json`, `--ui-mcp-proxy` | — | Cursor-compatible MCP server definitions. |
| Multi-model serving | `--models-dir`, `--models-max`, `--models-autoload` | — | v0.7.0 router mode; its Web UI also gains a model manager. |

All of them are ordinary panel parameters, so they follow the general rule: the
panel renders what **your** build declares (`llama-server --help` is the
authority, not a hardcoded list), flags your build lacks are dropped from the
command line and reported, never silently.

## Use-case recipes

The parameter editor has a **use case** row above the groups. A recipe sets
intent, not tuning:

- **Chat / completion** — embeddings, reranking and agent tools off.
- **Embeddings (RAG retrieval)** — `--embeddings` on.
- **Reranking (RAG rescoring)** — `--rerank` on.
- **Read-only tools** — `read_file`, `file_glob_search`, `grep_search`,
  `get_info`; excludes `exec_shell_command`, `write_file`, `edit_file`.
- **Full agent** — `--agent` (CORS proxy plus every built-in tool).

Recipes never set context size, layer counts, batch sizes or VRAM tiers, and
never a model path. That is a deliberate line: tuned numbers drift with every
llama.cpp release, which is also why this project has **no built-in preset
templates** (see the module docstring of `backend/core/preset_store.py`).
Recipes whose flags your build does not have are disabled in the UI, with the
reason in the tooltip — the same capability judgement that governs the command
line.

## A working two-instance layout on 8 GB

Retrieval and generation have different shapes, so run them as two instances;
the launcher assigns ports automatically and keeps both under one monitor.

1. Instance A — embedding model, use-case recipe **Embeddings**, small context
   (2048 is plenty for chunk embedding).
2. Instance B — chat model, use-case recipe **Chat** (or **Read-only tools** if
   you want the model to cite local files), context sized for
   prompt + retrieved chunks + answer.
3. Your client embeds the query against A, retrieves and optionally rescores,
   then sends prompt + chunks to B's `/v1/chat/completions`.

Measured on this layout: a 86 MB Q4_K_M embedding model serving 768-dimensional
vectors from `/v1/embeddings` while a 4 B Q4_K_M chat model was loaded at
`-c 8192` with read-only tools on the second instance, both healthy at the same
time on an 8 GB card.

## The v0.7.0 single-slot embedding crash

Loading an embedding model with one slot and **without** `--embeddings` aborts
inside llama.cpp:

```text
init: embeddings required but some input tokens were not marked as outputs -> overriding
llama-context.cpp:2384: GGML_ASSERT(n_outputs_max <= cparams.n_outputs_max) failed
```

Reproduced with `-np 1` alone; `-np 1 --embeddings` and the default slot count
both start normally, and `--kv-unified`, `--flash-attn` and `--models-dir` make
no difference. This is upstream behaviour, not something the launcher
introduces — but the launcher's default slot count is 1, so it is easy to hit.

Both workarounds are verified end to end through the launcher: tick **Enable
embeddings endpoint**, or set **Parallel slots** to 2 or more. When it does
happen, the failure message quotes the assertion and names both workarounds
instead of leaving a bare exit code.

## Security notes

- `--agent` and `--tools` pin `--cors-origins` to localhost upstream, and they
  can execute shell commands and write files. The read-only recipe exists
  because most retrieval use cases do not need `exec_shell_command`.
- `--tools-runtime docker:<image>` / `ssh:<target>` runs the tools outside the
  host; prefer it when you want tools at all but not on your own filesystem.
- Instances listen on `127.0.0.1` by default. Exposing one to the LAN requires
  an API key — the launcher rejects a non-loopback host without one, and
  `extra_args` cannot be used to override `--host`, `--port`, `--api-key`,
  `--path` or `--log-file`.

## What the launcher does not do

- No indexing, chunking, vector store or retrieval UI. That is client-side
  work; embedding a specific vector database here would age badly.
- No model downloading. v0.7.0's Web UI model manager can download models, but
  Chat's WebUI mode embeds that UI in a sandboxed iframe (scripts, forms,
  same-origin) which does not include downloads — use the external-browser
  button in the Chat toolbar for that, or the launcher's own model library page
  to inspect what is already on disk. Native Chat is the default mode.
- No built-in presets or tuned VRAM tiers, for the drift reason above.
