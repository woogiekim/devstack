"""Regression tests for the ccstack → devstack rename's 7 compat shims plus
the docker legacy-resource detection guidance feature.

Contract: this file is TDD failing-first. Tests are written against the
post-rename behavior; they fail on the pre-rename ccstack codebase and
pass once the rename + shims are implemented.

Shims under test:
    1. env_read(name) helper — DEVSTACK_* preferred, CCSTACK_* fallback
       with one-time stderr deprecation warning.
    2. State home resolution — prefer ~/.devstack; fall back to existing
       ~/.ccstack (do not auto-move data).
    3. Manifest discovery — .devstack.json preferred, .ccstack.json fallback.
    4. Overrides discovery — .devstack-overrides.json preferred,
       .ccstack-overrides.json fallback (repo-level).
    5. Service-id prefix alias — ccstack.infra.* accepted wherever
       devstack.infra.* is parsed/matched.
    6. Persisted JSON keys — read either ccstack_managed_environment OR
       devstack_managed_environment; write only devstack_*.
    7. Dual local-yml emit — devstack-local.yml + ccstack-local.yml with
       identical content.

Plus:
    docker_legacy_guidance — when starting a workspace whose new project
    has no containers but legacy ccstack_<ws> containers/volumes exist,
    print structured option-list guidance (NOT free-text yes/no).
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

# The tool may be named either `devstack` (post-rename) or `ccstack`
# (pre-rename). Both paths are checked so the tests stay valid through the
# git mv step. The test FILE is the same; only the source-file location
# may change.
CANDIDATES = [ROOT / "devstack", ROOT / "ccstack"]
TOOL_PATH = next((p for p in CANDIDATES if p.exists()), CANDIDATES[0])


def _load_tool():
    loader = SourceFileLoader("devstack_module_shims", str(TOOL_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


tool = _load_tool()


@contextmanager
def _clean_env(*keys):
    """Temporarily unset the given env keys for the duration of the block."""
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# Shim 1 — env_read(name) helper
# ---------------------------------------------------------------------------


class EnvReadShimTests(unittest.TestCase):
    def setUp(self) -> None:
        # The env_read helper emits its deprecation warning at most once per
        # process. Reset the warned-set between tests so each test observes
        # the warning independently.
        if hasattr(tool, "_reset_env_read_warned_for_tests"):
            tool._reset_env_read_warned_for_tests()

    def test_env_read_prefers_devstack_when_both_set(self) -> None:
        with _clean_env("DEVSTACK_HOME", "CCSTACK_HOME"):
            os.environ["DEVSTACK_HOME"] = "/new/path"
            os.environ["CCSTACK_HOME"] = "/old/path"
            self.assertEqual(tool.env_read("HOME"), "/new/path")

    def test_env_read_returns_devstack_value_when_only_devstack_set(self) -> None:
        with _clean_env("DEVSTACK_HOME", "CCSTACK_HOME"):
            os.environ["DEVSTACK_HOME"] = "/new/path"
            self.assertEqual(tool.env_read("HOME"), "/new/path")

    def test_env_read_falls_back_to_ccstack_when_only_legacy_set(self) -> None:
        with _clean_env("DEVSTACK_HOME", "CCSTACK_HOME"):
            os.environ["CCSTACK_HOME"] = "/old/path"
            with redirect_stderr(io.StringIO()) as buf:
                self.assertEqual(tool.env_read("HOME"), "/old/path")
            # Deprecation warning must mention the legacy var by full name.
            self.assertIn("CCSTACK_HOME", buf.getvalue())
            self.assertIn("DEVSTACK_HOME", buf.getvalue())

    def test_env_read_warns_only_once_per_var(self) -> None:
        with _clean_env("DEVSTACK_HOME", "CCSTACK_HOME"):
            os.environ["CCSTACK_HOME"] = "/old/path"
            with redirect_stderr(io.StringIO()) as buf1:
                tool.env_read("HOME")
            with redirect_stderr(io.StringIO()) as buf2:
                tool.env_read("HOME")
            self.assertIn("CCSTACK_HOME", buf1.getvalue())
            self.assertEqual(buf2.getvalue(), "")

    def test_env_read_returns_default_when_neither_set(self) -> None:
        with _clean_env("DEVSTACK_HOME", "CCSTACK_HOME"):
            self.assertEqual(tool.env_read("HOME", "/default"), "/default")
            self.assertIsNone(tool.env_read("HOME"))


# ---------------------------------------------------------------------------
# Shim 2 — State home resolution
# ---------------------------------------------------------------------------


class StateHomeResolutionTests(unittest.TestCase):
    """resolve_state_home(home_dir) — prefer ~/.devstack; fall back to
    ~/.ccstack only when ~/.devstack does not exist and ~/.ccstack does.
    Never auto-moves data.
    """

    def test_prefers_devstack_when_only_devstack_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".devstack").mkdir()
            self.assertEqual(tool.resolve_state_home(home), home / ".devstack")

    def test_falls_back_to_ccstack_when_only_ccstack_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".ccstack").mkdir()
            self.assertEqual(tool.resolve_state_home(home), home / ".ccstack")

    def test_prefers_devstack_when_both_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".devstack").mkdir()
            (home / ".ccstack").mkdir()
            self.assertEqual(tool.resolve_state_home(home), home / ".devstack")

    def test_returns_devstack_when_neither_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(tool.resolve_state_home(home), home / ".devstack")

    def test_does_not_move_data_on_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".ccstack").mkdir()
            (home / ".ccstack" / "marker").write_text("present")
            tool.resolve_state_home(home)
            # ccstack contents untouched
            self.assertTrue((home / ".ccstack" / "marker").exists())
            # devstack NOT auto-created
            self.assertFalse((home / ".devstack").exists())


# ---------------------------------------------------------------------------
# Shim 3 — Manifest filename discovery
# ---------------------------------------------------------------------------


class ManifestDiscoveryTests(unittest.TestCase):
    """resolve_manifest_path(root) — .devstack.json preferred,
    .ccstack.json fallback. Returns None when neither exists.
    """

    def test_prefers_devstack_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".devstack.json").write_text("{}")
            (root / ".ccstack.json").write_text("{}")
            self.assertEqual(tool.resolve_manifest_path(root),
                             root / ".devstack.json")

    def test_falls_back_to_ccstack_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".ccstack.json").write_text("{}")
            self.assertEqual(tool.resolve_manifest_path(root),
                             root / ".ccstack.json")

    def test_returns_none_when_neither_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(tool.resolve_manifest_path(Path(tmp)))


# ---------------------------------------------------------------------------
# Shim 4 — Repo-level overrides filename fallback
# ---------------------------------------------------------------------------


class OverridesDiscoveryTests(unittest.TestCase):
    """load_domain_overrides looks up .devstack-overrides.json first, then
    .ccstack-overrides.json. Either is accepted.
    """

    def test_loads_devstack_overrides_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".devstack-overrides.json").write_text(
                json.dumps({"key": "from-devstack"}))
            tool.reset_domain_overrides_cache()
            self.assertEqual(
                tool.load_domain_overrides(root, "ws-test"),
                {"key": "from-devstack"})

    def test_falls_back_to_ccstack_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".ccstack-overrides.json").write_text(
                json.dumps({"key": "from-ccstack"}))
            tool.reset_domain_overrides_cache()
            self.assertEqual(
                tool.load_domain_overrides(root, "ws-test"),
                {"key": "from-ccstack"})

    def test_devstack_overrides_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".devstack-overrides.json").write_text(
                json.dumps({"key": "from-devstack"}))
            (root / ".ccstack-overrides.json").write_text(
                json.dumps({"key": "from-ccstack"}))
            tool.reset_domain_overrides_cache()
            self.assertEqual(
                tool.load_domain_overrides(root, "ws-test"),
                {"key": "from-devstack"})


# ---------------------------------------------------------------------------
# Shim 5 — Service-id prefix alias
# ---------------------------------------------------------------------------


class ServiceIdPrefixAliasTests(unittest.TestCase):
    """ccstack.infra.* must be accepted wherever devstack.infra.* is
    parsed/matched. The helper is_managed_infra_service_name(name)
    encapsulates both forms.
    """

    def test_devstack_infra_name_recognized(self) -> None:
        self.assertTrue(tool.is_managed_infra_service_name("devstack.infra.postgres"))

    def test_ccstack_infra_name_recognized_as_alias(self) -> None:
        self.assertTrue(tool.is_managed_infra_service_name("ccstack.infra.postgres"))

    def test_unrelated_names_rejected(self) -> None:
        self.assertFalse(tool.is_managed_infra_service_name("postgres"))
        self.assertFalse(tool.is_managed_infra_service_name("foo.bar.baz"))
        self.assertFalse(tool.is_managed_infra_service_name(""))

    def test_managed_infra_prefix_constant_is_devstack(self) -> None:
        self.assertEqual(tool.MANAGED_INFRA_PREFIX, "devstack.infra")


# ---------------------------------------------------------------------------
# Shim 6 — Persisted JSON keys dual-read / new-write
# ---------------------------------------------------------------------------


class PersistedJsonKeysDualReadTests(unittest.TestCase):
    """managed_environment_from_config(config) and
    status_notes_from_service(service) accept either ccstack_* or
    devstack_* keys. Writes use only devstack_*.
    """

    def test_reads_ccstack_managed_environment(self) -> None:
        config = {"ccstack_managed_environment": {"x": 1}}
        self.assertEqual(tool.read_managed_environment(config), {"x": 1})

    def test_reads_devstack_managed_environment(self) -> None:
        config = {"devstack_managed_environment": {"y": 2}}
        self.assertEqual(tool.read_managed_environment(config), {"y": 2})

    def test_devstack_key_wins_when_both_present(self) -> None:
        config = {
            "ccstack_managed_environment": {"x": 1},
            "devstack_managed_environment": {"y": 2},
        }
        self.assertEqual(tool.read_managed_environment(config), {"y": 2})

    def test_returns_none_when_neither_present(self) -> None:
        self.assertIsNone(tool.read_managed_environment({}))

    def test_reads_ccstack_status_notes(self) -> None:
        service = {"ccstack_status_notes": ["a"]}
        self.assertEqual(tool.read_status_notes(service), ["a"])

    def test_reads_devstack_status_notes(self) -> None:
        service = {"devstack_status_notes": ["b"]}
        self.assertEqual(tool.read_status_notes(service), ["b"])

    def test_write_managed_environment_uses_devstack_key(self) -> None:
        config = {"ccstack_managed_environment": {"legacy": True}}
        tool.write_managed_environment(config, {"new": True})
        # New write uses devstack_*; legacy key is left in place
        # (do not silently delete user data).
        self.assertEqual(config.get("devstack_managed_environment"), {"new": True})


# ---------------------------------------------------------------------------
# Shim 7 — Dual local-yml emit
# ---------------------------------------------------------------------------


class DualLocalYmlEmitTests(unittest.TestCase):
    """write_managed_local_yml_dual(dir) writes BOTH
    devstack-local.yml (canonical) and ccstack-local.yml (legacy alias)
    with identical content.
    """

    def test_dual_emit_writes_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            infra = Path(tmp)
            tool.write_managed_local_yml_dual(infra)
            self.assertTrue((infra / "devstack-local.yml").is_file())
            self.assertTrue((infra / "ccstack-local.yml").is_file())

    def test_dual_emit_content_is_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            infra = Path(tmp)
            tool.write_managed_local_yml_dual(infra)
            new = (infra / "devstack-local.yml").read_bytes()
            legacy = (infra / "ccstack-local.yml").read_bytes()
            self.assertEqual(new, legacy)


# ---------------------------------------------------------------------------
# Docker legacy resource detection guidance
# ---------------------------------------------------------------------------


class DockerLegacyGuidanceTests(unittest.TestCase):
    """When starting a workspace whose new project has no containers but
    legacy ccstack_<ws> containers/volumes exist, the tool must print a
    structured option-list notice — NOT a free-text yes/no question.

    The guidance helper returns the formatted text; the test asserts the
    structure (no '...할까요?' / 'Shall I' / 'Should I' / 'Do you want me to'
    phrasing) and that the legacy resource names and the exact adoption /
    cleanup commands appear.
    """

    def test_guidance_lists_legacy_resources(self) -> None:
        text = tool.format_docker_legacy_guidance(
            workspace_name="shop",
            legacy_containers=["ccstack_shop_postgres", "ccstack_shop_redis"],
            legacy_volumes=["ccstack_shop_postgres_data"],
            legacy_networks=["ccstack_shop_net"],
        )
        self.assertIn("ccstack_shop_postgres", text)
        self.assertIn("ccstack_shop_redis", text)
        self.assertIn("ccstack_shop_postgres_data", text)
        self.assertIn("ccstack_shop_net", text)

    def test_guidance_includes_adoption_and_cleanup_commands(self) -> None:
        text = tool.format_docker_legacy_guidance(
            workspace_name="shop",
            legacy_containers=["ccstack_shop_postgres"],
            legacy_volumes=["ccstack_shop_postgres_data"],
            legacy_networks=[],
        )
        # At least one docker command suggestion is required.
        self.assertTrue(
            "docker rename" in text
            or "docker volume" in text
            or "docker rm" in text
            or "docker network" in text,
            f"Expected at least one docker adoption/cleanup command, got:\n{text}",
        )

    def test_guidance_is_structured_not_yes_no(self) -> None:
        text = tool.format_docker_legacy_guidance(
            workspace_name="shop",
            legacy_containers=["ccstack_shop_postgres"],
            legacy_volumes=[],
            legacy_networks=[],
        )
        # Forbidden free-text yes/no approval phrasings.
        forbidden_lower = [
            "할까요?",
            "shall i",
            "should i",
            "do you want me to",
            "would you like me to",
            "ok to proceed?",
            "yes/no",
            "y/n)",
        ]
        lower = text.lower()
        for needle in forbidden_lower:
            self.assertNotIn(needle, lower,
                             f"Guidance must not include yes/no phrasing: {needle!r}")

    def test_guidance_empty_when_no_legacy_resources(self) -> None:
        text = tool.format_docker_legacy_guidance(
            workspace_name="shop",
            legacy_containers=[],
            legacy_volumes=[],
            legacy_networks=[],
        )
        self.assertEqual(text, "")


# ---------------------------------------------------------------------------
# Canonical defaults sanity (post-rename baseline)
# ---------------------------------------------------------------------------


class CanonicalDefaultsTests(unittest.TestCase):
    """Surface-level constants should reflect the new canonical name."""

    def test_managed_infra_prefix_is_devstack(self) -> None:
        self.assertEqual(tool.MANAGED_INFRA_PREFIX, "devstack.infra")

    def test_compose_project_name_uses_devstack(self) -> None:
        # Workspace name "shop" => devstack_shop
        self.assertEqual(tool.managed_compose_project_name("shop"), "devstack_shop")

    def test_compose_network_name_uses_devstack(self) -> None:
        self.assertEqual(tool.managed_compose_network_name("shop"), "devstack_shop_net")

    def test_db_credential_literal_unchanged(self) -> None:
        """The DB user/password literal MUST stay 'ccstack' for volume compat.
        Postgres / mysql / mariadb all use the literal 'ccstack' as both
        username AND password."""
        for engine in ("postgresql", "mysql", "mariadb"):
            self.assertEqual(tool.MANAGED_DATABASES[engine]["username"], "ccstack",
                             f"{engine} username must remain 'ccstack' literal")
            self.assertEqual(tool.MANAGED_DATABASES[engine]["password"], "ccstack",
                             f"{engine} password must remain 'ccstack' literal")
        # MSSQL password literal is also preserved (different string).
        self.assertEqual(tool.MANAGED_DATABASES["mssql"]["password"],
                         "Ccstack-local-1433!")


if __name__ == "__main__":
    unittest.main()
