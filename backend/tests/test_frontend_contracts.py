"""Static guards for frontend contracts that a unit test cannot see.

Each assertion maps to a defect that shipped (or nearly shipped) and would
otherwise only be caught by opening the packaged app. Narrow on purpose.

Migrated from the 1.0 suite for the 2.0 React app. Two of the old assertions are
now covered by vitest instead of source-text matching (`tests/i18n.test.ts` pins
dictionary parity; the draft/baseline settings logic is gone entirely because the
2.0 sheet derives `dirty` from the presence of a draft). What remains here is what
only the *boundary* can check.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_SRC = REPO_ROOT / "ui" / "src"


def _ui_sources() -> list[Path]:
    return sorted(
        [path for path in UI_SRC.rglob("*") if path.suffix in {".ts", ".tsx"} and path.is_file()]
    )


def test_every_custom_response_header_the_frontend_reads_is_cors_exposed():
    """Every `X-LunarShroud-*` header the UI reads must be in the CORS expose list.

    The packaged page origin is `tauri.localhost`, so a fetch to
    `http://127.0.0.1:<port>` is **cross-origin**: the browser only lets JS read
    the CORS-safelisted headers plus whatever `Access-Control-Expose-Headers`
    names. On 2026-10-25 the list contained only `Dropped-Turns`, so knowledge
    citations (`Sources`) and the miss notice (`Knowledge`) read `null` in the
    installed build forever: no citations rendered, nothing persisted, no export
    section — while the backend really had injected the documents (the answer
    carried `[1][2]` markers).

    Neither the source-run channel (same-origin) nor `TestClient` (no CORS
    filtering) can see this. So this scans the **frontend source** for header read
    sites and reconciles them against the real middleware configuration — reading
    `app.user_middleware` rather than the source text, so a formatting change
    cannot fool the test.
    """
    backend_dir = REPO_ROOT / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import main as backend_main  # noqa: E402
    from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

    exposed: set[str] = set()
    for middleware in backend_main.app.user_middleware:
        if middleware.cls is CORSMiddleware:
            exposed = {
                str(item).lower() for item in middleware.kwargs.get("expose_headers", [])
            }
            break
    assert exposed, "no CORSMiddleware / expose_headers found in app.user_middleware"

    read_pattern = re.compile(
        r"""headers(?:\?\.)?\s*\.?\s*get(?:\?\.)?\(\s*['"`](x-lunarshroud-[a-z-]+)['"`]""",
        re.IGNORECASE,
    )
    offenders: dict[str, set[str]] = {}
    for source_path in _ui_sources():
        source = source_path.read_text(encoding="utf-8", errors="replace")
        for match in read_pattern.finditer(source):
            header = match.group(1).lower()
            if header not in exposed:
                offenders.setdefault(header, set()).add(source_path.name)

    assert not offenders, (
        "the UI reads custom headers that are not CORS-exposed; in the packaged "
        f"(cross-origin) build these get() calls always return null: "
        f"{ {k: sorted(v) for k, v in offenders.items()} }. "
        "Add the header to the expose_headers list in backend/api/middleware.py"
    )


def test_the_three_chat_contract_headers_are_still_read_by_the_ui():
    """Reverse direction: the middleware exposes headers the UI must actually use.

    Without this, removing a read site (say, dropping citation handling) would let
    the expose list drift toward "exposing something nobody reads" while the
    feature silently disappears.
    """
    sources = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in _ui_sources())
    for header in (
        "x-lunarshroud-sources",
        "x-lunarshroud-knowledge",
        "x-lunarshroud-dropped-turns",
    ):
        assert header in sources.lower(), f"the UI no longer reads {header}"


