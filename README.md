# devstack

`devstack` is a workspace-scoped local server orchestration CLI for MSA monorepos. From a single `.devstack.json` manifest, it derives the Docker infrastructure, brings up Gradle/Spring and non-JVM services, generates a per-workspace local config overlay, exposes a thin web UI for the common controls, and runs an AI-friendly doctor with heartbeat + cancel for unhealthy starts.

> **Rename note.** The tool was previously called `ccstack`. The legacy
> `ccstack` command symlink, `~/.ccstack` state directory, `.ccstack.json`
> manifest filename, `.ccstack-overrides.json` repo overrides, `CCSTACK_*`
> env vars, `ccstack.infra.*` service-id prefix, `ccstack_managed_environment`
> /`ccstack_status_notes` persisted JSON keys, and `ccstack-local.yml`
> classpath import name are all accepted for one release as backward-
> compatibility shims. The new canonical names (`devstack`, `~/.devstack`,
> `.devstack.json`, `.devstack-overrides.json`, `DEVSTACK_*`,
> `devstack.infra.*`, `devstack_managed_environment` /
> `devstack_status_notes`, `devstack-local.yml`) are preferred everywhere.
> See [TEAM_ROLLOUT.md](TEAM_ROLLOUT.md) for the rollout invariant.

## Table of Contents

