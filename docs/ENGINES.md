# External inference engines (vLLM, SGLang)

LunarShroud runs **llama.cpp** itself: it spawns `llama-server.exe`, talks to it over
loopback, and manages its lifecycle. An **external engine** is different — it is a
server *you* started, somewhere this machine can reach, and the launcher's only claim
is "I can reach this address and it speaks the OpenAI wire format".

This page explains what that means in practice, what the launcher does and does not do
for you, and how to get a vLLM or SGLang server reachable from Windows.

## Why this is not "install vLLM"

vLLM's own documentation states plainly that **it does not support Windows natively**
and points at WSL2 as the workaround. SGLang has the same constraint. That is a property
of those projects, not a limitation this launcher could paper over.

Adding a "install vLLM" button would mean:

* downloading several gigabytes of Python packages and CUDA wheels,
* requiring a WSL distribution and a compatible NVIDIA driver,
* a failure surface (missing driver, wrong CUDA version, incompatible GPU) that produces
  messages no user can act on,
* and a permanent violation of the offline guarantee: the launcher would need the
  network to set itself up.

None of those are acceptable for a Windows desktop app whose stated premise is "no
account, no network, everything stays on this machine". So the launcher does the part it
can do honestly: **register, probe, route and display**. Installation is yours.

## What the launcher does

| Step | What happens |
|---|---|
| Register | Stores `{kind, label, base_url, model, api_key_env}` in `engines.json` under the data directory. |
| Validate | Only `http`/`https` and **loopback** by default. A remote host is refused unless `LUNARSHROUD_ALLOW_REMOTE_ENGINES=1`. |
| Probe | `GET /v1/models`. The result (reachable, model list, or an actionable error) is written back so a failed engine stays visibly failed. |
| Chat | `POST /v1/chat/completions` with the engine's configured model name and an optional bearer token. |
| Report capabilities | `llama_cpp` supports `chat, tokenize, health, props, embeddings, devices`; `openai_compat` supports `chat, models` only. |

### The capability difference matters

An OpenAI-compatible server has **no `/tokenize` endpoint**. The launcher therefore
cannot count tokens exactly for such an engine, and the UI labels every count as an
estimate. That label is not modesty — a context meter driven by a character heuristic
that presented itself as exact would make the trimming decision look authoritative while
being a guess, and a user would lose the tail of a long conversation with no explanation.

The estimate splits by script (`1.6` characters per token for CJK, `4` for Latin) because
a single ratio under-counts Chinese badly enough to break a budget.

### Secrets

`api_key_env` holds the **name of an environment variable**, never the key. The value is
read from the process environment at request time, so:

* it never reaches `engines.json`, a backup, or a support bundle;
* rotating the variable takes effect on the next request;
* a pasted key is rejected at validation time — a hyphen, an equals sign, a space or a
  leading digit all indicate a secret rather than a variable name.

## Getting a server reachable from Windows

### vLLM or SGLang inside WSL2

Both are Linux-only. Run them in WSL2 and forward the port so Windows can reach it:

```powershell
# In WSL: start the server listening on all interfaces of the WSL VM
wsl -- bash -lc "vllm serve Qwen/Qwen2.5-7B-Instruct --host 0.0.0.0 --port 8000"
```

WSL2 normally makes `localhost` work in one direction only. The reliable arrangement is
an explicit port proxy from Windows:

```powershell
$wslIp = (wsl hostname -I).Trim().Split(" ")[0]
netsh interface portproxy add v4tov4 listenaddress=127.0.0.1 listenport=8000 `
  connectaddress=$wslIp connectport=8000
```

Then register `http://127.0.0.1:8000` in the launcher (Settings → runtimes, or
`POST /api/engines`). The address stays loopback from the launcher's point of view,
which is what the validation expects.

> `netsh portproxy` rules persist across reboots but break when the WSL VM gets a new IP.
> Re-run the command after a WSL restart, or pin the address in `.wslconfig`.

### A server on another machine

Refused by default. Setting `LUNARSHROUD_ALLOW_REMOTE_ENGINES=1` permits a LAN address —
and puts an address in a file that gets backed up. Only do this on a network you control,
and set `api_key_env` to a variable that actually holds a key.

## Error messages you will see

The three cases users actually hit are named explicitly rather than surfaced as
`ConnectError`:

| Message | Meaning |
|---|---|
| `nothing is listening at that address; start the engine, and if it runs in WSL forward the port to Windows` | Nothing bound to the port, or the port proxy is missing. |
| `the engine did not answer in time; it may still be loading the model` | Reachable but slow — usually a cold model load. |
| `the address answered but not with HTTP; check the port and scheme` | Something else is on that port. |
| `the engine rejected the credentials; check the API key variable` | 401/403 from the engine. |

## What is deliberately not supported

* **Installing or updating an engine.** No network, no package manager, no WSL provisioning.
* **Multi-node serving.** The launcher is a desktop front end.
* **Exact token counts on `openai_compat`.** See above — estimates only, labelled.
* **Launching the engine.** The launcher never spawns an external engine; `launch` in the
  API response is `external` for everything except a managed `llama-server`.

## API surface

All routes are on the launcher app and require the session token.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/engines` | List registered engines with their last probe outcome. |
| POST | `/api/engines` | Create or update one engine (validates kind, URL, label, key variable). |
| DELETE | `/api/engines/{engine_id}` | Remove a registration. |
| POST | `/api/engines/{engine_id}/probe` | Check reachability, record the result, return the model list. |
| GET | `/api/engines/{engine_id}/capabilities` | Capability names plus `exact_token_count`. |