def test_frontend_never_falls_back_to_a_hardcoded_backend_port_inside_tauri():
    """AGENTS §2.2: in Tauri the shell vouches for the peer, so there is no safe URL
    until it does. A hardcoded port would hand the token to whatever holds it.

    The check is narrow: inside the Tauri branch of session resolution there must
    be no literal port. The browser/source-run branch legitimately falls back to
    the page origin, and a dev-only override reads an env var.
    """
    client = (UI_SRC / "lib" / "api" / "client.ts").read_text(encoding="utf-8")

    tauri_branch = client.split("if (inTauri())", 1)
    assert len(tauri_branch) == 2, "cannot locate the Tauri branch in client.ts"
    branch = tauri_branch[1].split("const location = window.location", 1)[0]

    assert "17520" not in branch, (
        "the Tauri session branch mentions a hardcoded port; it must throw instead "
        "of connecting to an unverified peer"
    )
    assert "get_backend_session" in branch, "the Tauri branch must obtain the session from the shell"


def test_token_travels_in_a_header_never_in_a_url():
    """The session token must not appear in a query string or WebSocket URL.

    URLs end up in logs, history and crash reports; the token is the only thing
    standing between a local process and `PUT /api/system/config`.
    """
    client = (UI_SRC / "lib" / "api" / "client.ts").read_text(encoding="utf-8")
    # Token assignment must only ever be a header value.
    for match in re.finditer(r"headers\['X-LunarShroud-Token'\]\s*=\s*([^;]+);", client):
        assert "session.token" in match.group(1), (
            f"unexpected token source: {match.group(1).strip()}"
        )
    # No template string may interpolate a token into a URL.
    for match in re.finditer(r"`[^`]*\$\{[^}]*token[^}]*\}[^`]*`", client):
        text = match.group(0)
        assert "X-LunarShroud-Token" in text or "protocol" in text or "subprotocol" in text.lower(), (
            f"token interpolated into a URL: {text[:120]}"
        )


def test_the_conversation_list_call_matches_the_response_envelope():
    """An envelope mismatch must fail here, not as "No conversations yet".

    2026-11-23: `chatsApi.list` was typed `apiGet<Conversation[]>` while
    `GET /api/chats` returns `{"conversations": [...]}`. The reply was a valid 200,
    so nothing errored — the sidebar's `sortConversations` received an **object**,
    threw inside a `useMemo`, and every user saw an empty list no matter how many
    conversations they had. Found by a browser test that needed an existing
    conversation to click; nothing in the unit suite could see it.

    The assertion is bidirectional: if the route ever stops wrapping the list, this
    test fails too, rather than silently leaving a now-useless unwrap in place.
    """
    route = (REPO_ROOT / "backend" / "api" / "chats.py").read_text(encoding="utf-8")
    list_route = route.split('@router.get("/api/chats")', 1)[1].split("@router.", 1)[0]
    backend_wraps = '"conversations"' in list_route

    api = (UI_SRC / "lib" / "api" / "index.ts").read_text(encoding="utf-8")
    ui_line = next(
        (item for item in api.splitlines() if "/api/chats${qs({ limit })}" in item),
        None,
    )
    assert ui_line is not None, "cannot find the conversation list call in the UI"
    # The annotation may be on the same line or the following one.
    call_block = api[api.index(ui_line) : api.index(ui_line) + 400]
    ui_expects_envelope = "conversations: Conversation[]" in call_block
    ui_expects_bare = "apiGet<Conversation[]>" in call_block

    assert backend_wraps == ui_expects_envelope, (
        f"backend wraps the list: {backend_wraps}; UI expects an envelope: "
        f"{ui_expects_envelope}. Line: {ui_line.strip()}"
    )
    assert not ui_expects_bare, "the conversation list is not a bare array"


