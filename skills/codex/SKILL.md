---
name: devstack
description: Use when the user asks to start, stop, restart, inspect, or manage local project servers in an MSA monorepo with devstack (legacy name ccstack), including full or lightweight startup, Docker infrastructure, application service lifecycle, logs, health checks, and current-branch restarts.
metadata:
  short-description: Manage local MSA servers
---

# devstack

Use this skill when the user asks to start, stop, restart, inspect, or manage local project servers in an MSA monorepo that has a `.devstack.json` (or legacy `.ccstack.json`) manifest. The tool was renamed from `ccstack` to `devstack`; the legacy `ccstack` command symlink and `.ccstack.json` filename remain accepted for one release.

## Prerequisites

- `devstack` is installed from `${DEVSTACK_HOME}/install`.
- `DEVSTACK_HOME` points to the shared devstack checkout, for example:

  ```bash
  export DEVSTACK_HOME="$HOME/Developments/devstack"
  export PATH="$HOME/.local/bin:$PATH"
  ```

  The legacy `CCSTACK_HOME` env var is read as a fallback for one release.

## Workflow

1. From a repository that has `.devstack.json` (or legacy `.ccstack.json`), prefer the `devstack` command over manual `bootRun`, `docker compose`, or PID commands.
2. Check current state first:

   ```bash
   devstack status
   ```

3. Start a full profile:

   ```bash
   devstack up
   devstack up app
   ```

4. Start a lightweight profile:

   ```bash
   devstack up app --light
   ```

5. Restart one service with current branch code:

   ```bash
   devstack restart app.api
   devstack restart app.web
   ```

6. Inspect logs:

   ```bash
   devstack logs app.web
   devstack logs app.api -f
   ```

7. Open the local web UI when the user wants a visual control surface:

   ```bash
   devstack ui
   devstack ui --no-open
   ```

## Rules

- Do not guess Gradle tasks or ports when `.devstack.json` defines them.
- If `devstack` is not found, tell the user to run `${DEVSTACK_HOME}/install` or export `PATH` and `DEVSTACK_HOME`.
- After `up` or `restart`, run `status` and report ports, health, and logs.
- If `devstack` reports a port conflict, surface the conflicting port and avoid killing unrelated processes unless the user asked for cleanup.
