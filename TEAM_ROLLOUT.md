# devstack Team Rollout

## Goal

`devstack` gives the team one shared command set for local MSA server orchestration across repositories. The tool was previously called `ccstack`; the legacy command name and `.ccstack.json` manifest filename are accepted as backward-compatibility shims for one release.

It supports:

- workspace-level startup
- profile-based startup such as full and light
- Docker infrastructure startup
- individual Fixity/Proxy restart with current branch code
- status, logs, stop, and down commands
- Codex and Claude AI skill installation

## Team Deployability Invariant

devstack fixes must be distributable from repository-tracked files. Runtime
artifacts under `~/.devstack` (or legacy `~/.ccstack` for upgraders), including
generated workspace configs, pids, and logs, are per-developer state.
Do not ship a fix whose only effect is editing those local artifacts.

For startup, recovery, Analyze, Apply, or UI behavior changes:

- implement the behavior in `devstack`, project manifests, tests, or docs
- prove a clean `~/.devstack` state can regenerate the required config through
  Analyze/Apply
- keep credentials and machine-specific paths outside the repository
- document any direct local-state edit as a diagnostic step only, not as the
  team rollout mechanism

## Installation

From a repository that contains `.devstack.json` (or legacy `.ccstack.json`):

```bash
~/Developments/devstack/install
```

If `devstack` is not found after installation, add this to the shell profile:

```bash
export PATH="$HOME/.local/bin:$PATH"
export DEVSTACK_HOME="$HOME/Developments/devstack"
```

The installer also creates a legacy `ccstack` symlink under `$HOME/.local/bin`
pointing at the same binary, so existing shell aliases / shebangs continue to
work for one release.

Verify:

```bash
devstack --help
devstack status
```

## Daily Usage

```bash
devstack status
devstack up contents
devstack light contents
devstack ui
```

Keep the default workflow small: check status, start the normal target, start
the lightweight target, or open the UI. The UI shows these common actions first
and keeps service-level controls in `Advanced controls`.

Advanced commands remain available:

```bash
devstack restart contents.fixity
devstack restart contents.proxy
devstack logs contents.proxy -f
devstack down contents
```

## Multi-Repository Usage

The installer registers each repository in:

```text
~/.devstack/workspaces.json
# (existing ~/.ccstack/workspaces.json is read as a fallback for one release)
```

Run a registered workspace from any directory:

```bash
devstack --workspace sample-shop status
devstack --workspace sample-shop up app
devstack workspace list
devstack workspace default sample-shop
```

Workspace resolution order is `--root`, then `--workspace`, then the nearest
`.devstack.json` (or `.ccstack.json` legacy filename), then a registered
default workspace. If there is exactly one registered workspace, devstack can
use it as the safe default. When multiple workspaces exist and no default is
configured, set one with `devstack workspace default <name>` or pass
`--workspace`.

Each new repository only needs:

- `.devstack.json`
- optional compose files
- optional mock servers

## Backward-Compatibility Shims (one release)

The rename introduces seven compat shims. Each shim is exercised by a
regression test in `tests/test_devstack_compat_shims.py`. All shims will be
removed one release after the rename — see the per-shim deprecation entry
below.

1. **`CCSTACK_*` env vars** — `DEVSTACK_X` is preferred; `CCSTACK_X` is
   accepted as a fallback with a one-time stderr deprecation warning.
2. **`~/.ccstack` state home** — `~/.devstack` is preferred; the existing
   `~/.ccstack` is honored only when `~/.devstack` does not exist. Data is
   never auto-moved; copy or rename the directory when upgrading.
3. **`.ccstack.json` manifest** — `.devstack.json` is preferred; the legacy
   filename is accepted as a fallback at every lookup site.
4. **`.ccstack-overrides.json` repo overrides** — `.devstack-overrides.json`
   is preferred; the legacy filename is accepted as a fallback.
5. **`ccstack.infra.*` service-id prefix** — accepted as an alias for
   `devstack.infra.*` wherever managed-infra membership is tested.
6. **Persisted JSON keys** — `devstack_managed_environment` and
   `devstack_status_notes` are written by the tool; reads accept either the
   new key or the legacy `ccstack_managed_environment` /
   `ccstack_status_notes` spellings.
7. **Dual `*-local.yml` emit** — `materialize_managed_environment` writes
   BOTH `devstack-local.yml` (canonical) and `ccstack-local.yml` (legacy
   alias) with byte-identical content so previously-built user app jars
   whose Spring config references `classpath:ccstack-local.yml` keep
   importing their overlay file.

**Docker legacy resource detection.** When devstack starts a workspace whose
new project (`devstack_<ws>`) has no containers but legacy `ccstack_<ws>`
containers / volumes / networks still exist on the host, the tool prints a
structured option-list notice with the legacy resource names and the exact
adoption / cleanup commands. devstack does NOT auto-migrate the resources;
operators run the printed commands themselves. The notice is never phrased as
a free-text yes/no question (a PostToolUse host hook rejects such phrasing).

## DB Credentials Are NOT Renamed

The MySQL / PostgreSQL / MariaDB managed databases keep the literal `ccstack`
as both username and password, and the MSSQL container keeps the literal
`Ccstack-local-1433!` password. These literals are part of the data volume's
authentication state — renaming them would invalidate existing volumes.
Operators who want devstack-named credentials must drop and recreate the
managed data volumes.

## AI Agent Usage

The installer copies skill files into:

```text
~/.codex/skills/devstack/SKILL.md            (canonical)
~/.codex/skills/ccstack/SKILL.md             (legacy alias, one release)
~/.claude/agents/skills/devstack.md          (canonical)
~/.claude/agents/skills/ccstack.md           (legacy alias, one release)
```

After installation, Codex and Claude should prefer `devstack` when users ask to start, restart, stop, or inspect local servers.

## Recommended Rollout

1. Share this branch/MR with the team.
2. Have 2-3 developers install and run `devstack status`, `devstack light contents`, and `devstack restart contents.proxy`.
3. Add missing project manifests incrementally — new manifests should use `.devstack.json`; existing `.ccstack.json` files keep working.
4. When stable, extract `tools/devstack` into a small shared tool repository or distribute it through the team's standard dotfiles/bootstrap flow.
5. Keep project-specific service definitions in each repository's `.devstack.json` (or legacy `.ccstack.json` if not yet renamed).

## Repository Directory Rename (Manual Follow-up)

This rollout does NOT rename the repository directory itself (`ccstack/`
stays). When the team is ready, the directory can be renamed with:

```bash
mv ~/Developments/ccstack ~/Developments/devstack
# update DEVSTACK_HOME in shell profile
export DEVSTACK_HOME="$HOME/Developments/devstack"
# re-run the installer so the symlinks point at the new path
~/Developments/devstack/install
```

The git repository's remote URL also keeps its current name until the team
chooses to rename the remote.

## Sharing Message

```text
로컬 MSA 서버 실행을 공통화하려고 devstack을 추가했습니다.
(기존 ccstack 이름은 한 릴리스 동안 별칭으로 계속 동작합니다.)

설치:
  tools/devstack/install

자주 쓰는 명령:
  devstack status
  devstack up contents
  devstack light contents
  devstack ui

필요할 때만 쓰는 명령:
  devstack restart contents.fixity
  devstack restart contents.proxy
  devstack logs contents.proxy -f

Codex/Claude용 skill도 같이 설치됩니다.
다른 프로젝트는 .devstack.json만 추가하면 같은 명령 체계로 붙일 수 있습니다.
기존에 .ccstack.json을 쓰던 저장소도 그대로 동작합니다.
```