def test_the_storage_report_matches_the_ui_type():
    """`/api/system/storage` returns an **object map**, not an array.

    This is the same defect class as the conversation envelope above, and it shipped
    in 2.0.0: the route builds `databases` as `{label: {path, writable, reason}}`
    while `StorageReport.databases` was declared `Array<{name, path, writable,
    error?}>`. `apiGet<T>` never validates at runtime (`return payload as T`), so
    the mismatch survived to `SettingsSheet.tsx`'s `.map` and crashed the whole
    window with `TypeError: s.data.databases.map is not a function` when the user
    opened Settings → Developer.

    Two things are asserted, because either half alone is not enough: the backend
    must key the map by label and expose `path`/`writable`/`reason`, and the UI type
    must not claim an array with a `name` field the route never sends.
    """
    route = (REPO_ROOT / "backend" / "api" / "system.py").read_text(encoding="utf-8")
    storage_route = route.split('@router.get("/api/system/storage")', 1)[1].split("@router.", 1)[0]

    # The route initialises `databases` as a dict literal and fills it by label.
    assert '"databases": {}' in storage_route, (
        "the storage route no longer starts `databases` as a dict; update the UI type "
        "and this assertion together"
    )
    assert 'report["databases"][label]' in storage_route, (
        "the storage route should key its entries by database label"
    )
    for field in ('"path"', '"writable"', '"reason"'):
        assert field in storage_route, f"the storage entry is missing {field}"

    types = (UI_SRC / "lib" / "api" / "types.ts").read_text(encoding="utf-8")
    # The interface body contains nested braces (`Record<string, {…}>`), so a naive
    # `split("}")` cuts it short. Slice from the declaration to the next top-level
    # `export` instead.
    report_block = types.split("export interface StorageReport", 1)[1]
    report_block = report_block.split("\nexport ", 1)[0]
    # Whitespace-insensitive: the declaration wraps across lines, so
    # `Record<` and `string,` end up separated by a newline plus indentation.
    compact = re.sub(r"\s+", " ", report_block)
    assert "Array<" not in compact, (
        "StorageReport.databases is typed as an array but the route returns an object "
        "map; `.map` on it crashes the Developer tab"
    )
    assert re.search(r"databases: Record<\s*string,", compact), (
        "StorageReport.databases must be a Record keyed by database label"
    )


def test_the_system_paths_type_names_the_fields_the_route_returns():
    """`SystemPaths` invented three field names the route never sends.

    The route returns `logs_dir` / `python_version` / `listening_port`; the UI type
    declared `log_dir` / `python` / `launcher_port`. Because every read goes through
    `String(value ?? '')`, nothing failed loudly — `PendingPage` rendered
    `(listening )` and the Developer tab showed blank rows.

    The check is a set comparison in both directions, so a field added to the route
    without updating the type fails just as loudly as the reverse.
    """
    route = (REPO_ROOT / "backend" / "api" / "system.py").read_text(encoding="utf-8")
    paths_route = route.split('@router.get("/api/system/paths")', 1)[1].split("@router.", 1)[0]
    returned = set(re.findall(r'^\s{8}"(\w+)":', paths_route, re.M))
    assert returned, "cannot extract the field names from api_system_paths"

    types = (UI_SRC / "lib" / "api" / "types.ts").read_text(encoding="utf-8")
    block = types.split("export interface SystemPaths", 1)[1].split("\n}", 1)[0]
    declared = set(re.findall(r"^\s{2}(\w+)\??:", block, re.M))
    assert declared, "cannot extract the field names from SystemPaths"

    missing = sorted(returned - declared)
    invented = sorted(declared - returned)
    assert not missing, f"SystemPaths is missing route fields: {missing}"
    assert not invented, f"SystemPaths declares fields the route never sends: {invented}"


# ---------------------------------------------------------------------------
# Config contract
#
# `PUT /api/system/config` rejects unknown keys outright
# (`config.update_config`: "unsupported config key(s)"). So a frontend field
# whose name is not a `DEFAULT_CONFIG` key does not degrade gracefully — the
# whole settings form becomes unsaveable. That is precisely what 2.0.0 shipped:
# the model directory was sent as `model_directory` while the backend key is
# `default_model_dir`, so Save always failed with 不支持的配置项 (2026-11-24,
# reported by the user), and because every reader used the same invented name
# the field also always rendered empty.
# ---------------------------------------------------------------------------


