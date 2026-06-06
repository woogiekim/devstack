.PHONY: test test-smoke test-all help

PYTHON ?= python3

help:
	@echo "Available targets:"
	@echo "  test              Run unit tests"
	@echo "  test-smoke        Run API smoke tests (2xx/3xx responses only)"
	@echo "  test-all          Run unit and smoke tests"

# Run core unit tests first: this keeps fast local feedback and avoids dependency on
# runtime project state. test_devstack_ui contains the bulk of the UI / engine
# tests; test_devstack_compat_shims is the rename-specific failing-first
# regression suite added by the ccstack → devstack rename task.
test:
	$(PYTHON) -m unittest -v tests.test_devstack_ui tests.test_devstack_compat_shims tests.test_remote_config_strategies tests.test_doctor_observability

# API smoke checks are optional by default. If no workspace can be resolved from
# env/registry, tests are skipped. Set DEVSTACK_SMOKE_WORKSPACE (or
# DEVSTACK_SMOKE_WORKSPACE_ROOT, or the legacy CCSTACK_SMOKE_* aliases) before
# invoking this target to exercise a real workspace; otherwise the smoke tests
# skip cleanly.
test-smoke:
	DEVSTACK_SMOKE_HOST=$${DEVSTACK_SMOKE_HOST:-$${CCSTACK_SMOKE_HOST:-127.0.0.1}} \
	CCSTACK_SMOKE_HOST=$${CCSTACK_SMOKE_HOST:-$${DEVSTACK_SMOKE_HOST:-127.0.0.1}} \
	$(PYTHON) -m unittest -v tests.test_devstack_smoke_api

test-all:
	$(MAKE) test
	$(MAKE) test-smoke
