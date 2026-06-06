# devstack

Use this skill when a user asks Claude to start, stop, restart, inspect, or manage local project servers in an MSA monorepo that has a `.devstack.json` (or legacy `.ccstack.json`) manifest.

## Workflow

Prefer the installed `devstack` command (legacy alias `ccstack` also resolves to the same binary). The shared checkout should be available through `DEVSTACK_HOME`, for example:

```bash
export DEVSTACK_HOME="$HOME/Developments/devstack"
export PATH="$HOME/.local/bin:$PATH"
```

Common commands:

```bash
devstack status
devstack up
devstack up app --light
devstack restart app.api
devstack restart app.web
devstack logs app.web
devstack down app
devstack ui
```

## Operating Rules

- Treat `.devstack.json` as the source of truth for local server tasks, ports, dependencies, health checks, and logs. The legacy `.ccstack.json` filename is accepted as a fallback for one release.
- If `devstack` is not found, tell the user to run `${DEVSTACK_HOME}/install` or export `PATH` and `DEVSTACK_HOME`.
- Use `restart <service>` to apply current branch code for a single service.
- Use `up <profile> --light` for lightweight startup when available.
- Use `ui` when the user wants a local browser control surface instead of terminal commands.
- Verify with `status` after startup or restart.
- Do not manually kill unrelated processes unless the user explicitly asks for cleanup.