def _default_config_keys() -> set[str]:
    backend_dir = REPO_ROOT / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import config as backend_config  # noqa: E402

    return set(backend_config.DEFAULT_CONFIG)


def _app_config_fields() -> set[str]:
    types = (UI_SRC / "lib" / "api" / "types.ts").read_text(encoding="utf-8")
    block = types.split("export interface AppConfig", 1)[1].split("\n}", 1)[0]
    return set(re.findall(r"^\s{2}(\w+)\??:", block, re.M))


def test_the_app_config_type_matches_the_backend_config_keys():
    """Bidirectional: a key on either side without the other is a red test.

    One direction is the 400 the user hit ("frontend invents a key"). The other
    is subtler: a backend key with no typed field means the settings sheet cannot
    render it, so the setting exists and is unreachable. `AppConfig` therefore
    carries **no index signature** — `[key: string]: unknown` would let a typo
    typecheck, which is how this class of defect survives `tsc --noEmit`.
    """
    backend_keys = _default_config_keys()
    ui_fields = _app_config_fields()
    assert backend_keys, "DEFAULT_CONFIG is empty"

    missing = sorted(backend_keys - ui_fields)
    invented = sorted(ui_fields - backend_keys)

    assert not invented, (
        "AppConfig declares fields that are not config keys; PUT /api/system/config "
        f"will reject them with 400 unsupported config key(s): {invented}"
    )
    assert not missing, (
        f"AppConfig is missing backend config keys, so the UI cannot edit them: {missing}"
    )


def test_every_key_the_settings_sheet_writes_exists_in_the_backend():
    """The sheet's own field names, scanned from its source.

    The type test above only sees `types.ts`. This one sees what the settings
    components actually put into the PUT body, so a page that bypasses the type —
    or a later tab copied from a stale example — is still caught.

    Both write shapes are scanned: the tabs use `patch({ key: value })` (the shared
    helper from `useConfigForm`), and a direct `setDraft({ ...draft, key })` is
    still legal. Requiring at least one hit is deliberate: a gate that matches
    nothing must fail rather than pass vacuously, which it did when the tabs were
    refactored and this scan was fresh.
    """
    settings_dir = UI_SRC / "features" / "settings"
    sources = [
        path
        for path in settings_dir.rglob("*.tsx")
        if path.is_file() and "tabs" in path.parts or path.name == "SettingsSheet.tsx"
    ]
    assert sources, "no settings sources found"

    written: dict[str, str] = {}
    for path in sources:
        text = path.read_text(encoding="utf-8")
        # `patch({ key: value })` — the shared helper every tab uses.
        for key in re.findall(r"\bpatch\(\{([^{}]*)\}", text, re.S):
            for name in re.findall(r"(\w+)\s*:", key):
                written.setdefault(name, path.name)
        # Multi-line patch({ ... }) calls where the object spans lines.
        for match in re.finditer(r"\bpatch\(\{(.*?)\}\s*\)", text, re.S):
            for name in re.findall(r"(\w+)\s*:", match.group(1)):
                written.setdefault(name, path.name)
        # Direct draft writes: setDraft({ ...draft, key: value }).
        for key in re.findall(r"setDraft\(\{[^}]*?\b(\w+):", text, re.S):
            written.setdefault(key, path.name)
        for key in re.findall(r"\.\.\.draft,\s*\n\s*(\w+):", text):
            written.setdefault(key, path.name)
        # `onChange={(changes) => patch(changes)}` style: the key lives in the
        # caller's object literal, which the first pattern already covers.

    assert written, (
        "could not find any settings-field writes; if the write shape changed, "
        "update this gate rather than deleting it"
    )
    backend_keys = _default_config_keys()
    offenders = sorted(key for key in written if key not in backend_keys)
    assert not offenders, (
        "the settings sheet writes keys the backend rejects with 400 "
        f"unsupported config key(s): "
        f"{[(key, written[key]) for key in offenders]}"
    )


