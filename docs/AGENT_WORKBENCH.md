# Agent workbench

The workbench lets a local model read, write and run commands inside **one folder you
authorize**. Every write and every command asks for your approval first, with the actual
arguments laid out before you decide.

It is not a sandbox. It is a set of constraints that bound what a mistaken decision can
reach, plus a gate that makes sure the decision is yours.

## The tools

| Tool | Risk | Approval | What it can reach |
|---|---|---|---|
| `read_file` | read-only | no prompt | one file inside the authorized folder |
| `file_glob_search` | read-only | no prompt | file names matching a glob, inside the folder |
| `grep_search` | read-only | no prompt | literal text in files inside the folder |
| `write_file` | write | **every call** | creates or replaces one file inside the folder |
| `edit_file` | write | **every call** | replaces an exact substring in one file inside the folder |
| `run_command` | command | **every call** | one program, no shell, inside the folder |

Reads skip the prompt because the worst outcome of a wrong read is reading a file that was
already in a folder you chose. They still pass through the same path sandbox and are still
logged as events, so the transcript shows every file the model opened.

## The path sandbox

Every path a tool receives is resolved by `chat_tools.resolve_inside`, which calls
`Path.resolve()` **before** comparing against the authorized root. That ordering is what
makes symlinks and Windows junctions ineffective: a link pointing at `C:\Users` resolves to
`C:\Users`, which is not inside the root.

Rejected outright:

- absolute paths (`C:\Windows\...`, `/etc/passwd`)
- UNC paths (`\\server\share`) — they initiate a network request
- drive-relative paths (`D:foo`), whose meaning depends on the process working directory
- `..` traversal that climbs out of the root
- a prefix-sharing sibling (`root-evil` is not inside `root`)

The root itself is a legal target only for the search tools, where "search everything"
is the common case. Reading a directory is meaningless, so `read_file` rejects it.

## Command execution

`run_command` takes `argv` as a **list**: the program first, then its arguments.

- **No shell.** `subprocess.run(..., shell=False)`. A shell would give `;`, `&&`,
  backticks and variable expansion meaning, turning the model's argument string into a
  program.
- **No bare-name resolution.** `argv[0]` must either be an absolute path or resolve
  through `PATH` to one; otherwise the call is refused. Windows' `CreateProcess` searches
  the *launcher's own* working directory first for a bare name, so a file planted there
  would run in place of the intended program. Measured: a planted `agentprobe.exe` in the
  launcher directory executed before this rule existed.
- **Sanitized environment.** `sanitized_child_env()` strips every `LUNARSHROUD_*`
  variable. Those carry the launcher's session token; a spawned process must not inherit
  it (AGENTS §2.4).
- **Bounded.** 300 s wall clock, 64 KB of captured output; exceeding either is reported,
  not silently truncated.
- **Fixed working directory**, resolved through the same sandbox, so a command cannot be
  pointed outside the folder.

Risk classification in the approval card is coarse on purpose: `rm`, `del`, `format`,
`powershell`, `cmd`, `reg`, `diskpart` and friends are marked **high-risk**, so they do
not look identical to `npm test` when you are clicking quickly. It is not an allow-list
and not a safety guarantee — it is a signal.

## Approval semantics

| Decision | Effect |
|---|---|
| Allow once | this call only |
| Allow for this tool | this **tool** for the rest of the run — not this argument set |
| Deny | the call does not run, and a structured refusal goes back to the model so it can choose another path |

Session approval is scoped to the tool name rather than to the arguments, because the
alternative is surprising: approving `npm test` would silently approve every later
command. The UI labels it "this tool" for that reason.

A pending approval **parks the run**. It never proceeds, and it never silently drops the
call either — dropping it would leave the model waiting for a result that never arrives.
A timeout (600 s) counts as **deny**: the absence of an answer is not consent.

## Stop conditions

Each limit produces a reported failure, never a hang:

| Budget | Value | Why |
|---|---|---|
| turns | 24 | a model can loop |
| wall clock | 900 s | catches a model that streams very slowly |
| tool calls | 60 | catches a loop that only calls tools |
| identical calls | 3 | a repeated call is not progress, and each repeat costs a round trip |

Cancellation is checked between turns, after a model reply and inside a tool batch — so
the stop button works even while a run is parked on an approval. A cancelled run reports
`cancelled` and can never also report `done`.

## Events

The UI renders exactly these event types, in order:

```
turn_started · assistant_message · tool_call · approval_requested · tool_started
tool_result · tool_denied · error · done · cancelled
```

`GET /api/agent/runs/{id}/events?after=<seq>` replays from a cursor and then follows the
run. That single path serves both live streaming and reconnect, so a dropped connection
resumes with no gap and no duplicates.

## Limits worth knowing

- **Approving a command is trusting the command.** The constraints above bound what the
  model can *reach* without your consent; they do not make an approved `rm -rf` safe.
- **The model can read anything in the folder you authorize.** Point it at a project
  directory, not at your home directory.
- **There is no network tool by default.** The product is offline by contract, and
  `web_search` is the one deliberate exception: it exists only when you enable it in
  Settings → 系统与引擎, point it at a search endpoint you host, and tick "allow web
  search" for a conversation. Every call is approved individually and the approval
  card shows the exact query that will leave your machine. With the setting off — the
  default — no network tool is offered to the model at all, and a stale tool name from
  an earlier turn is refused at the execute endpoint.
- **Files are written atomically** (temp file plus `os.replace`). A partial write is worse
  than no write — the next read would see a truncated body and the model would "repair" it
  with invented content.
- **`edit_file` requires an exact, unique match.** No regex, no fuzzy matching; a model
  that guesses at formatting produces a successful edit of the wrong region.