- [Install (Quick Start)](#install-quick-start)
- [What devstack Does](#what-devstack-does)
- [Daily Commands](#daily-commands)
- [Workspace Model: Bind → Analyze → Apply](#workspace-model-bind--analyze--apply)
- [Manifest](#manifest)
- [Web UI](#web-ui)
- [AI Doctor](#ai-doctor)
- [Backward Compatibility (`ccstack` → `devstack`)](#backward-compatibility-ccstack--devstack)
- [Testing & CI](#testing--ci)
- [Developing devstack Itself (Local Checkout)](#developing-devstack-itself-local-checkout)
- [Uninstall](#uninstall)

## Install (Quick Start)

One-touch remote install — paste this into a terminal on a clean machine:

```bash
curl -fsSL https://raw.githubusercontent.com/woogiekim/devstack/main/bootstrap | sh
```

The bootstrap is a small POSIX `sh` script (read it before you run it — it lives at [bootstrap](bootstrap) on the `main` branch). It will:

1. Clone (or update) the devstack repo into `DEVSTACK_HOME` (default `~/.devstack/src`).
2. Run the canonical installer from inside that checkout — symlinking `devstack` into `$HOME/.local/bin` (with `ccstack` kept as a one-release legacy alias), installing Codex/Claude skill files, and printing the `DEVSTACK_HOME` value to export from your shell profile.

Env overrides recognized by the bootstrap:

| Variable | Default | Purpose |
|---|---|---|
| `DEVSTACK_HOME` | `~/.devstack/src` | Checkout location used by the installer at runtime. Also exported for `devstack` itself. |
| `DEVSTACK_REPO` | `https://github.com/woogiekim/devstack.git` | Git remote to clone. Override for forks. |
| `DEVSTACK_REF` | `main` | Branch or tag to install. |
| `DEVSTACK_BIN_DIR` | `$HOME/.local/bin` | Where the `devstack` / `ccstack` symlinks land. |

After install, add `~/.local/bin` to `PATH` if it is not already on it:

```bash
export PATH="$HOME/.local/bin:$PATH"
export DEVSTACK_HOME="$HOME/.devstack/src"
```

Then verify and open the UI:

```bash
devstack --help
devstack ui
```

Re-running the same one-liner is idempotent: existing checkouts are updated in place (`git fetch --depth 1 origin <ref>` + `git reset --hard FETCH_HEAD`); fresh installs are a shallow clone.

## What devstack Does

devstack centralizes the things a developer typically does by hand when bringing up an MSA monorepo locally:

- **Docker infra derivation.** Reads `.devstack.json` profiles and brings up compose-backed services (`type: compose`) without per-developer compose YAML edits.
- **Gradle/Spring + non-JVM service analysis.** Recognises `type: gradle` services (bootRun + actuator health probes) and analyzes non-JVM services through the same workspace lens so docker-only and Node/Python helpers register in the workspace too.
- **Generated local config overlay.** Materializes a per-workspace `devstack-local.yml` (with a legacy `ccstack-local.yml` twin for one release) that the running app can import as `classpath:devstack-local.yml`. This is the "Apply" output of Bind → Analyze → Apply.
- **Web UI** — `devstack ui` opens a small browser control surface for `Status`, `Start light`, and `Stop`, with restart/logs/full-startup hidden under `Advanced controls`.
- **AI doctor with heartbeat + cancel.** The UI's doctor view streams structured observability events while a start-up runs and supports a clean cancel, so an agent (or operator) can stop the start when an actuator health check stalls.

## Daily Commands

```bash
devstack status
devstack up [target]
devstack light [target]
devstack ui
```

That is the primary path:

1. `devstack status` checks what is running.
2. `devstack up [target]` starts the normal target.
3. `devstack light [target]` starts the lightweight target when a `target-light` profile exists.
4. `devstack ui` opens the lightweight browser control surface.

Advanced commands stay available:

```bash
devstack up contents --light
devstack up contents --only contents.db,contents.proxy
devstack restart contents.fixity
devstack restart contents.proxy
devstack stop contents.proxy
devstack down contents
devstack logs contents.proxy -f
devstack workspace list
devstack workspace default my-workspace
```

## Workspace Model: Bind → Analyze → Apply

A workspace is a registered local checkout that has a `.devstack.json` manifest at its root.

1. **Bind** — register the repository:

   ```bash
   devstack workspace register my-workspace /path/to/repo --default
   ```

2. **Analyze** — devstack inspects services declared in the manifest (and any non-JVM helpers it can detect), and prepares an overlay plan.

3. **Apply** — devstack materializes the overlay (`devstack-local.yml` + the legacy `ccstack-local.yml` twin) and brings up the requested target.

Workspace resolution order for any command:

1. `--root <path>`
2. `--workspace <name>`
3. nearest parent directory containing `.devstack.json` (or legacy `.ccstack.json`)
4. the default registered workspace, or the only registered workspace when there is exactly one

If no workspace can be resolved safely, devstack prints guidance instead of guessing.

Manage workspaces:

```bash
devstack workspace register my-workspace /path/to/repo --default
devstack workspace list
devstack workspace default
devstack workspace default my-workspace
```

## Manifest

Each repository owns a `.devstack.json` file. Services can be `compose`, `gradle`, or `command`. Legacy `.ccstack.json` is read as a fallback for one release.

```json
{
  "workspace": "my-workspace",
  "profiles": {
    "default": { "services": ["app.db", "app.fixity", "app.proxy"] },
    "app-light": { "services": ["app.db", "app.proxy"] }
  },
  "services": {
    "app.db": {
      "type": "compose",
      "compose": "${DEVSTACK_HOME}/examples/app.compose.yml",
      "service": "db"
    },
    "app.fixity": {
      "type": "gradle",
      "task": ":apps:fixity-app:app:bootRun",
      "port": 8080,
      "health": "http://localhost:8080/actuator/health",
      "depends_on": ["app.db"]
    }
  }
}
```

Repo-level overrides live in `.devstack-overrides.json` (or legacy `.ccstack-overrides.json` for one release).

## Web UI

Start the local browser UI from any registered workspace:

```bash
devstack ui
devstack ui --no-open
devstack ui --port 8770 --no-open
```

By default the UI listens on `http://127.0.0.1:8765` and opens the browser. When multiple workspaces are registered, the page includes a workspace selector and respects the registered default.

The primary controls are `Status`, `Start light`, and `Stop`. Service-level restart, logs, full startup, and `no deps` are kept under `Advanced controls`, so the common path stays small while command behavior remains compatible with terminal use.

Non-destructive verification (handy from a smoke check or AI agent):

```bash
devstack --root "$PWD" ui --no-open
curl -sS http://127.0.0.1:8765/api/meta
curl -sS -X POST -d 'action=status&target=all' http://127.0.0.1:8765/api/run
```

## AI Doctor

The doctor view in the UI (and its `/api/doctor/*` endpoints) emit structured observability events while a workspace start-up runs:

- **Heartbeat** — periodic events show that the doctor is still alive while an actuator health probe is being polled, so an agent can distinguish "stuck" from "slow".
- **Cancel** — a doctor run can be cancelled cleanly; the in-progress probe is interrupted and the workspace returns to a sane state instead of dangling.
- **Confidence labels** — health verdicts (`high` / `medium` / `low`) and the proof type (`actuator`, `docker_exec`, `tcp_only`) are surfaced so an agent can decide whether to act on a single signal or wait for another sample.

These are the inputs that let Codex / Claude `devstack` skills supervise an MSA start-up without screen-scraping logs.

## Backward Compatibility (`ccstack` → `devstack`)

The rename introduces seven compat shims, each exercised by a regression test in `tests/test_devstack_compat_shims.py`. All seven will be removed one release after the rename. The full list and the deprecation table live in [TEAM_ROLLOUT.md](TEAM_ROLLOUT.md); the short version is:

- `CCSTACK_*` env vars are accepted (one-time stderr deprecation warning); `DEVSTACK_*` are preferred.
- `~/.ccstack` is read only when `~/.devstack` does not exist; data is never auto-moved.
- `.ccstack.json` / `.ccstack-overrides.json` are read as fallbacks at every lookup site.
- `ccstack.infra.*` service-id prefix is accepted as an alias for `devstack.infra.*`.
- Persisted JSON reads accept both `ccstack_*` and `devstack_*` keys; writes only emit `devstack_*`.
- The managed-environment materializer emits BOTH `devstack-local.yml` (canonical) and `ccstack-local.yml` (legacy) so app jars whose Spring config references `classpath:ccstack-local.yml` keep importing the overlay.
- Codex / Claude skills are installed under both `devstack` and `ccstack` skill paths.

When devstack starts a workspace whose new project has no containers but legacy `ccstack_<ws>` resources still exist on the host, it prints a structured option-list notice with the adoption / cleanup commands (devstack never auto-migrates the resources; operators run the printed commands themselves).

## Testing & CI

Run the core unit tests:

```bash
python3 -m unittest discover -s tests
```

Or via the Makefile:

```bash
make test
```

The live-workspace API smoke suite is opt-in and skipped automatically when no workspace is reachable:

```bash
export DEVSTACK_SMOKE_ENABLED=1
export DEVSTACK_SMOKE_WORKSPACE=your-workspace
# optional: fixed project path, avoids registry lookup
export DEVSTACK_SMOKE_WORKSPACE_ROOT=/path/to/your-workspace
# required when a smoke endpoint is protected by Basic Auth
export DEVSTACK_SMOKE_BASIC_AUTH='user:password'
make test-smoke
```

Optional:

```bash
export DEVSTACK_SMOKE_HOST=127.0.0.1
```

Legacy `CCSTACK_SMOKE_*` env spellings are still honored as fallbacks for one release.

A baseline workflow at `.github/workflows/devstack-ci.yml` runs `make test` on every push, with optional smoke pass in the same pipeline.

## Developing devstack Itself (Local Checkout)

For contributors who want to edit `devstack` itself, the local-checkout install path is unchanged:

```bash
# from inside the checkout
./install
```

This is the same script the remote bootstrap execs after cloning. It links `devstack` into `$HOME/.local/bin` (with `ccstack` kept as a legacy alias symlink for one release), installs the Codex/Claude skill files into the local AI tool homes, and prints the `DEVSTACK_HOME` value that should be exported from your shell profile.

If you only want the AI skill files refreshed (e.g. after editing `skills/codex/SKILL.md`):

```bash
./install-ai-skills
```

See [TEAM_ROLLOUT.md](TEAM_ROLLOUT.md) for the team-wide rollout invariant: fixes must be distributable from repository-tracked files, never from local `~/.devstack` runtime state.

## Uninstall

Remove the command symlinks and AI skill files:

```bash
devstack uninstall --yes
# or, from a local checkout:
./uninstall --yes
```

By default uninstall keeps `~/.devstack` workspace registry, generated configs, pids, and logs. Add `--purge-state` only when you also want to remove devstack local state:

```bash
devstack uninstall --yes --purge-state
```