def test_the_system_overview_type_matches_the_route_it_reads():
    """`/api/system/overview` sends **GB** memory, and GPU `utilization`/`temperature`.

    The UI type said `memory_used_mb` / `memory_total_mb` / `gpu_available` and
    `utilization_percent` / `temperature_c`. Nothing threw — the status bar's RAM
    chip and the monitor's two GPU columns simply rendered `—` on every machine,
    forever. This is the quiet form of the same §2.41 defect as the conversation
    envelope, so it gets the same kind of boundary check.
    """
    main_source = (REPO_ROOT / "backend" / "main.py").read_text(encoding="utf-8")
    overview = main_source.split("def _collect_system_overview", 1)[1].split("\ndef ", 1)[0]
    returned = set(re.findall(r'^\s{8}"(\w+)":', overview, re.M))
    assert returned, "cannot extract the field names from _collect_system_overview"

    types = (UI_SRC / "lib" / "api" / "types.ts").read_text(encoding="utf-8")
    block = types.split("export interface SystemOverview", 1)[1].split("\n}", 1)[0]
    declared = set(re.findall(r"^\s{2}(\w+)\??:", block, re.M))

    missing = sorted(returned - declared)
    assert not missing, (
        f"SystemOverview is missing fields the route sends: {missing}. "
        "Reading them yields undefined, which renders as an em dash with no error."
    )

    # GPU rows: the same bidirectional check for the nested shape.
    gpu_source = (REPO_ROOT / "backend" / "core" / "gpu_monitor.py").read_text(encoding="utf-8")
    gpu_block = types.split("export interface GpuInfo", 1)[1].split("\n}", 1)[0]
    gpu_declared = set(re.findall(r"^\s{2}(\w+)\??:", gpu_block, re.M))
    for field in ("memory_used_mb", "memory_total_mb", "utilization", "temperature"):
        assert f'"{field}":' in gpu_source, (
            f"gpu_monitor no longer emits {field}; update GpuInfo and this test together"
        )
        assert field in gpu_declared, (
            f"GpuInfo is missing {field}, which gpu_monitor emits; the UI reads it as undefined"
        )
    for stale in ("utilization_percent", "temperature_c"):
        assert stale not in gpu_declared, (
            f"GpuInfo still declares {stale}; the backend has never sent that name"
        )


def test_numeric_route_ids_are_typed_as_numbers_in_the_client():
    """A route that declares `record_id: int` must not be typed `string` in the UI.

    This is the §2.41 "type is a lie" class in its quietest form: `enc(id)` accepts
    a string, the URL interpolates identically for `"7"` and `7`, and the request
    works — so nothing fails. It only breaks when a call site starts doing string
    work on the id (comparison, `parseInt`, a `Set<string>`), or when the type
    silently permits passing a slug where the server expects a row number.

    Measured while wiring the History actions: `historyApi.detail/remove/relaunch`
    were all typed `(id: string)` against `record_id: int`, and the first call site
    written against them had to be papered over with `String(record.id)`.
    """
    history_route = (REPO_ROOT / "backend" / "api" / "history.py").read_text(encoding="utf-8")
    assert "record_id: int" in history_route, (
        "the history routes no longer take an int id; update this test and the client"
    )

    api = (UI_SRC / "lib" / "api" / "index.ts").read_text(encoding="utf-8")
    block = api.split("export const historyApi", 1)[1].split("};", 1)[0]
    for method in ("detail", "remove", "relaunch"):
        match = re.search(rf"{method}:\s*\(id:\s*(\w+)\)", block)
        assert match, f"cannot find historyApi.{method} in the UI client"
        assert match.group(1) == "number", (
            f"historyApi.{method} is typed (id: {match.group(1)}) but the route takes "
            "an int; a string id typechecks and works until a call site does string "
            "work on it"
        )


