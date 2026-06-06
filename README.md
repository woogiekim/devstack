# devstack

`devstack` is a local server orchestration CLI for MSA monorepos. The CLI is project-agnostic: each repository declares a `.devstack.json` manifest, and the same command set can start Docker infrastructure, Gradle/Spring services, and individual services.

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

## Simple Daily Use

```bash
devstack up
devstack light contents
devstack status
devstack ui
```

That is the primary path:

1. `devstack status` checks what is running.
2. `devstack up [target]` starts the normal target.
3. `devstack light [target]` starts the lightweight target when a `target-light` profile exists.
4. `devstack ui` opens the lightweight browser control surface.

Advanced commands still work when needed:

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

Install the team command, workspace registry, and AI skills:

```bash
~/Developments/devstack/install
```

The installer links `devstack` into `$HOME/.local/bin` (with `ccstack` kept as a legacy alias symlink for one release), registers the current repository in `~/.devstack/workspaces.json` (or under `~/.ccstack/workspaces.json` if that directory exists and `~/.devstack/` does not), and installs Codex/Claude skills under both `devstack` and `ccstack` skill paths. It also prints the `DEVSTACK_HOME` value that should be exported from your shell profile.

Uninstall the command link and AI skills:

```bash
devstack uninstall --yes
# or, from this checkout:
~/Developments/devstack/uninstall --yes
```

By default uninstall keeps `~/.devstack` workspace registry, generated configs, pids, and logs. Add `--purge-state` only when you also want to remove devstack local state:

```bash
devstack uninstall --yes --purge-state
```

## Workspace Resolution

Most commands resolve a workspace in this order:

1. `--root <path>`
2. `--workspace <name>`
3. nearest parent directory containing `.devstack.json` (or legacy `.ccstack.json`)
4. the default registered workspace, or the only registered workspace when there is exactly one

If no workspace can be resolved safely, `devstack` prints guidance instead of guessing.

Manage registered workspaces:

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

## Team-Deployable Changes

Repository files are the source of truth for team rollout. Generated files under
`~/.devstack` are local runtime state; they can be inspected or purged during
debugging, but they must not be the only place a fix exists.

When changing startup, recovery, Analyze, Apply, or UI behavior, make the
repository code regenerate the intended local config from a clean state and add
tests for that path. See [TEAM_ROLLOUT.md](TEAM_ROLLOUT.md) for the rollout
invariant.

## AI Agent Usage

Agents should prefer `devstack` over ad hoc `bootRun`, `docker ps`, and manual PID management when a repository has `.devstack.json` (or legacy `.ccstack.json`).

Recommended flow:

1. Run `devstack status`.
2. Use `devstack up <target>`, `devstack light <target>`, or `restart <service>`.
3. Verify health with `status`.
4. Inspect logs with `logs <service>`.

## Web UI

Start a local browser UI from any workspace that has `.devstack.json`:

```bash
devstack ui
```

By default, the UI listens on `http://127.0.0.1:8765` and opens the browser.
It can also start outside a project directory when a workspace is registered.
When multiple workspaces are registered, the page includes a workspace selector.
Use `--no-open` when you only want to start the server:

```bash
devstack ui --no-open
devstack ui --port 8770 --no-open
```

The UI is a thin wrapper around the existing CLI. The primary controls are
`Status`, `Start light`, and `Stop`. Service-level restart, logs, full startup,
and `no deps` are kept under `Advanced controls` so the common path stays
small while command behavior remains compatible with terminal use.

Non-destructive verification:

```bash
python3 ~/Developments/devstack/devstack --root "$PWD" ui --no-open
curl -sS http://127.0.0.1:8765/api/meta
curl -sS -X POST -d 'action=status&target=all' http://127.0.0.1:8765/api/run

tmp_home=$(mktemp -d)
HOME="$tmp_home" python3 ~/Developments/devstack/devstack workspace register my-workspace "$PWD" --default
cd /tmp
HOME="$tmp_home" python3 ~/Developments/devstack/devstack status
```

API smoke verification for a live workspace:

```bash
export DEVSTACK_SMOKE_ENABLED=1
export DEVSTACK_SMOKE_WORKSPACE=your-workspace
# optional: fixed project path, avoids registry lookup
export DEVSTACK_SMOKE_WORKSPACE_ROOT=/path/to/your-workspace
# required when a smoke endpoint is protected by Basic Auth
export DEVSTACK_SMOKE_BASIC_AUTH='user:password'
python3 -m unittest -v tests.test_devstack_smoke_api
```

Optional:

```bash
export DEVSTACK_SMOKE_HOST=127.0.0.1
```

The legacy `CCSTACK_SMOKE_*` env spellings are still honored as fallbacks for one release.

Codex and Claude skill source files are kept under `skills`.

Install them into the local AI tool homes:

```bash
~/Developments/devstack/install-ai-skills
```

See [TEAM_ROLLOUT.md](TEAM_ROLLOUT.md) for team-wide onboarding.

## CI / Automation

```bash
# Run core tests only
make test

# Run API smoke tests (2xx/3xx only responses are accepted).
# If no workspace is reachable, smoke tests are skipped automatically.
make test-smoke
```

For GitHub Actions, use `make` targets directly. A baseline workflow is added at `.github/workflows/devstack-ci.yml` with: CI unit tests always required, optional smoke pass in the same pipeline.