def test_the_usage_summary_type_matches_the_route_it_reads():
    """§2.41 for the usage page: the type is an assertion, so reconcile it.

    `UsageSummary` drives a table now (per-model rows), where a renamed field does
    not throw — it renders an empty column, which is the quietest failure mode this
    project has shipped twice (`memory_used_mb`, `utilization_percent`).

    Both directions: a field the route emits but the type omits is invisible to the
    UI; a field the type invents reads `undefined` forever.
    """
    import sys

    backend_dir = REPO_ROOT / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    from core import usage_stats  # noqa: E402

    # The empty report is the shape every branch returns, so its keys are the
    # contract without needing a database.
    sample = usage_stats.summarize(7, now=0.0)
    backend_fields = set(sample)
    assert backend_fields, "summarize() returned nothing"

    api_types = (UI_SRC / "lib" / "api" / "types.ts").read_text(encoding="utf-8")
    block = api_types.split("export interface UsageSummary {", 1)
    assert len(block) == 2, "cannot find UsageSummary in types.ts"
    ui_fields = set(re.findall(r"^\s{2}(\w+)\??:", block[1].split("\n}", 1)[0], re.M))

    assert not sorted(backend_fields - ui_fields), (
        "usage fields the route returns but UsageSummary omits: "
        f"{sorted(backend_fields - ui_fields)}"
    )
    assert not sorted(ui_fields - backend_fields), (
        "usage fields UsageSummary declares that nothing sends: "
        f"{sorted(ui_fields - backend_fields)}"
    )

    expected_model_keys = {
        "model",
        "requests",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "avg_tokens_per_second",
    }
    row_block = api_types.split("export interface UsageModelRow {", 1)
    assert len(row_block) == 2, "cannot find UsageModelRow in types.ts"
    ui_model_fields = set(
        re.findall(r"^\s{2}(\w+)\??:", row_block[1].split("\n}", 1)[0], re.M)
    )
    assert ui_model_fields == expected_model_keys, (
        "UsageModelRow drifted from the row _model_rows builds: "
        f"{sorted(ui_model_fields ^ expected_model_keys)}"
    )


def test_the_appearance_profile_exports_only_real_config_keys():
    """Every key an exported appearance profile carries must be a config key.

    The export writes `APPEARANCE_KEYS`, and the import feeds those keys straight into
    the settings form, which PUTs them. A typo there would produce a profile that the
    user cannot import — `update_config` rejects unknown keys with 400, and the message
    ("unsupported config key(s)") tells someone holding a theme file nothing.

    Also checks the reverse intent: the list must stay narrow. `llama_server_path` or
    the gateway key appearing in a file called "-appearance-.json" would be a
    credential export wearing a theme file's name.
    """
    import sys

    backend_dir = REPO_ROOT / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import config as backend_config  # noqa: E402

    source = (
        UI_SRC / "features" / "settings" / "tabs" / "AppearanceTab.tsx"
    ).read_text(encoding="utf-8")
    block = source.split("const APPEARANCE_KEYS = [", 1)
    assert len(block) == 2, "cannot find APPEARANCE_KEYS in AppearanceTab.tsx"
    keys = re.findall(r"'([a-z_]+)'", block[1].split("] as const", 1)[0])
    assert keys, "APPEARANCE_KEYS is empty"

    config_keys = set(backend_config.DEFAULT_CONFIG)
    unknown = sorted(set(keys) - config_keys)
    assert not unknown, f"appearance profile exports keys the backend rejects: {unknown}"

    # Nothing secret may travel in a theme file.
    forbidden = {
        "api_server_key",
        "llama_server_path",
        "default_model_dir",
        "launcher_port",
        "web_search_endpoint",
        "model_directory",
    }
    leaked = sorted(set(keys) & forbidden)
    assert not leaked, (
        f"appearance profile carries non-appearance settings: {leaked}. A file the "
        "user shares as a theme must not contain machine paths or credentials."
    )
    """§2.41 for the usage page: the type is an assertion, so reconcile it.

    `UsageSummary` drives a table now (per-model rows), where a renamed field does
    not throw — it renders an empty column, which is the quietest failure mode this
    project has shipped twice (`memory_used_mb`, `utilization_percent`).

    Both directions: a field the route emits but the type omits is invisible to the
    UI; a field the type invents reads `undefined` forever.
    """
    import sys

    backend_dir = REPO_ROOT / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    from core import usage_stats  # noqa: E402

    # The empty report is the shape every branch returns, so its keys are the
    # contract without needing a database.
    sample = usage_stats.summarize(7, now=0.0)
    backend_fields = set(sample)
    assert backend_fields, "summarize() returned nothing"

    api_types = (UI_SRC / "lib" / "api" / "types.ts").read_text(encoding="utf-8")
    block = api_types.split("export interface UsageSummary {", 1)
    assert len(block) == 2, "cannot find UsageSummary in types.ts"
    ui_fields = set(re.findall(r"^\s{2}(\w+)\??:", block[1].split("\n}", 1)[0], re.M))

    assert not sorted(backend_fields - ui_fields), (
        "usage fields the route returns but UsageSummary omits: "
        f"{sorted(backend_fields - ui_fields)}"
    )
    assert not sorted(ui_fields - backend_fields), (
        "usage fields UsageSummary declares that nothing sends: "
        f"{sorted(ui_fields - backend_fields)}"
    )

    # The per-model table reads exactly these four columns.
    model_sample = sample["models"][0] if sample["models"] else None
    expected_model_keys = {
        "model",
        "requests",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "avg_tokens_per_second",
    }
    row_block = api_types.split("export interface UsageModelRow {", 1)
    assert len(row_block) == 2, "cannot find UsageModelRow in types.ts"
    ui_model_fields = set(
        re.findall(r"^\s{2}(\w+)\??:", row_block[1].split("\n}", 1)[0], re.M)
    )
    assert ui_model_fields == expected_model_keys, (
        "UsageModelRow drifted from the row _model_rows builds: "
        f"{sorted(ui_model_fields ^ expected_model_keys)}"
    )
    if model_sample is not None:
        assert set(model_sample) == expected_model_keys


def test_the_frontend_and_backend_agree_on_the_tool_catalogue():
    """Both sides declare tools; the name sets must match exactly.

    There are **two** declaration tables by design — `core/agent_declarations.py`
    for the agent loop and the chat proxy's server-side check, and
    `ui/src/lib/ported/chatTools.ts` for the chat page, which builds its request in
    the browser. That duplication was never reconciled, so a tool added or renamed
    on one side would ship as "the model asks for something the card cannot render"
    (or the reverse: a card for a tool that never gets declared).

    The filter test in `test_skill_store.py` covers the allow-list. This one covers
    the catalogue itself, in both directions, which is the cheapest way to keep two
    hand-maintained lists honest.
    """
    import sys

    backend_dir = REPO_ROOT / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    from core import agent_declarations  # noqa: E402

    backend_names = {
        item["function"]["name"]
        for item in agent_declarations.tool_declarations(web_search_enabled=True)
    }

    ui_source = (UI_SRC / "lib" / "ported" / "chatTools.ts").read_text(encoding="utf-8")

    def _names_in(block_name: str) -> set[str]:
        block = ui_source.split(f"export const {block_name}", 1)[1]
        block = block.split("];", 1)[0] if "];" in block.split("\n", 1)[1][:2000] else block[:4000]
        return set(re.findall(r"name:\s*'([a-z_]+)'", block))

    ui_names = _names_in("TOOL_DECLARATIONS") | _names_in("AGENT_TOOL_DECLARATIONS")
    # The web-search declaration is a single object, not an array.
    web_decl = ui_source.split("export const WEB_SEARCH_DECLARATION", 1)[1][:400]
    if "name: 'web_search'" in web_decl:
        ui_names.add("web_search")

    assert backend_names, "the backend declared no tools"
    assert ui_names, "could not extract tool names from the UI"

    missing_in_ui = sorted(backend_names - ui_names)
    missing_in_backend = sorted(ui_names - backend_names)
    assert not missing_in_ui, (
        f"tools declared by the backend but absent from chatTools.ts: {missing_in_ui}. "
        "The chat page builds its own request, so it will never offer them."
    )
    assert not missing_in_backend, (
        f"tools in chatTools.ts that the backend cannot dispatch: {missing_in_backend}. "
        "The model would be offered a name the executor refuses."
    )


def test_the_scheduled_task_type_names_the_fields_the_routes_return():
    """§2.41 applied to the scheduled-task surface.

    `ScheduledTask` / `ScheduledTaskRun` are hand-written mirrors of dicts built in
    `core/task_runner.py` and `storage/scheduled_tasks_db.py`. `apiGet<T>` only casts,
    so a renamed or missing field would render as a blank row with no error — the
    same silent shape as `SystemOverview.memory_used_mb`.

    Both directions are checked, and an empty extraction fails rather than passing
    vacuously.
    """
    backend_dir = REPO_ROOT / "backend"

    api = (UI_SRC / "lib" / "api" / "index.ts").read_text(encoding="utf-8")

    def _fields(interface: str) -> set[str]:
        block = api.split(f"export interface {interface} {{", 1)
        assert len(block) == 2, f"cannot find interface {interface} in the UI client"
        body = block[1].split("\n}", 1)[0]
        return set(re.findall(r"^\s{2}(\w+)\??:", body, re.M))

    # The task row's keys come from `_row_to_task`, which is the single place the
    # shape is built. `schedule_label` is added by `task_runner.list_tasks`.
    source = (backend_dir / "storage" / "scheduled_tasks_db.py").read_text(encoding="utf-8")
    row_block = source.split("def _row_to_task", 1)[1].split("\ndef ", 1)[0]
    backend_fields = set(re.findall(r'^\s{8}"(\w+)":', row_block, re.M))
    backend_fields.add("schedule_label")
    ui_fields = _fields("ScheduledTask")

    assert backend_fields, "could not extract the task row fields from _row_to_task"
    assert not sorted(backend_fields - ui_fields), (
        "fields the route returns but ScheduledTask does not declare: "
        f"{sorted(backend_fields - ui_fields)}"
    )
    assert not sorted(ui_fields - backend_fields), (
        "fields ScheduledTask declares that the route never sends: "
        f"{sorted(ui_fields - backend_fields)}"
    )

    run_block = source.split("def _row_to_run", 1)[1].split("\ndef ", 1)[0]
    backend_run = set(re.findall(r'^\s{8}"(\w+)":', run_block, re.M))
    ui_run = _fields("ScheduledTaskRun")
    assert backend_run, "could not extract the run row fields from _row_to_run"
    assert not sorted(backend_run - ui_run), (
        f"run fields the route returns but ScheduledTaskRun omits: {sorted(backend_run - ui_run)}"
    )
    assert not sorted(ui_run - backend_run), (
        f"run fields ScheduledTaskRun declares but nothing sends: {sorted(ui_run - backend_run)}"
    )

    # The status vocabulary is a closed set on both sides.
    assert "_RUNNING = \"running\"" in source and "_OK = \"ok\"" in source
    assert "_FAILED = \"failed\"" in source
    assert "'running' | 'ok' | 'failed'" in api, (
        "the UI's run status union no longer matches the storage constants"
    )


