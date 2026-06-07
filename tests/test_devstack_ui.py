import importlib.util
from importlib.machinery import SourceFileLoader
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
# Pick the renamed devstack file when present (post git mv);
# fall back to ccstack for any in-progress checkout.
DEVSTACK_PATH = ROOT / "devstack" if (ROOT / "devstack").exists() else ROOT / "ccstack"
CCSTACK_PATH = DEVSTACK_PATH  # legacy alias for older tests
SHOPPING_OVERRIDES_SOURCE = ROOT / "examples" / "overrides" / "sample-shop" / ".ccstack-overrides.json"


def load_ccstack():
    loader = SourceFileLoader("devstack_module", str(CCSTACK_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


ccstack = load_ccstack()


def deploy_shopping_overrides(workspace_root: Path) -> Path:
    """Copy the shipped shopping overrides into a test workspace root.

    Mirrors the supported real-world deployment: a workspace ships its own
    ``.ccstack-overrides.json`` at the repo root, and ccstack's engine reads
    it via ``load_domain_overrides``. Returns the destination path for
    assertion convenience.

    The function also clears the module-level ``_DOMAIN_OVERRIDES_CACHE`` so
    consecutive test cases that share a tempfile-based workspace key do not
    re-use a cached empty dict from a prior test.
    """
    workspace_root.mkdir(parents=True, exist_ok=True)
    dest = workspace_root / ".ccstack-overrides.json"
    shutil.copyfile(SHOPPING_OVERRIDES_SOURCE, dest)
    ccstack.reset_domain_overrides_cache()
    return dest


def shopping_overrides_dict() -> dict:
    """Return the shipped shopping overrides as a parsed dict for direct
    injection into engine functions that accept an ``overrides`` argument."""
    with SHOPPING_OVERRIDES_SOURCE.open("r", encoding="utf-8") as f:
        return json.load(f)


class CcstackUiTest(unittest.TestCase):
    def test_team_rollout_documents_deployability_invariant(self):
        text = (ROOT / "TEAM_ROLLOUT.md").read_text()

        self.assertIn("Team Deployability Invariant", text)
        self.assertIn("Do not ship a fix whose only effect is editing", text)
        # After the rename, the canonical local-state path is ``~/.devstack``;
        # the older ``~/.ccstack`` path is also documented as the legacy
        # upgrade source.
        self.assertIn("clean `~/.devstack` state can regenerate", text)
        self.assertIn("~/.ccstack", text)

    def test_datasource_scan_keeps_late_module_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(25):
                resource_dir = root / "apps" / f"service-{index}" / "src" / "main" / "resources"
                resource_dir.mkdir(parents=True)
                (resource_dir / "application-local.yml").write_text(
                    "\n".join(
                        [
                            "spring:",
                            "  datasource:",
                            f"    url: jdbc:mysql://${{db.service{index}.host}}/${{db.service{index}.db}}",
                        ]
                    )
                    + "\n"
                )

            datasources = ccstack.inspect_datasource_configs(root)

        self.assertEqual(len(datasources), 25)
        self.assertTrue(any("service-24" in item["path"] for item in datasources))

    def test_duplicate_generated_database_still_configures_each_service_datasource(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {
                "workspace": "shop",
                "profiles": {"default": {"services": ["apps.a", "apps.b"]}},
                "services": {
                    "apps.a": {
                        "type": "gradle",
                        "task": ":apps:a:bootRun",
                        "module": "apps/a",
                    },
                    "apps.b": {
                        "type": "gradle",
                        "task": ":apps:b:bootRun",
                        "module": "apps/b",
                    },
                },
            }
            persistence = {
                "datasources": [
                    {
                        "path": "apps/a/src/main/resources/application-local.yml",
                        "key": "spring.datasource.alpha.url",
                        "url": "jdbc:mysql://${db.alpha.host}/${db.alpha.db}",
                    },
                    {
                        "path": "apps/b/src/main/resources/application-local.yml",
                        "key": "spring.datasource.dpgcms.master.url",
                        "url": "jdbc:mysql://${db.boardmain.dbBoard.host}/${db.boardmain.dbBoard.db}",
                    },
                ],
            }

            plan = ccstack.build_local_environment_plan(root, "shop", config, persistence)

        generated = [item for item in plan["requirements"] if item["status"] == "generate"]
        configured = [item for item in plan["requirements"] if item["status"] == "configure"]
        self.assertEqual(len(generated), 1)
        self.assertEqual(len(configured), 1)
        self.assertTrue(all(item["technology"] == "mysql" for item in generated + configured))
        self.assertTrue(all("H2" not in item["reason"] for item in generated + configured))
        self.assertEqual(generated[0]["image"], "mysql:8.4")

        service_b_env = {
            item["name"]: item["value"]
            for item in plan["env"]
            if item["service"] == "apps.b"
        }
        self.assertIn("SPRING_DATASOURCE_DPGCMS_MASTER_URL", service_b_env)
        self.assertIn("jdbc:mysql://localhost:3306/", service_b_env["SPRING_DATASOURCE_DPGCMS_MASTER_URL"])
        self.assertEqual(service_b_env["DB_BOARDMAIN_DBBOARD_HOST"], "localhost")
        self.assertEqual(service_b_env["DB_BOARDMAIN_DBBOARD_USERNAME"], "ccstack")
        self.assertEqual(service_b_env["DB_BOARDMAIN_DBBOARD_PASSWORD"], "ccstack")

    def test_workspace_setup_generates_local_manifest_from_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shop"
            root.mkdir()
            (root / "compose.yml").write_text(
                """
services:
  db:
    image: postgres:16
  api:
    image: example/api
""".strip()
                + "\n"
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                name, manifest_path, source = ccstack.prepare_workspace_config(root, None, False)
                registry = ccstack.WorkspaceRegistry.load()
                registry.register(name, root, manifest_path)
                registry.default = name
                registry.save()

                workspace = ccstack.Workspace.load(argparse.Namespace(root=None, workspace=name))

        self.assertEqual(name, "shop")
        self.assertEqual(source, "generated")
        self.assertFalse((root / ".ccstack.json").exists())
        self.assertEqual(workspace.name, "shop")
        self.assertEqual(sorted(workspace.services), ["compose.api", "compose.db"])
        self.assertEqual(workspace.resolve_target("default"), ["compose.api", "compose.db"])

    def test_workspace_setup_disambiguates_duplicate_default_workspace_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            first_root = Path(tmp) / "team-a" / "shop"
            second_root = Path(tmp) / "team-b" / "shop"
            first_root.mkdir(parents=True)
            second_root.mkdir(parents=True)
            (first_root / "compose.yml").write_text("services:\n  db:\n    image: postgres:16\n")
            (second_root / "compose.yml").write_text("services:\n  db:\n    image: postgres:16\n")

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                first_name, first_manifest, first_source = ccstack.prepare_workspace_config(first_root, None, False)
                registry = ccstack.WorkspaceRegistry.load()
                registry.register(first_name, first_root, first_manifest if first_source == "generated" else None)
                registry.save()

                second_name, second_manifest, second_source = ccstack.prepare_workspace_config(second_root, None, False)
                second_config = json.loads(second_manifest.read_text())
                registry.register(second_name, second_root, second_manifest if second_source == "generated" else None)
                registry.save()

        self.assertEqual(first_name, "shop")
        self.assertEqual(second_name, f"shop-{ccstack.workspace_root_digest(second_root)}")
        self.assertEqual(second_config["workspace"], second_name)
        self.assertEqual(set(registry.workspaces), {first_name, second_name})

    def test_workspace_setup_rejects_explicit_duplicate_workspace_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            first_root = Path(tmp) / "team-a" / "shop"
            second_root = Path(tmp) / "team-b" / "shop"
            first_root.mkdir(parents=True)
            second_root.mkdir(parents=True)

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                registry = ccstack.WorkspaceRegistry.load()
                registry.register("shop", first_root)
                registry.save()

                with self.assertRaises(ccstack.CcstackError) as error:
                    ccstack.prepare_workspace_config(second_root, "shop", False)

        self.assertIn("workspace name 'shop' is already registered", str(error.exception))

    def test_workspace_draft_disambiguates_duplicate_repo_manifest_workspace_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            first_root = Path(tmp) / "team-a" / "shopping"
            second_root = Path(tmp) / "team-b" / "shopping"
            first_root.mkdir(parents=True)
            second_root.mkdir(parents=True)
            (first_root / ".ccstack.json").write_text(
                '{"workspace": "shopping", "profiles": {"default": {"services": []}}, "services": {}}\n'
            )
            (second_root / ".ccstack.json").write_text(
                '{"workspace": "shopping", "profiles": {"default": {"services": []}}, "services": {}}\n'
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                registry = ccstack.WorkspaceRegistry.load()
                registry.register("shopping", first_root)
                registry.save()

                draft = ccstack.analyze_workspace_draft(str(second_root))
                result = ccstack.apply_workspace_draft(draft["draft_id"])
                loaded_registry = ccstack.WorkspaceRegistry.load()
                generated_config = json.loads(Path(result["config"]).read_text())

        expected_name = f"shopping-{ccstack.workspace_root_digest(second_root)}"
        self.assertEqual(draft["workspace"], expected_name)
        self.assertEqual(result["name"], expected_name)
        self.assertEqual(result["config_source"], "managed")
        self.assertEqual(generated_config["workspace"], expected_name)
        self.assertEqual(
            set(loaded_registry.workspaces),
            {"shopping", expected_name},
        )

    def test_workspace_setup_preserves_existing_repo_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shop"
            root.mkdir()
            manifest = root / ".ccstack.json"
            manifest.write_text(
                '{"workspace": "team-shop", "profiles": {"default": {"services": []}}, "services": {}}\n'
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                name, manifest_path, source = ccstack.prepare_workspace_config(root, None, False)
                registry = ccstack.WorkspaceRegistry.load()
                registry.register(name, root, manifest_path if source == "generated" else None)
                registry.save()

        self.assertEqual(name, "team-shop")
        self.assertEqual(manifest_path, manifest)
        self.assertEqual(source, "repo")
        self.assertEqual(registry.manifests, {})

    def test_workspace_setup_generates_gradle_defaults_from_clean_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            module = root / "contents-api"
            resources = module / "src" / "main" / "resources"
            resources.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':contents-api'\n")
            (module / "build.gradle").write_text("plugins { id 'org.springframework.boot' version '3.2.0' }\n")
            (resources / "application.yml").write_text("server:\n  port: 18081\n")

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                name, manifest_path, source = ccstack.prepare_workspace_config(root, "shopping", False)
                config = json.loads(manifest_path.read_text())
                registry = ccstack.WorkspaceRegistry.load()
                registry.register(name, root, manifest_path if source == "generated" else None)
                registry.default = name
                registry.save()

                loaded = ccstack.Workspace.load(argparse.Namespace(root=None, workspace=name))

        self.assertEqual(name, "shopping")
        self.assertEqual(source, "generated")
        self.assertEqual(config["gradle_args"], loaded.config["gradle_args"])
        self.assertIn("--max-workers=1", config["gradle_args"])
        self.assertIn("-Dorg.gradle.workers.max=1", config["gradle_args"])
        self.assertIn(
            "-Dorg.gradle.jvmargs=-Xmx1024m -XX:MaxMetaspaceSize=512m -Dfile.encoding=UTF-8",
            config["gradle_args"],
        )
        self.assertNotIn("--init-script", config["gradle_args"])

    def test_workspace_setup_materializes_repo_gradle_manifest_without_editing_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            root.mkdir()
            manifest = root / ".ccstack.json"
            manifest_text = json.dumps(
                {
                    "workspace": "shopping",
                    "profiles": {"default": {"services": ["contents-api"]}},
                    "services": {
                        "contents-api": {
                            "type": "gradle",
                            "task": ":contents-api:bootRun",
                            "port": 18081,
                        },
                    },
                },
                indent=2,
            ) + "\n"
            manifest.write_text(manifest_text)

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                name, manifest_path, source = ccstack.prepare_workspace_config(root, None, False)
                config = json.loads(manifest_path.read_text())
                manifest_after = manifest.read_text()

        self.assertEqual(name, "shopping")
        self.assertEqual(source, "generated")
        self.assertEqual(manifest_after, manifest_text)
        self.assertNotEqual(manifest_path, manifest)
        self.assertIn("--max-workers=1", config["gradle_args"])

    def test_ui_workspace_wizard_analyzes_and_applies_arbitrary_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "acme" / "shopping"
            root.mkdir(parents=True)
            (root / "compose.yml").write_text(
                """
services:
  db:
    image: postgres:16
""".strip()
                + "\n"
            )
            (root / "db").mkdir()
            (root / "db" / "schema.sql").write_text("create table item(id bigint);\n")
            (root / "db" / "data.sql").write_text("insert into item(id) values (1);\n")
            entity_dir = root / "src" / "main" / "java" / "com" / "example"
            entity_dir.mkdir(parents=True)
            (entity_dir / "Item.java").write_text(
                """
package com.example;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

@Entity
@Table(name = "item")
public class Item {
    @Id
    @Column(name = "id")
    private Long id;

    @Column(name = "title")
    private String title;
}
""".strip()
                + "\n"
            )
            (entity_dir / "ItemRepository.java").write_text(
                """
package com.example;

import org.springframework.data.jpa.repository.JpaRepository;

public interface ItemRepository extends JpaRepository<Item, Long> {
}
""".strip()
                + "\n"
            )
            mapper_dir = root / "src" / "main" / "resources" / "mapper"
            mapper_dir.mkdir(parents=True)
            (mapper_dir / "ItemMapper.xml").write_text(
                """
<mapper namespace="com.example.ItemMapper">
  <select id="findAll" resultType="com.example.Item">
    select id, title from item
  </select>
</mapper>
""".strip()
                + "\n"
            )
            exposed_dir = root / "src" / "main" / "kotlin" / "com" / "example"
            exposed_dir.mkdir(parents=True)
            (exposed_dir / "OrdersTable.kt").write_text(
                """
package com.example

import org.jetbrains.exposed.sql.Table
import org.jetbrains.exposed.sql.insert
import org.jetbrains.exposed.sql.selectAll

object Orders : Table("orders") {
    val id = long("id")
    val itemId = long("item_id")
    val status = varchar("status", 30)
}

class OrderRepository {
    fun findAll() = Orders.selectAll()
    fun save(status: String) = Orders.insert { it[Orders.status] = status }
}
""".strip()
                + "\n"
            )
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                draft_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_analyze"],
                        "workspace_root": [str(root)],
                    },
                )
                apply_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_apply"],
                        "draft_id": [draft_result["draft_id"]],
                    },
                )
                registry = ccstack.WorkspaceRegistry.load()
                loaded = ccstack.Workspace.load(argparse.Namespace(root=None, workspace="shopping"))

        self.assertTrue(draft_result["ok"])
        self.assertIn("Persistence model:", draft_result["output"])
        self.assertIn("Data access:", draft_result["output"])
        self.assertIn("SQL file evidence (secondary):", draft_result["output"])
        self.assertEqual(draft_result["draft"]["analysis"]["confidence"], "high")
        persistence = draft_result["draft"]["analysis"]["persistence"]
        self.assertEqual(persistence["tables"][0]["name"], "item")
        self.assertEqual(
            [column["name"] for column in persistence["tables"][0]["columns"]],
            ["id", "title"],
        )
        self.assertIn("orders", [table["name"] for table in persistence["tables"]])
        exposed = persistence["exposed"]
        self.assertEqual(exposed["tables"][0]["table"], "orders")
        self.assertEqual(
            [column["name"] for column in exposed["tables"][0]["columns"]],
            ["id", "item_id", "status"],
        )
        self.assertEqual(
            sorted(query["operation"] for query in exposed["queries"]),
            ["insert", "selectAll"],
        )
        self.assertEqual(persistence["repositories"][0]["table"], "item")
        self.assertEqual(persistence["mappers"][0]["tables"], ["item"])
        self.assertTrue(apply_result["ok"])
        self.assertEqual(apply_result["workspace"], "shopping")
        self.assertEqual(registry.default, "shopping")
        self.assertEqual(Path(registry.workspaces["shopping"]), root.resolve())
        self.assertFalse((root / ".ccstack.json").exists())
        self.assertEqual(loaded.name, "shopping")
        self.assertEqual(loaded.resolve_target("default"), ["compose.db"])

    def test_ui_workspace_apply_populates_gradle_server_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            module = root / "contents-api"
            resources = module / "src" / "main" / "resources"
            entity_dir = module / "src" / "main" / "java" / "com" / "example"
            resources.mkdir(parents=True)
            entity_dir.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':contents-api'\n")
            (module / "build.gradle").write_text("plugins { id 'org.springframework.boot' version '3.2.0' }\n")
            (resources / "application.yml").write_text("server:\n  port: 18081\n")
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                draft_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_analyze"],
                        "workspace_root": [str(root)],
                    },
                )
                apply_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_apply"],
                        "draft_id": [draft_result["draft_id"]],
                    },
                )
                loaded = ccstack.Workspace.load(argparse.Namespace(root=None, workspace="shopping"))
                page = ccstack.ui_page(loaded, ccstack.WorkspaceRegistry.load(), False)

        self.assertTrue(draft_result["ok"])
        self.assertTrue(apply_result["ok"])
        self.assertEqual(loaded.resolve_target("default"), ["contents-api"])
        self.assertEqual(loaded.services["contents-api"]["type"], "gradle")
        self.assertEqual(loaded.services["contents-api"]["task"], ":contents-api:bootRun")
        self.assertEqual(loaded.services["contents-api"]["port"], 18081)
        self.assertIn("--max-workers=1", loaded.config["gradle_args"])
        self.assertIn("-Dorg.gradle.workers.max=1", loaded.config["gradle_args"])
        self.assertIn(
            "-Dorg.gradle.jvmargs=-Xmx1024m -XX:MaxMetaspaceSize=512m -Dfile.encoding=UTF-8",
            loaded.config["gradle_args"],
        )
        self.assertNotIn("--init-script", loaded.config["gradle_args"])
        self.assertIn("managed Gradle resource limits: enabled", apply_result["output"])
        self.assertIn('id="workspaceLifecycleState">Applied</strong>', page)
        self.assertIn('id="draftStatus">Applied</strong>', page)
        self.assertIn('const workspaceInitialStep = "apply";', page)
        self.assertIn('setWizardStep(workspaceInitialStep)', page)
        self.assertIn('let workspaceReady = true;', page)
        self.assertIn('function markWorkspaceApplied', page)
        self.assertIn('id="workspaceStatusPill"', page)
        self.assertIn('markWorkspaceApplied(data.workspace || workspace.value)', page)
        self.assertIn('window.setTimeout(() => {', page)
        self.assertIn('shopping · applied', page)
        self.assertNotIn('shopping · ready', page)
        self.assertNotIn('<option value="default">default</option>', page)
        self.assertIn('<option value="contents-api">contents-api</option>', page)

    def test_ui_service_management_controls_are_consolidated(self):
        workspace = ccstack.Workspace(
            Path("/tmp/shop"),
            {
                "workspace": "shop",
                "profiles": {"default": {"services": ["api"]}},
                "services": {
                    "api": {
                        "type": "gradle",
                        "task": ":api:bootRun",
                        "port": 18081,
                        "health": "http://localhost:18081/actuator/health",
                    },
                },
            },
        )

        page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry(), False)

        self.assertEqual(page.count(">Start target<"), 1)
        self.assertEqual(page.count(">Stop target<"), 1)
        self.assertIn('class="state-overview"', page)
        self.assertIn('id="servicesAttentionCount"', page)
        state_style = page[page.index("    .state-overview {"):page.index("    .workspace-toolbar {")]
        self.assertIn("display: flex;", state_style)
        self.assertIn("grid-template-columns: 8px auto auto;", state_style)
        self.assertIn(".state-dot {", state_style)
        self.assertIn(".state-item.attention .state-dot", state_style)
        self.assertIn(".state-item.stopped .state-dot", state_style)
        self.assertNotIn("border: 1px solid", state_style)
        self.assertNotIn("background: #fbfdff", state_style)
        self.assertIn('class="state-dot" aria-hidden="true"', page)
        self.assertIn('class="state-item stopped"', page)
        self.assertNotIn('class="operations-strip"', page)
        self.assertIn('id="service" hidden', page)

    def test_workspace_apply_merges_repo_manifest_with_detected_gradle_servers_and_unique_ports(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            contents = root / "apps" / "contents"
            orders = root / "apps" / "orders"
            for module, class_name in ((contents, "ContentsApplication"), (orders, "OrdersApplication")):
                resources = module / "src" / "main" / "resources"
                source = module / "src" / "main" / "java" / "com" / "example"
                resources.mkdir(parents=True)
                source.mkdir(parents=True)
                (module / "build.gradle").write_text(
                    """
plugins { id 'org.springframework.boot' version '3.2.0' }
dependencies {
  implementation 'org.springframework.boot:spring-boot-starter-actuator'
}
""".strip()
                    + "\n"
                )
                (resources / "application.yml").write_text("server:\n  port: 18081\n")
                (source / f"{class_name}.java").write_text(
                    f"""
package com.example;

import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class {class_name} {{
}}
""".strip()
                    + "\n"
                )
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':apps:contents'\ninclude ':apps:orders'\n")
            (root / ".ccstack.json").write_text(
                """
{
  "workspace": "shopping",
  "profiles": {
    "default": {
      "services": ["contents"]
    }
  },
  "services": {
    "contents": {
      "type": "gradle",
      "task": ":apps:contents:bootRun",
      "module": "apps/contents",
      "port": 18081,
      "health": "http://localhost:18081/actuator/health"
    }
  }
}
""".strip()
                + "\n"
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                draft = ccstack.analyze_workspace_draft(str(root))
                result = ccstack.apply_workspace_draft(draft["draft_id"])
                loaded = ccstack.Workspace.load(argparse.Namespace(root=None, workspace="shopping"))

        self.assertEqual(result["config_source"], "managed")
        self.assertEqual(loaded.resolve_target("default"), ["contents", "apps.orders"])
        self.assertEqual(loaded.services["contents"]["port"], 18081)
        self.assertEqual(loaded.services["apps.orders"]["type"], "gradle")
        self.assertEqual(loaded.services["apps.orders"]["task"], ":apps:orders:bootRun")
        self.assertEqual(loaded.services["apps.orders"]["port"], 18082)
        self.assertEqual(loaded.services["apps.orders"]["health"], "http://localhost:18082/actuator/health")
        self.assertEqual(loaded.services["apps.orders"]["env"]["SERVER_PORT"], "18082")
        self.assertIn("--server.port=18082", loaded.services["apps.orders"]["args"])
        self.assertEqual(
            len({service["port"] for service in loaded.services.values() if service.get("port")}),
            2,
        )

    def test_workspace_apply_avoids_ports_reserved_by_registered_workspaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            other_root = Path(tmp) / "contents-system"
            root = Path(tmp) / "shopping"
            other_root.mkdir()
            module = root / "contents-api"
            resources = module / "src" / "main" / "resources"
            source = module / "src" / "main" / "java" / "com" / "example"
            resources.mkdir(parents=True)
            source.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':contents-api'\n")
            (module / "build.gradle").write_text(
                """
plugins { id 'org.springframework.boot' version '3.2.0' }
dependencies {
  implementation 'org.springframework.boot:spring-boot-starter-actuator'
  implementation 'org.springframework.boot:spring-boot-starter-data-redis'
}
""".strip()
                + "\n"
            )
            (resources / "application.yml").write_text(
                """
server:
  port: 8080
spring:
  data:
    redis:
      host: localhost
""".strip()
                + "\n"
            )
            (source / "ContentsApplication.java").write_text(
                """
package com.example;

import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class ContentsApplication {
}
""".strip()
                + "\n"
            )
            (source / "RedisConfig.kt").write_text("fun redis() = config.useClusterServers()\n")

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                other_manifest = ccstack.generated_manifest_path("contents-system")
                ccstack.write_json(
                    other_manifest,
                    {
                        "workspace": "contents-system",
                        "profiles": {"default": {"services": ["apps.proxy.contents", "ccstack.infra.redis"]}},
                        "services": {
                            "apps.proxy.contents": {"type": "gradle", "port": 8080},
                            "apps.proxy.redirect": {"type": "gradle", "port": 8081},
                            "ccstack.infra.redis": {"type": "compose", "port": 6379},
                            "ccstack.infra.opensearch": {"type": "compose", "port": 9200},
                            "ccstack.infra.mock-http": {"type": "compose", "port": 1080},
                        },
                    },
                )
                registry = ccstack.WorkspaceRegistry.load()
                registry.register("contents-system", other_root, other_manifest)
                registry.save()

                draft = ccstack.analyze_workspace_draft(str(root))
                result = ccstack.apply_workspace_draft(draft["draft_id"])
                loaded = ccstack.Workspace.load(argparse.Namespace(root=None, workspace="shopping"))
                compose_text = ccstack.managed_infra_compose_path("shopping").read_text()

        self.assertEqual(result["config_source"], "managed")
        self.assertEqual(loaded.services["contents-api"]["port"], 8082)
        self.assertEqual(loaded.services["contents-api"]["health"], "http://localhost:8082/actuator/health")
        self.assertEqual(loaded.services["contents-api"]["env"]["SERVER_PORT"], "8082")
        self.assertIn("--server.port=8082", loaded.services["contents-api"]["args"])
        self.assertEqual(loaded.services["devstack.infra.redis"]["port"], 6380)
        self.assertEqual(loaded.services["devstack.infra.redis"]["project"], "devstack_shopping")
        self.assertNotIn(
            8080,
            {service["port"] for service in loaded.services.values() if service.get("port")},
        )
        self.assertNotIn(
            6379,
            {service["port"] for service in loaded.services.values() if service.get("port")},
        )
        self.assertIn('"6380:6379"', compose_text)
        self.assertNotIn('"6379:6379"', compose_text)
        self.assertIn("--cluster-announce-port 6380", compose_text)

    def test_workspace_analysis_assigns_port_to_web_modules_without_server_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            module = root / "apps" / "proxy" / "shopping"
            source = module / "src" / "main" / "kotlin" / "com" / "example"
            source.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':apps:proxy:shopping'\n")
            (module / "build.gradle.kts").write_text(
                """
plugins { id("org.springframework.boot") }
dependencies {
  implementation("org.springframework.boot:spring-boot-starter-web")
  implementation("org.springframework.boot:spring-boot-starter-actuator")
}
""".strip()
                + "\n"
            )
            (source / "ShoppingApplication.kt").write_text(
                """
package com.example

import org.springframework.boot.autoconfigure.SpringBootApplication
import org.springframework.boot.runApplication

@SpringBootApplication
class ShoppingApplication

fun main(args: Array<String>) {
    runApplication<ShoppingApplication>(*args)
}
""".strip()
                + "\n"
            )

            config = ccstack.inspect_workspace(root, "shopping", reserved_ports={8080, 8081})

        service = config["services"]["apps.proxy.shopping"]
        self.assertEqual(service["port"], 8082)
        self.assertEqual(service["health"], "http://localhost:8082/actuator/health")
        self.assertEqual(service["env"]["SERVER_PORT"], "8082")
        self.assertEqual(service["env"]["MANAGEMENT_SERVER_PORT"], "8082")
        self.assertIn("--server.port=8082", service["args"])

    def test_workspace_apply_generates_auto_run_bundles_from_service_families(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            modules = [
                ("apps/proxy/acme/contents", "ProxyContentsApplication"),
                ("apps/proxy/acme/member", "ProxyMemberApplication"),
                ("apps/fixity-pricecompare/products", "ProductsApplication"),
                ("apps/fixity-pricecompare/catalog", "CatalogApplication"),
                ("apps/fixity-events/events", "EventsApplication"),
            ]
            for module_path, class_name in modules:
                module = root / module_path
                resources = module / "src" / "main" / "resources"
                source = module / "src" / "main" / "java" / "com" / "example"
                resources.mkdir(parents=True)
                source.mkdir(parents=True)
                (module / "build.gradle").write_text(
                    "plugins { id 'org.springframework.boot' version '3.2.0' }\n"
                )
                (resources / "application.yml").write_text("server:\n  port: 18081\n")
                (source / f"{class_name}.java").write_text(
                    f"""
package com.example;

import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class {class_name} {{
}}
""".strip()
                    + "\n"
                )
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text(
                "\n".join(f"include ':{path.replace('/', ':')}'" for path, _ in modules) + "\n"
            )
            (root / ".ccstack.json").write_text(
                """
{
  "workspace": "shopping",
  "profiles": {
    "default": {
      "services": []
    },
    "proxy.acme": {
      "services": ["apps.proxy.acme.contents", "apps.proxy.acme.member"]
    },
	    "fixity-pricecompare": {
	      "services": ["apps.fixity-pricecompare.catalog", "apps.fixity-pricecompare.products"]
	    },
	    "integration": {
	      "services": ["apps.proxy.acme.contents"]
	    },
	    "infra": {
	      "services": ["ccstack.infra.kafka", "ccstack.infra.redis"]
	    }
  },
  "services": {
    "ccstack.infra.redis": {
      "type": "compose",
      "compose": "docker-compose.yml",
      "service": "redis"
    },
    "ccstack.infra.kafka": {
      "type": "compose",
      "compose": "docker-compose.yml",
      "service": "kafka"
    }
  }
}
""".strip()
                + "\n"
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                draft = ccstack.analyze_workspace_draft(str(root))
                ccstack.apply_workspace_draft(draft["draft_id"])
                loaded = ccstack.Workspace.load(argparse.Namespace(root=None, workspace="shopping"))

        self.assertEqual(
            loaded.resolve_target("proxy"),
            ["apps.proxy.acme.contents", "apps.proxy.acme.member"],
        )
        self.assertEqual(
            loaded.resolve_target("fixity"),
            [
                "apps.fixity-events.events",
                "apps.fixity-pricecompare.catalog",
                "apps.fixity-pricecompare.products",
            ],
        )
        # Shim 5 — when the input workspace manifest used the legacy
        # ``ccstack.infra.*`` prefix, the resolved profile targets preserve
        # those legacy names. Future migration tooling can canonicalize.
        self.assertEqual(
            loaded.resolve_target("integration"),
            ["ccstack.infra.kafka", "ccstack.infra.redis"],
        )
        self.assertNotIn("proxy.acme", loaded.profiles)
        self.assertNotIn("fixity-pricecompare", loaded.profiles)
        self.assertNotIn("fixity-events", loaded.profiles)
        self.assertNotIn("infra", loaded.profiles)
        self.assertNotIn(":apps:proxy:acme:contents:bootRun", loaded.profiles)

    def test_workspace_apply_generates_managed_database_infra_from_datasource(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            module = root / "contents-api"
            resources = module / "src" / "main" / "resources"
            entity_dir = module / "src" / "main" / "java" / "com" / "example"
            resources.mkdir(parents=True)
            entity_dir.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':contents-api'\n")
            (module / "build.gradle").write_text("plugins { id 'org.springframework.boot' version '3.2.0' }\n")
            (resources / "application.yml").write_text(
                """
server:
  port: 18081
spring:
  datasource:
    url: jdbc:postgresql://localhost:5432/shop
    username: app
    password: app
""".strip()
                + "\n"
            )
            (entity_dir / "Item.java").write_text(
                """
package com.example;

import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

@Entity
@Table(name = "item")
public class Item {
    @Id
    private Long id;
}
""".strip()
                + "\n"
            )
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                draft_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_analyze"],
                        "workspace_root": [str(root)],
                    },
                )
                apply_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_apply"],
                        "draft_id": [draft_result["draft_id"]],
                    },
                )
                loaded = ccstack.Workspace.load(argparse.Namespace(root=None, workspace="shopping"))
                compose_path = Path(loaded.services["devstack.infra.postgres"]["compose"])
                env_path = ccstack.managed_env_path("shopping")
                schema_path = ccstack.managed_schema_path("shopping", "postgresql")
                dml_path = ccstack.managed_dml_path("shopping", "postgresql")
                compose_exists = compose_path.exists()
                compose_text = compose_path.read_text() if compose_exists else ""
                env_exists = env_path.exists()
                schema_text = schema_path.read_text()
                dml_exists = dml_path.exists()

        self.assertTrue(draft_result["ok"])
        self.assertIn("Local environment plan:", draft_result["output"])
        self.assertIn("DDL/DML plan:", draft_result["output"])
        self.assertIn("DDL: infer 1 table(s) from persistence code", draft_result["output"])
        self.assertIn("generate database postgresql as devstack.infra.postgres", draft_result["output"])
        self.assertTrue(apply_result["ok"])
        self.assertIn("managed infra compose:", apply_result["output"])
        self.assertIn("devstack.infra.postgres", loaded.services)
        self.assertEqual(loaded.services["devstack.infra.postgres"]["service"], "postgres")
        self.assertIn("devstack.infra.postgres", loaded.services["contents-api"]["depends_on"])
        self.assertEqual(
            loaded.services["contents-api"]["env"]["SPRING_DATASOURCE_URL"],
            "jdbc:postgresql://localhost:5432/shop",
        )
        self.assertTrue(compose_exists)
        self.assertIn("postgres:16-alpine", compose_text)
        self.assertIn("/docker-entrypoint-initdb.d/010-devstack-local-schema.sql", compose_text)
        self.assertNotIn("/docker-entrypoint-initdb.d/020-devstack-local-dml.sql", compose_text)
        self.assertNotIn("volumes:\n  /", compose_text)
        self.assertIn('CREATE TABLE IF NOT EXISTS "item"', schema_text)
        self.assertFalse(dml_exists)
        self.assertTrue(env_exists)

    def test_workspace_apply_generates_managed_mssql_infra_for_sqlserver_datasource(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            module = root / "contents-api"
            resources = module / "src" / "main" / "resources"
            entity_dir = module / "src" / "main" / "java" / "com" / "example"
            resources.mkdir(parents=True)
            entity_dir.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':contents-api'\n")
            (module / "build.gradle").write_text("plugins { id 'org.springframework.boot' version '3.2.0' }\n")
            (resources / "application-local.yml").write_text(
                """
server:
  port: 18081
spring:
  datasource:
    vendor1:
      jdbc-url: jdbc:sqlserver://192.168.213.128:1433;databaseName=VENDOR1
      username: remote
      password: secret
""".strip()
                + "\n"
            )
            (entity_dir / "Article.java").write_text(
                """
package com.example;

import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

@Entity
@Table(name = "NOTES_BOX")
public class Article {
    @Id
    private Long kbNo;
}
""".strip()
                + "\n"
            )
            # Deploy the shipped shopping overrides into the workspace root so
            # the generic engine produces the historical notes_box seed DML.
            deploy_shopping_overrides(root)
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                draft_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_analyze"],
                        "workspace_root": [str(root)],
                    },
                )
                apply_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_apply"],
                        "draft_id": [draft_result["draft_id"]],
                    },
                )
                loaded = ccstack.Workspace.load(argparse.Namespace(root=None, workspace="shopping"))
                mssql_service = loaded.services["devstack.infra.mssql"]
                compose_path = Path(mssql_service["compose"])
                schema_path = ccstack.managed_schema_path("shopping", "mssql")
                dml_path = ccstack.managed_dml_path("shopping", "mssql")
                compose_text = compose_path.read_text()
                schema_text = schema_path.read_text()
                dml_exists = dml_path.exists()

        service_env = loaded.services["contents-api"]["env"]
        self.assertTrue(draft_result["ok"])
        self.assertTrue(apply_result["ok"])
        self.assertIn("devstack.infra.mssql", loaded.services)
        self.assertEqual(loaded.services["devstack.infra.mssql"]["service"], "mssql")
        self.assertEqual(mssql_service["engine"], "mssql")
        self.assertEqual(mssql_service["database"], "VENDOR1")
        self.assertEqual(mssql_service["username"], "sa")
        self.assertEqual(mssql_service["password"], "Ccstack-local-1433!")
        self.assertEqual(mssql_service["init_schema"], "/devstack-init/010-devstack-local-schema.sql")
        self.assertIn("devstack.infra.mssql", loaded.services["contents-api"]["depends_on"])
        self.assertIn("mcr.microsoft.com/mssql/server:2022-latest", compose_text)
        self.assertIn('ACCEPT_EULA: "Y"', compose_text)
        self.assertIn("MSSQL_SA_PASSWORD", compose_text)
        self.assertIn("/devstack-init/010-devstack-local-schema.sql", compose_text)
        self.assertIn("IF OBJECT_ID(N'NOTES_BOX', N'U') IS NULL", schema_text)
        self.assertIn("CREATE TABLE NOTES_BOX", schema_text)
        self.assertTrue(dml_exists)
        self.assertIn("SPRING_DATASOURCE_VENDOR1_JDBC_URL", service_env)
        self.assertEqual(
            service_env["SPRING_DATASOURCE_VENDOR1_JDBC_URL"],
            "jdbc:sqlserver://localhost:1433;databaseName=VENDOR1;encrypt=false;trustServerCertificate=true",
        )
        self.assertEqual(service_env["SPRING_DATASOURCE_VENDOR1_DRIVER_CLASS_NAME"], "com.microsoft.sqlserver.jdbc.SQLServerDriver")
        self.assertEqual(service_env["SPRING_DATASOURCE_VENDOR1_USERNAME"], "sa")
        self.assertEqual(service_env["SPRING_DATASOURCE_VENDOR1_PASSWORD"], "Ccstack-local-1433!")

    def test_workspace_apply_materializes_schema_from_exposed_and_jdbc_sql(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            module = root / "contents-api"
            resources = module / "src" / "main" / "resources"
            source_dir = module / "src" / "main" / "kotlin" / "com" / "example"
            resources.mkdir(parents=True)
            source_dir.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':contents-api'\n")
            (module / "build.gradle").write_text("plugins { id 'org.springframework.boot' version '3.2.0' }\n")
            (resources / "application-local.yml").write_text(
                """
server:
  port: 18081
spring:
  datasource:
    vendor1:
      jdbc-url: jdbc:sqlserver://192.168.213.128:1433;databaseName=VENDOR1
      username: remote
      password: secret
""".strip()
                + "\n"
            )
            (source_dir / "ModerationTables.kt").write_text(
                """
package com.example

import org.jetbrains.exposed.dao.id.IntIdTable
import org.jetbrains.exposed.sql.javatime.date
import org.jetbrains.exposed.sql.javatime.datetime
import org.jetbrains.exposed.sql.javatime.time

object NormalListTable : IntIdTable("tGenericList", "nListSeq") {
    val nBoardSeq = integer("nBoardSeq")
    val sTitle = varchar("sTitle", 200)
    val dtCreateDate = date("dtCreateDate")
    val dtCreateTime = time("dtCreateTime")
    val dtUpdatedAt = datetime("dtUpdatedAt")
    val nConfirmType = byte("nConfirmType")
}
""".strip()
                + "\n"
            )
            (source_dir / "ReportRepository.kt").write_text(
                '''
package com.example

import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate

class ReportRepository(private val jdbc: NamedParameterJdbcTemplate) {
    fun findReportInfo() = jdbc.query(
        """
        SELECT REPORT_CODE, REPORT_TITLE, REPORT_NAME
        FROM NOTESYS_REPORT_INFO WITH (READUNCOMMITTED)
        WHERE USE_YN = 'Y'
        """.trimIndent(),
    ) { rs, _ -> rs.getString("REPORT_CODE") }

    fun findLocalContent() = jdbc.query(
        """
        SELECT kb.KB_NO,
               kb.KK_CODE,
               kb.KB_TITLE,
               kb.KB_CONTENT,
               kb.USERID,
               kb.KB_NICKNAME,
               kb.KB_REGDATE,
               kb.KB_DEL_FLAG,
               kb.KB_DISPLAY_FLAG,
               kb.IS_NOTICE,
               kb.KB_READCNT,
               kb.KB_CHILD_NUM,
               kb.G_CATEGORY,
               kb.G_MODELNO,
               kb.KB_NEWS_FLAG,
               source.SOURCE_FLAG,
               source.SOURCE_NAME
          FROM NOTES_BOX kb WITH (READUNCOMMITTED)
          LEFT JOIN NOTES_BOX_NEWS_SOURCE source WITH (READUNCOMMITTED)
                 ON kb.KB_NEWS_FLAG = source.SOURCE_FLAG
         WHERE kb.KB_NO = :contentId
        """.trimIndent(),
        mapOf("contentId" to 1),
    ) { rs, _ -> rs.getLong("KB_NO") }
}
'''.strip()
                + "\n"
            )
            # Deploy the shipped shopping overrides into the workspace root so
            # the generic engine produces the historical notes_box / notesys /
            # tgenericlist seed DML expected by this regression test.
            deploy_shopping_overrides(root)
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                draft_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_analyze"],
                        "workspace_root": [str(root)],
                    },
                )
                apply_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_apply"],
                        "draft_id": [draft_result["draft_id"]],
                    },
                )
                loaded = ccstack.Workspace.load(argparse.Namespace(root=None, workspace="shopping"))
                mssql_schema = ccstack.managed_schema_path("shopping", "mssql")
                mssql_dml = ccstack.managed_dml_path("shopping", "mssql")
                mssql_exists = mssql_schema.exists()
                mssql_text = mssql_schema.read_text()
                mssql_dml_exists = mssql_dml.exists()
                mssql_dml_text = mssql_dml.read_text()

        persistence = draft_result["draft"]["analysis"]["persistence"]
        service_env = loaded.services["contents-api"]["env"]
        self.assertTrue(apply_result["ok"])
        self.assertIn("jdbc", persistence)
        self.assertIn("NOTESYS_REPORT_INFO", [table["name"] for table in persistence["tables"]])
        normal_table = next(table for table in persistence["tables"] if table["name"] == "tGenericList")
        self.assertEqual(
            [column["name"] for column in normal_table["columns"][:5]],
            ["nListSeq", "nBoardSeq", "sTitle", "dtCreateDate", "dtCreateTime"],
        )
        self.assertTrue(service_env["SPRING_DATASOURCE_VENDOR1_JDBC_URL"].startswith("jdbc:sqlserver://localhost:1433"))
        self.assertTrue(mssql_exists)
        self.assertIn("IF OBJECT_ID(N'NOTESYS_REPORT_INFO', N'U') IS NULL", mssql_text)
        self.assertIn("CREATE TABLE NOTESYS_REPORT_INFO", mssql_text)
        self.assertIn("USE_YN VARCHAR", mssql_text)
        self.assertIn("dtCreateTime TIME", mssql_text)
        self.assertIn("dtUpdatedAt DATETIME2", mssql_text)
        self.assertNotIn("dtUpdatedAt TIMESTAMP", mssql_text)
        self.assertTrue(mssql_dml_exists)
        self.assertIn("IF NOT EXISTS", mssql_dml_text)
        self.assertIn("INSERT INTO NOTES_BOX", mssql_dml_text)
        # Seed value from examples/overrides/sample-shop/.ccstack-overrides.json
        # — preserved per spec: "do not change mock seed strings inside
        # examples/overrides". The DML emits the override value verbatim.
        self.assertIn("ccstack local content", mssql_dml_text)
        self.assertIn("INSERT INTO NOTES_BOX_NEWS_SOURCE", mssql_dml_text)
        self.assertIn("INSERT INTO NOTESYS_REPORT_INFO", mssql_dml_text)

    def test_jdbc_sql_column_inference_keeps_join_columns_on_own_tables(self):
        sql = """
        SELECT KK_CODE,
               T1.KB_NO AS kb_no,
               B.SOURCE_FLAG AS source_flag
          FROM NOTES_BOX T1 WITH (READUNCOMMITTED)
          LEFT OUTER JOIN NOTES_BOX_NEWS_SOURCE B WITH (READUNCOMMITTED)
                       ON T1.KB_NEWS_FLAG = B.SOURCE_FLAG
         WHERE T1.KB_NO = ?
        """

        table_columns = ccstack.extract_sql_table_columns(sql)

        self.assertIn({"name": "KB_NO"}, table_columns["NOTES_BOX"])
        self.assertIn({"name": "KB_NEWS_FLAG"}, table_columns["NOTES_BOX"])
        self.assertIn({"name": "SOURCE_FLAG"}, table_columns["NOTES_BOX_NEWS_SOURCE"])
        self.assertNotIn({"name": "KK_CODE"}, table_columns["NOTES_BOX_NEWS_SOURCE"])

    def test_workspace_apply_generates_generic_seed_data_for_content_like_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            module = root / "contents-api"
            resources = module / "src" / "main" / "resources"
            source_dir = module / "src" / "main" / "kotlin" / "com" / "example"
            resources.mkdir(parents=True)
            source_dir.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':contents-api'\n")
            (module / "build.gradle").write_text("plugins { id 'org.springframework.boot' version '3.2.0' }\n")
            (resources / "application-local.yml").write_text(
                """
server:
  port: 18081
spring:
  datasource:
    url: jdbc:postgresql://localhost:5432/contentdb
    username: remote
    password: secret
""".strip()
                + "\n"
            )
            (source_dir / "ArticleRepository.kt").write_text(
                '''
package com.example

import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate

class ArticleRepository(private val jdbc: NamedParameterJdbcTemplate) {
    fun findArticles() = jdbc.query(
        """
        SELECT article_id, title, body, deleted, hidden, created_at
          FROM article
         WHERE deleted = false
           AND hidden = false
         ORDER BY created_at DESC
        """.trimIndent(),
    ) { rs, _ -> rs.getString("title") }
}
'''.strip()
                + "\n"
            )
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                draft_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_analyze"],
                        "workspace_root": [str(root)],
                    },
                )
                apply_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_apply"],
                        "draft_id": [draft_result["draft_id"]],
                    },
                )
                dml_path = ccstack.managed_dml_path("shopping", "postgresql")
                dml_text = dml_path.read_text()

        self.assertTrue(apply_result["ok"])
        self.assertIn('INSERT INTO "article"', dml_text)
        self.assertIn("devstack local article", dml_text)
        self.assertIn("ON CONFLICT DO NOTHING", dml_text)

    def test_workspace_apply_adds_gradle_local_init_for_private_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            module = root / "contents-api"
            resources = module / "src" / "main" / "resources"
            resources.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text(
                """
pluginManagement {
  repositories {
    maven {
      url = uri("http://artifacts.example.com/artifactory/official-local/")
    }
  }
}
include ':contents-api'
""".strip()
                + "\n"
            )
            (module / "build.gradle").write_text("plugins { id 'org.springframework.boot' version '3.2.0' }\n")
            (resources / "application.yml").write_text("server:\n  port: 18081\n")
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                draft_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_analyze"],
                        "workspace_root": [str(root)],
                    },
                )
                apply_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_apply"],
                        "draft_id": [draft_result["draft_id"]],
                    },
                )
                loaded = ccstack.Workspace.load(argparse.Namespace(root=None, workspace="shopping"))
                init_path = ccstack.managed_gradle_init_path("shopping")
                init_exists = init_path.exists()
                init_text = init_path.read_text()

        self.assertTrue(draft_result["ok"])
        self.assertTrue(apply_result["ok"])
        self.assertNotIn("--offline", loaded.config["gradle_args"])
        self.assertIn("--max-workers=1", loaded.config["gradle_args"])
        self.assertIn("-Dorg.gradle.workers.max=1", loaded.config["gradle_args"])
        self.assertIn(
            "-Dorg.gradle.jvmargs=-Xmx1024m -XX:MaxMetaspaceSize=512m -Dfile.encoding=UTF-8",
            loaded.config["gradle_args"],
        )
        self.assertIn("--init-script", loaded.config["gradle_args"])
        self.assertIn(str(init_path), loaded.config["gradle_args"])
        self.assertTrue(init_exists)
        self.assertNotIn("repositories.removeAll", init_text)
        self.assertIn("repositories.mavenLocal()", init_text)
        self.assertIn("repositories.maven { url = uri('https://jitpack.io') }", init_text)

    def test_managed_gradle_resource_args_preserve_explicit_overrides(self):
        merged = ccstack.merge_managed_gradle_args(
            [
                "--max-workers=4",
                "-Dorg.gradle.jvmargs=-Xmx2g",
            ],
            ccstack.MANAGED_GRADLE_RESOURCE_ARGS,
        )

        self.assertIn("--max-workers=4", merged)
        self.assertIn("-Dorg.gradle.jvmargs=-Xmx2g", merged)
        self.assertIn("-Dorg.gradle.workers.max=1", merged)
        self.assertNotIn("--max-workers=1", merged)
        self.assertNotIn(
            "-Dorg.gradle.jvmargs=-Xmx1024m -XX:MaxMetaspaceSize=512m -Dfile.encoding=UTF-8",
            merged,
        )

    def test_managed_gradle_resource_args_apply_without_gradle_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            root.mkdir()
            config = {
                "workspace": "shopping",
                "profiles": {"default": {"services": ["contents-api"]}},
                "services": {
                    "contents-api": {
                        "type": "gradle",
                        "task": ":contents-api:bootRun",
                        "port": 18081,
                    },
                },
            }

            managed, materialized = ccstack.materialize_managed_environment(
                "shopping",
                root,
                config,
                {"environment": {"generated": [], "env": [], "args": [], "requirements": []}, "persistence": {}},
            )

        self.assertIn("--max-workers=1", managed["gradle_args"])
        self.assertIn("-Dorg.gradle.workers.max=1", managed["gradle_args"])
        self.assertIn(
            "-Dorg.gradle.jvmargs=-Xmx1024m -XX:MaxMetaspaceSize=512m -Dfile.encoding=UTF-8",
            managed["gradle_args"],
        )
        self.assertNotIn("--init-script", managed["gradle_args"])
        self.assertEqual(materialized["gradle_resource_args"], ccstack.MANAGED_GRADLE_RESOURCE_ARGS)
        self.assertEqual(materialized["gradle_init"], "")

    def test_workspace_apply_generates_gradle_defaults_from_clean_team_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            module = root / "contents-api"
            resources = module / "src" / "main" / "resources"
            resources.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':contents-api'\n")
            (module / "build.gradle").write_text("plugins { id 'org.springframework.boot' version '3.2.0' }\n")
            (resources / "application.yml").write_text("server:\n  port: 18081\n")
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                manifest_path = ccstack.generated_manifest_path("shopping")
                self.assertFalse(manifest_path.exists())

                draft_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_analyze"],
                        "workspace_root": [str(root)],
                    },
                )
                apply_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_apply"],
                        "draft_id": [draft_result["draft_id"]],
                    },
                )
                generated_config = json.loads(manifest_path.read_text())
                loaded = ccstack.Workspace.load(argparse.Namespace(root=None, workspace="shopping"))

        self.assertTrue(draft_result["ok"])
        self.assertTrue(apply_result["ok"])
        self.assertEqual(generated_config["gradle_args"], loaded.config["gradle_args"])
        self.assertIn("--max-workers=1", generated_config["gradle_args"])
        self.assertIn("-Dorg.gradle.workers.max=1", generated_config["gradle_args"])
        self.assertIn(
            "-Dorg.gradle.jvmargs=-Xmx1024m -XX:MaxMetaspaceSize=512m -Dfile.encoding=UTF-8",
            generated_config["gradle_args"],
        )
        self.assertNotIn("--init-script", generated_config["gradle_args"])

    def test_prepare_workspace_config_avoids_registered_workspace_ports(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            other_root = Path(tmp) / "contents-system"
            root = Path(tmp) / "shopping"
            other_root.mkdir()
            module = root / "contents-api"
            resources = module / "src" / "main" / "resources"
            source = module / "src" / "main" / "java" / "com" / "example"
            resources.mkdir(parents=True)
            source.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':contents-api'\n")
            (module / "build.gradle").write_text(
                """
plugins { id 'org.springframework.boot' version '3.2.0' }
dependencies {
  implementation 'org.springframework.boot:spring-boot-starter-actuator'
}
""".strip()
                + "\n"
            )
            (resources / "application.yml").write_text("server:\n  port: 8080\n")
            (source / "ContentsApplication.java").write_text(
                """
package com.example;

import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class ContentsApplication {
}
""".strip()
                + "\n"
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                other_manifest = ccstack.generated_manifest_path("contents-system")
                ccstack.write_json(
                    other_manifest,
                    {
                        "workspace": "contents-system",
                        "profiles": {"default": {"services": ["apps.proxy.contents"]}},
                        "services": {
                            "apps.proxy.contents": {"type": "gradle", "port": 8080},
                            "apps.proxy.redirect": {"type": "gradle", "port": 8081},
                        },
                    },
                )
                registry = ccstack.WorkspaceRegistry.load()
                registry.register("contents-system", other_root, other_manifest)
                registry.save()

                name, manifest_path, source_type = ccstack.prepare_workspace_config(root, "shopping", False)
                generated_config = json.loads(manifest_path.read_text())

        self.assertEqual(name, "shopping")
        self.assertEqual(source_type, "generated")
        self.assertEqual(generated_config["services"]["contents-api"]["port"], 8082)
        self.assertEqual(
            generated_config["services"]["contents-api"]["health"],
            "http://localhost:8082/actuator/health",
        )
        self.assertEqual(generated_config["services"]["contents-api"]["env"]["SERVER_PORT"], "8082")
        self.assertIn("--server.port=8082", generated_config["services"]["contents-api"]["args"])

    def test_prepare_workspace_config_preserves_active_managed_ports_when_restoring_detected_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            contents = root / "apps" / "proxy" / "acme" / "contents"
            contents_source = contents / "src" / "main" / "kotlin" / "com" / "example"
            contents_resources = contents / "src" / "main" / "resources"
            contents_source.mkdir(parents=True)
            contents_resources.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':apps:proxy:acme:contents'\n")
            (root / ".ccstack.json").write_text(
                json.dumps(
                    {
                        "workspace": "shopping",
                        "profiles": {
                            "default": {
                                "services": [
                                    "apps.proxy.acme.events",
                                    "apps.fixity-contents.contents",
                                ]
                            }
                        },
                        "services": {
                            "apps.proxy.acme.events": {
                                "type": "gradle",
                                "task": ":apps:proxy:acme:events:bootRun",
                                "module": "apps/proxy/acme/events",
                                "port": 8085,
                            },
                            "apps.fixity-contents.contents": {
                                "type": "gradle",
                                "task": ":apps:fixity-contents:contents:bootRun",
                                "module": "apps/fixity-contents/contents",
                                "port": 8094,
                            },
                        },
                    }
                )
                + "\n"
            )
            (contents / "build.gradle.kts").write_text(
                """
plugins { id("org.springframework.boot") }
dependencies {
  implementation("org.springframework.boot:spring-boot-starter-web")
  implementation("org.springframework.boot:spring-boot-starter-actuator")
}
""".strip()
                + "\n"
            )
            (contents_resources / "application-local.yml").write_text("server:\n  port: 8085\n")
            (contents_resources / "application-batch.yml").write_text(
                "spring:\n  main:\n    web-application-type: none\n"
            )
            (contents_source / "ContentsApplication.kt").write_text(
                """
package com.example

import org.springframework.boot.autoconfigure.SpringBootApplication
import org.springframework.boot.runApplication
import org.springframework.boot.SpringApplication

@SpringBootApplication
class ContentsApplication

fun main(args: Array<String>) {
    runApplication<ContentsApplication>(*args)
}

fun stopBatchContext() {
    SpringApplication.exit(null)
}
""".strip()
                + "\n"
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                active_manifest = ccstack.generated_manifest_path("shopping")
                ccstack.write_json(
                    active_manifest,
                    {
                        "workspace": "shopping",
                        "profiles": {
                            "default": {
                                "services": [
                                    "apps.proxy.acme.events",
                                    "apps.fixity-contents.contents",
                                ]
                            }
                        },
                        "services": {
                            "apps.proxy.acme.events": {
                                "type": "gradle",
                                "task": ":apps:proxy:acme:events:bootRun",
                                "module": "apps/proxy/acme/events",
                                "port": 8085,
                            },
                            "apps.fixity-contents.contents": {
                                "type": "gradle",
                                "task": ":apps:fixity-contents:contents:bootRun",
                                "module": "apps/fixity-contents/contents",
                                "port": 8094,
                            },
                        },
                    },
                )
                registry = ccstack.WorkspaceRegistry.load()
                registry.register("shopping", root, active_manifest)
                registry.save()

                name, manifest_path, source_type = ccstack.prepare_workspace_config(root, "shopping", False)
                generated_config = json.loads(manifest_path.read_text())

        self.assertEqual(name, "shopping")
        self.assertEqual(source_type, "generated")
        self.assertEqual(generated_config["services"]["apps.proxy.acme.events"]["port"], 8085)
        self.assertEqual(generated_config["services"]["apps.fixity-contents.contents"]["port"], 8094)
        self.assertEqual(generated_config["services"]["apps.proxy.acme.contents"]["port"], 8086)
        self.assertIn("apps.proxy.acme.contents", generated_config["profiles"]["default"]["services"])

    def test_start_gradle_includes_global_and_service_gradle_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            root.mkdir()
            (root / "gradlew").write_text("#!/bin/sh\n")
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "shopping",
                    "gradle_args": ["--offline"],
                    "profiles": {},
                    "services": {},
                },
            )
            service = {
                "type": "gradle",
                "task": ":contents-api:bootRun",
                "gradle_args": ["--init-script", "${CCSTACK_ROOT}/infra/init.gradle"],
            }

            with mock.patch.object(workspace, "spawn") as spawn:
                workspace.start_gradle("contents-api", service)

        spawn.assert_called_once()
        self.assertEqual(
            spawn.call_args.args[1],
            [
                str(root / "gradlew"),
                "--offline",
                "--init-script",
                str(root / "infra" / "init.gradle"),
                ":contents-api:bootRun",
            ],
        )

    def test_gradle_process_env_auto_selects_compatible_java_for_gradle_85(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            wrapper = root / "gradle" / "wrapper"
            wrapper.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "build.gradle").write_text("kotlin { jvmToolchain(17) }\n")
            (wrapper / "gradle-wrapper.properties").write_text(
                "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.5-bin.zip\n"
            )
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "shopping",
                    "profiles": {},
                    "services": {},
                },
            )

            with mock.patch.dict(ccstack.os.environ, {"JAVA_HOME": "/jdk24"}, clear=True), \
                    mock.patch.object(ccstack, "current_java_major", return_value=24), \
                    mock.patch.object(ccstack, "java_home_candidates", return_value=[Path("/jdk17")]), \
                    mock.patch.object(ccstack, "java_major_for_home", return_value=17):
                env = workspace.process_env({"type": "gradle", "task": ":contents-api:bootRun"})

        self.assertEqual(env["JAVA_HOME"], "/jdk17")

    def test_gradle_process_env_prefers_project_java_target_over_supported_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            wrapper = root / "gradle" / "wrapper"
            wrapper.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "build.gradle").write_text("kotlin { jvmToolchain(17) }\n")
            (wrapper / "gradle-wrapper.properties").write_text(
                "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.5-bin.zip\n"
            )
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "shopping",
                    "profiles": {},
                    "services": {},
                },
            )

            with mock.patch.dict(ccstack.os.environ, {"JAVA_HOME": "/jdk21"}, clear=True), \
                    mock.patch.object(ccstack, "current_java_major", return_value=21), \
                    mock.patch.object(ccstack, "java_home_candidates", return_value=[Path("/jdk17")]), \
                    mock.patch.object(ccstack, "java_major_for_home", return_value=17):
                env = workspace.process_env({"type": "gradle", "task": ":contents-api:bootRun"})

        self.assertEqual(env["JAVA_HOME"], "/jdk17")

    def test_gradle_process_env_respects_explicit_ccstack_java_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            wrapper = root / "gradle" / "wrapper"
            wrapper.mkdir(parents=True)
            (wrapper / "gradle-wrapper.properties").write_text(
                "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.5-bin.zip\n"
            )
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "shopping",
                    "profiles": {},
                    "services": {},
                },
            )

            with mock.patch.dict(ccstack.os.environ, {"CCSTACK_JAVA_HOME": "/custom-jdk"}, clear=True), \
                    mock.patch.object(ccstack, "compatible_gradle_java_home") as compatible:
                env = workspace.process_env({"type": "gradle", "task": ":contents-api:bootRun"})

        self.assertEqual(env["JAVA_HOME"], "/custom-jdk")
        compatible.assert_not_called()

    def test_workspace_analysis_reports_runtime_infra_and_manual_review_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            module = root / "contents-api"
            resources = module / "src" / "main" / "resources"
            source_dir = module / "src" / "main" / "java" / "com" / "example"
            resources.mkdir(parents=True)
            source_dir.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':contents-api'\n")
            (module / "build.gradle").write_text(
                """
plugins { id 'org.springframework.boot' version '3.2.0' }
dependencies {
  implementation 'org.springframework.kafka:spring-kafka'
  implementation 'org.springframework.boot:spring-boot-starter-data-redis'
}
""".strip()
                + "\n"
            )
            (resources / "application.yml").write_text(
                """
server:
  port: 18081
spring:
  data:
    redis:
      host: localhost
  kafka:
    bootstrap-servers: localhost:9092
  elasticsearch:
    uris: http://search.example.com:9200
  data:
    mongodb:
      host: localhost
      port: 27017
  config:
    import: configserver:http://config.example.com
catalog:
  base-url: https://catalog.example.com
  token: ${CATALOG_TOKEN:dev}
build:
  maven-url: https://repo.example.com/maven
""".strip()
                + "\n"
            )
            (resources / "application-local.yml").write_text("spring:\n  profiles:\n    active: local\n")
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "start-local.sh").write_text("#!/bin/sh\n./gradlew :contents-api:bootRun\n")
            (source_dir / "CatalogClient.java").write_text(
                """
package com.example;

import org.springframework.web.client.RestTemplate;

class CatalogClient {
    private final RestTemplate restTemplate = new RestTemplate();
}
""".strip()
                + "\n"
            )
            (source_dir / "ChannelRepository.java").write_text(
                """
package com.example;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;

@ConditionalOnProperty(name = "site.channel", havingValue = "VENDOR1")
class ChannelRepository {
}
""".strip()
                + "\n"
            )

            config = ccstack.inspect_workspace(root, "shopping")
            analysis = ccstack.analyze_workspace_config(root, config)

        environment = analysis["environment"]
        generated_requirements = {item["technology"]: item for item in environment["generated"]}
        generated = {item["technology"] for item in environment["generated"]}
        manual = {item["technology"] for item in environment["manual"]}
        env_names = {item["name"] for item in environment["env"]}
        env_values = {item["name"]: item["value"] for item in environment["env"]}
        arg_values = {item["value"] for item in environment["args"]}
        variable_names = {item["name"] for item in environment["variables"]}
        profile_names = {item["profile"] for item in environment["profiles"]}
        run_script_paths = {item["path"] for item in environment["run_scripts"]}

        self.assertIn("redis", generated)
        self.assertIn("kafka", generated)
        self.assertIn("mongodb", generated)
        self.assertIn("opensearch/elasticsearch", generated)
        self.assertEqual(generated_requirements["kafka"]["image"], "apache/kafka:3.7.0")
        self.assertEqual(generated_requirements["mongodb"]["image"], "mongo:6.0")
        self.assertEqual(
            generated_requirements["kafka"]["environment"]["KAFKA_ADVERTISED_LISTENERS"],
            "PLAINTEXT://localhost:9092",
        )
        self.assertEqual(
            generated_requirements["opensearch/elasticsearch"]["environment"]["OPENSEARCH_INITIAL_ADMIN_PASSWORD"],
            "N0nProd-Search!927",
        )
        self.assertIn("SPRING_KAFKA_BOOTSTRAP_SERVERS", env_names)
        self.assertEqual(env_values["SPRING_KAFKA_PROPERTIES_SECURITY_PROTOCOL"], "PLAINTEXT")
        self.assertEqual(env_values["DEV_CMN_KAFKA_DNW_PRICECOMPARE_BE_TEAM_BROKER_SERVER"], "localhost:9092")
        self.assertEqual(env_values["INSPECTION_EVENT_BOOTSTRAP_SERVERS"], "localhost:9092")
        self.assertIn("--spring.kafka.bootstrap-servers=localhost:9092", arg_values)
        self.assertIn("--spring.kafka.properties.security.protocol=PLAINTEXT", arg_values)
        self.assertIn("--inspection.event.bootstrap-servers=localhost:9092", arg_values)
        self.assertIn("--inspection.event.security-protocol=PLAINTEXT", arg_values)
        self.assertIn("SPRING_DATA_MONGODB_HOST", env_names)
        self.assertIn("SPRING_DATA_MONGODB_PORT", env_names)
        self.assertIn("SPRING_DATA_MONGODB_DATABASE", env_names)
        self.assertIn("SPRING_DATA_MONGODB_AUTHENTICATION_DATABASE", env_names)
        self.assertNotIn("SPRING_DATA_MONGODB_URI", env_names)
        self.assertIn("--spring.data.mongodb.host=localhost", arg_values)
        self.assertIn("--spring.data.mongodb.port=27017", arg_values)
        self.assertIn("--spring.data.mongodb.database=ccstack", arg_values)
        self.assertIn("--spring.data.mongodb.authentication-database=admin", arg_values)
        self.assertNotIn("kafka", manual)
        self.assertTrue(environment["external"])
        self.assertIn("SPRING_CLOUD_CONFIG_ENABLED", env_names)
        self.assertIn("REDIS_HOST", env_names)
        self.assertIn("CMN_VALKEY_HOST", env_names)
        self.assertIn("CMN_VALKEY_PORT", env_names)
        self.assertIn("CMN_VALKEY_PASSWORD", env_names)
        self.assertIn("PROXY_CACHE_MODE", env_names)
        self.assertEqual(env_values["PROXY_CACHE_MODE"], "redisson")
        self.assertIn("SPRING_AUTOCONFIGURE_EXCLUDE", env_names)
        self.assertIn("SPRING_PROFILES_ACTIVE", env_names)
        self.assertIn("SITE_CHANNEL", env_names)
        self.assertNotIn("BUILD_MAVEN_URL", env_names)
        self.assertIn("--cmn.valkey.host=localhost", arg_values)
        self.assertIn("--cmn.valkey.port=6379", arg_values)
        self.assertIn("--cmn.valkey.password=", arg_values)
        self.assertIn("--spring.config.import=classpath:devstack-local.yml", arg_values)
        self.assertIn("CATALOG_TOKEN", variable_names)
        self.assertIn("local", profile_names)
        self.assertIn("scripts/start-local.sh", run_script_paths)
        self.assertIn("some infrastructure or external dependencies require manual review before local generation", analysis["warnings"])

    def test_workspace_analysis_configures_valkey_properties_when_redis_is_provided(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            source = root / "apps" / "fixity-samplegoods" / "samplegoods" / "src" / "main" / "resources"
            kotlin = root / "apps" / "fixity-samplegoods" / "samplegoods" / "src" / "main" / "kotlin"
            source.mkdir(parents=True)
            kotlin.mkdir(parents=True)
            (source / "application-local.yml").write_text(
                "redisson:\n"
                "  singleServerConfig:\n"
                "    address: redis://${cmn.valkey.host}:${cmn.valkey.port}\n"
                "    password: ${cmn.valkey.password}\n"
            )
            (kotlin / "RedissonConfig.kt").write_text("fun redis() = config.useClusterServers()\n")
            config = {
                "workspace": "shopping",
                "profiles": {"default": {"services": ["ccstack.infra.redis", "apps.fixity-samplegoods.samplegoods"]}},
                "services": {
                    "ccstack.infra.redis": {
                        "type": "compose",
                        "service": "redis",
                        "engine": "redis",
                        "port": 6379,
                    },
                    "apps.fixity-samplegoods.samplegoods": {
                        "type": "gradle",
                        "task": ":apps:fixity-samplegoods:samplegoods:bootRun",
                        "module": "apps/fixity-samplegoods/samplegoods",
                    },
                },
            }

            analysis = ccstack.analyze_workspace_config(root, config)

        generated = {item["service_name"]: item for item in analysis["environment"]["generated"]}
        env = {
            item["name"]: item["value"]
            for item in analysis["environment"]["env"]
            if item["service"] == "apps.fixity-samplegoods.samplegoods"
        }
        args = {
            item["value"]
            for item in analysis["environment"]["args"]
            if item["service"] == "apps.fixity-samplegoods.samplegoods"
        }
        self.assertEqual(env["CMN_VALKEY_HOST"], "localhost")
        self.assertEqual(env["CMN_VALKEY_PORT"], "6379")
        self.assertEqual(env["CMN_VALKEY_PASSWORD"], "")
        self.assertEqual(env["PROXY_CACHE_MODE"], "redisson")
        self.assertIn("--cmn.valkey.host=localhost", args)
        self.assertIn("--cmn.valkey.port=6379", args)
        self.assertIn("--cmn.valkey.password=", args)
        # Shim 5 — the analyzer accepts a service whose id uses the legacy
        # ``ccstack.infra.*`` prefix; the generated payload preserves the
        # caller's input name.
        self.assertEqual(generated["ccstack.infra.redis"]["id"], "redis-cluster")
        self.assertIn("--cluster-enabled yes", generated["ccstack.infra.redis"]["command"])
        self.assertIn("--cluster-announce-port 6379", generated["ccstack.infra.redis"]["command"])

    def test_workspace_recovery_materialization_preserves_existing_managed_infra_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            root.mkdir()
            compose = ccstack.managed_infra_compose_path("shopping")
            config = {
                "workspace": "shopping",
                "profiles": {
                    "default": {
                        "services": [
                            "ccstack.infra.redis",
                            "ccstack.infra.mysql",
                            "ccstack.infra.mariadb",
                            "ccstack.infra.kafka",
                            "ccstack.infra.mssql",
                            "ccstack.infra.mongo",
                            "ccstack.infra.opensearch",
                            "ccstack.infra.mock-http",
                            "apps.fixity-samplegoods.samplegoods",
                        ]
                    }
                },
                "services": {
                    "ccstack.infra.redis": {
                        "type": "compose",
                        "compose": str(compose),
                        "service": "redis",
                        "engine": "redis",
                        "port": 6379,
                    },
                    "ccstack.infra.mysql": {
                        "type": "compose",
                        "compose": str(compose),
                        "service": "mysql",
                        "engine": "mysql",
                        "port": 3306,
                        "database": "shopping",
                        "username": "ccstack",
                        "password": "ccstack",
                    },
                    "ccstack.infra.mariadb": {
                        "type": "compose",
                        "compose": str(compose),
                        "service": "mariadb",
                        "engine": "mariadb",
                        "port": 3340,
                        "database": "shopping_maria",
                        "username": "ccstack",
                        "password": "ccstack",
                    },
                    "ccstack.infra.kafka": {
                        "type": "compose",
                        "compose": str(compose),
                        "service": "kafka",
                        "engine": "kafka",
                        "port": 9092,
                    },
                    "ccstack.infra.mssql": {
                        "type": "compose",
                        "compose": str(compose),
                        "service": "mssql",
                        "engine": "mssql",
                        "port": 1433,
                        "database": "knowboxdb2",
                        "username": "sa",
                        "password": "Ccstack-local-1433!",
                    },
                    "ccstack.infra.mongo": {
                        "type": "compose",
                        "compose": str(compose),
                        "service": "mongo",
                        "engine": "mongodb",
                        "port": 27017,
                    },
                    "ccstack.infra.opensearch": {
                        "type": "compose",
                        "compose": str(compose),
                        "service": "opensearch",
                        "engine": "opensearch/elasticsearch",
                        "port": 9200,
                    },
                    "ccstack.infra.mock-http": {
                        "type": "compose",
                        "compose": str(compose),
                        "service": "mock-http",
                        "engine": "mockserver",
                        "port": 1080,
                    },
                    "apps.fixity-samplegoods.samplegoods": {
                        "type": "gradle",
                        "task": ":apps:fixity-samplegoods:samplegoods:bootRun",
                    },
                },
            }
            analysis = {
                "environment": {
                    "generated": [
                        {
                            "id": "redis-cluster",
                            "kind": "redis",
                            "technology": "redis",
                            "status": "generate",
                            "service_name": "ccstack.infra.redis",
                            "compose_service": "redis",
                            "image": "redis:7-alpine",
                            "port": 6379,
                            "container_port": 6379,
                            "command": ccstack.redis_cluster_command(),
                            "environment": {},
                            "volumes": [],
                        }
                    ],
                    "env": [],
                    "args": [],
                    "requirements": [],
                    "manual": [],
                },
                "persistence": {"tables": [], "datasources": []},
            }

            ccstack.materialize_managed_environment("shopping", root, config, analysis)

        text = compose.read_text()
        self.assertIn("redis:", text)
        self.assertIn("mysql:", text)
        self.assertIn("mariadb:", text)
        self.assertIn("kafka:", text)
        self.assertIn("mssql:", text)
        self.assertIn("mongo:", text)
        self.assertIn("opensearch:", text)
        self.assertIn("mock-http:", text)
        self.assertIn("mcr.microsoft.com/mssql/server:2022-latest", text)
        self.assertIn("mysql:8.4", text)
        self.assertIn("mariadb:11.4", text)
        self.assertIn("mongo:6.0", text)
        self.assertIn("opensearchproject/opensearch:2.15.0", text)
        self.assertIn("mockserver/mockserver:5.15.0", text)
        self.assertIn("MYSQL_DATABASE: \"shopping\"", text)
        self.assertIn("MYSQL_DATABASE: \"shopping_maria\"", text)
        self.assertIn("MSSQL_SA_PASSWORD: \"Ccstack-local-1433!\"", text)
        self.assertIn("--cluster-enabled yes", text)

    def test_managed_infra_compose_refresh_uses_generated_service_ports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            root.mkdir()

            with mock.patch.object(ccstack, "STATE_HOME", Path(tmp) / ".ccstack" / "state"):
                compose = ccstack.managed_infra_compose_path("shopping")
                compose.parent.mkdir(parents=True)
                compose.write_text(
                    "\n".join(
                        [
                            "# stale",
                            "services:",
                            "  redis:",
                            "    image: \"redis:7-alpine\"",
                            "    ports:",
                            "      - \"6379:6379\"",
                            "  opensearch:",
                            "    image: \"opensearchproject/opensearch:2.15.0\"",
                            "    ports:",
                            "      - \"9200:9200\"",
                        ]
                    )
                    + "\n"
                )
                config = {
                    "workspace": "shopping",
                    "profiles": {"default": {"services": ["ccstack.infra.redis", "ccstack.infra.opensearch"]}},
                    "services": {
                        "ccstack.infra.redis": {
                            "type": "compose",
                            "compose": str(compose),
                            "project": "devstack_shopping",
                            "service": "redis",
                            "engine": "redis",
                            "port": 6380,
                            "image": "redis:7-alpine",
                            "container_port": 6379,
                            "command": ccstack.redis_cluster_command(6380),
                            "environment": {},
                            "volumes": [],
                        },
                        "ccstack.infra.opensearch": {
                            "type": "compose",
                            "compose": str(compose),
                            "project": "devstack_shopping",
                            "service": "opensearch",
                            "engine": "opensearch/elasticsearch",
                            "port": 9201,
                            "image": "opensearchproject/opensearch:2.15.0",
                            "container_port": 9200,
                            "environment": {
                                "discovery.type": "single-node",
                                "plugins.security.disabled": "true",
                            },
                            "volumes": ["ccstack-opensearch-data:/usr/share/opensearch/data"],
                        },
                    },
                }
                workspace = ccstack.Workspace(root, config)

                workspace.refresh_managed_infra_compose(compose)

                text = compose.read_text()

        self.assertIn('"6380:6379"', text)
        self.assertIn("--cluster-announce-port 6380", text)
        self.assertIn('"9201:9200"', text)
        self.assertNotIn('"6379:6379"', text)
        self.assertNotIn('"9200:9200"', text)

    def test_managed_gradle_init_uses_map_literal_when_no_graphql_dummy_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            init_path = Path(tmp) / "devstack-local-only.gradle"

            ccstack.write_managed_gradle_init(init_path, include_config_server_patch=True)

            text = init_path.read_text()

        self.assertIn("def devstackGraphqlDummySources = [:]", text)
        self.assertIn("devstackGraphqlDummySources.containsKey(project.path)", text)
        self.assertIn("def devstackPatchDgsCodegenSchemaPaths = { project ->", text)
        self.assertIn("from(source) { include '**/*.graphql'; include '**/*.graphqls' }", text)
        self.assertIn("def devstackSchemaPaths = []", text)
        self.assertIn("new File(devstackSchemaDir, \"source-${devstackSchemaPathIndex++}\")", text)
        self.assertIn("task.schemaPaths = devstackSchemaPaths", text)
        self.assertIn("task.filesMatching(['**/*.yml', '**/*.yaml', '**/*.properties']) {", text)
        self.assertIn("filter { line ->", text)
        self.assertIn("SET LOCK_TIMEOUT 30000", text)
        self.assertNotIn("file.filter { line ->", text)
        self.assertIn("def devstackConfigServerOverrideProperties = [", text)
        # Mandatory classpath import (no `optional:` prefix) — missing
        # devstack-local.yml now fails fast instead of silently degrading.
        self.assertIn("'spring.config.import': 'classpath:devstack-local.yml'", text)
        self.assertNotIn("'spring.config.import': 'optional:classpath:devstack-local.yml'", text)
        self.assertIn("def devstackPatchConfigServerBootTasks = { project ->", text)
        self.assertIn("task.systemProperty(key, value)", text)
        self.assertNotIn("def devstackGraphqlDummySources = []", text)
        # Path A: bootRun env injection constant + apply loop are always present
        # when the config-server patch is enabled (empty map literal when no env supplied).
        self.assertIn("def devstackBootRunEnvironment = [:]", text)
        self.assertIn("def envForTask = devstackBootRunEnvironment[task.path] ?: [:]", text)
        self.assertIn("envForTask.each { k, v -> task.environment(k, v) }", text)

    def test_managed_gradle_init_emits_boot_run_environment_with_per_task_env(self):
        """Path A: write_managed_gradle_init emits a per-task env map and applies it
        via task.environment(k, v) inside devstackPatchConfigServerBootTasks so each
        gradle bootRun task receives its manifest env (e.g. SPRING_DATASOURCE_*).
        """
        boot_env = {
            ":apps:fixity-contents:contents:bootRun": {
                "SPRING_DATASOURCE_BLMASTERBOARD_URL": "jdbc:mysql://localhost:3306/shopping",
                "SPRING_DATASOURCE_BLMASTERBOARD_USERNAME": "ccstack",
            },
            ":apps:proxy:acme:contents:bootRun": {
                "FOO_BAR": "baz",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            init_path = Path(tmp) / "devstack-local-only.gradle"

            ccstack.write_managed_gradle_init(
                init_path,
                include_config_server_patch=True,
                boot_run_environment=boot_env,
            )

            text = init_path.read_text()

        # Map literal exists and has both task entries, with safe JSON-escaped values
        self.assertIn("def devstackBootRunEnvironment = [", text)
        self.assertIn(
            "\":apps:fixity-contents:contents:bootRun\": ["
            "\"SPRING_DATASOURCE_BLMASTERBOARD_URL\": "
            "\"jdbc:mysql://localhost:3306/shopping\", "
            "\"SPRING_DATASOURCE_BLMASTERBOARD_USERNAME\": \"ccstack\"]",
            text,
        )
        self.assertIn(
            "\":apps:proxy:acme:contents:bootRun\": [\"FOO_BAR\": \"baz\"]",
            text,
        )
        # The existing 3 systemProperty calls must be preserved (additive only).
        self.assertIn("task.systemProperty(key, value)", text)
        # New apply loop
        self.assertIn("def envForTask = devstackBootRunEnvironment[task.path] ?: [:]", text)
        self.assertIn("envForTask.each { k, v -> task.environment(k, v) }", text)

    def test_materialize_managed_environment_passes_bootrun_env_to_gradle_init(self):
        """Path A: materialize_managed_environment must collect env from every
        gradle service whose task ends in :bootRun and forward it to
        write_managed_gradle_init so the generated init script can inject it.
        Non-bootRun gradle services must be ignored.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            root.mkdir(parents=True)
            config = {
                "workspace": "shopping",
                "profiles": {"default": {"services": ["apps.fixity-contents.contents"]}},
                "services": {
                    "apps.fixity-contents.contents": {
                        "type": "gradle",
                        "task": ":apps:fixity-contents:contents:bootRun",
                        "module": "apps/fixity-contents/contents",
                        "env": {
                            "SPRING_DATASOURCE_BLMASTERBOARD_URL": "jdbc:mysql://localhost:3306/shopping",
                            "SPRING_DATASOURCE_BLMASTERBOARD_USERNAME": "ccstack",
                        },
                    },
                    "apps.misc.somejob": {
                        "type": "gradle",
                        "task": ":apps:misc:somejob:assemble",
                        "env": {"SHOULD_BE_IGNORED": "1"},
                    },
                },
            }
            analysis = {
                "environment": {
                    "generated": [],
                    "env": [],
                    "args": [],
                    "requirements": [
                        # triggers include_config_server_patch=True
                        {"id": "config-server-local-overrides"},
                    ],
                },
                "persistence": {},
            }

            _, materialized = ccstack.materialize_managed_environment(
                "shopping", root, config, analysis
            )

            init_text = Path(materialized["gradle_init"]).read_text()

        # The bootRun service env is injected into the init script.
        self.assertIn("def devstackBootRunEnvironment = [", init_text)
        self.assertIn(":apps:fixity-contents:contents:bootRun", init_text)
        self.assertIn("jdbc:mysql://localhost:3306/shopping", init_text)
        # The non-bootRun gradle service must NOT leak into the map.
        self.assertNotIn("SHOULD_BE_IGNORED", init_text)
        self.assertNotIn(":apps:misc:somejob:assemble", init_text)
        # And the apply loop is present.
        self.assertIn("envForTask.each { k, v -> task.environment(k, v) }", init_text)

    def test_materialize_managed_environment_preserves_persisted_runtime_dependencies(self):
        """Path B (defensive seed): when a previous workspace apply has persisted
        ccstack_managed_environment.gradle_runtime_dependencies into the manifest,
        a subsequent regeneration whose freshly-built analysis produces an EMPTY
        gradle_runtime_dependencies map (e.g. because no mysql requirement is
        re-derived this round) MUST preserve the previously-persisted entries
        rather than silently overwrite them with [:]. This is the idempotency
        contract: the on-disk init script's devstackRuntimeDependencies must
        survive regeneration so fixity bootRun classpaths keep mysql-connector-j.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            root.mkdir(parents=True)
            persisted_runtime_deps = {
                ":apps:fixity-contents:contents": ["com.mysql:mysql-connector-j:8.0.33"],
                ":apps:proxy:acme:contents": ["com.mysql:mysql-connector-j:8.0.33"],
            }
            config = {
                "workspace": "shopping",
                "profiles": {"default": {"services": ["apps.fixity-contents.contents"]}},
                "services": {
                    "apps.fixity-contents.contents": {
                        "type": "gradle",
                        "task": ":apps:fixity-contents:contents:bootRun",
                        "module": "apps/fixity-contents/contents",
                        "env": {
                            "SPRING_DATASOURCE_URL": "jdbc:mysql://localhost:3306/shopping",
                        },
                    },
                    "apps.proxy.acme.contents": {
                        "type": "gradle",
                        "task": ":apps:proxy:acme:contents:bootRun",
                        "module": "apps/proxy/acme/contents",
                        "env": {
                            "SPRING_DATASOURCE_URL": "jdbc:mysql://localhost:3306/shopping",
                        },
                    },
                },
                # Previously persisted by an earlier apply — the defensive seed
                # must read this back and merge it into the fresh map.
                "ccstack_managed_environment": {
                    "gradle_runtime_dependencies": persisted_runtime_deps,
                },
            }
            # Freshly-built analysis WITHOUT any mysql requirements / env_overrides
            # — exactly the degraded path that previously dropped runtime deps.
            # include_config_server_patch is enabled so the init script is written
            # (otherwise the gate at line ~1237 would return early).
            analysis = {
                "environment": {
                    "generated": [],
                    "env": [],
                    "args": [],
                    "requirements": [
                        {"id": "config-server-local-overrides"},
                    ],
                },
                "persistence": {},
            }

            _, materialized = ccstack.materialize_managed_environment(
                "shopping", root, config, analysis
            )

            init_text = Path(materialized["gradle_init"]).read_text()

        # Both persisted entries survive into the materialized block.
        self.assertEqual(
            materialized["gradle_runtime_dependencies"],
            persisted_runtime_deps,
        )
        # And the generated init script's devstackRuntimeDependencies map
        # contains both project paths and the mysql coordinate.
        self.assertIn(
            "\":apps:fixity-contents:contents\": [\"com.mysql:mysql-connector-j:8.0.33\"]",
            init_text,
        )
        self.assertIn(
            "\":apps:proxy:acme:contents\": [\"com.mysql:mysql-connector-j:8.0.33\"]",
            init_text,
        )
        # The devstackRuntimeDependencies constant must NOT be the empty map literal
        # — the bug scenario is precisely "[:]" overwriting good values.
        self.assertNotIn("def devstackRuntimeDependencies = [:]", init_text)
        # The apply closure that does project.dependencies.add('runtimeOnly', ...)
        # must still be wired in.
        self.assertIn("project.dependencies.add('runtimeOnly', dependency)", init_text)

    def test_workspace_apply_adds_mybatis_mapper_scan_generated_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            module = root / "apps" / "catalog-content"
            mapper_dir = module / "src" / "main" / "kotlin" / "com" / "example" / "catalog" / "persistence"
            app_dir = module / "src" / "main" / "kotlin" / "com" / "example" / "catalog"
            mapper_dir.mkdir(parents=True)
            app_dir.mkdir(parents=True, exist_ok=True)
            (app_dir / "CatalogApplication.kt").write_text(
                """
package com.example.catalog

import org.springframework.boot.autoconfigure.SpringBootApplication

@SpringBootApplication
class CatalogApplication
""".strip()
                + "\n"
            )
            (mapper_dir / "CatalogMapper.kt").write_text(
                """
package com.example.catalog.persistence

import org.apache.ibatis.annotations.Mapper

@Mapper
interface CatalogMapper
""".strip()
                + "\n"
            )
            config = {
                "workspace": "shopping",
                "profiles": {"default": {"services": ["apps.catalog-content"]}},
                "services": {
                    "apps.catalog-content": {
                        "type": "gradle",
                        "task": ":apps:catalog-content:bootRun",
                        "module": "apps/catalog-content",
                    }
                },
            }

            managed, materialized = ccstack.materialize_managed_environment(
                "shopping",
                root,
                config,
                {"environment": {"generated": [], "env": [], "args": [], "requirements": []}, "persistence": {}},
            )
            generated_source = (
                ccstack.managed_mybatis_mapper_scan_source_dir("shopping", "apps.catalog-content")
                / "com"
                / "example"
                / "catalog"
                / "devstack"
                / "local"
                / "DevstackLocalMybatisMapperScanConfig.java"
            )
            init_text = Path(materialized["gradle_init"]).read_text()
            source_text = generated_source.read_text()

        self.assertIn("--init-script", managed["gradle_args"])
        self.assertIn("devstackMybatisMapperScanSources", init_text)
        self.assertIn('":apps:catalog-content"', init_text)
        self.assertIn(
            "@MapperScan(basePackages = {\"com.example.catalog.persistence\"}, sqlSessionFactoryRef = \"devstackLocalSqlSessionFactory\")",
            source_text,
        )
        self.assertIn("@Bean(name = \"devstackLocalDataSource\")", source_text)
        self.assertIn("@Bean(name = \"devstackLocalSqlSessionFactory\")", source_text)
        self.assertIn("@Qualifier(\"devstackLocalDataSource\") DataSource dataSource", source_text)
        self.assertNotIn("jdbc:h2:", source_text)
        self.assertNotIn("org.h2.Driver", source_text)
        self.assertIn("package com.example.catalog.devstack.local;", source_text)

    def test_workspace_apply_skips_datasource_when_app_owns_one(self):
        """Regression: when the app declares its own @Bean DataSource(s), the
        generated DevstackLocalMybatisMapperScanConfig must NOT register a
        competing devstackLocalDataSource bean. Reproduces the
        apps.fixity-pricecompare.catalog-content collision where ccstack's
        bean joined altimasterDataSource + altislaveDataSource and Spring
        raised NoUniqueBeanDefinitionException.

        Contract verified:
          C1 - complement-only: ccstack emits the @Bean DataSource iff the
                                app does not own one.
          C2 - deterministic: decided at materialization time (this test
                              runs only the materializer; no Spring context).
          C3 - non-invasive: no app file is mutated; only the generated
                             tree under managed_mybatis_mapper_scan_source_dir
                             changes.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            module = root / "apps" / "fixity-pricecompare" / "catalog-content"
            mapper_dir = (
                module
                / "src"
                / "main"
                / "kotlin"
                / "com"
                / "shopping"
                / "catalogcontent"
                / "detail"
                / "adapter"
                / "outbound"
                / "persistence"
                / "adapter"
            )
            app_dir = module / "src" / "main" / "kotlin" / "com" / "shopping" / "catalogcontent"
            config_dir = app_dir / "config"
            mapper_dir.mkdir(parents=True)
            app_dir.mkdir(parents=True, exist_ok=True)
            config_dir.mkdir(parents=True, exist_ok=True)
            (app_dir / "CatalogContentApplication.kt").write_text(
                """
package com.shopping.catalogcontent

import org.springframework.boot.autoconfigure.SpringBootApplication

@SpringBootApplication
class CatalogContentApplication
""".strip()
                + "\n"
            )
            (mapper_dir / "DetailContentMyBatisMapper.kt").write_text(
                """
package com.shopping.catalogcontent.detail.adapter.outbound.persistence.adapter

import org.apache.ibatis.annotations.Mapper

@Mapper
interface DetailContentMyBatisMapper
""".strip()
                + "\n"
            )
            # The app declares TWO @Bean DataSource methods - exactly the
            # catalog-content shape (altimaster + altislave). ccstack must
            # detect this and skip emitting its own DataSource bean.
            (config_dir / "DataSourceConfig.kt").write_text(
                """
package com.shopping.catalogcontent.config

import javax.sql.DataSource
import org.springframework.boot.jdbc.DataSourceBuilder
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration
import org.springframework.context.annotation.Primary

@Configuration
class DataSourceConfig {
    @Bean(name = ["altimasterDataSource"])
    @Primary
    fun altimasterDataSource(): DataSource = DataSourceBuilder.create().build()

    @Bean(name = ["altislaveDataSource"])
    fun altislaveDataSource(): DataSource = DataSourceBuilder.create().build()
}
""".strip()
                + "\n"
            )
            config = {
                "workspace": "shopping",
                "profiles": {"default": {"services": ["apps.fixity-pricecompare.catalog-content"]}},
                "services": {
                    "apps.fixity-pricecompare.catalog-content": {
                        "type": "gradle",
                        "task": ":apps:fixity-pricecompare:catalog-content:bootRun",
                        "module": "apps/fixity-pricecompare/catalog-content",
                    }
                },
            }

            managed, materialized = ccstack.materialize_managed_environment(
                "shopping",
                root,
                config,
                {"environment": {"generated": [], "env": [], "args": [], "requirements": []}, "persistence": {}},
            )
            generated_source = (
                ccstack.managed_mybatis_mapper_scan_source_dir(
                    "shopping", "apps.fixity-pricecompare.catalog-content"
                )
                / "com"
                / "shopping"
                / "catalogcontent"
                / "devstack"
                / "local"
                / "DevstackLocalMybatisMapperScanConfig.java"
            )
            init_text = Path(materialized["gradle_init"]).read_text()
            source_text = generated_source.read_text()

        # The wiring is still emitted - ccstack continues to register the
        # MapperScan + SqlSessionFactory under unique names that do not
        # collide with the app's beans.
        self.assertIn("--init-script", managed["gradle_args"])
        self.assertIn("devstackMybatisMapperScanSources", init_text)
        self.assertIn(
            '":apps:fixity-pricecompare:catalog-content"',
            init_text,
        )
        self.assertIn(
            "@MapperScan(basePackages = "
            "{\"com.shopping.catalogcontent.detail.adapter.outbound.persistence.adapter\"}, "
            "sqlSessionFactoryRef = \"devstackLocalSqlSessionFactory\")",
            source_text,
        )
        self.assertIn(
            "@Bean(name = \"devstackLocalSqlSessionFactory\")",
            source_text,
        )
        self.assertIn(
            "package com.shopping.catalogcontent.devstack.local;",
            source_text,
        )

        # The harm surface - ccstack's competing DataSource bean and its
        # supporting machinery - MUST NOT be emitted when the app owns
        # at least one @Bean DataSource. These four assertions reproduce
        # the catalog-content failure mode: under the old generator, every
        # one of them fails because the bean and the DataSourceBuilder
        # import are unconditionally written.
        self.assertNotIn(
            "@Bean(name = \"devstackLocalDataSource\")",
            source_text,
        )
        self.assertNotIn(
            "@ConditionalOnMissingBean(DataSource.class)",
            source_text,
        )
        self.assertNotIn("DataSourceBuilder", source_text)
        self.assertNotIn(
            "@Qualifier(\"devstackLocalDataSource\") DataSource dataSource",
            source_text,
        )

    def test_workspace_analysis_reports_gradle_private_repository_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            root.mkdir()
            (root / "settings.gradle.kts").write_text(
                """
pluginManagement {
  val artifactoryUser = providers.gradleProperty("artifactoryUser")
    .orElse(providers.environmentVariable("ARTIFACTORY_USER")).getOrElse("")
  val artifactoryToken = providers.gradleProperty("artifactoryToken")
    .orElse(providers.environmentVariable("ARTIFACTORY_TOKEN")).getOrElse("")
  repositories {
    maven {
      url = uri("http://artifacts.example.com/artifactory/official-local/")
      credentials {
        username = artifactoryUser
        password = artifactoryToken
      }
    }
  }
}
""".strip()
                + "\n"
            )

            requirements = ccstack.inspect_gradle_private_repository_requirements(root)

        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0]["technology"], "gradle-private-repository")
        self.assertIn("ARTIFACTORY_USER", requirements[0]["required_env"])
        self.assertIn("ARTIFACTORY_TOKEN", requirements[0]["required_env"])
        self.assertIn("artifactoryUser", requirements[0]["required_properties"])
        self.assertIn("artifactoryToken", requirements[0]["required_properties"])

    def test_private_gradle_hosts_env_unset_yields_empty_tuple(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CCSTACK_PRIVATE_GRADLE_HOSTS", None)
            hosts = ccstack.private_gradle_repository_hosts()
        self.assertEqual(hosts, ())

    def test_private_gradle_hosts_env_empty_yields_empty_tuple(self):
        with mock.patch.dict(os.environ, {"CCSTACK_PRIVATE_GRADLE_HOSTS": ""}):
            hosts = ccstack.private_gradle_repository_hosts()
        self.assertEqual(hosts, ())

    def test_private_gradle_hosts_env_single_value(self):
        with mock.patch.dict(os.environ, {"CCSTACK_PRIVATE_GRADLE_HOSTS": "art.example.com"}):
            hosts = ccstack.private_gradle_repository_hosts()
        self.assertEqual(hosts, ("art.example.com",))

    def test_private_gradle_hosts_env_comma_separated(self):
        with mock.patch.dict(os.environ, {"CCSTACK_PRIVATE_GRADLE_HOSTS": "a.example.com, b.example.com ,c.example.com"}):
            hosts = ccstack.private_gradle_repository_hosts()
        self.assertEqual(hosts, ("a.example.com", "b.example.com", "c.example.com"))

    def test_inspect_gradle_private_repository_still_detects_via_artifactory_keyword_when_env_unset(self):
        # Backward-compat check: with no env var, the generic "artifactory" keyword
        # path still triggers detection. The previous hardcoded company host is no
        # longer required as a default.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            root.mkdir()
            (root / "settings.gradle.kts").write_text(
                """
pluginManagement {
  val artifactoryUser = providers.gradleProperty("artifactoryUser")
    .orElse(providers.environmentVariable("ARTIFACTORY_USER")).getOrElse("")
  repositories {
    maven {
      url = uri("http://repo.example.com/artifactory/internal-local/")
      credentials {
        username = artifactoryUser
      }
    }
  }
}
""".strip()
                + "\n"
            )

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CCSTACK_PRIVATE_GRADLE_HOSTS", None)
                requirements = ccstack.inspect_gradle_private_repository_requirements(root)

        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0]["technology"], "gradle-private-repository")

    def test_inspect_gradle_private_repository_uses_env_var_host(self):
        # When the gradle text has no artifactory/credentials keyword but does
        # reference a host listed in CCSTACK_PRIVATE_GRADLE_HOSTS, the env-var
        # path must still trigger detection.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            root.mkdir()
            (root / "settings.gradle.kts").write_text(
                """
pluginManagement {
  val user = providers.gradleProperty("repoUser")
    .orElse(providers.environmentVariable("REPO_USER")).getOrElse("")
  repositories {
    maven {
      url = uri("http://internal.example.org/maven-local/")
    }
  }
}
""".strip()
                + "\n"
            )

            with mock.patch.dict(os.environ, {"CCSTACK_PRIVATE_GRADLE_HOSTS": "internal.example.org"}):
                requirements = ccstack.inspect_gradle_private_repository_requirements(root)

        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0]["technology"], "gradle-private-repository")

    def test_smoke_class_skips_when_smoke_workspace_unset(self):
        # When CCSTACK_SMOKE_ENABLED=1 but CCSTACK_SMOKE_WORKSPACE is unset/empty,
        # the smoke test must raise SkipTest with a clear message rather than
        # silently defaulting to a company-specific workspace name.
        # Renamed test file after the canonical rename; the older filename
        # is left here as a fallback for in-progress checkouts.
        smoke_module_path = ROOT / "tests" / "test_devstack_smoke_api.py"
        if not smoke_module_path.exists():
            smoke_module_path = ROOT / "tests" / "test_ccstack_smoke_api.py"
        loader = SourceFileLoader("ccstack_smoke_under_test", str(smoke_module_path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        smoke_module = importlib.util.module_from_spec(spec)
        loader.exec_module(smoke_module)

        env_overrides = {"CCSTACK_SMOKE_ENABLED": "1"}
        with mock.patch.dict(os.environ, env_overrides, clear=False):
            os.environ.pop("DEVSTACK_SMOKE_WORKSPACE", None)
            os.environ.pop("DEVSTACK_SMOKE_WORKSPACE_ROOT", None)
            os.environ.pop("CCSTACK_SMOKE_WORKSPACE", None)
            os.environ.pop("CCSTACK_SMOKE_WORKSPACE_ROOT", None)
            with self.assertRaises(unittest.SkipTest) as ctx:
                smoke_module.CcstackWorkspaceSmokeTest.setUpClass()
        # The skip message now references the canonical DEVSTACK_SMOKE_* env
        # names; the legacy CCSTACK_SMOKE_* names are still honored at read
        # time and mentioned as a fallback in the message.
        self.assertIn("DEVSTACK_SMOKE_WORKSPACE", str(ctx.exception))

    def test_managed_gradle_init_keeps_private_repos_and_adds_public_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            init_path = Path(tmp) / "devstack-local-only.gradle"

            ccstack.write_managed_gradle_init(init_path)

            text = init_path.read_text()

        self.assertNotIn("repositories.removeAll", text)
        self.assertIn("def devstackRoutePublicRuntimeRepositories = { repositories ->", text)
        self.assertIn("filter { includeGroup('com.microsoft.sqlserver') }", text)
        self.assertIn("devstackRoutePublicRuntimeRepositories(project.repositories)", text)
        self.assertIn("repositories.mavenLocal()", text)
        self.assertIn("repositories.mavenCentral()", text)
        self.assertIn("repositories.maven { url = uri('https://jitpack.io') }", text)
        self.assertIn("repositories.maven { url = uri('https://packages.confluent.io/maven/') }", text)

    def test_symbolic_datasource_uses_docker_database_even_when_other_db_compose_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            module = root / "apps" / "fixity-events" / "events"
            resources = module / "src" / "main" / "resources"
            resources.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':apps:fixity-events:events'\n")
            (module / "build.gradle").write_text("plugins { id 'org.springframework.boot' version '3.2.0' }\n")
            (resources / "application-local.yml").write_text(
                """
spring:
  datasource:
    event-master-eventblog:
      url: jdbc:mysql://${db.eventmaster.eventblog.host}/${db.eventmaster.eventblog.db}?useSSL=false
      username: ${db.eventmaster.eventblog.username}
      password: ${db.eventmaster.eventblog.password}
""".strip()
                + "\n"
            )
            config = {
                "workspace": "shopping",
                "profiles": {"default": {"services": ["contents.db", "apps.fixity-events.events"]}},
                "services": {
                    "contents.db": {"type": "compose", "compose": "compose.yml", "service": "contents-db"},
                    "apps.fixity-events.events": {
                        "type": "gradle",
                        "task": ":apps:fixity-events:events:bootRun",
                        "module": "apps/fixity-events/events",
                    },
                },
            }
            persistence = {"datasources": ccstack.inspect_datasource_configs(root), "tables": []}

            environment = ccstack.build_local_environment_plan(root, "shopping", config, persistence)

        requirements = environment["requirements"]
        self.assertTrue(
            any(
                item["status"] == "generate"
                and item["technology"] == "mysql"
                and item["image"] == "mysql:8.4"
                for item in requirements
            )
        )
        env = {
            (item["service"], item["name"]): item["value"]
            for item in environment["env"]
        }
        self.assertIn("jdbc:mysql://localhost:3306/", env[("apps.fixity-events.events", "SPRING_DATASOURCE_EVENT_MASTER_EVENTBLOG_URL")])
        self.assertEqual(env[("apps.fixity-events.events", "DB_EVENTMASTER_EVENTBLOG_HOST")], "localhost")
        self.assertNotIn(("contents.db", "SPRING_DATASOURCE_EVENT_MASTER_EVENTBLOG_URL"), env)

    def test_recovery_materialization_replaces_stale_h2_datasource_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            module = root / "apps" / "fixity-contents" / "contents"
            resources = module / "src" / "main" / "resources"
            resources.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':apps:fixity-contents:contents'\n")
            (module / "build.gradle").write_text("plugins { id 'org.springframework.boot' version '3.2.0' }\n")
            (resources / "application-local.yml").write_text(
                """
spring:
  datasource:
    blmasterboard:
      url: jdbc:mysql://${db.boardmain.dbBoard.host}/${db.boardmain.dbBoard.db}?useSSL=false
      username: ${db.boardmain.dbBoard.username}
      password: ${db.boardmain.dbBoard.password}
""".strip()
                + "\n"
            )
            config = {
                "workspace": "shopping",
                "profiles": {"default": {"services": ["ccstack.infra.mysql", "apps.fixity-contents.contents"]}},
                "services": {
                    "ccstack.infra.mysql": {
                        "type": "compose",
                        "compose": str(ccstack.managed_infra_compose_path("shopping")),
                        "service": "mysql",
                        "engine": "mysql",
                        "port": 3306,
                        "database": "shopping",
                        "username": "ccstack",
                        "password": "ccstack",
                    },
                    "apps.fixity-contents.contents": {
                        "type": "gradle",
                        "task": ":apps:fixity-contents:contents:bootRun",
                        "module": "apps/fixity-contents/contents",
                        "env": {
                            "SPRING_DATASOURCE_BLMASTERBOARD_DRIVER_CLASS_NAME": "org.h2.Driver",
                            "SPRING_DATASOURCE_BLMASTERBOARD_URL": "jdbc:h2:mem:shopping",
                            "SPRING_DATASOURCE_BLMASTERBOARD_USERNAME": "sa",
                            "SPRING_DATASOURCE_BLMASTERBOARD_PASSWORD": "",
                            "DB_BOARDMAIN_DBBOARD_HOST": "localhost",
                            "DB_BOARDMAIN_DBBOARD_DB": "shopping",
                            "DB_BOARDMAIN_DBBOARD_USERNAME": "sa",
                            "DB_BOARDMAIN_DBBOARD_PASSWORD": "",
                        },
                    },
                },
            }
            analysis = ccstack.analyze_workspace_config(root, config)

            managed, _ = ccstack.materialize_managed_environment("shopping", root, config, analysis)

        env = managed["services"]["apps.fixity-contents.contents"]["env"]
        self.assertEqual(env["SPRING_DATASOURCE_BLMASTERBOARD_DRIVER_CLASS_NAME"], "com.mysql.cj.jdbc.Driver")
        self.assertEqual(env["SPRING_DATASOURCE_BLMASTERBOARD_URL"], "jdbc:mysql://localhost:3306/shopping")
        self.assertEqual(env["SPRING_DATASOURCE_BLMASTERBOARD_USERNAME"], "ccstack")
        self.assertEqual(env["SPRING_DATASOURCE_BLMASTERBOARD_PASSWORD"], "ccstack")
        self.assertEqual(env["DB_BOARDMAIN_DBBOARD_USERNAME"], "ccstack")
        self.assertEqual(env["DB_BOARDMAIN_DBBOARD_PASSWORD"], "ccstack")
        self.assertFalse(any("h2" in str(value).lower() for value in env.values()))

    def test_workspace_apply_replaces_legacy_mysql_driver_override(self):
        services = {
            "apps.proxy.acme.contents": {
                "type": "gradle",
                "task": ":apps:proxy:acme:contents:bootRun",
                "module": "apps/proxy/acme/contents",
                "env": {
                    "SPRING_DATASOURCE_DRIVER_CLASS_NAME": "com.mysql.jdbc.Driver",
                },
            },
        }
        overrides = [
            {
                "service": "apps.proxy.acme.contents",
                "name": "SPRING_DATASOURCE_DRIVER_CLASS_NAME",
                "value": "com.mysql.cj.jdbc.Driver",
                "source": "database-mysql-configure-12345678",
            }
        ]

        ccstack.apply_service_environment_overrides(services, overrides)

        env = services["apps.proxy.acme.contents"]["env"]
        self.assertEqual(env["SPRING_DATASOURCE_DRIVER_CLASS_NAME"], "com.mysql.cj.jdbc.Driver")

    def test_apply_replaces_stale_managed_routing_url_after_port_reshuffle(self):
        """Regression: a persisted ccstack-managed localhost routing URL must be
        replaced on re-apply when ports were reassigned.

        Live evidence (shopping workspace): CONTENTS_SERVICE_URL persisted as
        http://localhost:8087 from an epoch when fixity-contents owned 8087.
        ensure_unique_gradle_service_ports later reshuffled ports
        (fixity-contents=8096, proxy adcenter=8087), so the resolver now computes
        http://localhost:8096 — but the old env.setdefault behavior kept the stale
        8087 value. The override carries the external-http provenance source, so
        managed-ness is decided data-first."""
        services = {
            "apps.proxy.acme.contents": {
                "type": "gradle",
                "task": ":apps:proxy:acme:contents:bootRun",
                "module": "apps/proxy/acme/contents",
                "port": 8090,
                "env": {
                    # Stale: 8087 used to belong to fixity-contents.
                    "CONTENTS_SERVICE_URL": "http://localhost:8087",
                },
            },
            # adcenter now owns the old fixity-contents port.
            "apps.proxy.acme.adcenter": {
                "type": "gradle",
                "task": ":apps:proxy:acme:adcenter:bootRun",
                "module": "apps/proxy/acme/adcenter",
                "port": 8087,
            },
            # fixity-contents was reassigned to 8096.
            "apps.fixity-contents.contents": {
                "type": "gradle",
                "task": ":apps:fixity-contents:contents:bootRun",
                "module": "apps/fixity-contents/contents",
                "port": 8096,
            },
        }
        overrides = [
            {
                "service": "apps.proxy.acme.contents",
                "name": "CONTENTS_SERVICE_URL",
                "value": "http://localhost:8096",
                "source": "external-http-1",
            }
        ]

        ccstack.apply_service_environment_overrides(services, overrides)

        env = services["apps.proxy.acme.contents"]["env"]
        self.assertEqual(env["CONTENTS_SERVICE_URL"], "http://localhost:8096")

    def test_apply_replaces_stale_managed_routing_url_legacy_manifest_no_source(self):
        """Value-shape fallback: a legacy manifest whose override predates the
        external-http source marker must still get its stale localhost routing
        URL replaced, because the persisted port belongs to the managed routing
        port set (a current runtime service port)."""
        services = {
            "apps.proxy.acme.contents": {
                "type": "gradle",
                "module": "apps/proxy/acme/contents",
                "port": 8090,
                "env": {"CONTENTS_SERVICE_URL": "http://localhost:8087"},
            },
            # 8087 is a managed routing port (a current runtime service port),
            # so the persisted value is recognized as ccstack-generated.
            "apps.proxy.acme.adcenter": {
                "type": "gradle",
                "module": "apps/proxy/acme/adcenter",
                "port": 8087,
            },
        }
        overrides = [
            {
                "service": "apps.proxy.acme.contents",
                "name": "CONTENTS_SERVICE_URL",
                "value": "http://localhost:8096",
                # Legacy / non-routing source — exercises the value-shape fallback.
                "source": "persistence model",
            }
        ]

        ccstack.apply_service_environment_overrides(services, overrides)

        env = services["apps.proxy.acme.contents"]["env"]
        self.assertEqual(env["CONTENTS_SERVICE_URL"], "http://localhost:8096")

    def test_apply_preserves_user_set_non_localhost_routing_url(self):
        """A genuinely user-set non-localhost URL must be preserved even when the
        override carries the external-http routing source — ccstack only manages
        localhost routing values, never custom hosts."""
        services = {
            "apps.proxy.acme.contents": {
                "type": "gradle",
                "module": "apps/proxy/acme/contents",
                "port": 8090,
                "env": {"CONTENTS_SERVICE_URL": "https://contents.internal.corp/api"},
            },
            "apps.fixity-contents.contents": {
                "type": "gradle",
                "module": "apps/fixity-contents/contents",
                "port": 8096,
            },
        }
        overrides = [
            {
                "service": "apps.proxy.acme.contents",
                "name": "CONTENTS_SERVICE_URL",
                "value": "http://localhost:8096",
                "source": "external-http-1",
            }
        ]

        ccstack.apply_service_environment_overrides(services, overrides)

        env = services["apps.proxy.acme.contents"]["env"]
        self.assertEqual(env["CONTENTS_SERVICE_URL"], "https://contents.internal.corp/api")

    def test_apply_preserves_user_localhost_stub_on_non_managed_port(self):
        """A developer's hand-set localhost stub on a NON-managed port with a
        non-routing source must be preserved — the value-shape fallback only
        fires for ports in the managed routing port set, so we do not clobber a
        personal stub like http://localhost:9999."""
        services = {
            "apps.proxy.acme.contents": {
                "type": "gradle",
                "module": "apps/proxy/acme/contents",
                "port": 8090,
                "env": {"MY_LOCAL_STUB_URL": "http://localhost:9999"},
            },
            "apps.fixity-contents.contents": {
                "type": "gradle",
                "module": "apps/fixity-contents/contents",
                "port": 8096,
            },
        }
        overrides = [
            {
                "service": "apps.proxy.acme.contents",
                "name": "MY_LOCAL_STUB_URL",
                "value": "http://localhost:8096",
                "source": "persistence model",
            }
        ]

        ccstack.apply_service_environment_overrides(services, overrides)

        env = services["apps.proxy.acme.contents"]["env"]
        self.assertEqual(env["MY_LOCAL_STUB_URL"], "http://localhost:9999")

    def test_apply_replaces_stale_managed_routing_host_port_form(self):
        """The managed value shape includes bare ``host:port`` strings (e.g.
        registry endpoints like ``localhost:1080``), not just full URLs. A stale
        bare localhost:port whose port is the managed mockserver port must be
        replaced on re-apply with an external-http routing source."""
        services = {
            "apps.proxy.acme.contents": {
                "type": "gradle",
                "module": "apps/proxy/acme/contents",
                "port": 8090,
                "env": {"REGISTRY_SERVER": "localhost:1080"},
            },
            "apps.fixity-contents.contents": {
                "type": "gradle",
                "module": "apps/fixity-contents/contents",
                "port": 8096,
            },
        }
        overrides = [
            {
                "service": "apps.proxy.acme.contents",
                "name": "REGISTRY_SERVER",
                "value": "localhost:8096",
                "source": "external-http-1",
            }
        ]

        ccstack.apply_service_environment_overrides(services, overrides)

        env = services["apps.proxy.acme.contents"]["env"]
        self.assertEqual(env["REGISTRY_SERVER"], "localhost:8096")

    def test_apply_managed_routing_no_change_when_value_matches(self):
        """When the persisted managed routing value already equals the freshly
        computed value, apply is a no-op (no spurious mutation, idempotent)."""
        services = {
            "apps.proxy.acme.contents": {
                "type": "gradle",
                "module": "apps/proxy/acme/contents",
                "port": 8090,
                "env": {"CONTENTS_SERVICE_URL": "http://localhost:8096"},
            },
            "apps.fixity-contents.contents": {
                "type": "gradle",
                "module": "apps/fixity-contents/contents",
                "port": 8096,
            },
        }
        overrides = [
            {
                "service": "apps.proxy.acme.contents",
                "name": "CONTENTS_SERVICE_URL",
                "value": "http://localhost:8096",
                "source": "external-http-1",
            }
        ]

        ccstack.apply_service_environment_overrides(services, overrides)

        env = services["apps.proxy.acme.contents"]["env"]
        self.assertEqual(env["CONTENTS_SERVICE_URL"], "http://localhost:8096")

    def test_should_replace_managed_routing_keeps_h2_and_driver_rules_intact(self):
        """The new managed-routing branch must not disturb the existing H2 /
        legacy-driver replacement rules. Stale H2 values replace regardless of
        source; database-* driver rules still fire; and a non-localhost,
        non-H2 user value with a database-* source is still preserved."""
        # Stale H2 datasource URL — replaced (existing rule).
        self.assertTrue(
            ccstack.should_replace_environment_override(
                {"SPRING_DATASOURCE_URL": "jdbc:h2:mem:shopping"},
                {
                    "name": "SPRING_DATASOURCE_URL",
                    "value": "jdbc:mysql://localhost:3306/shopping",
                    "source": "database-mysql-configure-abcd1234",
                },
            )
        )
        # Legacy mysql driver with database-* source — replaced (existing rule).
        self.assertTrue(
            ccstack.should_replace_environment_override(
                {"SPRING_DATASOURCE_DRIVER_CLASS_NAME": "com.mysql.jdbc.Driver"},
                {
                    "name": "SPRING_DATASOURCE_DRIVER_CLASS_NAME",
                    "value": "com.mysql.cj.jdbc.Driver",
                    "source": "database-mysql-configure-abcd1234",
                },
            )
        )
        # A user-set non-localhost, non-H2 value with a database-* source and no
        # stale H2 on the service must be preserved (no over-replacement).
        self.assertFalse(
            ccstack.should_replace_environment_override(
                {"SPRING_DATASOURCE_USERNAME": "appuser"},
                {
                    "name": "SPRING_DATASOURCE_USERNAME",
                    "value": "ccstack",
                    "source": "database-mysql-configure-abcd1234",
                },
                had_stale_h2_datasource=False,
            )
        )

    def test_p6spy_datasource_generates_wrapped_database_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            module = root / "apps" / "fixity-distribution" / "distribution"
            resources = module / "src" / "main" / "resources"
            resources.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':apps:fixity-distribution:distribution'\n")
            (module / "build.gradle").write_text("plugins { id 'org.springframework.boot' version '3.2.0' }\n")
            (resources / "application-local.yml").write_text(
                """
spring:
  datasource:
    db-distribution-master:
      url: jdbc:p6spy:mariadb://${db.billingmaster.dbDistribution2.host}/${db.billingmaster.dbDistribution2.db}?useSSL=false
""".strip()
                + "\n"
            )
            config = ccstack.inspect_workspace(root, "shopping")
            persistence = {"datasources": ccstack.inspect_datasource_configs(root), "tables": []}

            environment = ccstack.build_local_environment_plan(root, "shopping", config, persistence)

        generated = [item for item in environment["generated"] if item.get("kind") == "database"]
        self.assertEqual(generated[0]["technology"], "mariadb")
        self.assertEqual(generated[0]["image"], "mariadb:11.4")
        env = {
            (item["service"], item["name"]): item["value"]
            for item in environment["env"]
        }
        self.assertEqual(
            env[("apps.fixity-distribution.distribution", "SPRING_DATASOURCE_DB_DISTRIBUTION_MASTER_URL")],
            "jdbc:mariadb://localhost:3306/db_billingmaster_dbDistribution2_db",
        )

    def test_workspace_analysis_marks_entrypoint_modules_for_boot_plugin_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            module = root / "apps" / "proxy" / "acme" / "shopping"
            source_dir = module / "src" / "main" / "kotlin" / "com" / "example"
            resources = module / "src" / "main" / "resources"
            source_dir.mkdir(parents=True)
            resources.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle.kts").write_text('include("apps:proxy:acme:shopping")\n')
            (root / "build.gradle.kts").write_text(
                'plugins { id("org.springframework.boot") version "3.2.0" apply false }\n'
            )
            (module / "build.gradle.kts").write_text(
                """
plugins {
    java
}
dependencies {
    implementation("org.springframework.boot:spring-boot-starter-web")
}
""".strip()
                + "\n"
            )
            (source_dir / "ShoppingApplication.kt").write_text(
                """
package com.example

import org.springframework.boot.autoconfigure.SpringBootApplication
import org.springframework.boot.runApplication

@SpringBootApplication
class ShoppingApplication

fun main(args: Array<String>) {
    runApplication<ShoppingApplication>(*args)
}
""".strip()
                + "\n"
            )
            (resources / "application.yml").write_text("server:\n  port: 18087\n")

            config = ccstack.inspect_workspace(root, "shopping")
            managed, materialized = ccstack.materialize_managed_environment(
                "shopping",
                root,
                config,
                {"environment": {"generated": [], "env": [], "args": [], "requirements": []}, "persistence": {}},
            )

        service = managed["services"]["apps.proxy.acme.shopping"]
        self.assertEqual(service["port"], 18087)
        self.assertNotIn("health", service)
        self.assertTrue(service["apply_spring_boot_plugin"])
        self.assertIn("--init-script", managed["gradle_args"])
        self.assertTrue(materialized["gradle_boot_plugin_modules"])
        init_text = Path(materialized["gradle_init"]).read_text()
        self.assertIn('":apps:proxy:acme:shopping"', init_text)
        self.assertIn("project.pluginManager.apply('org.springframework.boot')", init_text)

    def test_gradle_port_override_does_not_add_default_health_without_actuator(self):
        service = {
            "type": "gradle",
            "task": ":apps:proxy:acme:billing:bootRun",
            "port": 8081,
            "env": {},
            "args": [],
        }

        ccstack.apply_gradle_service_port_override(service, 8081, 18081)

        self.assertEqual(service["port"], 18081)
        self.assertNotIn("health", service)
        self.assertEqual(service["env"]["SERVER_PORT"], "18081")
        self.assertIn("--server.port=18081", service["args"])

    def test_workspace_apply_adds_local_graphql_dummy_resolver_for_empty_schema_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            module = root / "apps" / "proxy" / "acme" / "billing"
            schema_dir = module / "src" / "main" / "resources" / "graphql"
            source_dir = module / "src" / "main" / "kotlin" / "com" / "example"
            schema_dir.mkdir(parents=True)
            source_dir.mkdir(parents=True)
            (source_dir / "Application.kt").write_text(
                """
package com.example.billing

import org.springframework.boot.autoconfigure.SpringBootApplication

@SpringBootApplication
class Application
""".strip()
                + "\n"
            )
            (schema_dir / "schema.graphqls").write_text(
                """
type Query {
    shows: [Show!]!
    actors: [Actor!]!
}
type Show {
    title: String
    actors: [Actor!]!
}
type Actor {
    name: String
}
""".strip()
                + "\n"
            )
            config = {
                "services": {
                    "apps.proxy.acme.billing": {
                        "type": "gradle",
                        "task": ":apps:proxy:acme:billing:bootRun",
                        "module": "apps/proxy/acme/billing",
                        "port": 8081,
                    }
                }
            }

            managed, materialized = ccstack.materialize_managed_environment(
                "shopping",
                root,
                config,
                {"environment": {"generated": [], "env": [], "args": [], "requirements": []}, "persistence": {}},
            )

        self.assertIn("--init-script", managed["gradle_args"])
        self.assertIn(":apps:proxy:acme:billing", materialized["gradle_graphql_dummy_modules"])
        source = Path(materialized["gradle_graphql_dummy_modules"][":apps:proxy:acme:billing"])
        resolver = source / "com" / "example" / "billing" / "devstack" / "local" / "DevstackLocalGraphqlDummyData.java"
        resolver_text = resolver.read_text()
        self.assertIn("package com.example.billing.devstack.local;", resolver_text)
        self.assertIn("@DgsQuery", resolver_text)
        self.assertIn("public List<Map<String, Object>> shows()", resolver_text)
        self.assertIn("devstack local show", resolver_text)
        self.assertIn("public List<Map<String, Object>> actors()", resolver_text)
        init_text = Path(materialized["gradle_init"]).read_text()
        self.assertIn("devstackGraphqlDummySources", init_text)
        self.assertIn('":apps:proxy:acme:billing":', init_text)

    def test_workspace_apply_adds_mockserver_dummy_expectations_for_external_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            source = root / "apps" / "proxy" / "acme" / "contents" / "src" / "main" / "resources"
            source.mkdir(parents=True)
            (source / "application.yml").write_text(
                "backend:\n  contents-url: http://contents-backend.example\n"
            )
            # Deploy the shipped shopping overrides so the engine emits the
            # historical /content-display samplegoods-style expectations rather
            # than the generic catchall-only output.
            deploy_shopping_overrides(root)
            config = {
                "workspace": "shopping",
                "services": {
                    "apps.proxy.acme.contents": {
                        "type": "gradle",
                        "task": ":apps:proxy:acme:contents:bootRun",
                        "module": "apps/proxy/acme/contents",
                    }
                },
            }
            persistence = {"tables": [], "datasources": []}
            environment = ccstack.build_local_environment_plan(root, "shopping", config, persistence)

            managed, materialized = ccstack.materialize_managed_environment(
                "shopping",
                root,
                config,
                {"environment": environment, "persistence": persistence},
            )

        mock_service = managed["services"]["devstack.infra.mock-http"]
        expectation_path = Path(materialized["mockserver_expectations"])
        expectation_text = expectation_path.read_text()
        compose_text = Path(mock_service["compose"]).read_text()
        self.assertIn("MOCKSERVER_INITIALIZATION_JSON_PATH", compose_text)
        self.assertIn(str(expectation_path), compose_text)
        self.assertIn("/content-display", expectation_text)
        self.assertIn('\\"message\\": \\"devstack local dummy\\"', expectation_text)
        # Seed strings from examples/overrides/sample-shop/.ccstack-overrides.json
        # — preserved per spec.
        self.assertIn("ccstack local content display", expectation_text)

    def test_workspace_apply_routes_local_service_url_to_mockserver_for_api_dummy_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            source = root / "apps" / "proxy" / "acme" / "samplegoods" / "src" / "main" / "resources"
            source.mkdir(parents=True)
            (source / "application-local.yml").write_text(
                "samplegoods:\n  service:\n    url: http://localhost:8084\n"
            )
            # Deploy the shipped shopping overrides so the engine emits the
            # historical /sample-goods route and sample_goods_page envelope.
            deploy_shopping_overrides(root)
            config = {
                "workspace": "shopping",
                "services": {
                    "apps.proxy.acme.samplegoods": {
                        "type": "gradle",
                        "task": ":apps:proxy:acme:samplegoods:bootRun",
                        "module": "apps/proxy/acme/samplegoods",
                    }
                },
            }
            persistence = {"tables": [], "datasources": []}
            environment = ccstack.build_local_environment_plan(root, "shopping", config, persistence)

            managed, materialized = ccstack.materialize_managed_environment(
                "shopping",
                root,
                config,
                {"environment": environment, "persistence": persistence},
            )

        service = managed["services"]["apps.proxy.acme.samplegoods"]
        expectation_text = Path(materialized["mockserver_expectations"]).read_text()
        self.assertEqual(service["env"]["SAMPLEGOODS_SERVICE_URL"], "http://localhost:1080")
        self.assertIn("/sample-goods", expectation_text)
        self.assertIn("ccstack local samplegoods goods", expectation_text)
        self.assertIn('\\"pageInfo\\": {\\"type\\": \\"offset\\"', expectation_text)

    def test_external_http_candidate_routes_to_sibling_gradle_app_port(self):
        """Regression: when a proxy app's external-HTTP candidate names a
        sibling gradle app in the same workspace manifest, the env override
        must point at the sibling's real local port, never the managed
        mockserver. Live evidence: shopping workspace proxy was calling
        localhost:1081 (mock 404) instead of fixity-contents at 8096."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            proxy_resources = root / "apps" / "proxy" / "acme" / "contents" / "src" / "main" / "resources"
            proxy_resources.mkdir(parents=True)
            (proxy_resources / "application.yml").write_text(
                "fixity:\n"
                "  contents:\n"
                "    service-url: http://localhost:1080/user-articles\n"
            )
            config = {
                "workspace": "shopping",
                "services": {
                    "apps.proxy.acme.contents": {
                        "type": "gradle",
                        "task": ":apps:proxy:acme:contents:bootRun",
                        "module": "apps/proxy/acme/contents",
                        "port": 8090,
                    },
                    "apps.fixity-contents.contents": {
                        "type": "gradle",
                        "task": ":apps:fixity-contents:contents:bootRun",
                        "module": "apps/fixity-contents/contents",
                        "port": 8096,
                    },
                },
            }
            persistence = {"tables": [], "datasources": []}
            environment = ccstack.build_local_environment_plan(root, "shopping", config, persistence)

        proxy_env = {
            (item["service"], item["name"]): item["value"]
            for item in environment["env"]
            if item["service"] == "apps.proxy.acme.contents"
        }
        target_key = ("apps.proxy.acme.contents", "FIXITY_CONTENTS_SERVICE_URL")
        self.assertIn(target_key, proxy_env)
        self.assertEqual(proxy_env[target_key], "http://localhost:8096")
        self.assertNotIn(":1080", proxy_env[target_key])
        # Requirement reason must surface the sibling routing so users see why
        # the env override differs from the mockserver default.
        external_reqs = [
            r for r in environment["requirements"]
            if r.get("kind") == "external_http" and r.get("technology") == "http"
        ]
        self.assertTrue(external_reqs, "expected at least one external_http requirement")
        sibling_routed = [r for r in external_reqs if "sibling" in r.get("reason", "").lower()]
        self.assertTrue(
            sibling_routed,
            f"expected a sibling-routed requirement, got reasons: "
            f"{[r.get('reason') for r in external_reqs]}",
        )

    def test_external_http_candidate_falls_back_to_mockserver_without_sibling(self):
        """Conservative complement to the sibling-routing test: when NO
        gradle sibling matches the candidate, the env override must keep its
        pre-existing behavior (point at the managed mockserver port)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            proxy_resources = root / "apps" / "proxy" / "acme" / "contents" / "src" / "main" / "resources"
            proxy_resources.mkdir(parents=True)
            (proxy_resources / "application.yml").write_text(
                "external:\n"
                "  partner:\n"
                "    service-url: http://partner.example.com/api/v1\n"
            )
            config = {
                "workspace": "shopping",
                "services": {
                    "apps.proxy.acme.contents": {
                        "type": "gradle",
                        "task": ":apps:proxy:acme:contents:bootRun",
                        "module": "apps/proxy/acme/contents",
                        "port": 8090,
                    },
                },
            }
            persistence = {"tables": [], "datasources": []}
            environment = ccstack.build_local_environment_plan(root, "shopping", config, persistence)

        proxy_env = {
            (item["service"], item["name"]): item["value"]
            for item in environment["env"]
            if item["service"] == "apps.proxy.acme.contents"
        }
        target_key = ("apps.proxy.acme.contents", "EXTERNAL_PARTNER_SERVICE_URL")
        self.assertIn(target_key, proxy_env)
        self.assertEqual(proxy_env[target_key], "http://localhost:1080")
        # The requirement reason must be the mockserver wording, not the
        # sibling routing wording.
        external_reqs = [
            r for r in environment["requirements"]
            if r.get("kind") == "external_http" and r.get("technology") == "http"
        ]
        self.assertTrue(external_reqs)
        self.assertTrue(
            all("sibling" not in r.get("reason", "").lower() for r in external_reqs),
            f"unexpected sibling reason on mock-only fallback: "
            f"{[r.get('reason') for r in external_reqs]}",
        )

    def test_external_http_candidate_matches_sibling_by_url_hostname(self):
        """When the candidate URL uses a docker-style sibling hostname
        (e.g. http://fixity-contents:8080/...), the override must rewrite to
        the sibling's REAL local port from the manifest, not the URL's
        original docker port and not the mockserver port."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            proxy_resources = root / "apps" / "proxy" / "acme" / "contents" / "src" / "main" / "resources"
            proxy_resources.mkdir(parents=True)
            (proxy_resources / "application.yml").write_text(
                "fixity:\n"
                "  service-url: http://fixity-contents:8080/api/foo\n"
            )
            config = {
                "workspace": "shopping",
                "services": {
                    "apps.proxy.acme.contents": {
                        "type": "gradle",
                        "task": ":apps:proxy:acme:contents:bootRun",
                        "module": "apps/proxy/acme/contents",
                        "port": 8090,
                    },
                    "apps.fixity-contents.contents": {
                        "type": "gradle",
                        "task": ":apps:fixity-contents:contents:bootRun",
                        "module": "apps/fixity-contents/contents",
                        "port": 8096,
                    },
                },
            }
            persistence = {"tables": [], "datasources": []}
            environment = ccstack.build_local_environment_plan(root, "shopping", config, persistence)

        proxy_env = {
            (item["service"], item["name"]): item["value"]
            for item in environment["env"]
            if item["service"] == "apps.proxy.acme.contents"
        }
        target_key = ("apps.proxy.acme.contents", "FIXITY_SERVICE_URL")
        self.assertIn(target_key, proxy_env)
        self.assertEqual(proxy_env[target_key], "http://localhost:8096")

    def test_sibling_routing_resolves_command_service_with_known_port(self):
        """The generalized sibling resolver matches `type: command` services
        that declare an explicit positive integer port — not just gradle
        services. A proxy app that points at a command sibling by hostname
        receives the sibling's real local port."""
        candidate = {
            "path": "apps/proxy/acme/contents/src/main/resources/application.yml",
            "key": "shipping.service.url",
            "value": "http://shipping-service.example:9000/api",
            "confidence": "medium",
        }
        services = {
            "apps.proxy.acme.contents": {
                "type": "gradle",
                "module": "apps/proxy/acme/contents",
                "port": 8090,
            },
            "apps.command.shipping-service": {
                "type": "command",
                "module": "apps/command/shipping-service",
                "command": "python shipping_server.py",
                "port": 9000,
            },
        }

        port = ccstack.sibling_gradle_service_port(candidate, services)
        self.assertEqual(port, 9000, "command-type sibling with explicit port must be matched")

    def test_sibling_routing_resolves_compose_service_with_known_port(self):
        """The generalized sibling resolver matches `type: compose` services
        with explicit positive integer ports. Useful when a workspace runs a
        sibling service in a non-managed-infra docker container — the proxy
        app must route to the compose service's host port."""
        candidate = {
            "path": "apps/proxy/acme/contents/src/main/resources/application.yml",
            "key": "legacy-billing.service.url",
            "value": "http://legacy-billing.example/api",
            "confidence": "medium",
        }
        services = {
            "apps.proxy.acme.contents": {
                "type": "gradle",
                "module": "apps/proxy/acme/contents",
                "port": 8090,
            },
            "apps.compose.legacy-billing": {
                "type": "compose",
                "compose": "/path/to/docker-compose.legacy.yml",
                "service": "legacy-billing",
                "port": 7000,
            },
        }

        port = ccstack.sibling_gradle_service_port(candidate, services)
        self.assertEqual(port, 7000, "compose-type sibling with explicit port must be matched")

    def test_sibling_routing_excludes_managed_infra_services(self):
        """The generalized resolver MUST NOT route to managed infra services
        whose name starts with `devstack.infra.` even if such a service has a
        port that matches the candidate. Those services are the mock/db/cache
        target of routing, never a sibling — including them would create
        circular routing (mockserver-by-port collapsing back to mockserver).
        """
        candidate = {
            "path": "apps/proxy/acme/contents/src/main/resources/application.yml",
            "key": "any.upstream.url",
            "value": "http://localhost:1080/anything",
            "confidence": "medium",
        }
        services = {
            "apps.proxy.acme.contents": {
                "type": "gradle",
                "module": "apps/proxy/acme/contents",
                "port": 8090,
            },
            "ccstack.infra.mock-http": {
                "type": "compose",
                "service": "mock-http",
                "port": 1080,
            },
        }

        port = ccstack.sibling_gradle_service_port(candidate, services)
        self.assertIsNone(
            port,
            "managed-infra services must be excluded from the sibling index",
        )

    def test_runtime_service_port_index_includes_all_routable_types(self):
        """The runtime_service_port_index enumerates every type listed in
        SIBLING_ROUTABLE_SERVICE_TYPES that declares a positive integer port.
        Confirms the type-filter generalization without going through the
        full sibling resolver."""
        services = {
            "apps.x.gradle-app": {"type": "gradle", "port": 8001},
            "apps.x.command-app": {"type": "command", "port": 8002},
            "apps.x.compose-app": {"type": "compose", "port": 8003},
            "apps.x.no-port":     {"type": "gradle"},
            "apps.x.zero-port":   {"type": "gradle", "port": 0},
            "apps.x.string-port": {"type": "gradle", "port": "not-an-int"},
            "ccstack.infra.mock-http": {"type": "compose", "port": 1080},
            "apps.x.unknown-type": {"type": "custom", "port": 9999},
        }

        index = ccstack.runtime_service_port_index(services)
        included_names = sorted(entry["name"] for entry in index)
        self.assertEqual(
            included_names,
            ["apps.x.command-app", "apps.x.compose-app", "apps.x.gradle-app"],
        )

    def test_write_mockserver_expectations_appends_catchall_dummy(self):
        """Generic, domain-agnostic catch-all expectation must be appended as
        the LAST mockserver expectation so unmatched routes return 200 with a
        benign empty-collection envelope instead of mockserver's default 404.
        Replaces the previous samplegoods-only hardcoded behavior."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mockserver-expectations.json"
            # A non-samplegoods, non-content candidate — proves the catch-all is
            # appended regardless of keyword triggers.
            candidates = [
                {
                    "path": "apps/proxy/acme/contents/src/main/resources/application.yml",
                    "key": "external.unknown.service.url",
                    "value": "http://unknown-external.example/api",
                    "confidence": "medium",
                },
            ]
            ccstack.write_mockserver_expectations(path, candidates)
            expectations = json.loads(path.read_text())

        self.assertTrue(expectations, "expected at least one expectation written")
        last = expectations[-1]
        self.assertEqual(last["httpRequest"]["path"], "/.*")
        # No method filter — must catch every HTTP verb.
        self.assertNotIn("method", last["httpRequest"])
        self.assertEqual(last["httpResponse"]["statusCode"], 200)
        body = json.loads(last["httpResponse"]["body"])
        self.assertEqual(body["status"], 200)
        self.assertEqual(body["message"], "devstack local dummy")
        self.assertIn("items", body["data"])
        self.assertEqual(body["data"]["items"], [])
        self.assertEqual(body["data"]["totalElements"], 0)

    def test_write_mockserver_expectations_keeps_samplegoods_specific_with_catchall_last(self):
        """With the shipped shopping overrides loaded, the engine emits the
        historical samplegoods-specific expectations FIRST and the generic catch-all
        LAST. Mockserver evaluates expectations in registration order, so the
        specific samplegoods routes win over the trailing /.* fallback.

        Behavior-parity test: the assertion contract is identical to the
        pre-refactor hardcoded-engine version. The only change is that the
        samplegoods routes now come from the workspace overrides JSON rather than
        an in-engine literal branch.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mockserver-expectations.json"
            candidates = [
                {
                    "path": "apps/proxy/acme/samplegoods/src/main/resources/application-local.yml",
                    "key": "samplegoods.service.url",
                    "value": "http://localhost:8084",
                    "confidence": "medium",
                },
            ]
            overrides = shopping_overrides_dict()
            ccstack.write_mockserver_expectations(path, candidates, overrides=overrides)
            expectations = json.loads(path.read_text())

        paths = [exp.get("httpRequest", {}).get("path", "") for exp in expectations]
        self.assertIn("/sample-goods", paths)
        # The catch-all is the LAST entry — mockserver evaluates in
        # registration order, so the specific samplegoods routes win.
        self.assertEqual(paths[-1], "/.*")
        self.assertGreater(
            paths.index("/.*"),
            paths.index("/sample-goods"),
            "catch-all must be registered AFTER /sample-goods",
        )

    def test_write_mockserver_expectations_no_overrides_emits_only_value_path_and_catchall(self):
        """A workspace WITHOUT a domain overrides file produces purely generic
        mockserver output: the candidate's own URL path becomes a route, and
        the trailing generic /.* catchall is the only fallback. Crucially, no
        samplegoods/content-display literals appear — proving the engine has no
        baked-in shopping domain knowledge."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mockserver-expectations.json"
            candidates = [
                {
                    "path": "apps/proxy/acme/samplegoods/src/main/resources/application-local.yml",
                    "key": "samplegoods.service.url",
                    "value": "http://localhost:8084/api/v1/goods",
                    "confidence": "medium",
                },
            ]
            # No overrides argument => generic engine.
            ccstack.write_mockserver_expectations(path, candidates)
            expectations = json.loads(path.read_text())

        paths = [exp.get("httpRequest", {}).get("path", "") for exp in expectations]
        # Value-path route is present.
        self.assertIn("/api/v1/goods", paths)
        # Catchall is last.
        self.assertEqual(paths[-1], "/.*")
        # Domain-specific routes MUST NOT appear without overrides.
        self.assertNotIn("/sample-goods", paths)
        self.assertNotIn("/sample-categories", paths)
        self.assertNotIn("/content-display", paths)
        # Generic envelope is the only response body shape used.
        for expectation in expectations:
            body_text = expectation.get("httpResponse", {}).get("body", "")
            self.assertNotIn("ccstack local samplegoods", body_text)
            self.assertNotIn("contentDisplaySeq", body_text)

    # ------------------------------------------------------------------
    # Regression: mockserver self-identifying response headers.
    #
    # Every expectation ccstack writes (specific override responses AND the
    # generic catch-all) must carry two response headers so callers and
    # tests can detect mock-fallback usage post-hoc:
    #
    #   X-Devstack-Mock: true
    #   X-Devstack-Routed-From: <original endpoint host | "unmatched" | "unknown">
    #
    # The literal header pair is also the machine-readable marker a
    # follow-up status-counter task will grep for to aggregate mock-fallback
    # call counts.
    # ------------------------------------------------------------------

    def _header_pairs(self, headers):
        """Flatten the mockserver header list-of-objects into a dict of
        first-value lookups. Mockserver stores headers as
        [{"name": str, "values": [str, ...]}, ...]; tests want a flat
        dict for readable assertions."""
        result = {}
        for item in headers or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            values = item.get("values") or []
            if isinstance(name, str) and values:
                result[name] = values[0]
        return result

    def test_mockserver_catchall_carries_self_identifying_headers(self):
        """The generic catch-all expectation must declare
        X-Devstack-Mock: true and X-Devstack-Routed-From: unmatched alongside
        the pre-existing Content-Type so post-hoc inspection can prove a
        200 came from mockserver rather than a real sibling service."""
        expectation = ccstack.mockserver_catchall_expectation()
        headers = self._header_pairs(expectation["httpResponse"]["headers"])

        self.assertEqual(headers.get("X-Devstack-Mock"), "true")
        self.assertEqual(headers.get("X-Devstack-Routed-From"), "unmatched")
        # Additive: Content-Type still present.
        self.assertEqual(headers.get("Content-Type"), "application/json")
        # Body / priority / route untouched.
        self.assertEqual(expectation["httpRequest"]["path"], "/.*")
        self.assertEqual(expectation["httpResponse"]["statusCode"], 200)
        self.assertEqual(expectation["priority"], -100)
        body = json.loads(expectation["httpResponse"]["body"])
        self.assertEqual(body["message"], "devstack local dummy")

    def test_mockserver_specific_expectation_carries_routed_from_host(self):
        """A value-path expectation generated from a candidate URL must
        stamp X-Devstack-Routed-From with that candidate's hostname so a
        test can identify which real upstream the mock was shadowing."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mockserver-expectations.json"
            candidates = [
                {
                    "path": "apps/proxy/acme/samplegoods/src/main/resources/application-local.yml",
                    "key": "external.partner.service.url",
                    "value": "http://partner-api.example.com/api/v1/goods",
                    "confidence": "medium",
                },
            ]
            ccstack.write_mockserver_expectations(path, candidates)
            expectations = json.loads(path.read_text())

        # Locate the value-path expectation by route.
        match = next(
            exp for exp in expectations
            if exp.get("httpRequest", {}).get("path") == "/api/v1/goods"
        )
        headers = self._header_pairs(match["httpResponse"]["headers"])
        self.assertEqual(headers.get("X-Devstack-Mock"), "true")
        self.assertEqual(headers.get("X-Devstack-Routed-From"), "partner-api.example.com")
        # Body unchanged: generic envelope.
        body = json.loads(match["httpResponse"]["body"])
        self.assertEqual(body["message"], "devstack local dummy")

    def test_mockserver_override_route_expectation_carries_routed_from_host(self):
        """An override-driven key_route_triggers expectation (e.g. the
        shopping workspace's samplegoods-* family) must attribute to the
        candidate hostname that triggered the keyword, not "unknown"."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mockserver-expectations.json"
            candidates = [
                {
                    "path": "apps/proxy/acme/samplegoods/src/main/resources/application-local.yml",
                    "key": "samplegoods.service.url",
                    "value": "http://samplegoods-api.acme.com",
                    "confidence": "medium",
                },
            ]
            overrides = shopping_overrides_dict()
            ccstack.write_mockserver_expectations(path, candidates, overrides=overrides)
            expectations = json.loads(path.read_text())

        # Pick one override-driven route that came from key_route_triggers.
        match = next(
            exp for exp in expectations
            if exp.get("httpRequest", {}).get("path") == "/sample-goods"
        )
        headers = self._header_pairs(match["httpResponse"]["headers"])
        self.assertEqual(headers.get("X-Devstack-Mock"), "true")
        self.assertEqual(headers.get("X-Devstack-Routed-From"), "samplegoods-api.acme.com")
        self.assertEqual(headers.get("Content-Type"), "application/json")

        # The override-driven body is preserved unchanged (proves the
        # change is purely header-additive).
        body = json.loads(match["httpResponse"]["body"])
        self.assertIn("data", body)
        self.assertTrue(
            any("ccstack local samplegoods goods" == row.get("goodsName") for row in body.get("data") or []),
            "expected the override response body to survive header stamping",
        )

    def test_write_mockserver_expectations_stamps_every_expectation(self):
        """Top-down invariant: every entry in the materialized file —
        specific routes AND the catch-all — must carry both
        self-identifying headers. Status-counter tooling depends on
        scanning the file with this guarantee."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mockserver-expectations.json"
            candidates = [
                {
                    "path": "apps/proxy/acme/samplegoods/src/main/resources/application-local.yml",
                    "key": "samplegoods.service.url",
                    "value": "http://samplegoods-api.acme.com/api/v1/goods",
                    "confidence": "medium",
                },
                {
                    "path": "apps/proxy/acme/contents/src/main/resources/application-local.yml",
                    "key": "contents.service.url",
                    "value": "http://contents-api.acme.com",
                    "confidence": "medium",
                },
            ]
            overrides = shopping_overrides_dict()
            ccstack.write_mockserver_expectations(path, candidates, overrides=overrides)
            expectation_text = path.read_text()
            expectations = json.loads(expectation_text)

        self.assertGreater(len(expectations), 1, "expected specific + catchall expectations")
        for expectation in expectations:
            headers = self._header_pairs(expectation["httpResponse"]["headers"])
            with self.subTest(route=expectation["httpRequest"].get("path")):
                self.assertEqual(headers.get("X-Devstack-Mock"), "true")
                routed_from = headers.get("X-Devstack-Routed-From")
                self.assertIsNotNone(routed_from)
                self.assertNotEqual(routed_from, "")
                # Content-Type is additive (still present).
                self.assertEqual(headers.get("Content-Type"), "application/json")

        # Catch-all is last AND attributes to "unmatched" so status
        # counters can separate it from per-host specific responses.
        last = expectations[-1]
        self.assertEqual(last["httpRequest"]["path"], "/.*")
        self.assertEqual(
            self._header_pairs(last["httpResponse"]["headers"]).get("X-Devstack-Routed-From"),
            "unmatched",
        )

        # Machine-readable marker for the follow-up status-counter task:
        # the literal header pair must appear verbatim in the JSON file,
        # so a simple substring scan can count mock-fallback usage without
        # needing to parse mockserver's schema.
        self.assertIn('"X-Devstack-Mock"', expectation_text)
        self.assertIn('"X-Devstack-Routed-From"', expectation_text)

    def test_mockserver_json_expectation_default_routed_from_is_unknown(self):
        """The low-level builder must default to ``unknown`` when the
        caller has no host context (e.g. the runtime injector path in
        ``ensure_mockserver_expectation_routes`` which only receives bare
        route strings). The header is always present so callers can
        rely on its existence."""
        # Default kwarg path (no host context).
        default_expectation = ccstack.mockserver_json_expectation(
            "/runtime/route", {"status": 200, "message": "ok"},
        )
        default_headers = self._header_pairs(default_expectation["httpResponse"]["headers"])
        self.assertEqual(default_headers.get("X-Devstack-Mock"), "true")
        self.assertEqual(default_headers.get("X-Devstack-Routed-From"), "unknown")

        # Explicit routed_from path.
        stamped = ccstack.mockserver_json_expectation(
            "/explicit/route",
            {"status": 200, "message": "ok"},
            routed_from="upstream.example.com",
        )
        stamped_headers = self._header_pairs(stamped["httpResponse"]["headers"])
        self.assertEqual(stamped_headers.get("X-Devstack-Routed-From"), "upstream.example.com")

    def test_mockserver_dummy_routes_back_compat_returns_route_strings(self):
        """``mockserver_dummy_routes`` is the back-compat wrapper around
        the new ``mockserver_dummy_routes_with_hosts`` tuple helper.
        Existing external callers (and the 227-test baseline) still get
        a flat List[str]; new callers can drop down to the tuple variant
        when they need host attribution."""
        candidates = [
            {
                "path": "apps/proxy/acme/samplegoods/src/main/resources/application-local.yml",
                "key": "samplegoods.service.url",
                "value": "http://samplegoods-api.acme.com/api/v1/goods",
                "confidence": "medium",
            },
        ]
        flat = ccstack.mockserver_dummy_routes(candidates)
        self.assertEqual(flat, ["/api/v1/goods"])

        tuples = ccstack.mockserver_dummy_routes_with_hosts(candidates)
        self.assertEqual(tuples, [("/api/v1/goods", "samplegoods-api.acme.com")])

    def test_workspace_analysis_configures_spring_batch_for_local_database_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            module = root / "apps" / "proxy" / "acme" / "contents"
            module.mkdir(parents=True)
            (module / "build.gradle.kts").write_text(
                """
dependencies {
    implementation("org.springframework.batch:spring-batch-core")
}
""".strip()
                + "\n"
            )
            config = {
                "services": {
                    "apps.proxy.acme.contents": {
                        "type": "gradle",
                        "task": ":apps:proxy:acme:contents:bootRun",
                        "module": "apps/proxy/acme/contents",
                    },
                    "apps.proxy.acme.shopping": {
                        "type": "gradle",
                        "task": ":apps:proxy:acme:shopping:bootRun",
                        "module": "apps/proxy/acme/shopping",
                    },
                }
            }
            persistence = {
                "datasources": [
                    {
                        "path": "apps/proxy/acme/contents/src/main/resources/application.yml",
                        "engine": "mysql",
                        "url": "jdbc:mysql://${db.contents.host}:3306/contents",
                        "host": "${db.contents.host}",
                    }
                ],
                "tables": [{"name": "contents", "columns": [{"name": "id", "type": "long"}]}],
            }

            environment = ccstack.build_local_environment_plan(root, "shopping", config, persistence)
            managed, materialized = ccstack.materialize_managed_environment(
                "shopping",
                root,
                config,
                {"environment": environment, "persistence": persistence},
            )

        env = {(item["service"], item["name"]): item["value"] for item in environment["env"]}
        args = {(item["service"], item["value"]) for item in environment["args"]}
        generated = {item["technology"]: item for item in environment["generated"]}
        self.assertEqual(generated["mysql"]["image"], "mysql:8.4")
        self.assertIn(
            "jdbc:mysql://localhost:3306/contents",
            env[("apps.proxy.acme.contents", "SPRING_DATASOURCE_URL")],
        )
        self.assertEqual(env[("apps.proxy.acme.contents", "SPRING_DATASOURCE_PORT")], "3306")
        self.assertEqual(env[("apps.proxy.acme.contents", "DB_CONTENTS_PORT")], "3306")
        self.assertNotIn(
            "jdbc:h2:",
            env[("apps.proxy.acme.contents", "SPRING_DATASOURCE_URL")],
        )
        self.assertEqual(
            env[("apps.proxy.acme.contents", "SPRING_BATCH_JDBC_INITIALIZE_SCHEMA")],
            "never",
        )
        self.assertEqual(env[("apps.proxy.acme.contents", "SPRING_BATCH_JOB_ENABLED")], "false")
        self.assertIn(
            ("apps.proxy.acme.contents", "--spring.batch.jdbc.initialize-schema=never"),
            args,
        )
        self.assertIn(("apps.proxy.acme.contents", "--spring.batch.job.enabled=false"), args)
        self.assertNotIn(("apps.proxy.acme.shopping", "SPRING_BATCH_JOB_ENABLED"), env)
        self.assertEqual(
            materialized["gradle_runtime_dependencies"],
            {":apps:proxy:acme:contents": ["com.mysql:mysql-connector-j:8.0.33"]},
        )
        self.assertIn(
            '":apps:proxy:acme:contents": ["com.mysql:mysql-connector-j:8.0.33"]',
            Path(materialized["gradle_init"]).read_text(),
        )

    def test_workspace_analysis_ignores_dynamic_sql_table_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            source = root / "apps" / "proxy" / "acme" / "contents" / "src" / "main" / "kotlin"
            source.mkdir(parents=True)
            (source / "Repository.kt").write_text(
                """
class Repository {
    fun find(fromClause: String) = jdbcTemplate.query(
        "select id, status from $fromClause where status = :status"
    )
    fun stable() = jdbcTemplate.query("select id, title from article where display = 'Y'")
}
""".strip()
                + "\n"
            )

            persistence = ccstack.inspect_persistence(root)
            schema = ccstack.render_local_schema_sql(persistence["tables"], "h2")

        table_names = [table["name"] for table in persistence["tables"]]
        self.assertNotIn("$fromClause", table_names)
        self.assertIn("article", table_names)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS $fromClause", schema)

    def test_workspace_analysis_ignores_sql_clause_tokens_as_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            mapper_dir = root / "apps" / "fixity-shopping" / "keyword" / "src" / "main" / "resources" / "mapper"
            mapper_dir.mkdir(parents=True)
            (mapper_dir / "KeywordMapper.xml").write_text(
                """
<mapper namespace="com.example.KeywordMapper">
  <select id="find">
    select id, name from keyword where id = #{id} for update
    union all select id, name from keyword_audit where created_at between :from and :to
  </select>
</mapper>
""".strip()
                + "\n"
            )

            persistence = ccstack.inspect_persistence(root)
            schema = ccstack.render_local_schema_sql(persistence["tables"], "h2")

        table_names = [table["name"] for table in persistence["tables"]]
        self.assertIn("keyword", table_names)
        self.assertIn("keyword_audit", table_names)
        self.assertNotIn("for", table_names)
        self.assertNotIn("to", table_names)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS for", schema)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS to", schema)

    def test_workspace_apply_creates_h2_schemas_for_qualified_table_names(self):
        schema = ccstack.render_local_schema_sql(
            [
                {
                    "name": "dbBilling.tGoods",
                    "columns": [
                        {"name": "nSellerSeq", "type": "long"},
                        {"name": "sDisplayYN", "type": "string"},
                    ],
                }
            ],
            "h2",
        )

        self.assertIn("CREATE SCHEMA IF NOT EXISTS dbBilling;", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS dbBilling.tGoods", schema)
        self.assertLess(
            schema.index("CREATE SCHEMA IF NOT EXISTS dbBilling;"),
            schema.index("CREATE TABLE IF NOT EXISTS dbBilling.tGoods"),
        )

    def test_workspace_apply_creates_mssql_schemas_for_qualified_table_names(self):
        schema = ccstack.render_local_schema_sql(
            [
                {
                    "name": "s_commerce.t_catalog_content_provider",
                    "columns": [
                        {"name": "provider_no", "type": "long", "id": True},
                    ],
                }
            ],
            "mssql",
        )

        self.assertIn("IF SCHEMA_ID(N's_commerce') IS NULL EXEC(N'CREATE SCHEMA [s_commerce]');", schema)
        self.assertIn("IF OBJECT_ID(N's_commerce.t_catalog_content_provider', N'U') IS NULL", schema)
        self.assertLess(
            schema.index("IF SCHEMA_ID(N's_commerce') IS NULL"),
            schema.index("CREATE TABLE s_commerce.t_catalog_content_provider"),
        )

    def test_local_mssql_schema_adds_missing_columns_for_existing_local_tables(self):
        schema = ccstack.render_create_table_statement(
            {
                "name": "s_commerce.t_catalog_content_provider",
                "columns": [
                    {"name": "provider_no", "type": "long", "id": True},
                    {"name": "insert_date_time", "type": "timestamp"},
                ],
            },
            "mssql",
        )

        self.assertIn("IF COL_LENGTH(N's_commerce.t_catalog_content_provider', N'insert_date_time') IS NULL", schema)
        self.assertIn("ALTER TABLE s_commerce.t_catalog_content_provider ADD insert_date_time DATETIME2;", schema)

    def test_h2_init_scripts_use_content_versioned_paths(self):
        path = Path("/tmp/devstack-local-schema-h2.sql")
        first = ccstack.versioned_sql_path(path, "CREATE TABLE sample (id INT);")
        second = ccstack.versioned_sql_path(path, "CREATE TABLE sample (id BIGINT);")

        self.assertRegex(first.name, r"^devstack-local-schema-h2-[0-9a-f]{12}\.sql$")
        self.assertNotEqual(first, second)

    def test_local_h2_schema_uses_permissive_string_lengths_for_seed_data(self):
        self.assertEqual(
            ccstack.local_sql_type({"name": "kind", "type": "string", "length": 6}, "h2"),
            "VARCHAR(255)",
        )
        self.assertEqual(
            ccstack.local_sql_type({"name": "kind", "type": "string", "length": 1024}, "h2"),
            "VARCHAR(1024)",
        )

    def test_local_h2_schema_quotes_mixed_case_identifiers_for_exposed(self):
        schema = ccstack.render_create_table_statement(
            {
                "name": "tSampleGoods",
                "columns": [
                    {"name": "nGoodsSeq", "type": "long", "id": True},
                    {"name": "sGoodsName", "type": "string"},
                ],
            },
            "h2",
        )

        self.assertIn("CREATE TABLE IF NOT EXISTS tSampleGoods", schema)
        self.assertIn('"nGoodsSeq" BIGINT', schema)
        self.assertIn('PRIMARY KEY ("nGoodsSeq")', schema)

    def test_local_h2_schema_leaves_snake_case_identifiers_unquoted_for_exposed(self):
        table = {
            "name": "s_commerce.t_catalog_content_provider",
            "columns": [
                {"name": "provider_no", "type": "long", "id": True},
                {"name": "provider_name", "type": "string"},
                {"name": "external_provide_status", "type": "string"},
            ],
        }

        schema = ccstack.render_create_table_statement(table, "h2")
        dml = ccstack.render_local_dml_sql([table], "h2")

        self.assertIn("CREATE TABLE IF NOT EXISTS s_commerce.t_catalog_content_provider", schema)
        self.assertIn("provider_no BIGINT", schema)
        self.assertIn("PRIMARY KEY (provider_no)", schema)
        self.assertIn(
            "MERGE INTO s_commerce.t_catalog_content_provider "
            "(provider_no, provider_name, external_provide_status) KEY(provider_no)",
            dml,
        )

    def test_local_schema_merges_duplicate_table_columns_before_dml(self):
        tables = [
            {
                "name": "tGenericList",
                "columns": [
                    {"name": "nListSeq", "type": "long", "id": True},
                    {"name": "sTitle", "type": "string"},
                ],
            },
            {
                "name": "tGenericList",
                "columns": [
                    {"name": "nListSeq", "type": "long", "id": True},
                    {"name": "sMemberIP", "type": "string"},
                    {"name": "nListStatus", "type": "int"},
                ],
            },
        ]

        schema = ccstack.render_local_schema_sql(tables, "h2")
        dml = ccstack.render_local_dml_sql(tables, "h2")

        self.assertEqual(schema.count("CREATE TABLE IF NOT EXISTS tGenericList"), 1)
        self.assertIn('"sMemberIP" VARCHAR(255)', schema)
        self.assertIn('"nListStatus" INT', schema)
        self.assertIn("MERGE INTO tGenericList", dml)
        self.assertIn('"sMemberIP"', dml)

    def test_exposed_scan_detects_custom_enumeration_by_value_columns(self):
        columns = ccstack.inspect_exposed_columns(
            """
            val dayDiscountYN =
                enumerationByValue<YN, String>("sDayDiscountYN", ColumnSqlType.CHAR_1)
            val state = enumerationByValue<GoodsState, Int>("nState", ColumnSqlType.TINYINT)
            val displayYN = enumerationByValue<YN, String>("emDisplayYN", ColumnSqlType.ENUM)
            """
        )

        names = {column["name"] for column in columns}
        self.assertIn("sDayDiscountYN", names)
        self.assertEqual(
            next(column["type"] for column in columns if column["name"] == "nState"),
            "integer",
        )
        self.assertIn("emDisplayYN", names)

    def test_exposed_scan_detects_timestamp_with_timezone_columns(self):
        columns = ccstack.inspect_exposed_columns(
            """
            val insertDateTime = timestampWithTimeZone("insert_date_time")
            val updateDateTime = timestampWithTimeZone("update_date_time").nullable()
            """
        )

        by_name = {column["name"]: column for column in columns}
        self.assertEqual(by_name["insert_date_time"]["type"], "timestamp")
        self.assertEqual(by_name["update_date_time"]["type"], "timestamp")
        self.assertTrue(by_name["update_date_time"]["nullable"])

    def test_local_seed_values_use_known_shopping_enum_literals(self):
        """With the shipped shopping overrides loaded, the engine emits the
        historical ENABLED / USE / WISH enum literals for the shopping
        column names. Behavior parity proven via the overrides path."""
        overrides = shopping_overrides_dict()
        self.assertEqual(
            ccstack.seed_value_for_column(
                {"name": "external_provide_status", "type": "string"}, "provider", overrides
            ),
            "ENABLED",
        )
        self.assertEqual(
            ccstack.seed_value_for_column(
                {"name": "folder_display_status", "type": "string"}, "wish", overrides
            ),
            "USE",
        )
        self.assertEqual(
            ccstack.seed_value_for_column(
                {"name": "folder_origin_service_type", "type": "string"}, "wish", overrides
            ),
            "WISH",
        )

    def test_local_seed_values_no_overrides_use_only_generic_rules(self):
        """Without any overrides loaded, shopping-specific column names
        receive the engine's generic name/type heuristics — never the
        ENABLED/USE/WISH enum literals. Proves the engine has no baked-in
        shopping domain knowledge."""
        # `external_provide_status` matches the generic "status" path =>
        # generic "ccstack" fallback for unknown string columns.
        self.assertNotEqual(
            ccstack.seed_value_for_column({"name": "external_provide_status", "type": "string"}, "provider"),
            "ENABLED",
        )
        # `folder_display_status` contains "display" which the generic rule
        # routes to the Y/1/true display-flag default — NOT "USE".
        self.assertNotEqual(
            ccstack.seed_value_for_column({"name": "folder_display_status", "type": "string"}, "wish"),
            "USE",
        )
        # `folder_origin_service_type` is a plain string column with no
        # match against the generic heuristics — returns "ccstack".
        self.assertNotEqual(
            ccstack.seed_value_for_column({"name": "folder_origin_service_type", "type": "string"}, "wish"),
            "WISH",
        )

    def test_rest_dummy_verification_requires_2xx_non_empty_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Deploy the shipped shopping overrides into the workspace root so
            # the engine emits the historical samplegoods REST probes for
            # workspaces whose service name contains "samplegoods".
            deploy_shopping_overrides(root)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {"apps.fixity-samplegoods.samplegoods": {"port": 8099}},
                },
            )

            class Response:
                status = 200

                def __init__(self, body):
                    self.body = body

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return False

                def read(self):
                    return json.dumps(self.body).encode("utf-8")

            def fake_urlopen(request, timeout):
                url = request if isinstance(request, str) else request.full_url
                if "/sample-goods" in url:
                    return Response({"data": [{"goodsSeq": 1, "goodsName": "devstack dummy goods"}]})
                if "/featured-goods" in url:
                    return Response({"data": [{"goodsSeq": 1, "goodsName": "devstack dummy goods"}]})
                return Response({"data": [{"categorySeq": 1, "categoryName": "devstack dummy category"}]})

            with mock.patch.object(ccstack.urllib.request, "urlopen", side_effect=fake_urlopen):
                verified, detail = workspace.verify_service_api_dummy_data("apps.fixity-samplegoods.samplegoods")

        self.assertTrue(verified)
        self.assertIn("REST samplegoods goods", detail)

    def test_rest_dummy_verification_fails_when_any_samplegoods_probe_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Deploy the shipped shopping overrides into the workspace root so
            # the engine emits the historical samplegoods REST probes.
            deploy_shopping_overrides(root)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {"apps.fixity-samplegoods.samplegoods": {"port": 8099}},
                },
            )

            class Response:
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return False

                def read(self):
                    return json.dumps({"data": []}).encode("utf-8")

            with mock.patch.object(ccstack.urllib.request, "urlopen", return_value=Response()):
                verified, detail = workspace.verify_service_api_dummy_data("apps.fixity-samplegoods.samplegoods")

        self.assertFalse(verified)
        self.assertIn("empty data", detail)

    def test_local_dml_seeds_samplegoods_goods_with_valid_domain_values(self):
        """With the shipped shopping overrides loaded, the engine seeds the
        tSample* tables with the historical preferred-column rows. Behavior
        parity proven via the overrides path: the same assertion contract
        as the pre-refactor hardcoded-engine version."""
        tables = [
            {
                "name": "tSampleCategory",
                "columns": [
                    {"name": "nSampleCategorySeq", "type": "long", "id": True},
                    {"name": "sSampleCategoryName", "type": "varchar"},
                    {"name": "emDisplayYN", "type": "enumerationByValue"},
                    {"name": "nSort", "type": "integer"},
                    {"name": "dtCreateDate", "type": "date"},
                    {"name": "dtUpdateDate", "type": "date"},
                    {"name": "emCategoryType", "type": "enumerationByValue"},
                ],
            },
            {
                "name": "tSampleGoods",
                "columns": [
                    {"name": "nGoodsSeq", "type": "long", "id": True},
                    {"name": "nMarketPlaceSeq", "type": "integer"},
                    {"name": "nSellerSeq", "type": "long"},
                    {"name": "nProductSeq", "type": "long"},
                    {"name": "sGoodsName", "type": "varchar"},
                    {"name": "nStandardPrice", "type": "long"},
                    {"name": "nSalePrice", "type": "long"},
                    {"name": "nDeliverySeq", "type": "long"},
                    {"name": "nDeliveryPrice", "type": "long"},
                    {"name": "fDiscountRatio", "type": "float"},
                    {"name": "sDayDiscountYN", "type": "enumerationByValue"},
                    {"name": "nOneBuyLimit", "type": "integer"},
                    {"name": "nState", "type": "integer"},
                    {"name": "sDisplayYN", "type": "enumerationByValue"},
                    {"name": "sDisplayBlogYN", "type": "enumerationByValue"},
                    {"name": "dtSaleStartDate", "type": "date"},
                    {"name": "dtSaleStartTime", "type": "time"},
                    {"name": "dtSaleEndDate", "type": "date"},
                    {"name": "dtSaleEndTime", "type": "time"},
                    {"name": "dtDisplayStartDate", "type": "datetime"},
                    {"name": "dtUpdateDate", "type": "datetime"},
                    {"name": "sStockCountDisplayYN", "type": "enumerationByValue"},
                    {"name": "emPayMethod", "type": "enumerationByValue"},
                    {"name": "emOrderPageShortCut", "type": "enumerationByValue"},
                ],
            },
        ]

        overrides = shopping_overrides_dict()
        dml = ccstack.render_local_dml_sql(tables, "h2", overrides=overrides)

        self.assertIn("MERGE INTO tSampleCategory", dml)
        self.assertIn("'N'", dml)
        self.assertIn("MERGE INTO tSampleGoods", dml)
        self.assertIn("ccstack local samplegoods goods", dml)
        self.assertIn('"nState"', dml)
        self.assertIn("5", dml)
        schema = ccstack.render_local_schema_sql(tables, "h2")
        self.assertIn('"nState" INT', schema)

    def test_local_seed_statements_no_overrides_use_only_generic_seed(self):
        """Without any overrides loaded, the engine emits ONLY the generic
        seed statement for each detected table — no domain literals appear.
        Proves the engine has no baked-in shopping domain knowledge: the
        tSampleGoods table receives a generic 3-row INSERT, never the
        "ccstack local samplegoods goods" literal from the shopping overrides."""
        tables = [
            {
                "name": "tSampleGoods",
                "columns": [
                    {"name": "nGoodsSeq", "type": "long", "id": True},
                    {"name": "sTitle", "type": "varchar"},
                    {"name": "nState", "type": "integer"},
                ],
            },
        ]

        # No overrides argument => generic engine.
        dml = ccstack.render_local_dml_sql(tables, "h2")

        self.assertIn("MERGE INTO tSampleGoods", dml)
        # The shopping-specific domain literal MUST NOT appear without
        # overrides — the generic engine builds its label from the table
        # name (lowercased, "_" -> " ") => "tsamplegoods".
        self.assertNotIn("ccstack local samplegoods goods", dml)
        # Title-shaped columns use the label-based generic string. The 3-row
        # contract offsets textual rows with " 2"/" 3" suffixes.
        self.assertIn("devstack local tsamplegoods", dml.lower())
        # Exactly 3 row-tuples present, matching the GENERIC_LOCAL_SEED_ROW_COUNT
        # contract (3-5 rows, default 3).
        self.assertEqual(dml.lower().count("devstack local tsamplegoods"), 3)

    def test_local_seed_values_match_inferred_date_and_time_types(self):
        self.assertEqual(
            ccstack.seed_value_for_column({"name": "dtCreateDate", "type": "date"}, "sample"),
            "2026-01-01",
        )
        self.assertEqual(
            ccstack.seed_value_for_column({"name": "dtCreateTime", "type": "time"}, "sample"),
            "10:00:00",
        )
        self.assertEqual(
            ccstack.seed_value_for_column({"name": "dtCreatedAt", "type": "timestamp"}, "sample"),
            "2026-01-01 10:00:00",
        )
        self.assertEqual(
            ccstack.seed_value_for_column({"name": "dtDisplayStartTime", "type": "timestamp"}, "sample"),
            "2026-01-01 10:00:00",
        )
        self.assertEqual(
            ccstack.seed_value_for_column({"name": "nDisplaySort", "type": "int"}, "sample"),
            1,
        )
        self.assertEqual(
            ccstack.seed_value_for_column({"name": "nContentSeq", "type": "long"}, "sample"),
            1,
        )
        self.assertEqual(
            ccstack.seed_value_for_column({"name": "sDisplayYN", "type": "string"}, "sample"),
            "Y",
        )

    def test_workspace_analysis_does_not_treat_plain_main_modules_as_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            api = root / "contents-api"
            batch = root / "contents-batch"
            library = root / "contents-core"
            lib_cache = root / "lib" / "cache"
            apps_cache = root / "apps" / "fixity-shopping" / "cache"
            (api / "src" / "main" / "resources").mkdir(parents=True)
            (batch / "src" / "main" / "kotlin").mkdir(parents=True)
            (library / "src" / "main" / "java").mkdir(parents=True)
            (lib_cache / "src" / "main" / "java").mkdir(parents=True)
            (apps_cache / "src" / "main" / "resources").mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "build.gradle").write_text(
                "plugins { id 'org.springframework.boot' version '3.2.0' apply false }\n"
            )
            (root / "settings.gradle").write_text(
                "include ':contents-api', ':contents-batch', ':contents-core', ':lib:cache', ':apps:fixity-shopping:cache'\n"
            )
            (api / "build.gradle").write_text("plugins { id 'org.springframework.boot' version '3.2.0' }\n")
            (api / "src" / "main" / "resources" / "application.yml").write_text("server:\n  port: 18081\n")
            (batch / "build.gradle").write_text("plugins { id 'application' }\n")
            (batch / "src" / "main" / "kotlin" / "BatchJob.kt").write_text(
                "fun main() { println(\"batch\") }\n"
            )
            (library / "build.gradle").write_text("plugins { id 'java-library' }\n")
            (library / "src" / "main" / "java" / "Tool.java").write_text(
                "public class Tool { public static void main(String[] args) {} }\n"
            )
            (lib_cache / "build.gradle").write_text(
                """
plugins { id 'org.springframework.boot' version '3.2.0' }
tasks.register('bootRun') {}
""".strip()
                + "\n"
            )
            (lib_cache / "src" / "main" / "java" / "CacheHelper.java").write_text(
                "class CacheHelper {}\n"
            )
            (apps_cache / "build.gradle").write_text(
                """
plugins { id 'java-library' }
dependencies { implementation 'org.springframework.boot:spring-boot-starter-cache' }
""".strip()
                + "\n"
            )
            (apps_cache / "src" / "main" / "resources" / "shopping-cache.yml").write_text(
                "spring:\n  cache:\n    type: caffeine\n"
            )

            config = ccstack.inspect_workspace(root, "shopping")

        self.assertEqual(sorted(config["services"]), ["contents-api"])
        self.assertEqual(config["profiles"]["default"]["services"], ["contents-api"])

    def test_workspace_analysis_excludes_one_shot_web_none_applications(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            api = root / "apps" / "api"
            backfill = root / "apps" / "backfill"
            stream = root / "apps" / "cdc"
            (api / "src" / "main" / "resources").mkdir(parents=True)
            (backfill / "src" / "main" / "kotlin" / "com" / "example").mkdir(parents=True)
            (backfill / "src" / "main" / "resources").mkdir(parents=True)
            (stream / "src" / "main" / "kotlin" / "com" / "example").mkdir(parents=True)
            (stream / "src" / "main" / "resources").mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':apps:api', ':apps:backfill', ':apps:cdc'\n")
            (api / "build.gradle").write_text("plugins { id 'org.springframework.boot' version '3.2.0' }\n")
            (api / "src" / "main" / "resources" / "application.yml").write_text("server:\n  port: 18081\n")
            (backfill / "build.gradle").write_text(
                "dependencies { implementation 'org.springframework.boot:spring-boot-starter' }\n"
            )
            (backfill / "src" / "main" / "resources" / "application.yml").write_text(
                "spring:\n  main:\n    web-application-type: none\n"
            )
            (backfill / "src" / "main" / "kotlin" / "com" / "example" / "BackfillApplication.kt").write_text(
                """
package com.example

import org.springframework.boot.autoconfigure.SpringBootApplication
import org.springframework.boot.runApplication

@SpringBootApplication
class BackfillApplication

fun main(args: Array<String>) {
    runApplication<BackfillApplication>(*args)
}
""".strip()
                + "\n"
            )
            (backfill / "src" / "main" / "kotlin" / "com" / "example" / "BackfillRunner.kt").write_text(
                """
package com.example

import org.springframework.boot.ExitCodeGenerator
import org.springframework.boot.SpringApplication
import org.springframework.boot.ApplicationRunner

class BackfillRunner : ApplicationRunner {
    override fun run(args: org.springframework.boot.ApplicationArguments) {
        SpringApplication.exit(null, ExitCodeGenerator { 0 })
    }
}
""".strip()
                + "\n"
            )
            (stream / "build.gradle").write_text(
                "dependencies { implementation 'org.springframework.boot:spring-boot-starter' }\n"
            )
            (stream / "src" / "main" / "resources" / "application.yml").write_text(
                "spring:\n  main:\n    web-application-type: none\n"
            )
            (stream / "src" / "main" / "kotlin" / "com" / "example" / "StreamApplication.kt").write_text(
                """
package com.example

import org.springframework.boot.autoconfigure.SpringBootApplication
import org.springframework.boot.runApplication

@SpringBootApplication
class StreamApplication

fun main(args: Array<String>) {
    runApplication<StreamApplication>(*args)
}
""".strip()
                + "\n"
            )

            config = ccstack.inspect_workspace(root, "shopping")

        self.assertEqual(sorted(config["services"]), ["apps.api", "apps.cdc"])
        self.assertNotIn("apps.backfill", config["services"])

    def test_workspace_analysis_keeps_server_module_when_only_batch_profile_is_web_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            contents = root / "apps" / "proxy" / "acme" / "contents"
            source = contents / "src" / "main" / "kotlin" / "com" / "example"
            resources = contents / "src" / "main" / "resources"
            source.mkdir(parents=True)
            resources.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':apps:proxy:acme:contents'\n")
            (contents / "build.gradle.kts").write_text(
                """
plugins { id("org.springframework.boot") }
dependencies {
  implementation("org.springframework.boot:spring-boot-starter-web")
  implementation("org.springframework.boot:spring-boot-starter-actuator")
}
""".strip()
                + "\n"
            )
            (resources / "application-local.yml").write_text("server:\n  port: 8083\n")
            (resources / "application-batch.yml").write_text(
                "spring:\n  main:\n    web-application-type: none\n"
            )
            (source / "ContentsApplication.kt").write_text(
                """
package com.example

import org.springframework.boot.autoconfigure.SpringBootApplication
import org.springframework.boot.runApplication
import org.springframework.boot.SpringApplication

@SpringBootApplication
class ContentsApplication

fun main(args: Array<String>) {
    runApplication<ContentsApplication>(*args)
}

fun stopBatchContext() {
    SpringApplication.exit(null)
}
""".strip()
                + "\n"
            )

            config = ccstack.inspect_workspace(root, "shopping")

        service = config["services"]["apps.proxy.acme.contents"]
        self.assertEqual(service["port"], 8083)
        self.assertEqual(service["health"], "http://localhost:8083/actuator/health")
        self.assertIn("apps.proxy.acme.contents", config["profiles"]["default"]["services"])

    def test_workspace_augment_restores_contents_proxy_from_batch_profile_web_none_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            contents = root / "apps" / "proxy" / "acme" / "contents"
            events = root / "apps" / "proxy" / "acme" / "events"
            contents_source = contents / "src" / "main" / "kotlin" / "com" / "example"
            events_source = events / "src" / "main" / "kotlin" / "com" / "example"
            contents_resources = contents / "src" / "main" / "resources"
            events_resources = events / "src" / "main" / "resources"
            contents_source.mkdir(parents=True)
            events_source.mkdir(parents=True)
            contents_resources.mkdir(parents=True)
            events_resources.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text(
                "include ':apps:proxy:acme:contents', ':apps:proxy:acme:events'\n"
            )
            for module in (contents, events):
                (module / "build.gradle.kts").write_text(
                    """
plugins { id("org.springframework.boot") }
dependencies { implementation("org.springframework.boot:spring-boot-starter-web") }
""".strip()
                    + "\n"
                )
            (contents_resources / "application-local.yml").write_text("server:\n  port: 8083\n")
            (contents_resources / "application-batch.yml").write_text(
                "spring:\n  main:\n    web-application-type: none\n"
            )
            (events_resources / "application.yml").write_text("server:\n  port: 8085\n")
            (contents_source / "ContentsApplication.kt").write_text(
                """
package com.example

import org.springframework.boot.autoconfigure.SpringBootApplication
import org.springframework.boot.runApplication
import org.springframework.boot.SpringApplication

@SpringBootApplication
class ContentsApplication

fun main(args: Array<String>) {
    runApplication<ContentsApplication>(*args)
}

fun stopBatchContext() {
    SpringApplication.exit(null)
}
""".strip()
                + "\n"
            )
            (events_source / "EventsApplication.kt").write_text(
                """
package com.example

import org.springframework.boot.autoconfigure.SpringBootApplication
import org.springframework.boot.runApplication

@SpringBootApplication
class EventsApplication

fun main(args: Array<String>) {
    runApplication<EventsApplication>(*args)
}
""".strip()
                + "\n"
            )
            active_config = {
                "workspace": "shopping",
                "profiles": {"default": {"services": ["apps.proxy.acme.events"]}},
                "services": {
                    "apps.proxy.acme.events": {
                        "type": "gradle",
                        "task": ":apps:proxy:acme:events:bootRun",
                        "module": "apps/proxy/acme/events",
                        "port": 8085,
                    }
                },
            }

            managed, augment = ccstack.augment_workspace_config(root, active_config)

        self.assertIn("apps.proxy.acme.contents", augment["added_services"])
        self.assertEqual(managed["services"]["apps.proxy.acme.contents"]["port"], 8083)
        self.assertEqual(managed["services"]["apps.proxy.acme.events"]["port"], 8085)
        self.assertIn("apps.proxy.acme.contents", managed["profiles"]["default"]["services"])

    def test_workspace_analysis_infers_actuator_health_under_servlet_context_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            api = root / "apps" / "proxy" / "acme" / "ai-vertical"
            resources = api / "src" / "main" / "resources"
            resources.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':apps:proxy:acme:ai-vertical'\n")
            (api / "build.gradle").write_text(
                """
plugins { id 'org.springframework.boot' version '3.2.0' }
dependencies { implementation 'org.springframework.boot:spring-boot-starter-actuator' }
""".strip()
                + "\n"
            )
            (resources / "application.yml").write_text(
                """
server:
  port: 9081
  servlet:
    context-path: /api/v1
management:
  endpoints:
    web:
      exposure:
        include: health
""".strip()
                + "\n"
            )

            config = ccstack.inspect_workspace(root, "shopping")

        self.assertEqual(
            config["services"]["apps.proxy.acme.ai-vertical"]["health"],
            "http://localhost:9081/api/v1/actuator/health",
        )

    def test_workspace_analysis_allows_library_like_module_with_explicit_server_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            lib_preview = root / "lib" / "preview"
            source_dir = lib_preview / "src" / "main" / "java" / "com" / "example"
            source_dir.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':lib:preview'\n")
            (lib_preview / "build.gradle").write_text(
                "plugins { id 'org.springframework.boot' version '3.2.0' }\n"
            )
            (source_dir / "PreviewApplication.java").write_text(
                """
package com.example;

import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class PreviewApplication {
}
""".strip()
                + "\n"
            )

            config = ccstack.inspect_workspace(root, "shopping")

        self.assertEqual(sorted(config["services"]), ["lib.preview"])
        self.assertEqual(config["services"]["lib.preview"]["task"], ":lib:preview:bootRun")
        self.assertEqual(config["profiles"], {"default": {"services": ["lib.preview"]}})
        self.assertNotIn(":lib:preview:bootRun", config["profiles"])

    def test_ui_run_bundle_targets_hide_execution_task_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {
                        "default": {"services": ["contents.api"]},
                        "contents": {"services": ["contents.api"]},
                        "contents-light": {"services": ["contents.api"]},
                        "fixity": {"services": ["contents.api"]},
                        "proxy": {"services": ["contents.api"]},
                        "integration": {"services": ["contents.api"]},
                        "proxy.acme": {"services": ["contents.api"]},
                        "fixity-pricecompare": {"services": ["contents.api"]},
                        "infra": {"services": ["contents.api"]},
                        ":apps:contents:api:bootRun": {"services": ["contents.api"]},
                    },
                    "services": {
                        "contents.api": {
                            "type": "gradle",
                            "task": ":apps:contents:api:bootRun",
                        },
                    },
                },
            )

            targets = ccstack.ui_base_targets(workspace)
            page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry.load(), False)

        self.assertEqual(targets, ["all", "fixity", "integration", "proxy"])
        self.assertIn('<label for="target">', page)
        self.assertIn("<span>Target</span>", page)
        self.assertIn("<select id=\"target\">", page)
        self.assertIn('<option value="all">all</option>', page)
        self.assertIn('<option value="fixity">fixity</option>', page)
        self.assertIn('<option value="integration">integration</option>', page)
        self.assertIn('<option value="proxy">proxy</option>', page)
        self.assertNotIn("<label for=\"target\">Run bundle</label>", page)
        self.assertNotIn("bundles", page)
        self.assertNotIn('value="default"', page)
        self.assertNotIn("contents</option>", page)
        self.assertNotIn("proxy.acme</option>", page)
        self.assertNotIn("fixity-pricecompare</option>", page)
        self.assertNotIn("infra</option>", page)
        self.assertNotIn(":apps:contents:api:bootRun</option>", page)

    def test_ui_workspace_analyze_reports_progress_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shop"
            root.mkdir()
            (root / "compose.yml").write_text(
                """
services:
  db:
    image: postgres:16
""".strip()
                + "\n"
            )
            messages = []

            with mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                draft = ccstack.analyze_workspace_draft(str(root), progress=messages.append)

        progress = "".join(messages)
        self.assertEqual(draft["workspace"], "shop")
        self.assertIn("Validating workspace path", progress)
        self.assertIn("Inspecting Docker Compose and Gradle/Spring services", progress)
        self.assertIn("Detected 1 service(s)", progress)
        self.assertIn("Inspecting persistence models and data access", progress)
        self.assertIn("Scanning runtime dependency signals", progress)
        self.assertIn("Inspecting external HTTP dependency candidates", progress)
        self.assertIn("Analysis complete. Review the result, then Apply", progress)

    def test_ui_workspace_analyze_stream_events_append_workspace_progress_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "empty"
            root.mkdir()

            workspace = ccstack.Workspace(root, ccstack.setup_workspace_config(root))
            page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry({}), False)

        handler_start = page.index("function appendWorkspaceAnalyzeEvent")
        handler_end = page.index('source.addEventListener("done"', handler_start)
        handler = page[handler_start:handler_end]

        self.assertIn("const chunk = workspaceAnalyzeProgressChunk(data)", handler)
        self.assertIn("appendWorkspaceProgress(chunk)", handler)
        self.assertIn('source.addEventListener("log", appendWorkspaceAnalyzeEvent)', handler)
        self.assertIn('source.addEventListener("progress", appendWorkspaceAnalyzeEvent)', handler)

    def test_ui_workspace_analyze_done_result_is_visible_without_opening_console(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for DOM-level generated UI verification")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shop"
            root.mkdir()

            workspace = ccstack.Workspace(root, ccstack.setup_workspace_config(root))
            page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry({}), False)

        script_start = page.index("<script>") + len("<script>")
        script_end = page.index("</script>", script_start)
        browser_script = page[script_start:script_end].replace(
            'buttons.forEach(button => button.addEventListener("click", () => run(button)));',
            'window.__run = run;\n    buttons.forEach(button => button.addEventListener("click", () => run(button)));',
        )
        node_harness = textwrap.dedent(
            """
            const vm = require("vm");
            const assert = require("assert");
            const browserScript = %s;

            class FakeClassList {
              constructor() {
                this.values = new Set();
              }
              add(...items) {
                for (const item of items) {
                  this.values.add(item);
                }
              }
              remove(...items) {
                for (const item of items) {
                  this.values.delete(item);
                }
              }
              toggle(item, force) {
                const shouldAdd = force === undefined ? !this.values.has(item) : Boolean(force);
                if (shouldAdd) {
                  this.values.add(item);
                } else {
                  this.values.delete(item);
                }
              }
              contains(item) {
                return this.values.has(item);
              }
            }

            class FakeElement {
              constructor(selector) {
                this.selector = selector;
                this.textContent = "";
                this.value = "";
                this.checked = false;
                this.disabled = false;
                this.dataset = {};
                this.attributes = {};
                this.listeners = {};
                this.classList = new FakeClassList();
                this.options = [];
                this.scrollTop = 0;
                this.scrollHeight = 0;
              }
              addEventListener(type, handler) {
                this.listeners[type] = handler;
              }
              setAttribute(name, value) {
                this.attributes[name] = String(value);
              }
              getAttribute(name) {
                return this.attributes[name];
              }
              querySelector(selector) {
                return element(this.selector + " " + selector);
              }
              querySelectorAll() {
                return [];
              }
              scrollIntoView() {}
              focus() {}
            }

            const elements = new Map();
            function element(selector) {
              if (!elements.has(selector)) {
                elements.set(selector, new FakeElement(selector));
              }
              return elements.get(selector);
            }

            const analyzeButton = element("button[data-action=workspace_analyze]");
            analyzeButton.dataset.action = "workspace_analyze";
            analyzeButton.dataset.requiresPath = "1";
            const workspace = element("#workspace");
            workspace.value = "shop";
            const workspaceRoot = element("#workspaceRoot");
            workspaceRoot.value = "/tmp/shop";
            const target = element("#target");
            target.value = "default";
            const mode = element("#mode");
            mode.value = "full";
            const noDeps = element("#noDeps");
            noDeps.checked = false;
            element("#workspaceProgressLog").textContent = "No analysis yet.";
            element("#outputPanel").setAttribute("aria-hidden", "true");

            let currentSource = null;
            class FakeEventSource {
              constructor(url) {
                this.url = url;
                this.listeners = {};
                this.closed = false;
                currentSource = this;
              }
              addEventListener(type, handler) {
                this.listeners[type] = handler;
              }
              close() {
                this.closed = true;
              }
              emit(type, data) {
                this.listeners[type]({data: JSON.stringify(data)});
              }
            }

            const document = {
              body: element("body"),
              querySelector(selector) {
                return element(selector);
              },
              querySelectorAll(selector) {
                if (selector === "button[data-action]" || selector === "[data-requires-workspace='1']") {
                  return [analyzeButton];
                }
                return [];
              },
              addEventListener() {}
            };
            const window = {
              EventSource: FakeEventSource,
              requestAnimationFrame(callback) {
                callback();
              },
              setTimeout(callback) {
                callback();
              },
              addEventListener() {},
              confirm() {
                return true;
              },
              location: {search: ""}
            };

            const context = {
              window,
              document,
              EventSource: FakeEventSource,
              URLSearchParams,
              Set,
              Array,
              Boolean,
              Number,
              String,
              JSON,
              console,
              fetch: async () => ({json: async () => ({ok: true, output: ""})}),
              navigator: {clipboard: {writeText: async () => {}}},
            };
            vm.runInNewContext(browserScript, context);

            (async () => {
              const clickResult = window.__run(analyzeButton).catch(error => {
                console.error(error.stack || error);
                process.exit(1);
              });
              await Promise.resolve();
              assert(currentSource, "Analyze did not create an EventSource");
              currentSource.emit("progress", {message: "Inspecting services"});
              currentSource.emit("done", {
                ok: true,
                status: "ok",
                elapsed: 1,
                draft_id: "draft-1",
                draft: {analysis: {confidence: "high"}},
                output: "workspace draft\\nInfrastructure\\nReview the draft"
              });
              await clickResult;

              const progressLog = element("#workspaceProgressLog").textContent;
              assert(progressLog.includes("Inspecting services"));
              assert(progressLog.includes("workspace draft"));
              assert(progressLog.includes("Infrastructure"));
              assert(progressLog.includes("Review the draft"));
              assert.strictEqual(element("#outputPanel").classList.contains("is-open"), false);
              assert.strictEqual(element("#outputPanel").classList.contains("is-minimized"), false);
              assert.strictEqual(element("#outputPanel").getAttribute("aria-hidden"), "true");
              assert.strictEqual(element("#workspaceProgress").classList.contains("is-running"), false);
              assert.strictEqual(element("#workspaceProgress").getAttribute("aria-busy"), "false");
              assert.strictEqual(document.body.classList.contains("busy"), false);

              const failureResult = window.__run(analyzeButton).catch(error => {
                console.error(error.stack || error);
                process.exit(1);
              });
              await Promise.resolve();
              assert(currentSource, "Analyze retry did not create an EventSource");
              currentSource.emit("done", {
                ok: false,
                status: "failed",
                elapsed: 1,
                output: "analysis failed: workspace root no longer exists"
              });
              await failureResult;

              const failureLog = element("#workspaceProgressLog").textContent;
              assert(failureLog.includes("analysis failed: workspace root no longer exists"));
              assert.strictEqual(element("#workspaceProgressState").textContent, "Analyze failed");
              assert.strictEqual(element("#workspaceProgress").classList.contains("is-running"), false);
            })().catch(error => {
              console.error(error.stack || error);
              process.exit(1);
            });
            """
            % json.dumps(browser_script)
        )

        completed = subprocess.run(
            [node, "-e", node_harness],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_build_local_environment_plan_reuses_runtime_scan_cache_and_reports_substeps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = root / "apps" / "api" / "src" / "main" / "resources"
            source = root / "apps" / "api" / "src" / "main" / "java" / "shop"
            resources.mkdir(parents=True)
            source.mkdir(parents=True)
            (resources / "application-local.yml").write_text("spring:\n  profiles:\n    active: local\n")
            (source / "Api.java").write_text(
                """
package shop;

import org.springframework.batch.core.Job;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;

@ConditionalOnProperty(name = "api.enabled", havingValue = "true")
class Api {
}
""".strip()
                + "\n"
            )
            config = {
                "workspace": "shop",
                "services": {
                    "api": {
                        "type": "gradle",
                        "task": ":apps:api:bootRun",
                        "module": "apps/api",
                    },
                },
            }
            messages = []

            with mock.patch.object(
                ccstack,
                "collect_runtime_scan_files",
                wraps=ccstack.collect_runtime_scan_files,
            ) as collect:
                plan = ccstack.build_local_environment_plan(
                    root,
                    "shop",
                    config,
                    {"tables": [], "datasources": []},
                    progress=messages.append,
                )

        self.assertEqual(collect.call_count, 1)
        self.assertIn("Scanning runtime dependency signals", messages)
        self.assertIn("Inspecting Spring Batch local requirements", messages)
        self.assertIn("Inspecting conditional Spring properties", messages)
        self.assertIn("Inspecting environment variables and run profiles", messages)
        self.assertIn("spring-local-profile", [item["id"] for item in plan["requirements"]])

    def test_ui_workspace_analyze_rejects_missing_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )

            result = ccstack.execute_ui_action(
                workspace,
                {
                    "action": ["workspace_analyze"],
                    "workspace_root": [str(Path(tmp) / "missing")],
                },
            )

        self.assertFalse(result["ok"])
        self.assertIn("workspace root is not a directory", result["output"])

    def test_ui_workspace_apply_requires_analyzed_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )

            result = ccstack.execute_ui_action(
                workspace,
                {
                    "action": ["workspace_apply"],
                    "draft_id": [""],
                },
            )

        self.assertFalse(result["ok"])
        self.assertIn("Run Analyze before Apply", result["output"])

    def test_workspace_apply_rejects_draft_when_root_was_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shop"
            root.mkdir()
            (root / "compose.yml").write_text("services:\n  db:\n    image: postgres:16\n")

            with mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                draft = ccstack.analyze_workspace_draft(str(root))
                ccstack.shutil.rmtree(root)

                with self.assertRaises(ccstack.CcstackError) as raised:
                    ccstack.apply_workspace_draft(draft["draft_id"])

        self.assertIn("workspace root no longer exists", str(raised.exception))

    def test_ui_workspace_delete_removes_registration_without_deleting_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shop"
            root.mkdir()
            manifest = root / ".ccstack.json"
            manifest.write_text(
                '{"workspace": "shop", "profiles": {"default": {"services": []}}, "services": {}}\n'
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                registry = ccstack.WorkspaceRegistry.load()
                registry.register("shop", root)
                registry.default = "shop"
                registry.save()
                workspace = ccstack.Workspace.load(argparse.Namespace(root=None, workspace="shop"))
                page = ccstack.ui_page(workspace, registry, False)
                result = ccstack.execute_ui_action(workspace, {"action": ["workspace_delete"]})
                registry = ccstack.WorkspaceRegistry.load()

                self.assertIn(">Delete</button>", page)
                self.assertIn(">Purge</button>", page)
                self.assertTrue(result["ok"])
                self.assertEqual(result["workspace"], ccstack.NEW_WORKSPACE_ID)
                self.assertNotIn("shop", registry.workspaces)
                self.assertIsNone(registry.default)
                self.assertTrue(root.exists())
                self.assertTrue(manifest.exists())

    def test_ui_workspace_purge_removes_ccstack_state_without_deleting_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shop"
            root.mkdir()
            (root / "compose.yml").write_text(
                """
services:
  db:
    image: postgres:16
""".strip()
                + "\n"
            )
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                draft_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_analyze"],
                        "workspace_root": [str(root)],
                    },
                )
                apply_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_apply"],
                        "draft_id": [draft_result["draft_id"]],
                    },
                )
                loaded = ccstack.Workspace.load(argparse.Namespace(root=None, workspace=apply_result["workspace"]))
                state_dir = ccstack.STATE_HOME / ccstack.safe_name(loaded.name)
                state_existed_before_purge = state_dir.exists()
                result = ccstack.execute_ui_action(
                    loaded,
                    {
                        "action": ["workspace_delete"],
                        "purge": ["1"],
                    },
                )
                registry = ccstack.WorkspaceRegistry.load()

                self.assertTrue(state_existed_before_purge)
                self.assertTrue(result["ok"])
                self.assertEqual(result["status"], "purged")
                self.assertIn("purged state:", result["output"])
                self.assertNotIn(loaded.name, registry.workspaces)
                self.assertFalse(state_dir.exists())
                self.assertTrue(root.exists())
                self.assertFalse((root / ".ccstack.json").exists())

    def test_ui_loads_add_workspace_wizard_without_registered_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "empty"
            root.mkdir()

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"), \
                    mock.patch.object(ccstack.Path, "cwd", return_value=root):
                workspace = ccstack.Workspace.load_for_ui(argparse.Namespace(root=None, workspace=None))
                page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry.load(), False)

        self.assertEqual(workspace.name, ccstack.NEW_WORKSPACE_ID)
        self.assertTrue(workspace.config["workspace_setup"])
        self.assertEqual(workspace.services, {})
        self.assertEqual(workspace.resolve_target("all"), [])
        self.assertNotIn("workspace-setup", page)
        self.assertNotIn("acme/shopping", page)
        self.assertIn("Add workspace", page)
        self.assertNotIn("Bind local path", page)
        self.assertIn("Set up path", page)
        self.assertIn('class="workspace-summary" aria-label="Workspace summary"', page)
        self.assertIn("grid-template-columns: minmax(112px, .8fr) minmax(0, 1.3fr) minmax(84px, .8fr);", page)
        self.assertIn(".summary-item span", page)
        self.assertIn("white-space: normal;", page)
        self.assertIn("overflow-wrap: anywhere;", page)
        self.assertIn("<span>Registration</span>", page)
        self.assertIn('id="workspaceLifecycleState">Setup needed</strong>', page)
        self.assertIn('id="draftStatus">No draft</strong>', page)
        self.assertIn('const workspaceInitialStep = "bind";', page)
        self.assertIn('let workspaceReady = false;', page)
        self.assertIn('<ol class="wizard" aria-label="Workspace setup progress">', page)
        self.assertIn('class="wizard-step is-active" data-step="bind" aria-current="step"', page)
        self.assertIn('class="wizard-marker" aria-hidden="true">1</span>', page)
        self.assertIn('class="wizard-label">Review</span>', page)
        self.assertIn("Workspace Setup", page)
        self.assertNotIn("Daily Run", page)
        self.assertIn('class="control-panel" id="controlPanel"', page)
        self.assertIn('class="control-viewport"', page)
        self.assertIn('class="control-pages" id="controlPages"', page)
        self.assertIn("select {\n      appearance: none;", page)
        self.assertIn("-webkit-appearance: none;", page)
        self.assertIn("padding-right: 38px;", page)
        self.assertIn("background-position: calc(100% - 20px) 50%, calc(100% - 14px) 50%;", page)
        self.assertIn('id="mainControlPage"', page)
        self.assertIn('id="advancedControlPage"', page)
        self.assertIn('id="openAdvanced"', page)
        self.assertIn('id="backAdvanced"', page)
        self.assertIn("Advanced Settings", page)
        self.assertIn('const controlViewport = document.querySelector(".control-viewport")', page)
        self.assertIn("controlPanel.classList.toggle", page)
        self.assertIn("active.blur()", page)
        self.assertIn("controlViewport.scrollLeft = 0", page)
        self.assertIn("preventScroll: true", page)
        self.assertIn("Switch workspace and discard the current analyzed draft?", page)
        self.assertIn("setDraftStatus", page)
        self.assertIn('openAdvancedButton.addEventListener("click", () => setControlPage("advanced"))', page)
        self.assertIn('backAdvancedButton.addEventListener("click", () => setControlPage("main"))', page)
        self.assertIn('setControlPage("main", false)', page)
        self.assertNotIn("<details", page)
        self.assertNotIn("<summary", page)
        self.assertEqual(page.count(">Start target<"), 1)
        self.assertEqual(page.count(">Stop target<"), 1)
        self.assertIn('class="state-overview"', page)
        self.assertIn('id="servicesAttentionCount"', page)
        self.assertNotIn('class="operations-strip"', page)
        self.assertNotIn("<strong>Stack Controls</strong>", page)
        self.assertNotIn("<strong>Daily Run</strong>", page)
        self.assertNotIn("Start · Stop · Observe", page)
        self.assertNotIn('data-action="status"', page)
        self.assertNotIn(">Status</button>", page)
        self.assertIn(">Set up path</button>", page)
        self.assertIn('data-action="workspace_analyze" data-requires-path="1">Analyze</button>', page)
        self.assertIn(">Apply</button>", page)
        self.assertIn('class="workspace-progress" id="workspaceProgress"', page)
        self.assertIn('class="workspace-progress" id="workspaceProgress" aria-live="polite" aria-busy="false"', page)
        self.assertIn('id="workspaceProgressState"', page)
        self.assertIn('class="workspace-progress-spinner" id="workspaceProgressSpinner"', page)
        self.assertIn('id="workspaceProgressLog"', page)
        self.assertLess(page.index('id="workspaceProgress"'), page.index('class="buttons setup-buttons"'))
        self.assertNotIn('id="applyDraftInline"', page)
        self.assertNotIn('Apply analyzed workspace</button>', page)
        self.assertIn(".workspace-progress.is-running .workspace-progress-spinner", page)
        self.assertIn("function resetWorkspaceProgress", page)
        self.assertIn("function appendWorkspaceProgress", page)
        self.assertIn("function workspaceAnalyzeProgressChunk", page)
        self.assertIn("function appendWorkspaceAnalyzeEvent", page)
        self.assertIn("function setWorkspaceProgressRunning", page)
        self.assertIn('workspaceProgress.setAttribute("aria-busy", isRunning ? "true" : "false")', page)
        self.assertIn("function revealWorkspaceProgress", page)
        self.assertIn('workspaceProgress.scrollIntoView({ block: "nearest", inline: "nearest" })', page)
        self.assertIn("const chunk = workspaceAnalyzeProgressChunk(data)", page)
        self.assertIn("appendWorkspaceProgress(chunk)", page)
        self.assertIn('source.addEventListener("log", appendWorkspaceAnalyzeEvent)', page)
        self.assertIn('source.addEventListener("progress", appendWorkspaceAnalyzeEvent)', page)
        self.assertLess(
            page.index(r'resetWorkspaceProgress("Starting analysis...\n")'),
            page.index('setWorkspaceProgressState("Analyzing")', page.index('function streamWorkspaceAnalyze')),
        )
        self.assertIn("setWorkspaceProgressRunning(true)", page)
        self.assertIn("revealWorkspaceProgress();", page)
        self.assertIn("setWorkspaceProgressRunning(false)", page)
        self.assertIn("function finishWorkspaceAnalyze()", page)
        self.assertIn("activeActionServices.clear();", page)
        self.assertIn('activeAction = "";', page)
        self.assertIn("finally {\n            finishWorkspaceAnalyze();\n          }", page)
        self.assertIn('setWorkspaceProgressState("Analyzing")', page)
        self.assertIn('setWorkspaceProgressState("Review ready")', page)
        self.assertIn('setWorkspaceProgressState("Applied")', page)
        self.assertIn('class="buttons danger-buttons"', page)
        self.assertIn('id="browseWorkspace"', page)
        self.assertIn("/api/pick-directory", page)
        self.assertIn("/api/workspace/analyze/stream", page)
        self.assertIn("streamWorkspaceAnalyze", page)
        self.assertIn('workspaceRoot.addEventListener("keydown"', page)
        self.assertNotIn('id="activity"', page)
        self.assertIn('class="service-board-panel" aria-label="Service board panel"', page)
        self.assertIn('class="ai-doctor-aside" aria-label="AI Doctor panel"', page)
        self.assertGreater(page.index('class="ai-doctor-aside"'), page.index('class="service-board-panel" aria-label="Service board panel"'))
        service_panel = page[
            page.index('<section class="service-board-panel" aria-label="Service board panel">'):
            page.index('<aside class="ai-doctor-aside" aria-label="AI Doctor panel">')
        ]
        self.assertNotIn('id="aiDoctorPanel"', service_panel)
        self.assertNotIn('class="ai-doctor-panel"', service_panel)
        self.assertIn('class="service-workspace"', page)
        self.assertIn('class="service-roster"', page)
        self.assertIn('class="service-detail" aria-label="Selected service detail"', page)
        self.assertIn('class="service-board-actions" aria-label="Selected service actions"', page)
        self.assertIn('id="selectedServiceName"', page)
        self.assertIn('id="selectedServiceState"', page)
        self.assertIn('id="selectedServiceNext"', page)
        self.assertIn('id="selectedServiceRole"', page)
        self.assertIn('id="selectedServiceEndpoint"', page)
        self.assertIn('class="buttons service-board-buttons detail-actions"', page)
        self.assertIn('data-service-only="1"', page)
        self.assertIn('data-action="logs" data-requires-workspace="1" data-requires-service="1"', page)
        self.assertNotIn('class="service-log-viewer" id="serviceLogViewer" hidden aria-live="polite"', page)
        self.assertNotIn('id="serviceLogOutput"', page)
        self.assertNotIn("openServiceLogViewer", page)
        self.assertNotIn("applyServiceLogsResult", page)
        self.assertIn('data-action="recover" data-recover-scope="selected-service"', page)
        self.assertIn('>Recover</button>', page)
        self.assertNotIn('Recover selected', page)
        self.assertIn("Service actions are on the service board", page)
        self.assertIn("Execution Options", page)
        self.assertNotIn('id="stopFollow" type="button" disabled>Stop follow</button>', page)
        self.assertNotIn('<div class="control-title">Service Actions</div>', page)
        self.assertNotIn('Start selected</button>', page)
        self.assertNotIn('data-action="logs" data-log-panel-action="open"', page)
        self.assertNotIn('<button class="soft" data-action="follow_logs"', page)
        self.assertIn('id="followLogs" type="button">Follow</button>', page)
        self.assertIn('id="jumpToLatestLogs" type="button" hidden>Jump to latest</button>', page)
        self.assertNotIn('<button class="danger" data-action="clear_logs"', page)
        self.assertNotIn('class="buttons advanced-buttons"', page)
        self.assertIn('class="drawer-backdrop" id="drawerBackdrop" aria-hidden="true"', page)
        self.assertIn('class="output-panel" id="outputPanel" role="dialog" aria-labelledby="drawerTitle" aria-hidden="true"', page)
        self.assertIn('class="logs-backdrop" id="logsBackdrop" aria-hidden="true"', page)
        self.assertIn('class="logs-dock" id="logsDock"', page)
        self.assertIn('class="logs-panel" id="logsPanel" role="dialog"', page)
        self.assertIn('.logs-dock {\n      position: fixed;\n      inset: 0;', page)
        self.assertIn('.logs-panel {\n      position: absolute;\n      left: 50%;\n      top: 50%;', page)
        self.assertIn('.logs-shell {', page)
        self.assertIn('.logs-output-body {', page)
        self.assertIn('.logs-panel button,', page)
        self.assertIn('.logs-panel button.soft {', page)
        logs_style = page[page.index("    .logs-panel {"):page.index("    .logs-toggle {")]
        self.assertIn("background: var(--panel);", logs_style)
        self.assertIn("border: 1px solid var(--line);", logs_style)
        self.assertIn("background: var(--soft-blue);", logs_style)
        self.assertIn("color: var(--text);", logs_style)
        self.assertNotIn("#fffaf0", logs_style)
        self.assertNotIn("#fff3d8", logs_style)
        self.assertNotIn("#fffdf8", logs_style)
        self.assertNotIn("#d8b98a", logs_style)
        self.assertNotIn("#b8873f", logs_style)
        self.assertNotIn("#7a6042", logs_style)
        self.assertIn('id="logsOutput"', page)
        self.assertIn('id="logsServiceName"', page)
        self.assertIn('class="logs-toggle" id="logsToggle"', page)
        self.assertIn("pointer-events: auto;", page)
        self.assertIn("z-index: 2;", page)
        self.assertIn(".service-card-indicator", page)
        self.assertIn(".service-card-spinner", page)
        self.assertIn(".service-workspace {", page)
        self.assertIn("grid-template-columns: minmax(320px, .9fr) minmax(330px, .65fr);", page)
        self.assertIn(".service-roster,", page)
        self.assertIn(".service-detail {", page)
        self.assertIn(".service-card-main", page)
        self.assertIn(".service-card-state-wrap", page)
        self.assertIn("display: grid;", page)
        self.assertIn("min-width: 0;", page)
        self.assertIn("max-width: 100%;", page)
        self.assertIn("justify-items: end;", page)
        self.assertIn("width: fit-content;", page)
        self.assertIn("overflow: visible", page)
        self.assertNotIn("        \"indicator state\";", page)
        self.assertNotIn("      overflow: hidden;\n    }}\n    .service-card.is-selected", page)
        self.assertNotIn("grid-template-columns: 18px minmax(0, 1fr) minmax(0, max-content);", page)
        self.assertIn("min-height: 64px;", page)
        self.assertNotIn("min-height: 80px;", page)
        self.assertIn("padding: 4px 2px 2px;", page)
        self.assertIn(".service-card:hover:not(:disabled)", page)
        self.assertIn("transform: none;", page)
        self.assertIn(".service-card[hidden]", page)
        pending_card_style = page[
            page.index("    .service-card.is-pending {"):
            page.index("    .service-card-indicator {")
        ]
        self.assertIn("border-color: var(--line-strong);", pending_card_style)
        self.assertIn("background: linear-gradient(90deg, #f8fbff, #fff);", pending_card_style)
        self.assertIn(".service-card.is-pending.is-selected", pending_card_style)
        self.assertIn("border-color: var(--accent);", pending_card_style)
        self.assertIn("box-shadow: 0 0 0 3px rgba(20, 99, 255, .11);", pending_card_style)
        self.assertNotIn("inset 3px 0 0", pending_card_style)
        self.assertIn("display: none;", page)
        self.assertIn("padding: 9px 10px;", page)
        self.assertIn("align-items: center", page)
        self.assertIn("min-height: 100dvh", page)
        self.assertIn("height: 100dvh", page)
        self.assertIn("grid-template-rows: auto minmax(0, 1fr)", page)
        self.assertIn("grid-template-rows: minmax(0, 1fr)", page)
        self.assertIn("grid-template-columns: minmax(300px, 360px) minmax(0, 1fr) minmax(280px, 340px)", page)
        self.assertIn("grid-template-columns: minmax(300px, 360px) minmax(0, 1fr);", page)
        self.assertIn(".control-panel.is-advanced .control-pages", page)
        self.assertIn("transform: translateX(-50%)", page)
        self.assertIn(".control-page", page)
        self.assertIn('.control-page[aria-hidden="true"]', page)
        self.assertIn("flex: 0 0 auto", page)
        self.assertIn("height: auto", page)
        self.assertIn("max-width: 1780px", page)
        self.assertIn("position: fixed", page)
        self.assertIn("background: var(--panel)", page)
        self.assertIn("backdrop-filter: blur(2px)", page)
        self.assertIn("transform: scale(.12)", page)
        self.assertIn("transform-origin: calc(100% - 28px) calc(100% - 22px)", page)
        self.assertIn("transform .46s cubic-bezier(.16, 1, .3, 1)", page)
        self.assertIn(".output-panel.is-open", page)
        self.assertIn('class="console-dock" id="consoleDock"', page)
        self.assertIn(".console-dock {\n      position: fixed;", page)
        self.assertIn("pointer-events: none;", page)
        self.assertIn('class="console-toggle" id="consoleToggle"', page)
        self.assertIn('aria-controls="outputPanel" aria-expanded="false"', page)
        self.assertNotIn(".console-toggle.is-following", page)
        self.assertIn(".console-toggle.has-output", page)
        self.assertIn(".console-dock.is-open .output-panel", page)
        self.assertIn(".console-dock.is-open .console-toggle", page)
        self.assertIn(".console-dock.is-minimized .output-panel", page)
        self.assertIn(".console-dock.is-minimized .console-toggle", page)
        self.assertIn("visibility: hidden", page)
        self.assertIn("opacity: 0", page)
        self.assertIn("visibility: visible", page)
        self.assertIn("grid-template-columns: auto minmax(0, 1fr)", page)
        self.assertIn("padding-top: 2px", page)
        self.assertIn("grid-template-columns: 1fr", page)
        self.assertIn(".detail-health-strip", page)
        self.assertIn(".detail-actions", page)
        self.assertIn(".detail-next", page)
        self.assertIn("overflow-wrap: anywhere", page)
        self.assertIn("white-space: normal", page)
        self.assertIn("Service Board", page)
        self.assertIn("No services detected.", page)
        self.assertNotIn("Local startup flow", page)
        self.assertNotIn("text-overflow", page)
        self.assertIn("max-height: none", page)
        self.assertIn('class="drawer-header"', page)
        self.assertIn('id="drawerTitle"', page)
        self.assertIn('id="drawerMeta"', page)
        self.assertIn('id="closeOutputPanel"', page)
        self.assertIn("setDrawerContext", page)
        self.assertNotIn("setLogsDrawerContext", page)
        self.assertIn("setActionDrawerContext", page)
        self.assertIn("shouldOpenDrawerForAction(action)", page)
        self.assertIn("openOutputPanelForAction(action, button", page)
        self.assertIn("return false;", page)
        self.assertNotIn("function prepareRecoverLogs(service)", page)
        self.assertNotIn("function appendRecoverResultToLogs(data)", page)
        self.assertNotIn("prepareRecoverLogs(service);", page)
        self.assertNotIn("appendRecoverResultToLogs(data);", page)
        self.assertNotIn('logsStreamState.textContent = "recovering " + service;', page)
        self.assertNotIn('logsOutput.textContent = "Recovering " + service + "...', page)
        self.assertIn('if (shouldOpenDrawerForAction(action)) {', page)
        self.assertIn('if (action === "recover") {', page)
        self.assertNotIn('openOutputPanelForAction(action, button);\n      const actionServices = setActionServiceProgress(action, button)', page)
        self.assertIn("toggleOutputPanel", page)
        self.assertIn("updateConsoleToggleState", page)
        self.assertIn('consoleToggleButton.addEventListener("click", () => toggleOutputPanel())', page)
        self.assertNotIn("function selectedLogService()", page)
        self.assertNotIn("const service = selectedLogService();", page)
        self.assertIn("function openLogsPanel(serviceName, startLive = false)", page)
        self.assertIn("function closeLogsPanel(minimize = true)", page)
        self.assertIn('followLogsButton.addEventListener("click", () => followLogs())', page)
        self.assertIn('jumpToLatestLogsButton.addEventListener("click", () => jumpToLatestLogs())', page)
        self.assertIn('logsOutput.addEventListener("scroll", () => handleLogsOutputScroll(), { passive: true })', page)
        self.assertIn('resetLogsOutput(data.output || "");', page)
        self.assertIn('appendLiveLogChunk(data.chunk || "");', page)
        self.assertIn("function isLogsOutputAtBottom()", page)
        self.assertIn("function updateLogsJumpAffordance()", page)
        self.assertIn("function flushPendingLogChunks()", page)
        self.assertIn("const shouldFollow = logsAutoFollow && isLogsOutputAtBottom();", page)
        self.assertIn('jumpToLatestLogsButton.textContent = unseenLiveLogChunks > 0 ? "New logs" : "Jump to latest";', page)
        self.assertIn('followLogs();\n        if (workspaceReady)', page)
        self.assertNotIn("setServiceCardProgress(service, \"following\", false)", page)
        self.assertIn('setLogsStatus("following", "ok")', page)
        close_logs_panel = page[page.index("function closeLogsPanel"):page.index("function toggleLogsPanel")]
        self.assertIn('stopLiveLogs("stopped");', close_logs_panel)
        stop_live_logs = page[page.index("function stopLiveLogs"):page.index("async function loadServiceLogs")]
        self.assertNotIn("clearActiveServiceProgress()", stop_live_logs)
        self.assertIn("function minimizeOutputPanel()", page)
        self.assertIn('closeOutputPanelButton.addEventListener("pointerdown", handleMinimizeOutputPanel)', page)
        self.assertIn('closeOutputPanelButton.addEventListener("click", handleMinimizeOutputPanel)', page)
        self.assertIn('drawerBackdrop.addEventListener("click", () => {', page)
        self.assertIn("closeLogsPanel(false);", page)
        self.assertIn('window.addEventListener("pagehide", () => stopLiveLogs("stopped"))', page)
        self.assertIn("document.addEventListener(\"pointerdown\"", page)
        self.assertNotIn("logsPanel.contains(event.target)", page)
        self.assertIn("function setConsoleDrawerState", page)
        self.assertIn('setConsoleDrawerState("open")', page)
        self.assertIn('consoleDrawerStates = ["closed", "open", "minimized"]', page)
        self.assertIn("if (consoleDrawerState === \"open\") {", page)
        self.assertIn("document.addEventListener(\"pointerdown\"", page)
        self.assertIn("consoleDock.contains(event.target)", page)
        self.assertIn("AI Doctor", page)
        self.assertIn("Technical status, prompt, and log details", page)
        self.assertIn("Service logs", page)
        self.assertIn('aria-label="Minimize doctor panel"', page)
        self.assertIn("&minus;</button>", page)
        self.assertIn('class="console-toolbar" aria-label="Console tools"', page)
        self.assertNotIn('id="consoleModeLogs"', page)
        self.assertIn('data-console-mode="doctor"', page)
        self.assertNotIn('data-console-mode="terminal"', page)
        self.assertNotIn('data-console-mode="ai"', page)
        self.assertIn('class="ai-doctor-panel" id="aiDoctorPanel"', page)
        self.assertIn("section,\n    .ai-doctor-aside {", page)
        self.assertIn(".ai-doctor-aside {\n      align-self: stretch;", page)
        self.assertIn(".ai-doctor-aside { grid-column: auto; }", page)
        self.assertIn('class="ai-agent-picker" id="aiAgentPicker"', page)
        self.assertIn('data-ai-agent="codex"', page)
        self.assertIn('data-ai-agent="claude"', page)
        self.assertIn('data-ai-agent="gemini"', page)
        self.assertIn('class="doctor-run-status" aria-live="polite"', page)
        self.assertIn('id="doctorOutcome">Ready to diagnose</strong>', page)
        self.assertIn('id="doctorRecommendation"', page)
        self.assertIn('id="doctorDetailsButton" type="button" hidden disabled>Details</button>', page)
        self.assertIn('class="doctor-technical-details" id="doctorTechnicalDetails"', page)
        self.assertIn('class="doctor-technical-title">Technical details</span>', page)
        self.assertNotIn('id="doctorHostLabel"', page)
        self.assertNotIn('class="doctor-guided-steps"', page)
        self.assertNotIn('class="doctor-step', page)
        self.assertIn('name="doctorScope" value="all" checked', page)
        self.assertIn('name="doctorScope" value="failed"', page)
        self.assertIn('id="doctorServiceTargets"', page)
        self.assertIn('id="doctorTargetTools"', page)
        self.assertIn('id="doctorSelectAllFailed" type="button">Select all failed</button>', page)
        self.assertIn('id="doctorClearFailed" type="button">Clear failed</button>', page)
        self.assertIn('id="startDoctorWorkflow"', page)
        self.assertIn(">Start diagnosis</button>", page)
        self.assertIn("function startDoctorWorkflow()", page)
        self.assertIn(".doctor-service-target[hidden],", page)
        self.assertIn(".doctor-empty[hidden]", page)
        self.assertIn('startDoctorWorkflowButton.addEventListener("click", event => {', page)
        self.assertIn("event.stopPropagation()", page)
        self.assertIn('action: "doctor"', page)
        self.assertIn("ai_agent: activeAiAgent", page)
        self.assertIn("doctor_services: selectedServices.join(\",\")", page)
        self.assertIn("function setFailedDoctorTargets(selected)", page)
        self.assertIn('selectedCount + " of " + failedCount + " failed service target(s) selected."', page)
        self.assertIn('doctorSelectAllFailedButton.addEventListener("click"', page)
        self.assertIn("aiAgentButtons", page)
        self.assertIn("setActiveAiAgent", page)
        self.assertNotIn("startTerminalSession", page)
        self.assertNotIn("startAiConsoleSession", page)
        self.assertNotIn("consoleModeButtons", page)
        self.assertNotIn('@xterm/xterm@5.5.0', page)
        self.assertNotIn('@xterm/addon-fit@0.10.0', page)
        self.assertNotIn('id="terminalEmulator"', page)
        self.assertNotIn("ensureBrowserTerminal", page)
        self.assertNotIn("browserTerminal.onData", page)
        self.assertNotIn("browserTerminal.attachCustomKeyEventHandler", page)
        self.assertNotIn('sendConsoleSessionInput("\\r")', page)
        self.assertNotIn("queueAiSessionInput", page)
        self.assertNotIn("fitBrowserTerminal", page)
        self.assertNotIn('class="prompt-user">ccstack</span>', page)
        self.assertNotIn('class="prompt-path" id="promptPath"', page)
        self.assertNotIn('class="prompt-symbol">$</span>', page)
        self.assertNotIn('class="terminal-prompt"', page)
        self.assertNotIn('id="terminalPrompt"', page)
        self.assertNotIn('id="aiPrompt"', page)
        self.assertNotIn('id="shellCommand"', page)
        self.assertNotIn('id="runShellCommand"', page)
        self.assertNotIn("terminal-submit", page)
        self.assertNotIn("updatePromptPath", page)
        self.assertIn(".console-tools #clearOutput.soft", page)
        self.assertIn("scrollbar-color: #365a7c #07111f", page)
        self.assertNotIn("/api/console/session/start", page)
        self.assertNotIn("/api/console/session/input", page)
        self.assertNotIn("/api/console/session/resize", page)
        self.assertNotIn("/api/console/session/stream", page)
        self.assertNotIn("/api/console/session/stop", page)
        self.assertNotIn("openConsoleSessionStream", page)
        self.assertNotIn("sendConsoleSessionInput", page)
        self.assertNotIn("stopConsoleSession", page)
        self.assertNotIn('id="stopConsoleSession"', page)
        self.assertNotIn("/api/terminal/open", page)
        self.assertNotIn("Open shell", page)
        self.assertIn('let consoleDefaultModeOpened = false;', page)
        self.assertIn('function openDefaultConsoleMode()', page)
        self.assertIn('setConsoleMode("doctor");', page)
        self.assertIn('if (consoleDefaultModeOpened) {\n          openOutputPanel();\n        } else {\n          openDefaultConsoleMode();\n        }', page)
        self.assertIn("AI Doctor details", page)
        self.assertNotIn('class="service-picker service-recovery" for="recoverMode"', page)
        self.assertIn('data-action="recover" data-recover-scope="selected-service" data-requires-workspace="1" data-requires-service="1"', page)
        self.assertIn('data-recover-scope="selected-service"', page)
        self.assertIn(">Recover</button>", page)
        self.assertNotIn("Recover selected", page)
        self.assertIn("Repair selected service", page)
        self.assertIn("refresh workspace config and restart only this service", page)
        self.assertNotIn('data-requires-failed="1"', page)
        self.assertNotIn('<div class="control-title">Recovery</div>', page)
        self.assertIn("devstack://doctor", page)
        self.assertIn("devstack://logs", page)
        self.assertNotIn('<label for="lines">Log lines</label>', page)
        self.assertNotIn('id="decreaseLines"', page)
        self.assertNotIn('id="increaseLines"', page)
        self.assertNotIn(".output-panel .activity", page)
        self.assertNotIn('"workspace_analyze",\n        "workspace_apply"', page)
        self.assertIn("openOutputPanel", page)
        self.assertIn("closeOutputPanel", page)
        self.assertNotIn('setActionDrawerContext("folder picker");\n      openOutputPanel();', page)
        self.assertNotIn('setActionDrawerContext("bind");\n      openOutputPanel();', page)
        self.assertNotIn('setFlowStep("select");\n        openOutputPanel();', page)
        self.assertNotIn('setActionDrawerContext("workspace_analyze");\n        openOutputPanel();', page)
        self.assertIn("#streamState", page)
        self.assertIn("Analyzing workspace", page)
        self.assertIn("Starting stack", page)
        self.assertIn('id="serviceBoardStatus"', page)
        self.assertIn("refreshStatus", page)
        self.assertIn("window.setInterval(() => refreshStatus(false), 5000)", page)
        self.assertIn('setAllServiceCardsUnavailable("status unavailable", { preserveProgress: isBusy })', page)
        self.assertIn('serviceBoardStatus.textContent = "checking status"', page)

    def test_ui_doctor_workflow_launches_selected_ai_host_and_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_home = Path(tmp) / "state"
            process = mock.Mock(pid=4321)

            with mock.patch.object(ccstack, "STATE_HOME", state_home):
                workspace = ccstack.Workspace(
                    root,
                    {
                        "workspace": "test-shop",
                        "profiles": {},
                        "services": {
                            "contents.api": {
                                "type": "command",
                                "command": "sleep 60",
                            },
                            "contents.db": {
                                "type": "command",
                                "command": "sleep 60",
                            },
                        },
                    },
                )

                with mock.patch.object(workspace, "service_state", side_effect=lambda name, service: "failed" if name == "contents.api" else "ready"), \
                        mock.patch.object(ccstack.shutil, "which", return_value="/usr/local/bin/codex"), \
                        mock.patch.object(ccstack.subprocess, "Popen", return_value=process) as popen:
                    result = ccstack.execute_ui_action(
                        workspace,
                        {
                            "action": ["doctor"],
                            "ai_agent": ["codex"],
                            "doctor_scope": ["failed"],
                            "doctor_services": ["contents.api"],
                        },
                    )
                    prompt_exists = Path(result["prompt_path"]).exists()
                    log_parent = Path(result["log_path"]).parent
                    state = json.loads(Path(result["state_path"]).read_text())
                    popen_call = popen.call_args

        self.assertTrue(result["ok"])
        self.assertEqual(result["ai_agent"], "codex")
        self.assertEqual(result["doctor_scope"], "failed")
        self.assertEqual(result["doctor_targets"], ["contents.api"])
        self.assertEqual(result["pid"], 4321)
        self.assertEqual(result["status"], "doctor spawned")
        self.assertEqual(result["doctor_state"], "spawned")
        self.assertIn("doctor_run_id", result)
        self.assertIn("AI CLI host: codex", result["output"])
        self.assertIn("devstack home:", result["output"])
        self.assertIn("Doctor instructions:", result["output"])
        self.assertIn("devstack is not running service-specific recovery logic", result["output"])
        self.assertNotIn("devstack UI: startup is running in the background", result["output"])
        self.assertNotIn("Run Status to check readiness", result["output"])
        self.assertNotIn("password", result["output"].lower())
        self.assertTrue(prompt_exists)
        self.assertEqual(log_parent, state_home / "test-shop" / "logs")
        popen.assert_called_once()
        command = popen_call.args[0]
        self.assertEqual(command[:3], [sys.executable, str(CCSTACK_PATH), ccstack.AI_DOCTOR_RUNNER_COMMAND])
        self.assertEqual(popen_call.kwargs["cwd"], root)
        self.assertIn("CCSTACK_HOME", popen_call.kwargs["env"])
        self.assertIn("CCSTACK_DOCTOR_INSTRUCTION_PATHS", popen_call.kwargs["env"])
        self.assertTrue(popen_call.kwargs["start_new_session"])

        self.assertEqual(
            state["command"][:9],
            [
                "/usr/local/bin/codex",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "--model",
                ccstack.ai_doctor_lowest_codex_model(),
                "-c",
                f'model_reasoning_effort="{ccstack.ai_doctor_lowest_codex_reasoning_effort()}"',
                "--cd",
                str(root),
            ],
        )
        self.assertEqual(ccstack.ai_doctor_lowest_codex_model(), ccstack.AI_DOCTOR_CODEX_MODELS[-1])
        self.assertEqual(ccstack.ai_doctor_lowest_codex_reasoning_effort(), ccstack.AI_DOCTOR_CODEX_REASONING_EFFORTS[0])
        self.assertIn("Service scope: selected failed services", state["command"][9])
        self.assertIn("- contents.api", state["command"][9])
        self.assertIn("devstack checkout:", state["command"][9])
        self.assertIn("Doctor workflow instruction files:", state["command"][9])
        self.assertIn("The instruction paths above are provenance only.", state["command"][9])
        self.assertIn("Embedded Doctor instruction content:", state["command"][9])
        self.assertIn("Use this skill when the user asks to start, stop, restart, inspect, or manage local project servers", state["command"][9])
        self.assertIn("Delegate diagnosis, treatment/remediation, recovery, dummy data loading, and verification", state["command"][9])
        self.assertIn("This is a fully non-interactive host run.", state["command"][9])
        self.assertIn("Do not ask the user for approval, permission, confirmation, command execution, or pasted logs.", state["command"][9])
        self.assertIn("devstack logs <service> -n <lines>", state["command"][9])
        self.assertIn("do not use follow-mode log commands", state["command"][9])
        self.assertIn("finish with 'No treatment produced' or 'No remediation produced'", state["command"][9])
        self.assertIn("approve devstack commands, or paste logs", state["command"][9])
        self.assertIn("Do not stop at diagnosis when a remediable issue is found", state["command"][9])
        self.assertIn("No treatment produced", state["command"][9])
        self.assertIn("instead of fabricating a diagnosis", state["command"][9])
        self.assertNotIn("Read the devstack checkout and instruction files above explicitly", state["command"][9])

    def test_ui_doctor_claude_prompt_embeds_instruction_content_without_external_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_home = Path(tmp) / "state"
            process = mock.Mock(pid=4321)

            with mock.patch.object(ccstack, "STATE_HOME", state_home):
                workspace = ccstack.Workspace(
                    root,
                    {
                        "workspace": "test-shop",
                        "profiles": {},
                        "services": {
                            "contents.api": {
                                "type": "command",
                                "command": "sleep 60",
                            },
                        },
                    },
                )

                with mock.patch.object(ccstack.shutil, "which", return_value="/usr/local/bin/claude"), \
                        mock.patch.object(ccstack.subprocess, "Popen", return_value=process):
                    result = ccstack.execute_ui_action(
                        workspace,
                        {
                            "action": ["doctor"],
                            "ai_agent": ["claude"],
                            "doctor_scope": ["all"],
                        },
                    )
                    state = json.loads(Path(result["state_path"]).read_text())

        prompt = state["command"][7]

        self.assertTrue(result["ok"])
        self.assertEqual(
            state["command"][:7],
            [
                "/usr/local/bin/claude",
                "--model",
                ccstack.ai_doctor_lowest_claude_model(),
                "--effort",
                ccstack.ai_doctor_lowest_claude_effort(),
                "--dangerously-skip-permissions",
                "--print",
            ],
        )
        self.assertEqual(ccstack.ai_doctor_lowest_claude_model(), ccstack.AI_DOCTOR_CLAUDE_MODELS[-1])
        self.assertEqual(ccstack.ai_doctor_lowest_claude_effort(), ccstack.AI_DOCTOR_CLAUDE_EFFORTS[0])
        self.assertIn("Doctor workflow instruction files:", prompt)
        self.assertIn("The instruction paths above are provenance only.", prompt)
        self.assertIn("Do not call Read or otherwise access those instruction files", prompt)
        self.assertIn("Embedded Doctor instruction content:", prompt)
        # The skill header is now `# devstack` (rename) — legacy name removed
        # from the rendered prompt because the skill file itself was renamed.
        self.assertIn("# devstack", prompt)
        # The rule line uses the canonical manifest filename after the rename.
        self.assertIn("Treat `.devstack.json` as the source of truth", prompt)
        self.assertIn("fully non-interactive host run", prompt)
        self.assertIn("Do not ask the user for approval", prompt)
        self.assertIn("No remediation produced", prompt)
        self.assertNotIn("Read the devstack checkout and instruction files above explicitly", prompt)

    def test_ui_doctor_prompt_rejects_selected_failed_scope_without_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = ccstack.Workspace(
                Path(tmp),
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                        },
                    },
                },
            )

            result = ccstack.execute_ui_action(
                workspace,
                {
                    "action": ["doctor"],
                    "ai_agent": ["claude"],
                    "doctor_scope": ["failed"],
                },
            )

        self.assertFalse(result["ok"])
        self.assertIn("select at least one failed service", result["output"])

    def test_ui_doctor_status_reports_running_and_log_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_home = root / "state"

            with mock.patch.object(ccstack, "STATE_HOME", state_home):
                workspace = ccstack.Workspace(
                    root,
                    {
                        "workspace": "test-shop",
                        "profiles": {},
                        "services": {},
                    },
                )
                run_id = "doctor_codex_test"
                log_path = workspace.log_dir / "doctor_codex_test.log"
                log_path.write_text("raw doctor log\n", encoding="utf-8")
                state_path = ccstack.doctor_state_path(workspace, run_id)
                ccstack.write_json_file(
                    state_path,
                    {
                        "run_id": run_id,
                        "doctor_state": "spawned",
                        "pid": 4321,
                        "ai_agent": "codex",
                        "doctor_scope": "all",
                        "doctor_targets": ["contents.api"],
                        "log_path": str(log_path),
                        "prompt_path": str(workspace.state_dir / "doctor-prompts" / "prompt.md"),
                        "display_command": "codex exec --cd target <doctor prompt>",
                        "returncode": None,
                    },
                )

                with mock.patch.object(ccstack.os, "kill", return_value=None):
                    result = ccstack.execute_ui_action(
                        workspace,
                        {
                            "action": ["doctor_status"],
                            "doctor_run_id": [run_id],
                        },
                    )

        self.assertTrue(result["ok"])
        self.assertEqual(result["doctor_state"], "running")
        self.assertEqual(result["status"], "doctor running")
        self.assertEqual(result["status_class"], "running")
        self.assertIn("raw doctor log", result["output"])

    def test_ui_doctor_status_reports_host_failure_after_runner_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_home = root / "state"

            with mock.patch.object(ccstack, "STATE_HOME", state_home):
                workspace = ccstack.Workspace(
                    root,
                    {
                        "workspace": "test-shop",
                        "profiles": {},
                        "services": {},
                    },
                )
                run_id = "doctor_claude_test"
                log_path = workspace.log_dir / "doctor_claude_test.log"
                log_path.write_text("host failed\n", encoding="utf-8")
                state_path = ccstack.doctor_state_path(workspace, run_id)
                ccstack.write_json_file(
                    state_path,
                    {
                        "run_id": run_id,
                        "doctor_state": "running",
                        "pid": 4321,
                        "ai_agent": "claude",
                        "doctor_scope": "failed",
                        "doctor_targets": ["contents.api"],
                        "log_path": str(log_path),
                        "display_command": "claude --print <doctor prompt>",
                        "returncode": None,
                    },
                )

                with mock.patch.object(ccstack.os, "kill", side_effect=ProcessLookupError):
                    result = ccstack.execute_ui_action(
                        workspace,
                        {
                            "action": ["doctor_status"],
                            "doctor_run_id": [run_id],
                        },
                    )

        self.assertTrue(result["ok"])
        self.assertEqual(result["doctor_state"], "host_failed")
        self.assertEqual(result["status"], "doctor host failed")
        self.assertEqual(result["status_class"], "fail")
        self.assertIn("doctor host runner exited before recording completion", result["output"])

    def test_ui_doctor_status_reports_completed_run_with_no_useful_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_home = root / "state"

            with mock.patch.object(ccstack, "STATE_HOME", state_home):
                workspace = ccstack.Workspace(
                    root,
                    {
                        "workspace": "test-shop",
                        "profiles": {},
                        "services": {},
                    },
                )
                run_id = "doctor_claude_empty"
                prompt_path = workspace.state_dir / "doctor-prompts" / "prompt.md"
                log_path = workspace.log_dir / "doctor_claude_empty.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(
                    "\n".join(
                        [
                            "devstack doctor started at 20260527T011641Z",
                            "workspace: test-shop",
                            "project root: /tmp/shop",
                            "ai cli host: claude",
                            "scope: failed",
                            "targets: apps.fixity-adcenter.adcenter",
                            f"prompt: {prompt_path}",
                            "command: claude --print <doctor prompt>",
                            "devstack doctor host exited with status 0 at 2026-05-27T01:16:41+00:00",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                state_path = ccstack.doctor_state_path(workspace, run_id)
                ccstack.write_json_file(
                    state_path,
                    {
                        "run_id": run_id,
                        "doctor_state": "completed",
                        "pid": 4321,
                        "ai_agent": "claude",
                        "doctor_scope": "failed",
                        "doctor_targets": ["apps.fixity-adcenter.adcenter"],
                        "log_path": str(log_path),
                        "prompt_path": str(prompt_path),
                        "display_command": "claude --print <doctor prompt>",
                        "returncode": 0,
                    },
                )

                result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["doctor_status"],
                        "doctor_run_id": [run_id],
                    },
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["doctor_state"], "completed")
        self.assertEqual(result["doctor_outcome"], "no_useful_output")
        self.assertIn("Outcome: no useful output", result["output"])
        self.assertIn("No useful treatment or remediation was returned by the selected host.", result["output"])
        self.assertIn("retry AI Doctor with another host", result["output"])
        self.assertIn("Raw host log tail:", result["output"])

    def test_ui_doctor_status_reports_completed_run_with_treatment_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_home = root / "state"

            with mock.patch.object(ccstack, "STATE_HOME", state_home):
                workspace = ccstack.Workspace(
                    root,
                    {
                        "workspace": "test-shop",
                        "profiles": {},
                        "services": {},
                    },
                )
                run_id = "doctor_codex_treated"
                log_path = workspace.log_dir / "doctor_codex_treated.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(
                    "\n".join(
                        [
                            "devstack doctor started at 20260527T011641Z",
                            "Diagnosis: contents.api failed health checks.",
                            "Treatment: restarted contents.api and loaded dummy data.",
                            "Verification passed: health endpoint returned 200.",
                            "devstack doctor host exited with status 0 at 2026-05-27T01:16:41+00:00",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                state_path = ccstack.doctor_state_path(workspace, run_id)
                ccstack.write_json_file(
                    state_path,
                    {
                        "run_id": run_id,
                        "doctor_state": "completed",
                        "ai_agent": "codex",
                        "doctor_scope": "failed",
                        "doctor_targets": ["contents.api"],
                        "log_path": str(log_path),
                        "display_command": "codex exec --cd target <doctor prompt>",
                        "returncode": 0,
                    },
                )

                result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["doctor_status"],
                        "doctor_run_id": [run_id],
                    },
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["doctor_outcome"], "treatment_available")
        self.assertIn("Outcome: treatment available", result["output"])
        self.assertIn("Treatment: restarted contents.api", result["output"])

    def test_ui_doctor_status_reports_diagnosis_only_as_not_useful(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_home = root / "state"

            with mock.patch.object(ccstack, "STATE_HOME", state_home):
                workspace = ccstack.Workspace(
                    root,
                    {
                        "workspace": "test-shop",
                        "profiles": {},
                        "services": {},
                    },
                )
                run_id = "doctor_codex_diagnosis_only"
                log_path = workspace.log_dir / "doctor_codex_diagnosis_only.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(
                    "\n".join(
                        [
                            "devstack doctor started at 20260527T011641Z",
                            "Diagnosis: contents.api failed health checks.",
                            "The likely cause is a missing local database.",
                            "devstack doctor host exited with status 0 at 2026-05-27T01:16:41+00:00",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                state_path = ccstack.doctor_state_path(workspace, run_id)
                ccstack.write_json_file(
                    state_path,
                    {
                        "run_id": run_id,
                        "doctor_state": "completed",
                        "ai_agent": "codex",
                        "doctor_scope": "failed",
                        "doctor_targets": ["contents.api"],
                        "log_path": str(log_path),
                        "display_command": "codex exec --cd target <doctor prompt>",
                        "returncode": 0,
                    },
                )

                result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["doctor_status"],
                        "doctor_run_id": [run_id],
                    },
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["doctor_outcome"], "diagnosis_only")
        self.assertIn("Outcome: diagnosis only", result["output"])
        self.assertIn("Diagnosis was returned, but no treatment or remediation outcome was found.", result["output"])

    def test_ui_doctor_status_reports_explicit_no_treatment_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_home = root / "state"

            with mock.patch.object(ccstack, "STATE_HOME", state_home):
                workspace = ccstack.Workspace(
                    root,
                    {
                        "workspace": "test-shop",
                        "profiles": {},
                        "services": {},
                    },
                )
                run_id = "doctor_codex_no_treatment"
                log_path = workspace.log_dir / "doctor_codex_no_treatment.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(
                    "\n".join(
                        [
                            "devstack doctor started at 20260527T011641Z",
                            "Diagnosis: payment.api is unhealthy.",
                            "No treatment produced: cannot safely apply recovery because credentials are missing.",
                            "Handling path: operator must restore the local secret file, then rerun doctor.",
                            "devstack doctor host exited with status 0 at 2026-05-27T01:16:41+00:00",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                state_path = ccstack.doctor_state_path(workspace, run_id)
                ccstack.write_json_file(
                    state_path,
                    {
                        "run_id": run_id,
                        "doctor_state": "completed",
                        "ai_agent": "codex",
                        "doctor_scope": "failed",
                        "doctor_targets": ["payment.api"],
                        "log_path": str(log_path),
                        "display_command": "codex exec --cd target <doctor prompt>",
                        "returncode": 0,
                    },
                )

                result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["doctor_status"],
                        "doctor_run_id": [run_id],
                    },
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["doctor_outcome"], "no_treatment_produced")
        self.assertIn("Outcome: no treatment produced", result["output"])
        self.assertIn("No treatment or remediation was produced by the selected host.", result["output"])

    def test_pick_directory_response_returns_selected_path(self):
        with mock.patch.object(ccstack, "pick_directory", return_value=Path("/tmp/workspace")):
            result = ccstack.execute_pick_directory({"workspace_root": ["/tmp"]})

        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "/tmp/workspace")

    def test_pick_directory_response_handles_cancel(self):
        with mock.patch.object(ccstack, "pick_directory", return_value=None):
            result = ccstack.execute_pick_directory({"workspace_root": ["/tmp"]})

        self.assertFalse(result["ok"])
        self.assertTrue(result["cancelled"])

    def test_generic_console_command_and_session_helpers_are_removed(self):
        self.assertFalse(hasattr(ccstack, "execute_console_command"))
        self.assertFalse(hasattr(ccstack, "ConsolePtySession"))
        self.assertFalse(hasattr(ccstack, "start_console_session"))
        self.assertFalse(hasattr(ccstack, "console_session_command"))

    def test_console_initial_open_does_not_seed_workspace_status_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shop"
            root.mkdir()
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {"default": {"services": ["contents.api"]}},
                    "services": {
                        "contents.api": {
                            "type": "gradle",
                            "task": ":apps:contents:api:bootRun",
                            "port": 8080,
                            "health": "http://localhost:8080/actuator/health",
                        },
                    },
                },
            )

            page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry.load(), True)

        self.assertIn('<span id="command">devstack doctor</span>', page)
        self.assertIn('<pre id="output">Ready.</pre>', page)
        self.assertIn('data-console-mode="doctor"', page)
        self.assertNotIn('data-console-mode="terminal"', page)
        self.assertIn('let consoleDefaultModeOpened = false;', page)
        self.assertIn('function openDefaultConsoleMode()', page)
        self.assertIn('setConsoleMode("doctor");', page)
        self.assertIn("refreshStatus(false);", page)
        self.assertNotIn("refreshStatus(true);", page)
        self.assertNotIn("workspace: test-shop", page)
        self.assertNotIn("log=/", page)

    def test_uninstall_removes_installed_command_and_ai_skills(self):
        # The rename introduces BOTH the canonical ``devstack`` and the
        # legacy ``ccstack`` symlinks under BIN_DIR, plus dual-named AI
        # skill paths. ``uninstall_targets`` enumerates all six entries so
        # an uninstall sweep removes the complete install footprint.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "checkout" / "devstack"
            bin_dir = root / "bin"
            codex_home = root / "codex"
            claude_home = root / "claude"
            registry_path = root / ".devstack" / "workspaces.json"
            state_home = root / ".devstack" / "state"
            command_new = bin_dir / "devstack"
            command_legacy = bin_dir / "ccstack"
            codex_skill_new = codex_home / "skills" / "devstack" / "SKILL.md"
            codex_skill_legacy = codex_home / "skills" / "ccstack" / "SKILL.md"
            claude_skill_new = claude_home / "agents" / "skills" / "devstack.md"
            claude_skill_legacy = claude_home / "agents" / "skills" / "ccstack.md"

            script.parent.mkdir(parents=True)
            script.write_text("#!/usr/bin/env python3\n")
            bin_dir.mkdir()
            command_new.symlink_to(script)
            command_legacy.symlink_to(script)
            codex_skill_new.parent.mkdir(parents=True)
            codex_skill_new.write_text("devstack skill\n")
            codex_skill_legacy.parent.mkdir(parents=True)
            codex_skill_legacy.write_text("devstack skill\n")
            claude_skill_new.parent.mkdir(parents=True)
            claude_skill_new.write_text("devstack skill\n")
            claude_skill_legacy.write_text("devstack skill\n")
            registry_path.parent.mkdir(parents=True)
            registry_path.write_text("{}\n")
            state_home.mkdir(parents=True)

            targets = ccstack.uninstall_targets(
                script_path=script,
                bin_dir=bin_dir,
                codex_home=codex_home,
                claude_home=claude_home,
                registry_path=registry_path,
                state_home=state_home,
            )
            results = ccstack.apply_uninstall_targets(targets)

            # All six install footprints — both canonical and legacy — are
            # marked removable and removed.
            self.assertEqual(
                [result["status"] for result in results[:6]],
                ["removed"] * 6,
            )
            self.assertFalse(command_new.exists())
            self.assertFalse(command_legacy.exists())
            self.assertFalse(codex_skill_new.parent.exists())
            self.assertFalse(codex_skill_legacy.parent.exists())
            self.assertFalse(claude_skill_new.exists())
            self.assertFalse(claude_skill_legacy.exists())
            self.assertTrue(registry_path.exists())
            self.assertTrue(state_home.exists())

    def test_uninstall_skips_command_link_to_another_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "checkout" / "devstack"
            other_script = root / "other" / "devstack"
            bin_dir = root / "bin"
            command_new = bin_dir / "devstack"
            command_legacy = bin_dir / "ccstack"

            script.parent.mkdir(parents=True)
            script.write_text("#!/usr/bin/env python3\n")
            other_script.parent.mkdir(parents=True)
            other_script.write_text("#!/usr/bin/env python3\n")
            bin_dir.mkdir()
            # BOTH symlinks point to OTHER checkout — neither should be
            # touched by our uninstall.
            command_new.symlink_to(other_script)
            command_legacy.symlink_to(other_script)

            targets = ccstack.uninstall_targets(
                script_path=script,
                bin_dir=bin_dir,
                codex_home=root / "codex",
                claude_home=root / "claude",
            )
            results = ccstack.apply_uninstall_targets(targets)

            # Both command entries are skipped with the same "points to another
            # devstack checkout" reason; the canonical entry is index 0 and the
            # legacy alias entry is index 1.
            self.assertEqual(results[0]["status"], "skipped")
            self.assertIn("another devstack checkout", results[0]["reason"])
            self.assertEqual(results[1]["status"], "skipped")
            self.assertIn("another devstack checkout", results[1]["reason"])
            self.assertTrue(command_new.exists())
            self.assertTrue(command_legacy.exists())

    def test_uninstall_purge_state_removes_registry_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path = root / ".ccstack" / "workspaces.json"
            state_home = root / ".ccstack" / "state"

            registry_path.parent.mkdir(parents=True)
            registry_path.write_text("{}\n")
            state_home.mkdir(parents=True)
            (state_home / "workspace.json").write_text("{}\n")

            targets = ccstack.uninstall_targets(
                script_path=root / "checkout" / "ccstack",
                bin_dir=root / "bin",
                codex_home=root / "codex",
                claude_home=root / "claude",
                registry_path=registry_path,
                state_home=state_home,
                purge_state=True,
            )
            ccstack.apply_uninstall_targets(targets)

            self.assertFalse(registry_path.exists())
            self.assertFalse(state_home.exists())

    def test_http_ok_treats_connection_reset_as_unhealthy(self):
        with mock.patch.object(ccstack.urllib.request, "urlopen", side_effect=ConnectionResetError(54, "reset")):
            self.assertFalse(ccstack.http_ok("http://127.0.0.1:18083/health"))

    def test_clean_ui_command_output_hides_connection_reset_traceback(self):
        output = """Traceback (most recent call last):
  File "ccstack", line 1177, in http_ok
ConnectionResetError: [Errno 54] Connection reset by peer
"""

        cleaned = ccstack.clean_ui_command_output(output)

        self.assertIn("health check connection was reset", cleaned)
        self.assertNotIn("Traceback", cleaned)
        self.assertNotIn("ConnectionResetError", cleaned)

    def test_light_start_uses_background_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {
                        "contents": {"services": ["contents.db"]},
                        "contents-light": {"services": ["contents.db"]},
                    },
                    "services": {
                        "contents.db": {
                            "type": "command",
                            "command": "sleep 60",
                        },
                    },
                },
            )
            process = mock.Mock()
            process.pid = 12345

            with mock.patch.object(ccstack.subprocess, "Popen", return_value=process) as popen, \
                    mock.patch.object(ccstack.subprocess, "run") as run:
                result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["up"],
                        "target": ["contents"],
                        "light": ["1"],
                    },
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "started")
        self.assertEqual(result["returncode"], None)
        self.assertEqual(result["command"], "devstack up contents --light")
        self.assertIn("running in the background", result["output"])
        popen.assert_called_once()
        run.assert_not_called()

    def test_primary_start_all_uses_full_background_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {
                        "default": {"services": ["contents.db"]},
                    },
                    "services": {
                        "contents.db": {
                            "type": "command",
                            "command": "sleep 60",
                        },
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                        },
                    },
                },
            )
            process = mock.Mock()
            process.pid = 12345

            with mock.patch.object(ccstack.subprocess, "Popen", return_value=process) as popen, \
                    mock.patch.object(ccstack.subprocess, "run") as run:
                result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["up"],
                        "target": ["all"],
                    },
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "started")
        self.assertEqual(result["returncode"], None)
        self.assertEqual(result["command"], "devstack up all")
        self.assertIn("running in the background", result["output"])
        popen.assert_called_once()
        run.assert_not_called()

    def test_service_board_start_selected_passes_only_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {
                        "default": {"services": ["contents.db", "contents.api"]},
                    },
                    "services": {
                        "contents.db": {
                            "type": "command",
                            "command": "sleep 60",
                        },
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                        },
                    },
                },
            )
            process = mock.Mock()
            process.pid = 12345

            with mock.patch.object(ccstack.subprocess, "Popen", return_value=process) as popen, \
                    mock.patch.object(ccstack.subprocess, "run") as run:
                result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["up"],
                        "target": ["all"],
                        "service": ["contents.api"],
                        "only": ["contents.api"],
                    },
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "started")
        self.assertEqual(result["command"], "devstack up all --only contents.api")
        popen.assert_called_once()
        run.assert_not_called()

    def test_service_board_stop_selected_runs_single_service_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {
                        "default": {"services": ["contents.db", "contents.api"]},
                    },
                    "services": {
                        "contents.db": {
                            "type": "command",
                            "command": "sleep 60",
                        },
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                        },
                    },
                },
            )

            # UIB-3: `down` (and its single-service `stop` form) now dispatches
            # via run_devstack_command_background (subprocess.Popen) so the UI
            # never blocks for the full subprocess duration. Pin the new
            # background dispatch contract here.
            with mock.patch.object(ccstack.subprocess, "Popen") as popen:
                popen.return_value = mock.Mock(pid=33333)
                result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["down"],
                        "target": ["all"],
                        "service": ["contents.api"],
                        "only": ["contents.api"],
                    },
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["command"], "devstack stop contents.api")
        self.assertIn("running in the background", result["output"])
        args = popen.call_args.args[0]
        self.assertEqual(args[-2:], ["stop", "contents.api"])

    def test_ui_log_follow_command_uses_live_stream_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                        },
                    },
                },
            )

            command = ccstack.ui_log_follow_command(workspace, "contents.api", "80")

        self.assertEqual(
            command[-9:],
            ["--workspace", "test-shop", "--root", str(root), "logs", "contents.api", "-f", "-n", "80"],
        )

    def test_follow_logs_action_points_to_live_stream_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                        },
                    },
                },
            )

            with mock.patch.object(ccstack.subprocess, "Popen") as popen, \
                    mock.patch.object(ccstack.subprocess, "run") as run:
                result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["follow_logs"],
                        "service": ["contents.api"],
                        "lines": ["80"],
                    },
                )

        self.assertFalse(result["ok"])
        self.assertIn("live stream endpoint", result["output"])
        popen.assert_not_called()
        run.assert_not_called()

    def test_logs_keeps_finite_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                        },
                    },
                },
            )

            with mock.patch.object(ccstack.subprocess, "run") as run:
                run.return_value = mock.Mock(returncode=0, stdout="recent logs\n")
                result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["logs"],
                        "service": ["contents.api"],
                        "lines": ["80"],
                    },
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["command"], "devstack logs contents.api -n 80")
        self.assertEqual(result["output"], "recent logs\n")
        args = run.call_args.args[0]
        self.assertNotIn("-f", args)

    def test_clear_logs_truncates_managed_service_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "api.log"
            log_path.write_text("old log\n")
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                            "log": "api.log",
                        },
                    },
                },
            )

            result = ccstack.execute_ui_action(
                workspace,
                {
                    "action": ["clear_logs"],
                    "service": ["contents.api"],
                },
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["command"], "devstack clear-logs contents.api")
            self.assertEqual(log_path.read_text(), "")
            self.assertIn("cleared log:", result["output"])

    def test_clear_logs_rejects_compose_service_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compose = root / "compose.yml"
            compose.write_text("services: {}\n")
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.db": {
                            "type": "compose",
                            "compose": str(compose),
                            "service": "db",
                        },
                    },
                },
            )

            result = ccstack.execute_ui_action(
                workspace,
                {
                    "action": ["clear_logs"],
                    "service": ["contents.db"],
                },
            )

        self.assertFalse(result["ok"])
        self.assertIn("Docker-managed logs cannot be cleared safely", result["output"])

    def test_start_service_records_failure_without_prompt_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                            "log": "api.log",
                        },
                    },
                },
            )

            with mock.patch.object(workspace, "start_command", side_effect=ccstack.CcstackError("boom")):
                with self.assertRaises(ccstack.CcstackError) as raised:
                    workspace.start_service("contents.api")

        self.assertIn("boom", str(raised.exception))
        self.assertEqual(str(raised.exception), "boom")
        self.assertEqual(workspace.service_state("contents.api", workspace.service("contents.api")), "failed")
        self.assertTrue(workspace.service_failed("contents.api"))

    def test_service_state_treats_stale_pid_as_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                        },
                    },
                },
            )
            workspace.pid_path("contents.api").write_text("12345")

            with mock.patch.object(ccstack, "process_running", return_value=False):
                state = workspace.service_state("contents.api", workspace.service("contents.api"))

        self.assertEqual(state, "failed")

    def test_service_state_with_pid_and_closed_port_is_starting_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                            "port": 8080,
                        },
                    },
                },
            )
            workspace.pid_path("contents.api").write_text("12345")

            with mock.patch.object(ccstack, "process_running", return_value=True), \
                    mock.patch.object(ccstack, "port_open", return_value=False):
                state = workspace.service_state("contents.api", workspace.service("contents.api"))

        self.assertEqual(state, "starting")

    def test_service_state_with_pid_and_unhealthy_endpoint_without_port_is_starting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                            "health": "http://localhost:8080/actuator/health",
                        },
                    },
                },
            )
            workspace.pid_path("contents.api").write_text("12345")

            with mock.patch.object(ccstack, "process_running", return_value=True), \
                    mock.patch.object(ccstack, "http_ok", return_value=False):
                state = workspace.service_state("contents.api", workspace.service("contents.api"))

        self.assertEqual(state, "starting")

    def test_service_state_with_pid_open_port_and_unhealthy_endpoint_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                            "port": 8080,
                            "health": "http://localhost:8080/actuator/health",
                        },
                    },
                },
            )
            workspace.pid_path("contents.api").write_text("12345")

            with mock.patch.object(ccstack, "process_running", return_value=True), \
                    mock.patch.object(ccstack, "http_ok", return_value=False), \
                    mock.patch.object(ccstack, "port_open", return_value=True):
                state = workspace.service_state("contents.api", workspace.service("contents.api"))

        self.assertEqual(state, "unhealthy")

    def test_service_state_with_pid_and_open_port_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                            "port": 8080,
                        },
                    },
                },
            )
            workspace.pid_path("contents.api").write_text("12345")

            with mock.patch.object(ccstack, "process_running", return_value=True), \
                    mock.patch.object(ccstack, "port_open", return_value=True):
                state = workspace.service_state("contents.api", workspace.service("contents.api"))

        self.assertEqual(state, "ready")

    def test_service_state_clears_stale_failure_when_health_is_ok_without_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                            "health": "http://localhost:8080/actuator/health",
                        },
                    },
                },
            )
            workspace.mark_service_failed("contents.api", "old timeout")

            with mock.patch.object(ccstack, "http_ok", return_value=True):
                state = workspace.service_state("contents.api", workspace.service("contents.api"))

        self.assertEqual(state, "external")
        self.assertFalse(workspace.service_failed("contents.api"))

    def test_wait_for_ready_fails_when_port_never_opens_even_if_pid_is_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "default_timeout": 0,
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                            "port": 8080,
                        },
                    },
                },
            )
            workspace.pid_path("contents.api").write_text("12345")

            with mock.patch.object(ccstack, "process_running", return_value=True), \
                    mock.patch.object(ccstack, "port_open", return_value=False):
                with self.assertRaisesRegex(ccstack.CcstackError, "port 8080 did not open"):
                    workspace.wait_for_ready("contents.api", {"port": 8080})

    def test_gradle_services_get_longer_default_startup_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "gradle",
                            "task": ":apps:contents:api:bootRun",
                            "port": 8080,
                        },
                    },
                },
            )

        self.assertEqual(
            workspace.service_start_timeout(workspace.service("contents.api")),
            ccstack.DEFAULT_GRADLE_SERVICE_TIMEOUT,
        )

    def test_explicit_timeout_overrides_gradle_startup_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "gradle",
                            "task": ":apps:contents:api:bootRun",
                            "port": 8080,
                            "timeout": 12,
                        },
                    },
                },
            )

        self.assertEqual(workspace.service_start_timeout(workspace.service("contents.api")), 12)

    def test_initialize_managed_mssql_retries_until_sqlcmd_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "default_timeout": 5,
                    "profiles": {},
                    "services": {},
                },
            )
            service = {
                "type": "compose",
                "engine": "mssql",
                "compose": "compose.yml",
                "service": "mssql",
                "database": "knowboxdb2",
            }

            with mock.patch.object(
                workspace,
                "run_mssql_sqlcmd",
                side_effect=[ccstack.CcstackError("sql server not ready"), None],
            ) as sqlcmd, mock.patch.object(ccstack.time, "sleep") as sleep:
                workspace.initialize_managed_infra(service)

        self.assertEqual(sqlcmd.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_start_many_attempts_remaining_services_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.a": {"type": "command", "command": "a"},
                        "contents.b": {"type": "command", "command": "b"},
                    },
                },
            )

            with mock.patch.object(
                workspace,
                "start_service",
                side_effect=[ccstack.CcstackError("boom"), None],
            ) as start_service:
                with self.assertRaises(ccstack.CcstackError) as raised:
                    workspace.start_many(["contents.a", "contents.b"], include_deps=False)

        self.assertEqual(
            start_service.call_args_list,
            [
                mock.call("contents.a", include_deps=False),
                mock.call("contents.b", include_deps=False),
            ],
        )
        self.assertIn("some services failed to start", str(raised.exception))

    def test_stop_many_attempts_remaining_services_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.a": {"type": "command", "command": "a"},
                        "contents.b": {"type": "command", "command": "b"},
                    },
                },
            )

            with mock.patch.object(
                workspace,
                "stop_service",
                side_effect=[ccstack.CcstackError("boom"), None],
            ) as stop_service:
                with self.assertRaises(ccstack.CcstackError) as raised:
                    workspace.stop_many(["contents.a", "contents.b"])

        self.assertEqual(
            stop_service.call_args_list,
            [
                mock.call("contents.b"),
                mock.call("contents.a"),
            ],
        )
        self.assertIn("some services failed to stop", str(raised.exception))

    def test_compose_service_state_reads_docker_compose_status_without_container_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.db": {
                            "type": "compose",
                            "compose": "compose.yml",
                            "service": "db",
                        },
                    },
                },
            )

            with mock.patch.object(ccstack, "docker_compose_service_status", return_value="running"):
                running = workspace.service_state("contents.db", workspace.service("contents.db"))
            with mock.patch.object(ccstack, "docker_compose_service_status", return_value="exited"):
                stopped = workspace.service_state("contents.db", workspace.service("contents.db"))
            with mock.patch.object(ccstack, "docker_compose_service_status", return_value=None):
                missing = workspace.service_state("contents.db", workspace.service("contents.db"))

        self.assertEqual(running, "docker:up")
        self.assertEqual(stopped, "docker:down")
        self.assertEqual(missing, "docker:down")

    def test_docker_compose_service_status_parses_json_output(self):
        ccstack.COMPOSE_COMMAND_CACHE = None
        root = Path("/workspace/shop")
        compose = root / "compose.yml"
        output = '[{"Service":"db","State":"running"},{"Service":"cache","State":"exited"}]\n'

        with mock.patch.object(ccstack, "compose_command", return_value=["docker", "compose"]) as compose_cmd, \
                mock.patch.object(ccstack.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=output)

            status = ccstack.docker_compose_service_status(compose, root, "db")

        self.assertEqual(status, "running")
        compose_cmd.assert_called_once_with(root)
        run.assert_called_once_with(
            ["docker", "compose", "-f", str(compose), "ps", "--all", "--format", "json", "db"],
            cwd=root,
            stdout=ccstack.subprocess.PIPE,
            stderr=ccstack.subprocess.DEVNULL,
            text=True,
            check=False,
        )

    def test_docker_compose_service_status_uses_configured_project_name(self):
        root = Path("/workspace/shop")
        compose = root / "compose.yml"
        output = '[{"Service":"db","State":"running"}]\n'

        with mock.patch.object(ccstack, "compose_command", return_value=["docker", "compose"]), \
                mock.patch.object(ccstack.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=output)

            status = ccstack.docker_compose_service_status(compose, root, "db", "devstack_shop")

        self.assertEqual(status, "running")
        run.assert_called_once_with(
            ["docker", "compose", "-p", "devstack_shop", "-f", str(compose), "ps", "--all", "--format", "json", "db"],
            cwd=root,
            stdout=ccstack.subprocess.PIPE,
            stderr=ccstack.subprocess.DEVNULL,
            text=True,
            check=False,
        )

    def test_run_compose_uses_detected_compose_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compose = root / "compose.yml"
            compose.write_text("services:\n  db:\n    image: postgres\n")
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.db": {
                            "type": "compose",
                            "compose": str(compose),
                            "service": "db",
                        },
                    },
                },
            )

            with mock.patch.object(ccstack, "compose_command", return_value=["docker-compose"]), \
                    mock.patch.object(ccstack, "run") as run:
                run.return_value = None
                workspace.start_service("contents.db")

        run.assert_called_once_with(
            ["docker-compose", "-f", str(compose), "up", "-d", "db"],
            cwd=root,
        )

    def test_run_compose_uses_configured_project_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compose = root / "compose.yml"
            compose.write_text("services:\n  db:\n    image: postgres\n")
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.db": {
                            "type": "compose",
                            "compose": str(compose),
                            "project": "ccstack_test_shop",
                            "service": "db",
                        },
                    },
                },
            )

            with mock.patch.object(ccstack, "compose_command", return_value=["docker", "compose"]), \
                    mock.patch.object(ccstack, "run") as run:
                workspace.start_service("contents.db")

        run.assert_called_once_with(
            ["docker", "compose", "-p", "ccstack_test_shop", "-f", str(compose), "up", "-d", "db"],
            cwd=root,
        )

    def test_start_mssql_compose_applies_managed_schema_and_dml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compose = root / "compose.yml"
            compose.write_text("services:\n  mssql:\n    image: mcr.microsoft.com/mssql/server:2022-latest\n")
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "ccstack.infra.mssql": {
                            "type": "compose",
                            "compose": str(compose),
                            "service": "mssql",
                            "engine": "mssql",
                            "database": "shop",
                            "username": "sa",
                            "password": "Ccstack-local-1433!",
                            "init_schema": "/devstack-init/010-devstack-local-schema.sql",
                            "init_dml": "/devstack-init/020-devstack-local-dml.sql",
                        },
                    },
                },
            )

            with mock.patch.object(ccstack, "compose_command", return_value=["docker", "compose"]), \
                    mock.patch.object(ccstack, "run") as run:
                workspace.start_service("devstack.infra.mssql")

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["docker", "compose", "-f", str(compose), "up", "-d", "mssql"],
                [
                    "docker",
                    "compose",
                    "-f",
                    str(compose),
                    "exec",
                    "-T",
                    "mssql",
                    "/opt/mssql-tools18/bin/sqlcmd",
                    "-S",
                    "localhost",
                    "-U",
                    "sa",
                    "-P",
                    "Ccstack-local-1433!",
                    "-C",
                    "-Q",
                    "IF DB_ID(N'shop') IS NULL CREATE DATABASE [shop];",
                ],
                [
                    "docker",
                    "compose",
                    "-f",
                    str(compose),
                    "exec",
                    "-T",
                    "mssql",
                    "/opt/mssql-tools18/bin/sqlcmd",
                    "-S",
                    "localhost",
                    "-U",
                    "sa",
                    "-P",
                    "Ccstack-local-1433!",
                    "-C",
                    "-d",
                    "shop",
                    "-i",
                    "/devstack-init/010-devstack-local-schema.sql",
                ],
                [
                    "docker",
                    "compose",
                    "-f",
                    str(compose),
                    "exec",
                    "-T",
                    "mssql",
                    "/opt/mssql-tools18/bin/sqlcmd",
                    "-S",
                    "localhost",
                    "-U",
                    "sa",
                    "-P",
                    "Ccstack-local-1433!",
                    "-C",
                    "-d",
                    "shop",
                    "-i",
                    "/devstack-init/020-devstack-local-dml.sql",
                ],
            ],
        )

    def test_compose_command_prefers_docker_compose_when_available(self):
        ccstack.COMPOSE_COMMAND_CACHE = None
        with mock.patch.object(ccstack.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)

            command = ccstack.compose_command(Path("/workspace/shop"))

        self.assertEqual(command, ["docker", "compose"])
        run.assert_called_once()

    def test_docker_compose_falls_back_to_docker_compose_when_version_check_fails(self):
        ccstack.COMPOSE_COMMAND_CACHE = None
        def probe(*args, **kwargs):
            command = args[0]
            if command[:2] == ["docker", "compose"]:
                return mock.Mock(returncode=125)
            return mock.Mock(returncode=0)

        with mock.patch.object(ccstack.subprocess, "run") as run:
            run.side_effect = probe

            command = ccstack.compose_command(Path("/workspace/shop"))

        self.assertEqual(command, ["docker-compose"])

    def test_ui_page_exposes_recovery_controls_without_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                        },
                    },
                },
            )

            page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry.load(), False)

        self.assertIn('id="copyOutput"', page)
        self.assertNotIn('id="recoverMode"', page)
        self.assertIn('data-action="recover" data-recover-scope="selected-service" data-requires-workspace="1" data-requires-service="1"', page)
        self.assertNotIn('data-requires-failed="1"', page)
        self.assertNotIn("selectedServiceIsFailed", page)
        self.assertNotIn('button.dataset.requiresFailed === "1"', page)
        self.assertNotIn('data-action="recovery_prompt"', page)

    def test_ui_recover_reports_noop_success_when_api_is_already_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                        },
                    },
                },
            )

            with mock.patch.object(workspace, "service_state", return_value="ready"), \
                    mock.patch.object(workspace, "verify_service_api_dummy_data", return_value=(True, "GraphQL dummy data returned")):
                result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["recover"],
                        "service": ["contents.api"],
                    },
                )

        self.assertTrue(result["ok"])
        self.assertIn("API dummy-data verification already passes", result["output"])
        self.assertIn("restarted only: none", result["output"])

    def test_ui_recover_uses_failed_service_repair_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                        },
                    },
                },
            )

            with mock.patch.object(workspace, "service_state", return_value="failed"), \
                    mock.patch.object(workspace, "recover_failed_service", return_value="diagnosed\nrestarted\napi verification: passed") as recover:
                result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["recover"],
                        "service": ["contents.api"],
                    },
                )

        recover.assert_called_once_with("contents.api")
        self.assertTrue(result["ok"])
        self.assertEqual(result["command"], "devstack recover contents.api")
        self.assertIn("api verification: passed", result["output"])

    def test_ui_recover_uses_repair_flow_when_ready_service_fails_api_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "samplegoods": {
                            "type": "gradle",
                            "task": ":apps:proxy:acme:samplegoods:bootRun",
                            "port": 8093,
                            "health": "http://localhost:8093/actuator/health",
                        },
                    },
                },
            )

            with mock.patch.object(workspace, "service_state", return_value="ready"), \
                    mock.patch.object(workspace, "verify_service_api_dummy_data", return_value=(False, "GraphQL data is empty")), \
                    mock.patch.object(workspace, "recover_failed_service", return_value="api verification: passed") as recover:
                result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["recover"],
                        "service": ["samplegoods"],
                    },
                )

        recover.assert_called_once_with("samplegoods")
        self.assertTrue(result["ok"])
        self.assertIn("api verification: passed", result["output"])

    def test_ui_source_stale_detection_reports_old_running_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "ccstack"
            source.write_text("old")
            loaded = source.stat().st_mtime
            source.write_text("new")
            os.utime(source, (loaded + 2, loaded + 2))

            self.assertTrue(ccstack.ui_source_is_stale(source, loaded))
            self.assertIn("older in-memory version", ccstack.stale_ui_message())
            self.assertIn("retry the action", ccstack.stale_ui_message())

    def test_workspace_analyze_stream_reports_terminal_error_when_ui_source_is_stale(self):
        class Handler:
            def __init__(self):
                self.events = []
                self.responses = []
                self.headers = []
                self.ended = False

            def send_response(self, status):
                self.responses.append(status)

            def send_header(self, name, value):
                self.headers.append((name, value))

            def end_headers(self):
                self.ended = True

            def write_sse(self, event, payload):
                self.events.append((event, payload))

        handler = Handler()

        with mock.patch.object(ccstack, "ui_source_is_stale", return_value=True):
            ccstack.CcstackUiHandler.stream_workspace_analyze(
                handler,
                {"workspace_root": ["/tmp/shop"]},
            )

        self.assertEqual(handler.responses, [200])
        self.assertTrue(handler.ended)
        self.assertEqual(handler.events[0][0], "done")
        self.assertFalse(handler.events[0][1]["ok"])
        self.assertIn("older in-memory version", handler.events[0][1]["output"])

    def test_recover_failed_service_diagnoses_restarts_and_verifies_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "gradle",
                            "task": ":apps:contents:api:bootRun",
                            "port": 8080,
                        },
                    },
                },
            )
            workspace.mark_service_failed("contents.api", "port 8080 did not open")
            workspace.service_log_path("contents.api").write_text("startup failed\n")

            states = iter(["failed", "ready"])
            with mock.patch.object(workspace, "service_state", side_effect=lambda name, service: next(states)), \
                    mock.patch.object(workspace, "stop_service") as stop_service, \
                    mock.patch.object(workspace, "start_service") as start_service, \
                    mock.patch.object(workspace, "verify_service_api_dummy_data", return_value=(True, "GraphQL dummy data returned")):
                output = workspace.recover_failed_service("contents.api")

        stop_service.assert_called_once_with("contents.api", allow_port_owner=True)
        start_service.assert_called_once_with("contents.api")
        self.assertIn("diagnosis:", output)
        self.assertIn("failure: port 8080 did not open", output)
        self.assertIn("- devstack-managed local config overrides", output)
        self.assertIn("restarted only: contents.api", output)
        self.assertIn("api verification: passed - GraphQL dummy data returned", output)

    def test_recover_failed_service_reanalyzes_and_persists_local_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "gradle",
                            "task": ":apps:contents:api:bootRun",
                            "port": 8080,
                        },
                    },
                },
            )
            workspace.mark_service_failed("contents.api", "startup failed")
            analysis = {
                "environment": {
                    "generated": [],
                    "env": [{"service": "contents.api", "name": "LOCAL_ONLY", "value": "true", "source": "test"}],
                    "args": [{"service": "contents.api", "value": "--ccstack.local=true", "source": "test"}],
                    "requirements": [],
                    "manual": [],
                },
                "persistence": {"tables": [], "datasources": []},
            }

            states = iter(["failed", "ready"])
            with mock.patch.object(workspace, "service_state", side_effect=lambda name, service: next(states)), \
                    mock.patch.object(ccstack, "analyze_workspace_config", return_value=analysis) as analyze, \
                    mock.patch.object(workspace, "stop_service"), \
                    mock.patch.object(workspace, "start_service"), \
                    mock.patch.object(workspace, "verify_service_api_dummy_data", return_value=(True, "GraphQL dummy data returned")):
                output = workspace.recover_failed_service("contents.api")

        analyze.assert_called_once()
        generated = ccstack.generated_manifest_path("test-shop")
        saved = json.loads(generated.read_text())
        service = saved["services"]["contents.api"]
        self.assertEqual(service["env"]["LOCAL_ONLY"], "true")
        self.assertIn("--ccstack.local=true", service["args"])
        self.assertEqual(workspace.config["services"]["contents.api"]["env"]["LOCAL_ONLY"], "true")
        self.assertIn("- reanalyzed workspace local environment", output)
        self.assertIn("- persisted devstack-managed local runtime config", output)

    def test_recover_failed_service_routes_samplegoods_backend_to_mockserver(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Deploy the shipped shopping overrides so the engine's generic
            # api_mock_recovery rule applies the samplegoods-keyword recovery.
            deploy_shopping_overrides(root)
            service = {
                "type": "gradle",
                "task": ":apps:proxy:acme:samplegoods:bootRun",
                "port": 8093,
                "health": "http://localhost:8093/actuator/health",
            }
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {"apps.proxy.acme.samplegoods": service},
                },
            )

            states = iter(["ready", "ready"])
            verifications = iter([(False, "GraphQL data is empty"), (True, "GraphQL samplegoodsGoods: non-empty data returned")])
            with mock.patch.object(workspace, "service_state", side_effect=lambda name, svc: next(states)), \
                    mock.patch.object(workspace, "verify_service_api_dummy_data", side_effect=lambda name: next(verifications)), \
                    mock.patch.object(workspace, "stop_service"), \
                    mock.patch.object(workspace, "start_service"), \
                    mock.patch.object(workspace, "ensure_mockserver_expectation_routes") as ensure_mock:
                output = workspace.recover_failed_service("apps.proxy.acme.samplegoods")

        self.assertEqual(workspace.config["services"]["apps.proxy.acme.samplegoods"]["env"]["SAMPLEGOODS_SERVICE_URL"], "http://localhost:1080")
        ensure_mock.assert_called_once()
        self.assertIn("- samplegoods backend routed to ccstack mock-http", output)
        self.assertIn("api verification: passed - GraphQL samplegoodsGoods: non-empty data returned", output)

    def test_mockserver_expectation_routes_replace_stale_dummy_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Deploy the shipped shopping overrides into the workspace root so
            # the engine emits the historical sample_goods_page response body
            # when refreshing the /sample-goods route.
            deploy_shopping_overrides(root)
            workspace = ccstack.Workspace(
                root,
                {"workspace": "test-shop", "profiles": {}, "services": {}},
            )
            path = ccstack.managed_mockserver_expectations_path("test-shop")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                '[{"httpRequest":{"method":"GET","path":"/sample-goods"},"httpResponse":{"body":"stale"}}]\n'
            )

            with mock.patch.object(workspace, "put_mockserver_expectations") as put_expectations:
                workspace.ensure_mockserver_expectation_routes(["/sample-goods"])

        text = path.read_text()
        self.assertNotIn('"body":"stale"', text)
        self.assertIn("ccstack local samplegoods goods", text)
        put_expectations.assert_called_once()

    def test_put_mockserver_expectations_clears_stale_routes_before_put(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = ccstack.Workspace(
                Path(tmp),
                {"workspace": "test-shop", "profiles": {}, "services": {}},
            )
            overrides = shopping_overrides_dict()
            expectation = ccstack.mockserver_json_expectation(
                "/sample-goods",
                ccstack.mockserver_dummy_response_for_route("/sample-goods", overrides),
            )

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return False

            requests = []

            def capture(request, timeout):
                requests.append(request)
                return Response()

            with mock.patch.object(ccstack.urllib.request, "urlopen", side_effect=capture):
                workspace.put_mockserver_expectations([expectation])

        self.assertEqual([request.full_url for request in requests], [
            "http://localhost:1080/mockserver/clear",
            "http://localhost:1080/mockserver/expectation",
        ])
        self.assertEqual(json.loads(requests[0].data.decode("utf-8")), {"path": "/sample-goods"})
        self.assertIn("ccstack local samplegoods goods", requests[1].data.decode("utf-8"))

    def test_recover_skip_nonserver_starts_remaining_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {
                        "default": {
                            "services": ["lib.cache", "contents.api"],
                        },
                    },
                    "services": {
                        "lib.cache": {
                            "type": "gradle",
                            "task": ":lib:cache:bootRun",
                        },
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                        },
                    },
                },
            )

            with mock.patch.object(workspace, "start_service") as start_service:
                output = workspace.recover_service("lib.cache", mode="skip-if-nonserver")

        start_service.assert_called_once_with("contents.api", include_deps=False)
        self.assertIn("recovered by skipping non-server candidate: lib.cache", output)
        self.assertIn("- contents.api", output)

    def test_recover_skip_nonserver_rejects_real_server_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {"default": {"services": ["contents.api"]}},
                    "services": {
                        "contents.api": {
                            "type": "gradle",
                            "task": ":apps:contents:api:bootRun",
                            "port": 8080,
                            "health": "http://localhost:8080/actuator/health",
                        },
                    },
                },
            )

            with self.assertRaises(ccstack.CcstackError) as raised:
                workspace.recover_service("contents.api", mode="skip-if-nonserver")

        self.assertIn("not a safe non-server skip candidate", str(raised.exception))

    def test_recover_stop_port_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                            "port": 8080,
                        },
                    },
                },
            )

            with mock.patch.object(ccstack, "pid_for_listening_port", return_value=1234):
                with self.assertRaises(ccstack.CcstackError) as raised:
                    workspace.recover_service("contents.api", mode="stop-port-and-restart")

        self.assertIn("Re-run with confirmation", str(raised.exception))

    def test_recover_local_config_sets_safe_spring_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = {
                "type": "gradle",
                "task": ":apps:contents:api:bootRun",
            }
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": service,
                    },
                },
            )

            with mock.patch.object(workspace, "stop_service") as stop_service, \
                    mock.patch.object(workspace, "start_service") as start_service:
                output = workspace.recover_service("contents.api", mode="local-config-and-restart")

        stop_service.assert_called_once_with("contents.api", allow_port_owner=True)
        start_service.assert_called_once_with("contents.api")
        self.assertEqual(service["env"]["SPRING_CLOUD_CONFIG_ENABLED"], "false")
        self.assertEqual(service["env"]["SPRING_CLOUD_CONFIG_IMPORT_CHECK_ENABLED"], "false")
        # Mandatory classpath import — missing devstack-local.yml must fail fast
        # instead of silently degrading. See LOCAL_CONFIG_IMPORT (ccstack:~96).
        self.assertEqual(service["env"]["SPRING_CONFIG_IMPORT"], "classpath:devstack-local.yml")
        self.assertIn("--spring.cloud.config.enabled=false", service["args"])
        self.assertIn("--spring.cloud.config.import-check.enabled=false", service["args"])
        self.assertIn("--spring.config.import=classpath:devstack-local.yml", service["args"])
        self.assertIn("local Spring config overrides", output)

    def test_process_running_treats_zombie_as_not_running(self):
        with mock.patch.object(ccstack.os, "kill") as kill, \
                mock.patch.object(ccstack.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="Z+\n")

            self.assertFalse(ccstack.process_running(1234))

        kill.assert_called_once_with(1234, 0)

    def test_stop_service_kills_lingering_child_on_service_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                            "port": 8080,
                        },
                    },
                },
            )
            workspace.pid_path("contents.api").write_text("111")

            with mock.patch.object(ccstack, "terminate_pid_or_group") as terminate_pid, \
                    mock.patch.object(ccstack, "terminate_process_group") as terminate_group, \
                    mock.patch.object(ccstack, "pid_for_listening_port", side_effect=[222, None]), \
                    mock.patch.object(ccstack, "process_group_id", return_value=111), \
                    mock.patch.object(ccstack, "port_open", return_value=False), \
                    mock.patch.object(workspace, "wait_pid_exit") as wait_pid_exit:
                workspace.stop_service("contents.api")

        terminate_pid.assert_called_once_with(111, ccstack.signal.SIGTERM, "contents.api")
        terminate_group.assert_called_once_with(111, ccstack.signal.SIGTERM, "contents.api")
        self.assertEqual(
            wait_pid_exit.call_args_list,
            [
                mock.call(111, timeout=10),
                mock.call(222, timeout=5),
            ],
        )
        self.assertFalse(workspace.pid_path("contents.api").exists())

    def test_stop_service_does_not_kill_unrelated_port_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                            "port": 8080,
                        },
                    },
                },
            )
            workspace.pid_path("contents.api").write_text("111")

            with mock.patch.object(ccstack, "terminate_pid_or_group") as terminate_pid, \
                    mock.patch.object(ccstack, "terminate_process_group") as terminate_group, \
                    mock.patch.object(ccstack, "pid_for_listening_port", return_value=333), \
                    mock.patch.object(ccstack, "process_group_id", return_value=444), \
                    mock.patch.object(ccstack, "port_open", return_value=False), \
                    mock.patch.object(workspace, "wait_pid_exit") as wait_pid_exit:
                workspace.stop_service("contents.api")

        terminate_pid.assert_called_once_with(111, ccstack.signal.SIGTERM, "contents.api")
        terminate_group.assert_not_called()
        wait_pid_exit.assert_called_once_with(111, timeout=10)
        self.assertFalse(workspace.pid_path("contents.api").exists())

    def test_stop_service_kills_matching_gradle_child_port_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "gradle",
                            "task": ":apps:contents:api:bootRun",
                            "port": 8080,
                        },
                    },
                },
            )
            workspace.pid_path("contents.api").write_text("111")
            command = f"{root}/apps/contents/api/build/classes/kotlin/main com.example.Api --server.port=8080"

            with mock.patch.object(ccstack, "terminate_pid_or_group") as terminate_pid, \
                    mock.patch.object(ccstack, "pid_for_listening_port", side_effect=[222, None]), \
                    mock.patch.object(ccstack, "process_group_id", return_value=444), \
                    mock.patch.object(ccstack, "process_command", return_value=command), \
                    mock.patch.object(ccstack, "process_running", return_value=False), \
                    mock.patch.object(ccstack, "port_open", return_value=False), \
                    mock.patch.object(workspace, "wait_pid_exit") as wait_pid_exit:
                workspace.stop_service("contents.api")

        self.assertEqual(
            terminate_pid.call_args_list,
            [
                mock.call(111, ccstack.signal.SIGTERM, "contents.api"),
                mock.call(222, ccstack.signal.SIGTERM, "contents.api"),
            ],
        )
        self.assertEqual(
            wait_pid_exit.call_args_list,
            [
                mock.call(111, timeout=10),
                mock.call(222, timeout=10),
            ],
        )
        self.assertFalse(workspace.pid_path("contents.api").exists())

    def test_stop_service_marks_failed_when_port_remains_without_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop-port-failed",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                            "port": 8080,
                        },
                    },
                },
            )

            with mock.patch.object(ccstack, "port_open", return_value=True), \
                    mock.patch.object(
                        ccstack,
                        "listening_port_owner_summary",
                        return_value="docker container pas-mock (project=shopping, service=pas-mock)",
                    ):
                with self.assertRaisesRegex(ccstack.CcstackError, "stop did not release port 8080"):
                    workspace.stop_service("contents.api")

            self.assertTrue(workspace.service_failed("contents.api"))

    def test_explicit_stop_service_can_stop_configured_port_owner_without_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop-port-stop",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                            "port": 8080,
                        },
                    },
                },
            )

            with mock.patch.object(ccstack, "docker_container_for_host_port", return_value=None), \
                    mock.patch.object(ccstack, "pid_for_listening_port", return_value=222), \
                    mock.patch.object(ccstack, "terminate_pid_or_group") as terminate_pid, \
                    mock.patch.object(workspace, "wait_pid_exit") as wait_pid_exit, \
                    mock.patch.object(ccstack, "process_running", return_value=False), \
                    mock.patch.object(ccstack, "port_open", return_value=False):
                workspace.stop_service("contents.api", allow_port_owner=True)

        terminate_pid.assert_called_once_with(222, ccstack.signal.SIGTERM, "contents.api")
        wait_pid_exit.assert_called_once_with(222, timeout=10)

    def test_stop_by_port_stops_docker_container_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop-stop-docker",
                    "profiles": {},
                    "services": {
                        "contents.api": {
                            "type": "command",
                            "command": "sleep 60",
                            "port": 8080,
                            "stop_by_port": True,
                        },
                    },
                },
            )
            container = {"Id": "abcdef123456", "Name": "/pas-mock"}

            with mock.patch.object(ccstack, "docker_container_for_host_port", return_value=container), \
                    mock.patch.object(ccstack, "run") as run, \
                    mock.patch.object(workspace, "ensure_service_port_released") as ensure_released:
                workspace.stop_service("contents.api")

        run.assert_called_once_with(["docker", "stop", "abcdef123456"], cwd=root)
        ensure_released.assert_called_once_with("contents.api", workspace.services["contents.api"])

    def test_ui_service_board_renders_local_server_operations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {
                        "runtime": {"services": ["contents.api"]},
                        "infra": ["contents.db"],
                    },
                    "services": {
                        "contents.api": {
                            "type": "gradle",
                            "task": ":apps:contents:api:bootRun",
                            "port": 8080,
                            "health": "http://localhost:8080/actuator/health",
                        },
                        "contents.db": {
                            "type": "compose",
                            "compose": "compose.yml",
                            "service": "db",
                        },
                    },
                },
            )

            catalog = ccstack.ui_service_catalog(workspace)
            page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry.load(), False)

        self.assertEqual(
            [(item["name"], item["role"]) for item in catalog],
            [("contents.api", "server"), ("contents.db", "infra")],
        )
        self.assertIn('class="service-board" aria-label="Service board"', page)
        self.assertIn('class="service-board-panel" aria-label="Service board panel"', page)
        self.assertIn('class="service-board-actions" aria-label="Selected service actions"', page)
        self.assertIn('class="service-workspace"', page)
        self.assertIn('class="service-detail" aria-label="Selected service detail"', page)
        self.assertIn('class="service-search" for="serviceSearch"', page)
        self.assertIn('id="serviceSearch" type="search"', page)
        self.assertIn('id="selectedServiceName"', page)
        self.assertIn('id="selectedServiceNext"', page)
        self.assertIn('id="selectedServiceEndpoint"', page)
        self.assertIn('id="service" hidden', page)
        self.assertNotIn('class="service-picker" for="service"', page)
        self.assertIn('class="buttons service-board-buttons detail-actions"', page)
        self.assertIn('data-action="up" data-use-mode="1" data-service-only="1"', page)
        self.assertIn('data-action="down" data-service-only="1"', page)
        self.assertIn('data-action="logs" data-requires-workspace="1" data-requires-service="1"', page)
        self.assertNotIn('class="service-log-viewer" id="serviceLogViewer" hidden aria-live="polite"', page)
        self.assertIn('id="logsTitle">Service logs</h2>', page)
        self.assertNotIn('id="serviceLogOutput"', page)
        self.assertNotIn("openServiceLogViewer", page)
        self.assertNotIn("applyServiceLogsResult", page)
        self.assertIn('data-requires-service="1"', page)
        self.assertNotIn('data-action="logs" data-log-panel-action="open" data-requires-workspace="1" data-requires-service="1"', page)
        self.assertNotIn('<button class="soft" data-action="follow_logs"', page)
        self.assertIn('id="followLogs" type="button">Follow</button>', page)
        self.assertIn('id="jumpToLatestLogs" type="button" hidden>Jump to latest</button>', page)
        self.assertNotIn('<button class="danger" data-action="clear_logs"', page)
        self.assertIn('data-service-card="contents.api"', page)
        self.assertIn('title="contents.api"', page)
        self.assertIn('<span class="service-card-state-wrap">', page)
        self.assertIn('data-service-state>checking</span>', page)
        self.assertIn('card.addEventListener("click", event =>', page)
        self.assertIn("event.preventDefault()", page)
        self.assertIn('card.setAttribute("aria-pressed"', page)
        self.assertIn("server / gradle / :8080 / health", page)
        self.assertIn("infra / compose", page)
        self.assertIn("updateServiceBoardFromStatusOutput", page)
        self.assertIn("applyStatusSnapshot", page)
        self.assertIn("markServiceCardUnavailable", page)
        self.assertIn('setAllServiceCardsChecking()', page)
        self.assertIn("serviceMeta(entry)", page)
        self.assertIn("shouldOpenDrawerForAction(action)", page)
        self.assertIn("openOutputPanelForAction(action, button", page)
        self.assertIn("toggleOutputPanel", page)
        self.assertIn("updateConsoleToggleState", page)
        self.assertIn('class="console-toggle" id="consoleToggle"', page)
        self.assertIn('class="console-dock" id="consoleDock"', page)
        self.assertIn('class="logs-backdrop" id="logsBackdrop" aria-hidden="true"', page)
        self.assertIn('class="logs-dock" id="logsDock"', page)
        self.assertIn('class="logs-panel" id="logsPanel" role="dialog"', page)
        self.assertIn('.logs-shell {', page)
        self.assertIn('class="logs-toggle" id="logsToggle"', page)
        self.assertIn(".console-dock.is-open .console-toggle", page)
        self.assertIn(".console-dock.is-minimized .console-toggle", page)
        self.assertIn('data-console-mode="doctor"', page)
        self.assertNotIn('data-console-mode="terminal"', page)
        self.assertNotIn('data-console-mode="ai"', page)
        self.assertNotIn('id="consoleModeLogs"', page)
        self.assertNotIn('class="service-picker service-recovery" for="recoverMode"', page)
        self.assertIn('data-action="recover" data-recover-scope="selected-service" data-requires-workspace="1" data-requires-service="1"', page)
        self.assertIn('data-recover-scope="selected-service"', page)
        self.assertIn("recovering selected service", page)
        self.assertIn("let activeActionServices = new Set();", page)
        self.assertIn('let activeAction = "";', page)
        self.assertIn("function isServiceScopedActionButton(button)", page)
        self.assertIn('if (isBusy && action === "logs" && activeAction === "recover")', page)
        self.assertIn("button.disabled = false;", page)
        self.assertIn("button.disabled = activeActionServices.has(selectedService)", page)
        self.assertIn("const resolveProgress = options.resolveProgress ?? !isBusy", page)
        self.assertIn("setServiceCardState(name, state, health, { resolveProgress })", page)
        self.assertIn("setAllServiceCardsUnavailable(\"status unavailable\", { preserveProgress: isBusy })", page)
        self.assertIn("await refreshStatus(false, { resolveProgress: true })", page)
        self.assertNotIn('setFlowStep("select");\n        openOutputPanel();', page)
        self.assertIn(".service-card.status-external .service-dot", page)
        self.assertIn(".service-card.status-unhealthy .service-dot", page)
        self.assertIn(".service-card.status-unhealthy .service-card-state", page)
        self.assertIn(".service-card.is-pending", page)
        self.assertIn(".service-card.is-pending .service-card-spinner", page)
        pending_card_style = page[
            page.index("    .service-card.is-pending {"):
            page.index("    .service-card-indicator {")
        ]
        self.assertIn("border-color: var(--line-strong);", pending_card_style)
        self.assertIn("background: linear-gradient(90deg, #f8fbff, #fff);", pending_card_style)
        self.assertIn(".service-card.is-pending.is-selected", pending_card_style)
        self.assertIn("border-color: var(--accent);", pending_card_style)
        self.assertIn("box-shadow: 0 0 0 3px rgba(20, 99, 255, .11);", pending_card_style)
        self.assertNotIn("inset 3px 0 0", pending_card_style)
        self.assertIn('if (state === "external")', page)
        self.assertIn('health === "ok" || state === "ready" || state === "docker:up"', page)
        self.assertIn('if (state === "unhealthy")', page)
        self.assertIn('currentKind === "failed" || currentKind === "unhealthy"', page)
        self.assertNotIn('state.startsWith("pid:")', page)
        self.assertIn("function serviceProgressTarget(action)", page)
        self.assertIn("function isLifecycleAction(action)", page)
        self.assertIn("function serviceProgressTargetReached(targetKind, currentKind)", page)
        self.assertIn('return currentKind === "up" || currentKind === "external";', page)
        self.assertIn("pending && targetKind && !serviceProgressTargetReached(targetKind, kind)", page)
        self.assertIn("card.dataset.progressTargetKind = targetKind;", page)
        self.assertIn('function markServiceCardFailed(name, label = "failed")', page)
        self.assertIn('function markServiceCardsFailed(names, label = "failed")', page)
        self.assertIn("function delay(ms)", page)
        self.assertIn("serviceBoardStatus && activeProgressServices.size === 0", page)
        self.assertIn("function serviceStateLabel(state, health)", page)
        self.assertIn('if (state === "docker:up")', page)
        self.assertIn('return "infra up";', page)
        self.assertIn('if (state === "external")', page)
        self.assertIn('return "unmanaged";', page)
        self.assertIn('health === "ok" && state !== "external"', page)
        self.assertIn('return "healthy";', page)
        self.assertIn("stateLabel.textContent = serviceStateLabel(state, health)", page)
        self.assertNotIn('data-flow-step="start"', page)
        self.assertIn("const workspaceProfiles =", page)
        self.assertIn('const serviceSearch = document.querySelector("#serviceSearch")', page)
        self.assertIn("function filteredServiceNames()", page)
        self.assertIn("function updateServiceBoardFilter()", page)
        self.assertIn("function serviceSearchText(item)", page)
        self.assertIn("serviceSearch.value.trim().toLowerCase()", page)
        self.assertIn("serviceSearchText(item).includes(query)", page)
        self.assertIn('"runtime": ["contents.api"]', page)
        self.assertIn('"infra": ["contents.db"]', page)
        self.assertIn("function profileServiceNames(profile)", page)
        self.assertIn("if (profile && Array.isArray(profile.services))", page)
        self.assertIn("profileServiceNames(workspaceProfiles[profileName] || workspaceProfiles[selectedTarget])", page)
        self.assertIn("card.hidden = !visibleNames.includes(card.dataset.serviceName)", page)
        self.assertIn("option.hidden = option.value && !visibleNames.includes(option.value)", page)
        self.assertIn('const target = document.querySelector("#target");', page)
        self.assertIn('target.addEventListener("change", () => updateServiceBoardFilter())', page)
        self.assertIn('document.querySelector("#mode").addEventListener("change", () => updateServiceBoardFilter())', page)
        self.assertIn('serviceSearch.addEventListener("input", () => updateServiceBoardFilter())', page)
        self.assertIn("activeProgressServices", page)
        self.assertIn("const actionServices = setActionServiceProgress(action, button)", page)
        self.assertIn("actionSucceeded = Boolean(data && data.ok)", page)
        self.assertIn('if (workspaceReady && isLifecycleAction(action))', page)
        self.assertIn("await refreshStatus(false, { resolveProgress: true })", page)
        self.assertIn('if (isLifecycleAction(action) && !actionSucceeded)', page)
        self.assertIn('only: serviceOnly ? service : ""', page)
        self.assertIn('["up", "down"].includes(action) && button && button.dataset.serviceOnly === "1"', page)
        self.assertIn('button.dataset.requiresService === "1"', page)
        self.assertNotIn("function selectedLogService()", page)
        self.assertNotIn("const service = selectedLogService();", page)
        self.assertIn("function openLogsPanel(serviceName, startLive = false)", page)
        self.assertIn("function closeLogsPanel(minimize = true)", page)
        self.assertIn('followLogsButton.addEventListener("click", () => followLogs())', page)
        self.assertIn("return false;", page)
        self.assertNotIn("function prepareRecoverLogs(service)", page)
        self.assertNotIn("function appendRecoverResultToLogs(data)", page)
        self.assertNotIn("prepareRecoverLogs(service);", page)
        self.assertNotIn("appendRecoverResultToLogs(data);", page)
        self.assertNotIn('logsStreamState.textContent = "recovering " + service;', page)
        self.assertIn('if (shouldOpenDrawerForAction(action)) {', page)
        self.assertNotIn("setLogsDrawerContext(service)", page)
        self.assertIn('resetLogsOutput(data.output || "");', page)
        self.assertIn('appendLiveLogChunk(data.chunk || "");', page)
        self.assertIn("function isLogsOutputAtBottom()", page)
        self.assertIn("function updateLogsJumpAffordance()", page)
        self.assertIn("function flushPendingLogChunks()", page)
        self.assertIn("const shouldFollow = logsAutoFollow && isLogsOutputAtBottom();", page)
        self.assertIn('jumpToLatestLogsButton.textContent = unseenLiveLogChunks > 0 ? "New logs" : "Jump to latest";', page)
        self.assertIn('followLogs();\n        if (workspaceReady)', page)
        self.assertNotIn("setServiceCardProgress(service, \"following\", false)", page)
        self.assertIn('setLogsStatus("following", "ok")', page)
        close_logs_panel = page[page.index("function closeLogsPanel"):page.index("function toggleLogsPanel")]
        self.assertIn('stopLiveLogs("stopped");', close_logs_panel)
        stop_live_logs = page[page.index("function stopLiveLogs"):page.index("async function loadServiceLogs")]
        self.assertNotIn("clearActiveServiceProgress()", stop_live_logs)
        self.assertIn('closeOutputPanelButton.addEventListener("pointerdown", handleMinimizeOutputPanel)', page)
        self.assertIn('closeOutputPanelButton.addEventListener("click", handleMinimizeOutputPanel)', page)
        self.assertIn('drawerBackdrop.addEventListener("click", () => {', page)
        self.assertIn("closeLogsPanel(false);", page)
        self.assertIn('window.addEventListener("pagehide", () => stopLiveLogs("stopped"))', page)
        self.assertNotIn("/api/console/session/start", page)
        self.assertNotIn("/api/console/session/input", page)
        self.assertNotIn("/api/console/session/resize", page)
        self.assertNotIn("/api/console/session/stream", page)
        self.assertNotIn("/api/console/session/stop", page)
        self.assertNotIn("openWorkspaceTerminal", page)
        self.assertIn("clearActiveServiceProgress()", page)
        self.assertIn('card.dataset.progressClearOnStatus === "1"', page)
        self.assertNotIn('<div class="control-title">Service Actions</div>', page)
        self.assertNotIn('class="buttons advanced-buttons"', page)

    def test_compose_follow_logs_includes_tail_and_follow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compose = root / "compose.yml"
            compose.write_text("services: {}\n")
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "contents.db": {
                            "type": "compose",
                            "compose": str(compose),
                            "service": "db",
                        },
                    },
                },
            )

            with mock.patch.object(workspace, "run_compose") as run_compose:
                workspace.show_logs("contents.db", follow=True, lines=80)

        run_compose.assert_called_once()
        self.assertEqual(run_compose.call_args.args[1], ["logs", "--tail", "80", "-f", "db"])

    def test_ui_console_key_handler_routes_shift_enter_to_ai_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shop"
            root.mkdir()

        with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"), \
                mock.patch.object(ccstack.Path, "cwd", return_value=root):
            workspace = ccstack.Workspace.load_for_ui(argparse.Namespace(root=None, workspace=None))
            page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry.load(), False)

        self.assertNotIn("isShiftEnter", page)
        self.assertNotIn("queueAiSessionInput", page)
        self.assertNotIn('const isAiMode = outputPanel && outputPanel.dataset.consoleMode === "ai";', page)
        self.assertNotIn("sendConsoleSessionInput", page)

    def test_ui_console_and_logs_selectors_are_panel_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shop"
            root.mkdir()

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"), \
                    mock.patch.object(ccstack.Path, "cwd", return_value=root):
                workspace = ccstack.Workspace.load_for_ui(argparse.Namespace(root=None, workspace=None))
                page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry.load(), False)

        self.assertIn('const outputPanel = document.querySelector("#outputPanel");', page)
        self.assertIn('const aiDoctorPanel = document.querySelector("#aiDoctorPanel");', page)
        self.assertIn('const logsPanel = document.querySelector("#logsPanel");', page)
        self.assertNotIn('const terminalBody = outputPanel.querySelector(".terminal-body");', page)
        self.assertNotIn('const terminalEmulator = outputPanel.querySelector("#terminalEmulator");', page)
        self.assertNotIn('const consoleModeButtons = Array.from(outputPanel.querySelectorAll("button[data-console-mode]"));', page)
        self.assertIn('const aiAgentButtons = Array.from(aiDoctorPanel.querySelectorAll("[data-ai-agent]"));', page)
        self.assertIn('const logsOutput = logsPanel.querySelector("#logsOutput");', page)
        self.assertIn('const followLogsButton = logsPanel.querySelector("#followLogs");', page)
        self.assertNotIn('const terminalBody = document.querySelector(".terminal-body");', page)
        self.assertNotIn('document.querySelectorAll("button[data-console-mode]")', page)

    def test_ui_logs_markup_does_not_match_console_terminal_selectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shop"
            root.mkdir()

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"), \
                    mock.patch.object(ccstack.Path, "cwd", return_value=root):
                workspace = ccstack.Workspace.load_for_ui(argparse.Namespace(root=None, workspace=None))
                page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry.load(), False)

        logs_start = page.index('<aside class="logs-panel"')
        logs_end = page.index("</aside>", logs_start)
        logs_markup = page[logs_start:logs_end]
        console_start = page.index('<aside class="output-panel"')
        console_end = page.index("</aside>", console_start)
        console_markup = page[console_start:console_end]

        self.assertIn('class="logs-shell"', logs_markup)
        self.assertIn('class="logs-output"', logs_markup)
        self.assertNotIn('class="terminal"', logs_markup)
        self.assertNotIn('terminal-body', logs_markup)
        self.assertNotIn('terminal-emulator', logs_markup)
        self.assertNotIn('has-xterm', logs_markup)
        self.assertNotIn('data-console-mode', logs_markup)
        self.assertIn('data-console-mode="doctor"', console_markup)
        self.assertNotIn('data-console-mode="terminal"', console_markup)
        self.assertNotIn('data-console-mode="ai"', console_markup)
        self.assertNotIn('data-console-mode="logs"', console_markup)

    def test_ui_console_key_handler_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shop"
            root.mkdir()

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"), \
                    mock.patch.object(ccstack.Path, "cwd", return_value=root):
                workspace = ccstack.Workspace.load_for_ui(argparse.Namespace(root=None, workspace=None))
                page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry.load(), False)

        self.assertNotIn('if (consoleSessionId) {', page)
        self.assertNotIn('sendConsoleSessionInput("\\r");', page)
        self.assertNotIn("attachCustomKeyEventHandler", page)

    # ------------------------------------------------------------------
    # Startup datasource invariant
    # ------------------------------------------------------------------
    # The tests below cover the JDBC-URL startup invariant introduced to
    # eliminate the silent-degradation circuit (an app passing readiness
    # while actually bound to H2 / a stale fallback datasource). They also
    # cover the mandatory `LOCAL_CONFIG_IMPORT` constant change that blocks
    # the config-import side of the same circuit.

    def test_local_config_import_is_mandatory(self):
        """LOCAL_CONFIG_IMPORT must not use the `optional:` prefix.

        The `optional:` prefix tells Spring Boot to silently skip a missing
        config resource. For the ccstack-managed config-import path this
        was exactly the silent-degradation circuit we want to eliminate.
        Missing `devstack-local.yml` on the classpath must fail Spring Boot
        startup fast (ConfigDataResourceNotFoundException).
        """
        self.assertEqual(ccstack.LOCAL_CONFIG_IMPORT, "classpath:devstack-local.yml")
        self.assertFalse(
            ccstack.LOCAL_CONFIG_IMPORT.startswith("optional:"),
            msg="LOCAL_CONFIG_IMPORT must not be `optional:` — silent fallback is forbidden",
        )

    def test_managed_gradle_init_emits_mandatory_local_config_import(self):
        """write_managed_gradle_init with the config-server patch enabled must
        emit `'spring.config.import': 'classpath:devstack-local.yml'` (no
        `optional:` prefix) into the generated init script."""
        with tempfile.TemporaryDirectory() as tmp:
            init_path = Path(tmp) / "devstack-local-only.gradle"
            ccstack.write_managed_gradle_init(init_path, include_config_server_patch=True)
            text = init_path.read_text()

        self.assertIn("'spring.config.import': 'classpath:devstack-local.yml'", text)
        self.assertNotIn("'spring.config.import': 'optional:classpath:devstack-local.yml'", text)
        # The replacement regex inside the processResources filter must also
        # target the mandatory form so any source-tree `configserver:` URL
        # gets rewritten to the same value the systemProperty injection uses.
        self.assertIn("classpath:devstack-local.yml", text)

    # ------------------------------------------------------------------
    # devstack-local.yml classpath materialization (regression: 1ea4928)
    # ------------------------------------------------------------------
    # Commit 1ea4928 made `spring.config.import` mandatory (no `optional:`
    # prefix) to close the silent-degradation circuit, but did not actually
    # produce `devstack-local.yml` on the classpath. Every fixity-* / proxy:*
    # bootRun then failed with ConfigDataResourceNotFoundException. These
    # tests pin the fix: the file must exist under managed_infra_dir and
    # must be wired onto every Spring Boot module's runtime classpath via
    # the generated gradle init script.

    def test_materialize_managed_environment_emits_ccstack_local_yml_on_config_patch(self):
        """When the workspace plan requests the config-server patch
        (`requires_config_resource_patch=True`), materialize_managed_environment
        MUST write a `devstack-local.yml` file under `managed_infra_dir(name)`
        whose body is valid YAML and carries the `managed by devstack` marker.

        Without this file the mandatory `classpath:devstack-local.yml`
        import fails Spring Boot startup fast — the failure commit 1ea4928
        introduced and that this regression test guards against.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shopping"
            root.mkdir(parents=True)
            state_home = Path(tmp) / ".ccstack" / "state"
            config = {
                "workspace": "shopping",
                "profiles": {"default": {"services": ["apps.fixity-contents.contents"]}},
                "services": {
                    "apps.fixity-contents.contents": {
                        "type": "gradle",
                        "task": ":apps:fixity-contents:contents:bootRun",
                        "module": "apps/fixity-contents/contents",
                    },
                },
            }
            analysis = {
                "environment": {
                    "generated": [],
                    "env": [],
                    "args": [],
                    "requirements": [
                        # triggers include_config_server_patch=True
                        {"id": "config-server-local-overrides"},
                    ],
                },
                "persistence": {},
            }

            with mock.patch.object(ccstack, "STATE_HOME", state_home):
                ccstack.materialize_managed_environment(
                    "shopping", root, config, analysis
                )
                infra_dir = ccstack.managed_infra_dir("shopping")
                yml_path = infra_dir / "devstack-local.yml"

                self.assertTrue(
                    yml_path.exists(),
                    msg=f"devstack-local.yml must be written under managed_infra_dir; got {yml_path}",
                )
                yml_text = yml_path.read_text()

        # Structurally valid YAML — comment-only / empty-map bodies are accepted
        # by Spring's YamlPropertySourceLoader.
        self.assertIn("managed by devstack", yml_text)
        # File must parse as YAML (placeholder content is an empty doc + comment).
        try:
            import yaml  # type: ignore
            parsed = yaml.safe_load(yml_text)
            self.assertIn(
                parsed,
                (None, {}),
                msg="placeholder devstack-local.yml must be empty/null YAML",
            )
        except ImportError:
            # PyYAML is not a hard test dep; the comment-marker check above
            # is enough as a structural sanity guard.
            pass

    def test_materialize_managed_environment_skips_ccstack_local_yml_without_patch(self):
        """When the workspace plan does NOT request the config-server patch,
        materialize_managed_environment must NOT write `devstack-local.yml`.
        The file is workspace-scoped to the config-server patch path; emitting
        it for unrelated workspaces would leak state and confuse operators.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "plain"
            root.mkdir(parents=True)
            state_home = Path(tmp) / ".ccstack" / "state"
            config = {
                "workspace": "plain",
                "profiles": {"default": {"services": ["app.plain"]}},
                "services": {
                    "app.plain": {
                        "type": "gradle",
                        "task": ":app:plain:bootRun",
                        "module": "app/plain",
                    },
                },
            }
            analysis = {
                "environment": {
                    "generated": [],
                    "env": [],
                    "args": [],
                    # No config-server-local-overrides requirement here.
                    "requirements": [],
                },
                "persistence": {},
            }

            with mock.patch.object(ccstack, "STATE_HOME", state_home):
                ccstack.materialize_managed_environment(
                    "plain", root, config, analysis
                )
                infra_dir = ccstack.managed_infra_dir("plain")
                yml_path = infra_dir / "devstack-local.yml"

                self.assertFalse(
                    yml_path.exists(),
                    msg=f"devstack-local.yml must NOT be written when config-server patch is not requested; found {yml_path}",
                )

    def test_managed_gradle_init_wires_ccstack_local_yml_into_processresources(self):
        """The generated init script must put `devstack-local.yml` onto each
        Spring Boot module's runtime classpath by adding the managed infra
        dir as an extra source for the `processResources` task. Without this,
        the classpath does not contain the file and bootRun fails with
        ConfigDataResourceNotFoundException.
        """
        with tempfile.TemporaryDirectory() as tmp:
            infra_dir = Path(tmp) / ".ccstack" / "state" / "shopping" / "infra"
            infra_dir.mkdir(parents=True)
            (infra_dir / "devstack-local.yml").write_text(
                "# managed by devstack\n{}\n"
            )
            init_path = infra_dir / "devstack-local-only.gradle"

            ccstack.write_managed_gradle_init(
                init_path,
                include_config_server_patch=True,
                managed_infra_dir=infra_dir,
            )

            text = init_path.read_text()

        # The processResources task must receive an additional source for the
        # managed infra dir, filtered to devstack-local.yml, so the file lands
        # in build/resources/main/ and Spring's classpath resolver can find it.
        self.assertIn(
            "task.from(",
            text,
            msg="processResources patch must add task.from(<infra_dir>) to inject devstack-local.yml",
        )
        self.assertIn(
            "include 'devstack-local.yml'",
            text,
            msg="task.from(...) must restrict the include to devstack-local.yml",
        )
        # The path used inside the closure must reference the managed infra dir
        # we passed in (use the JSON-quoted Groovy literal form for safety).
        self.assertIn(json.dumps(str(infra_dir)), text)
        # Mandatory invariant — we must not regress the optional: ban.
        self.assertNotIn("'spring.config.import': 'optional:", text)

    def test_managed_gradle_init_without_infra_dir_skips_processresources_from(self):
        """Backward compatibility: when no `managed_infra_dir` is supplied,
        `write_managed_gradle_init` must NOT emit a `task.from(...)` wire.
        Existing callers (and existing tests at 2190+, 7339+) rely on the
        legacy shape of the init script."""
        with tempfile.TemporaryDirectory() as tmp:
            init_path = Path(tmp) / "devstack-local-only.gradle"
            ccstack.write_managed_gradle_init(
                init_path,
                include_config_server_patch=True,
            )
            text = init_path.read_text()

        self.assertNotIn("task.from(", text)
        # The rest of the config-server patch must still be present.
        self.assertIn("'spring.config.import': 'classpath:devstack-local.yml'", text)
        self.assertIn("def devstackPatchConfigServerImports = { project ->", text)

    def _actuator_env_response(self, value):
        """Helper: produce a urlopen-compatible response object whose body
        matches Spring Boot Actuator's `/env/{name}` JSON shape.

        Boot 2.x and 3.x both return `{"property": {"source": "...", "value": ...}}`
        for a single property lookup. We mimic exactly that envelope so
        fetch_actuator_env_property's parser sees a realistic shape.
        """
        body = json.dumps({"property": {"source": "managedEnv", "value": value}}).encode("utf-8")

        class _Response:
            status = 200

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, exc_type, exc, traceback):
                return False

            def read(self_inner):
                return body

        return _Response()

    def test_verify_managed_datasource_passes_when_actuator_matches(self):
        """When `/actuator/env/spring.datasource.url` reports the same URL
        that was planted on `service['env']`, verify_managed_datasource
        must return silently."""
        service = {
            "type": "gradle",
            "task": ":apps:shop:bootRun",
            "port": 8080,
            "env": {
                "SPRING_DATASOURCE_URL": "jdbc:mysql://localhost:3306/shop",
            },
        }
        captured_urls = []
        helper = self

        def fake_urlopen(url, timeout=None):  # noqa: ARG001 — match urllib signature
            captured_urls.append(url if isinstance(url, str) else url.full_url)
            return helper._actuator_env_response("jdbc:mysql://localhost:3306/shop")

        with mock.patch.object(ccstack.urllib.request, "urlopen", side_effect=fake_urlopen):
            ccstack.verify_managed_datasource("apps.shop", service)

        self.assertEqual(len(captured_urls), 1)
        self.assertIn("/actuator/env/spring.datasource.url", captured_urls[0])

    def test_verify_managed_datasource_raises_on_mismatch(self):
        """When actuator reports a JDBC URL different from the manifest's
        expected URL, verify_managed_datasource must raise CcstackError
        with `does not match` + `refusing to mark ready` markers so the
        caller marks the service FAILED instead of READY."""
        service = {
            "type": "gradle",
            "task": ":apps:shop:bootRun",
            "port": 8080,
            "env": {
                "SPRING_DATASOURCE_URL": "jdbc:mysql://localhost:3306/shop",
            },
        }
        helper = self

        def fake_urlopen(url, timeout=None):  # noqa: ARG001
            # App fell back to H2 silently — exactly the bug we are guarding.
            return helper._actuator_env_response("jdbc:h2:mem:testdb")

        with mock.patch.object(ccstack.urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(ccstack.CcstackError) as cm:
                ccstack.verify_managed_datasource("apps.shop", service)

        message = str(cm.exception)
        self.assertIn("does not match", message)
        self.assertIn("refusing to mark ready", message)
        self.assertIn("jdbc:h2:mem:testdb", message)
        self.assertIn("jdbc:mysql://localhost:3306/shop", message)

    def test_verify_managed_datasource_soft_passes_when_actuator_unreachable(self):
        """When the actuator env endpoint is unreachable (URLError /
        ConnectionError / unparseable response), verify_managed_datasource
        must NOT raise — that would be a false-positive failure for every
        non-Spring or actuator-disabled app — *provided the new DB liveness
        probe also passes*.

        Contract change (2026-06-04): the soft-pass branch is no longer
        unconditional. With actuator unreachable, ccstack now MUST
        independently prove each expected managed JDBC URL points at a live
        database before readiness can succeed. The probe is stubbed out
        here via the ``MANAGED_DATASOURCE_PROBE_SIGNALS`` test seam — full
        probe-failure coverage lives in the new
        ``test_verify_managed_datasource_soft_pass_*`` block below.
        """
        service = {
            "type": "gradle",
            "task": ":apps:shop:bootRun",
            "port": 8080,
            "env": {
                "SPRING_DATASOURCE_URL": "jdbc:mysql://localhost:3306/shop",
            },
        }

        import urllib.error  # local import; ccstack module already imports it but we reference here too

        def fake_urlopen(url, timeout=None):  # noqa: ARG001
            raise urllib.error.URLError("connection refused")

        # Stub the DB liveness probe so this test stays focused on the
        # WARN-line contract (the original purpose of this regression).
        def fake_probe(property_name, jdbc_url):  # noqa: ARG001
            return {
                "ok": True,
                "method": "tcp+greeting",
                "confidence": "high",
                "reason": "stubbed greeting validated",
                "host": "127.0.0.1",
                "port": 3306,
                "engine": "mysql",
            }

        captured_stderr = []
        original_stderr_write = ccstack.sys.stderr.write

        def capture_stderr(text):
            captured_stderr.append(text)
            return len(text)

        ccstack.consume_managed_datasource_probe_signals()  # clear leftovers
        with mock.patch.object(ccstack.urllib.request, "urlopen", side_effect=fake_urlopen), \
                mock.patch.object(ccstack, "probe_managed_jdbc_target", side_effect=fake_probe), \
                mock.patch.object(ccstack.sys.stderr, "write", side_effect=capture_stderr):
            ccstack.verify_managed_datasource("apps.shop", service)

        joined = "".join(captured_stderr)
        self.assertIn("WARN", joined)
        self.assertIn("apps.shop", joined)
        self.assertIn("actuator env unreachable", joined)
        # New WARN suffix — operators must see *why* readiness was permitted.
        self.assertIn("DB liveness proven", joined)
        self.assertIn("app-side binding unverified", joined)

    def test_verify_managed_datasource_is_noop_without_datasource_env(self):
        """A service with no `SPRING_DATASOURCE_*_URL` keys in its env (e.g.
        a non-database gradle service) must pass through silently, with no
        HTTP call attempted."""
        service = {
            "type": "gradle",
            "task": ":apps:misc:bootRun",
            "port": 8090,
            "env": {"SOMETHING_ELSE": "x"},
        }

        with mock.patch.object(ccstack.urllib.request, "urlopen") as urlopen:
            ccstack.verify_managed_datasource("apps.misc", service)

        urlopen.assert_not_called()

    def test_verify_managed_datasource_checks_per_app_url_variants(self):
        """When a service carries per-app URL variants such as
        `SPRING_DATASOURCE_BLMASTERBOARD_URL`, verify_managed_datasource
        must probe the matching Spring property name
        (`spring.datasource.blmasterboard.url`) and validate each one."""
        service = {
            "type": "gradle",
            "task": ":apps:fixity-contents:contents:bootRun",
            "port": 8095,
            "env": {
                "SPRING_DATASOURCE_URL": "jdbc:mysql://localhost:3306/shop",
                "SPRING_DATASOURCE_BLMASTERBOARD_URL": "jdbc:mysql://localhost:3306/board",
            },
        }
        helper = self
        captured_urls = []

        def fake_urlopen(url, timeout=None):  # noqa: ARG001
            full = url if isinstance(url, str) else url.full_url
            captured_urls.append(full)
            if "spring.datasource.url" in full and "blmasterboard" not in full:
                return helper._actuator_env_response("jdbc:mysql://localhost:3306/shop")
            if "spring.datasource.blmasterboard.url" in full:
                return helper._actuator_env_response("jdbc:mysql://localhost:3306/board")
            return helper._actuator_env_response(None)

        with mock.patch.object(ccstack.urllib.request, "urlopen", side_effect=fake_urlopen):
            ccstack.verify_managed_datasource("apps.contents", service)

        joined = " ".join(captured_urls)
        self.assertIn("/actuator/env/spring.datasource.url", joined)
        self.assertIn("/actuator/env/spring.datasource.blmasterboard.url", joined)

    def test_wait_for_ready_invokes_datasource_invariant_after_health(self):
        """wait_for_ready must call verify_managed_datasource after the
        health/port readiness loop succeeds — readiness is no longer
        signalled just by 'port listens' or 'health 2xx', but by 'port
        listens AND bound datasource URL matches the manifest'."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = ccstack.Workspace(
                Path(tmp),
                {
                    "workspace": "shop",
                    "profiles": {},
                    "services": {
                        "apps.shop": {
                            "type": "gradle",
                            "task": ":apps:shop:bootRun",
                            "port": 8080,
                            "health": "http://localhost:8080/actuator/health",
                            "env": {
                                "SPRING_DATASOURCE_URL": "jdbc:mysql://localhost:3306/shop",
                            },
                        }
                    },
                },
            )
            captured_calls = []

            def fake_verify(service_name, service):
                captured_calls.append((service_name, service.get("env", {}).get("SPRING_DATASOURCE_URL")))

            with mock.patch.object(ccstack, "http_ok", return_value=True), \
                    mock.patch.object(ccstack, "verify_managed_datasource", side_effect=fake_verify):
                workspace.wait_for_ready("apps.shop", workspace.service("apps.shop"))

        self.assertEqual(len(captured_calls), 1)
        self.assertEqual(captured_calls[0][0], "apps.shop")
        self.assertEqual(captured_calls[0][1], "jdbc:mysql://localhost:3306/shop")

    # ------------------------------------------------------------------
    # DB liveness probe regression tests (2026-06-04 contract change)
    # ------------------------------------------------------------------
    #
    # The soft-pass branch of verify_managed_datasource (actuator
    # unreachable for every probed property) used to be unconditional: a
    # single WARN line, then readiness succeeds. That semantics silently
    # passed the H2-fallback bug class the invariant was added to catch.
    #
    # The narrowed contract requires ccstack to independently probe each
    # expected managed JDBC URL. The tests below cover the four probe
    # outcomes (tcp+greeting / docker_exec / tcp_only / none), the
    # machine-readable signal pipeline, the failure-path CcstackError, and
    # the unchanged actuator-reachable path (regression guard).

    @staticmethod
    def _make_unreachable_actuator():
        """Helper: side_effect for urlopen that always raises URLError —
        models the actuator-unreachable arm of verify_managed_datasource."""
        import urllib.error

        def _raise(url, timeout=None):  # noqa: ARG001
            raise urllib.error.URLError("connection refused")

        return _raise

    def test_verify_managed_datasource_soft_pass_now_requires_db_liveness(self):
        """Contract: actuator unreachable + DB liveness probe ok=True
        (method=tcp+greeting, confidence=high) → no raise; WARN line
        names the proven method and explicitly states the app-side
        binding remains unverified; exactly one machine-readable signal
        is appended.
        """
        service = {
            "type": "gradle",
            "task": ":apps:shop:bootRun",
            "port": 8080,
            "env": {
                "SPRING_DATASOURCE_URL": "jdbc:mysql://localhost:3306/shop",
            },
        }

        def fake_probe(property_name, jdbc_url):
            self.assertEqual(property_name, "spring.datasource.url")
            self.assertEqual(jdbc_url, "jdbc:mysql://localhost:3306/shop")
            return {
                "ok": True,
                "method": "tcp+greeting",
                "confidence": "high",
                "reason": "MySQL greeting validated (server=8.0.36)",
                "host": "localhost",
                "port": 3306,
                "engine": "mysql",
            }

        captured_stderr = []

        def capture_stderr(text):
            captured_stderr.append(text)
            return len(text)

        ccstack.consume_managed_datasource_probe_signals()  # clear leftovers
        with mock.patch.object(ccstack.urllib.request, "urlopen", side_effect=self._make_unreachable_actuator()), \
                mock.patch.object(ccstack, "probe_managed_jdbc_target", side_effect=fake_probe), \
                mock.patch.object(ccstack.sys.stderr, "write", side_effect=capture_stderr):
            # No raise = readiness permitted.
            ccstack.verify_managed_datasource("apps.shop", service)

        joined = "".join(captured_stderr)
        self.assertIn("WARN", joined)
        self.assertIn("apps.shop", joined)
        self.assertIn("actuator env unreachable", joined)
        self.assertIn("DB liveness proven", joined)
        self.assertIn("tcp+greeting", joined)
        self.assertIn("confidence=high", joined)
        self.assertIn("app-side binding unverified", joined)

        signals = ccstack.consume_managed_datasource_probe_signals()
        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal["service"], "apps.shop")
        self.assertEqual(signal["property"], "spring.datasource.url")
        self.assertEqual(signal["jdbc_url"], "jdbc:mysql://localhost:3306/shop")
        self.assertTrue(signal["ok"])
        self.assertEqual(signal["method"], "tcp+greeting")
        self.assertEqual(signal["confidence"], "high")
        self.assertEqual(signal["engine"], "mysql")

    def test_verify_managed_datasource_soft_pass_denies_readiness_on_probe_failure(self):
        """Contract: actuator unreachable + probe ok=False →
        CcstackError raised; message names BOTH 'actuator env
        unreachable' AND 'DB liveness probe failed' AND the failing
        property's JDBC URL. A degraded signal is still appended so the
        follow-up status counter can record the denial.
        """
        service = {
            "type": "gradle",
            "task": ":apps:shop:bootRun",
            "port": 8080,
            "env": {
                "SPRING_DATASOURCE_URL": "jdbc:mysql://localhost:3306/shop",
            },
        }

        def fake_probe(property_name, jdbc_url):  # noqa: ARG001
            return {
                "ok": False,
                "method": "none",
                "confidence": "degraded",
                "reason": "no evidence of a live DB at 127.0.0.1:3306",
                "host": "localhost",
                "port": 3306,
                "engine": "mysql",
            }

        ccstack.consume_managed_datasource_probe_signals()
        with mock.patch.object(ccstack.urllib.request, "urlopen", side_effect=self._make_unreachable_actuator()), \
                mock.patch.object(ccstack, "probe_managed_jdbc_target", side_effect=fake_probe):
            with self.assertRaises(ccstack.CcstackError) as cm:
                ccstack.verify_managed_datasource("apps.shop", service)

        message = str(cm.exception)
        self.assertIn("apps.shop", message)
        self.assertIn("actuator env unreachable", message)
        self.assertIn("DB liveness probe", message)
        self.assertIn("failed", message)
        self.assertIn("refusing to mark ready", message)
        self.assertIn("spring.datasource.url", message)
        self.assertIn("jdbc:mysql://localhost:3306/shop", message)

        signals = ccstack.consume_managed_datasource_probe_signals()
        self.assertEqual(len(signals), 1)
        self.assertFalse(signals[0]["ok"])
        self.assertEqual(signals[0]["confidence"], "degraded")
        self.assertEqual(signals[0]["method"], "none")

    def test_verify_managed_datasource_soft_pass_uses_tcp_only_for_unknown_engine(self):
        """Contract: actuator unreachable + Postgres URL (greeting parser
        not feasible) + docker fallback unavailable → readiness still
        succeeds via TCP-only proof BUT the signal records
        confidence=low so the follow-up status counter can surface the
        soft success."""
        service = {
            "type": "gradle",
            "task": ":apps:reporting:bootRun",
            "port": 8088,
            "env": {
                "SPRING_DATASOURCE_URL": "jdbc:postgresql://localhost:5432/reporting",
            },
        }

        def fake_greeting(host, port):  # noqa: ARG001
            # Probe target host/port: orchestrator calls greeting only for
            # mysql/mariadb engines, so this should not actually be hit for
            # postgresql. Returning a failure is the safe default.
            return {"ok": False, "reason": "not invoked for postgresql", "server_version": None}

        def fake_docker(host, port, engine):  # noqa: ARG001
            return None  # fallback unavailable

        def fake_port_open(host, port):  # noqa: ARG001
            return True  # TCP-open simulates a live process

        ccstack.consume_managed_datasource_probe_signals()
        with mock.patch.object(ccstack.urllib.request, "urlopen", side_effect=self._make_unreachable_actuator()), \
                mock.patch.object(ccstack, "_mysql_greeting_alive", side_effect=fake_greeting), \
                mock.patch.object(ccstack, "_docker_exec_db_probe", side_effect=fake_docker), \
                mock.patch.object(ccstack, "port_open", side_effect=fake_port_open):
            ccstack.verify_managed_datasource("apps.reporting", service)

        signals = ccstack.consume_managed_datasource_probe_signals()
        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertTrue(signal["ok"])
        self.assertEqual(signal["method"], "tcp_only")
        self.assertEqual(signal["confidence"], "low")
        self.assertEqual(signal["engine"], "postgresql")
        self.assertIn("postgresql", signal["jdbc_url"])

    def test_verify_managed_datasource_soft_pass_falls_back_to_docker_exec(self):
        """Contract: actuator unreachable + MySQL greeting fails +
        docker_exec returns {ok: True} → readiness succeeds via the
        docker-exec method; signal records high confidence."""
        service = {
            "type": "gradle",
            "task": ":apps:shop:bootRun",
            "port": 8080,
            "env": {
                "SPRING_DATASOURCE_URL": "jdbc:mysql://localhost:3306/shop",
            },
        }

        def fake_greeting(host, port):  # noqa: ARG001
            return {"ok": False, "reason": "TCP/recv error: timed out", "server_version": None}

        def fake_docker(host, port, engine):  # noqa: ARG001
            return {"ok": True, "reason": "docker exec ccstack_mysql_1 mysql SELECT 1 returned 0"}

        ccstack.consume_managed_datasource_probe_signals()
        with mock.patch.object(ccstack.urllib.request, "urlopen", side_effect=self._make_unreachable_actuator()), \
                mock.patch.object(ccstack, "_mysql_greeting_alive", side_effect=fake_greeting), \
                mock.patch.object(ccstack, "_docker_exec_db_probe", side_effect=fake_docker):
            # No raise = readiness permitted via docker fallback.
            ccstack.verify_managed_datasource("apps.shop", service)

        signals = ccstack.consume_managed_datasource_probe_signals()
        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertTrue(signal["ok"])
        self.assertEqual(signal["method"], "docker_exec")
        self.assertEqual(signal["confidence"], "high")

    def test_probe_managed_jdbc_target_signal_fields_are_machine_readable(self):
        """Contract: each entry in MANAGED_DATASOURCE_PROBE_SIGNALS
        carries the documented shape (service, property, jdbc_url, host,
        port, engine, ok, method, confidence, reason, ts). The follow-up
        status-counter task must be able to aggregate without re-parsing
        free-form text — probe results are the source of truth."""
        # Direct call to probe helper to confirm the orchestrator returns
        # the documented dict shape, then verify_managed_datasource pipes
        # those fields into the signal list verbatim.

        def fake_greeting(host, port):  # noqa: ARG001
            return {
                "ok": True,
                "reason": "MySQL greeting validated (protocol_version=10, server=8.0.36)",
                "server_version": "8.0.36",
            }

        def fake_docker(host, port, engine):  # noqa: ARG001
            return None

        result = ccstack.probe_managed_jdbc_target(
            "spring.datasource.url",
            "jdbc:mysql://localhost:3306/shop",
            greeting_probe=fake_greeting,
            docker_probe=fake_docker,
        )

        for key in ("ok", "method", "confidence", "reason", "host", "port", "engine"):
            self.assertIn(key, result, f"missing key {key!r} in probe result")
        self.assertIsInstance(result["ok"], bool)
        self.assertIsInstance(result["port"], int)
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "tcp+greeting")
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["engine"], "mysql")
        # Probe-host shim: host string in the result should be the host
        # as it appeared in the JDBC URL (manifest accuracy), NOT the
        # post-shim 127.0.0.1.
        self.assertEqual(result["host"], "localhost")

    def test_mysql_greeting_validates_protocol_byte_and_version(self):
        """Direct unit test on the byte-level greeting parser. Crafts the
        first ~30 bytes of three handshake variants:

        - Good MySQL 8.0.36 handshake (protocol byte 10, ASCII version,
          NUL terminator) → ok=True, server_version="8.0.36".
        - Bad protocol byte (0x09 instead of 0x0a) → ok=False.
        - Missing NUL terminator → ok=False.
        """
        good = bytearray()
        # 3-byte payload length (LE) — use 40 as a plausible value
        good.extend(b"\x28\x00\x00")
        # sequence id
        good.append(0x00)
        # protocol version 10
        good.append(0x0a)
        # server-version "8.0.36" NUL-terminated
        good.extend(b"8.0.36\x00")
        # trailing handshake bytes are ignored by the validator
        good.extend(b"\x00" * 16)
        good_bytes = bytes(good)

        bad_proto = bytearray()
        bad_proto.extend(b"\x28\x00\x00")
        bad_proto.append(0x00)
        bad_proto.append(0x09)  # not 10
        bad_proto.extend(b"8.0.36\x00")
        bad_proto.extend(b"\x00" * 16)
        bad_proto_bytes = bytes(bad_proto)

        no_nul = bytearray()
        no_nul.extend(b"\x28\x00\x00")
        no_nul.append(0x00)
        no_nul.append(0x0a)
        no_nul.extend(b"8.0.36-no-terminator")  # no NUL
        no_nul.extend(b"X" * 8)
        no_nul_bytes = bytes(no_nul)

        # Drive _mysql_greeting_alive by monkey-patching the socket so the
        # parser sees exactly the byte sequences above.
        original_socket = ccstack.socket.socket

        class _FakeSocket:
            def __init__(self, payload):
                self._payload = payload
                self.timeout = None

            def settimeout(self, timeout):
                self.timeout = timeout

            def connect(self, addr):
                self._addr = addr

            def recv(self, _):
                return self._payload

            def close(self):
                pass

        for payload, expect_ok, expected_version in (
            (good_bytes, True, "8.0.36"),
            (bad_proto_bytes, False, None),
            (no_nul_bytes, False, None),
        ):
            with mock.patch.object(ccstack.socket, "socket", lambda *a, **kw: _FakeSocket(payload)):
                result = ccstack._mysql_greeting_alive("127.0.0.1", 3306, timeout=0.1)
            self.assertEqual(
                result["ok"], expect_ok,
                f"payload prefix={payload[:6].hex()} expected ok={expect_ok}, got {result}",
            )
            if expect_ok:
                self.assertEqual(result["server_version"], expected_version)
                self.assertIn("MySQL greeting validated", result["reason"])
            else:
                self.assertIsNone(result["server_version"])

        # Sanity: the original socket class is still in place.
        self.assertIs(ccstack.socket.socket, original_socket)

    def test_verify_managed_datasource_actuator_reachable_path_unchanged(self):
        """Regression guard: when actuator is reachable AND reports a
        matching URL for every probed property, the function must:
        - return silently (no raise),
        - NOT invoke the DB liveness probe (would add startup latency for
          the common case), and
        - NOT append any signals (no machine-readable noise on the
          happy path).
        """
        service = {
            "type": "gradle",
            "task": ":apps:shop:bootRun",
            "port": 8080,
            "env": {
                "SPRING_DATASOURCE_URL": "jdbc:mysql://localhost:3306/shop",
            },
        }

        def fake_urlopen(url, timeout=None):  # noqa: ARG001
            return self._actuator_env_response("jdbc:mysql://localhost:3306/shop")

        probe_calls = []

        def fake_probe(property_name, jdbc_url):  # noqa: ARG001
            probe_calls.append((property_name, jdbc_url))
            return {
                "ok": True,
                "method": "tcp+greeting",
                "confidence": "high",
                "reason": "unused",
                "host": "localhost",
                "port": 3306,
                "engine": "mysql",
            }

        ccstack.consume_managed_datasource_probe_signals()
        with mock.patch.object(ccstack.urllib.request, "urlopen", side_effect=fake_urlopen), \
                mock.patch.object(ccstack, "probe_managed_jdbc_target", side_effect=fake_probe):
            ccstack.verify_managed_datasource("apps.shop", service)

        self.assertEqual(probe_calls, [], "DB liveness probe must NOT run on the actuator-reachable happy path")
        self.assertEqual(
            ccstack.consume_managed_datasource_probe_signals(), [],
            "no signal must be appended on the actuator-reachable happy path",
        )

    def test_inspect_workspace_detects_package_json_as_command_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "node-app"
            root.mkdir()
            (root / "package.json").write_text(
                json.dumps({"name": "node-app", "scripts": {"start": "node server.js"}})
            )

            config = ccstack.inspect_workspace(root, "node-app")

        services = config["services"]
        self.assertIn("node-app", services)
        self.assertEqual(services["node-app"]["type"], "command")
        self.assertEqual(services["node-app"]["command"], "npm start")
        self.assertNotIn("module", services["node-app"])

    def test_inspect_workspace_detects_package_json_dev_script_when_start_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "next-app"
            root.mkdir()
            (root / "package.json").write_text(
                json.dumps({"name": "next-app", "scripts": {"dev": "next dev"}})
            )

            config = ccstack.inspect_workspace(root, "next-app")

        self.assertEqual(config["services"]["next-app"]["command"], "npm run dev")

    def test_inspect_workspace_detects_pyproject_toml_as_command_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "py-app"
            root.mkdir()
            (root / "pyproject.toml").write_text(
                "[project]\n"
                "name = \"py-app\"\n"
                "[project.scripts]\n"
                "py-app = \"py_app.cli:main\"\n"
            )

            config = ccstack.inspect_workspace(root, "py-app")

        service = config["services"]["py-app"]
        self.assertEqual(service["type"], "command")
        self.assertEqual(service["command"], "py-app")

    def test_inspect_workspace_pyproject_falls_back_to_entrypoint_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "flask-app"
            root.mkdir()
            (root / "pyproject.toml").write_text("[project]\nname = \"flask-app\"\n")
            (root / "app.py").write_text("# entry\n")

            config = ccstack.inspect_workspace(root, "flask-app")

        self.assertEqual(config["services"]["flask-app"]["command"], "python app.py")

    def test_inspect_workspace_detects_requirements_txt_as_command_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "django-app"
            root.mkdir()
            (root / "requirements.txt").write_text("django\n")
            (root / "manage.py").write_text("# django manage\n")

            config = ccstack.inspect_workspace(root, "django-app")

        service = config["services"]["django-app"]
        self.assertEqual(service["type"], "command")
        self.assertEqual(service["command"], "python manage.py runserver")

    def test_inspect_workspace_detects_go_mod_as_command_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "go-app"
            root.mkdir()
            (root / "go.mod").write_text("module example.com/go-app\n\ngo 1.22\n")

            config = ccstack.inspect_workspace(root, "go-app")

        service = config["services"]["go-app"]
        self.assertEqual(service["type"], "command")
        self.assertEqual(service["command"], "go run .")

    def test_inspect_workspace_detects_cargo_toml_as_command_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "rust-app"
            root.mkdir()
            (root / "Cargo.toml").write_text("[package]\nname = \"rust-app\"\nversion = \"0.1.0\"\n")

            config = ccstack.inspect_workspace(root, "rust-app")

        service = config["services"]["rust-app"]
        self.assertEqual(service["type"], "command")
        self.assertEqual(service["command"], "cargo run")

    def test_inspect_workspace_detects_nested_module_command_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "monorepo"
            root.mkdir()
            web_dir = root / "services" / "web"
            web_dir.mkdir(parents=True)
            (web_dir / "package.json").write_text(json.dumps({"name": "web"}))

            config = ccstack.inspect_workspace(root, "monorepo")

        self.assertIn("services.web", config["services"])
        service = config["services"]["services.web"]
        self.assertEqual(service["type"], "command")
        self.assertEqual(service["module"], "services/web")

    def test_inspect_workspace_gradle_precedence_when_command_collides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "polyglot"
            root.mkdir()
            module = root / "apps" / "service"
            resources = module / "src" / "main" / "resources"
            sources = module / "src" / "main" / "java" / "com" / "example"
            resources.mkdir(parents=True)
            sources.mkdir(parents=True)
            (module / "build.gradle").write_text(
                "plugins { id 'org.springframework.boot' version '3.2.0' }\n"
            )
            (resources / "application.yml").write_text("server:\n  port: 18090\n")
            (sources / "ServiceApplication.java").write_text(
                "package com.example;\n"
                "import org.springframework.boot.autoconfigure.SpringBootApplication;\n"
                "@SpringBootApplication public class ServiceApplication {}\n"
            )
            (module / "package.json").write_text(json.dumps({"name": "service"}))
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':apps:service'\n")

            config = ccstack.inspect_workspace(root, "polyglot")

        services = config["services"]
        # The gradle-detected name takes precedence — no duplicate command service is added.
        gradle_names = [name for name, svc in services.items() if svc.get("type") == "gradle"]
        command_names = [name for name, svc in services.items() if svc.get("type") == "command"]
        self.assertTrue(gradle_names, "expected at least one gradle-detected service")
        for cname in command_names:
            self.assertNotIn(cname, gradle_names)

    def test_inspect_workspace_command_services_ignore_node_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ignored"
            root.mkdir()
            (root / "package.json").write_text(json.dumps({"name": "root"}))
            nested = root / "node_modules" / "left-pad"
            nested.mkdir(parents=True)
            (nested / "package.json").write_text(json.dumps({"name": "left-pad"}))

            config = ccstack.inspect_workspace(root, "ignored")

        # Only the root package.json yields a service; node_modules/* is skipped.
        command_services = {name: svc for name, svc in config["services"].items() if svc.get("type") == "command"}
        self.assertEqual(list(command_services.keys()), ["ignored"])

    def test_inspect_datasource_configs_surfaces_dotenv_database_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "DATABASE_URL=postgres://app:secret@localhost:5432/app\n"
                "ANOTHER=ignored\n"
            )

            datasources = ccstack.inspect_datasource_configs(root)

        self.assertEqual(len(datasources), 1)
        entry = datasources[0]
        self.assertEqual(entry["key"], "DATABASE_URL")
        self.assertEqual(entry["url"], "postgres://app:secret@localhost:5432/app")
        self.assertEqual(entry["path"], ".env")

    def test_inspect_datasource_configs_surfaces_jdbc_in_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.local").write_text(
                "SPRING_DATASOURCE_URL=jdbc:mysql://localhost:3306/app\n"
            )

            datasources = ccstack.inspect_datasource_configs(root)

        self.assertEqual(len(datasources), 1)
        self.assertEqual(datasources[0]["key"], "SPRING_DATASOURCE_URL")
        self.assertTrue(datasources[0]["url"].startswith("jdbc:mysql:"))

    def test_inspect_datasource_configs_ignores_dotenv_non_database_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "NODE_ENV=production\n"
                "API_KEY=abc123\n"
                "REQUEST_TIMEOUT_MS=5000\n"
            )

            datasources = ccstack.inspect_datasource_configs(root)

        self.assertEqual(datasources, [])

    def test_inspect_datasource_configs_skips_dotenv_example_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.example").write_text(
                "DATABASE_URL=postgres://example/example\n"
            )

            datasources = ccstack.inspect_datasource_configs(root)

        self.assertEqual(datasources, [])

    def test_inspect_datasource_configs_preserves_properties_file_scanning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = root / "src" / "main" / "resources"
            resources.mkdir(parents=True)
            (resources / "application.yml").write_text(
                "spring:\n  datasource:\n    url: jdbc:mysql://localhost/db\n"
            )

            datasources = ccstack.inspect_datasource_configs(root)

        self.assertEqual(len(datasources), 1)
        self.assertEqual(datasources[0]["url"], "jdbc:mysql://localhost/db")
    def test_write_managed_compose_emits_per_workspace_network_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            compose_path = Path(tmp) / "docker-compose.devstack.yml"
            requirements = [
                {
                    "compose_service": "shop_db",
                    "image": "mysql:8",
                    "port": 13306,
                    "container_port": 3306,
                    "environment": {"MYSQL_ROOT_PASSWORD": "root"},
                },
                {
                    "compose_service": "shop_cache",
                    "image": "redis:7",
                    "port": 16379,
                    "container_port": 6379,
                },
            ]

            ccstack.write_managed_compose(compose_path, requirements, "shop")
            text = compose_path.read_text()

        # Canonical compose network name after the rename — was
        # ``ccstack_shop_net`` pre-rename. The writer emits the new prefix
        # only; the helper ``managed_compose_network_name`` is the single
        # source of truth.
        expected_network = "devstack_shop_net"
        self.assertIn("networks:\n", text)
        self.assertIn(f"  {expected_network}:\n", text)
        self.assertIn(f"    name: {expected_network}\n", text)
        # Both service blocks must explicitly join the per-workspace network.
        self.assertEqual(text.count(f"      - {expected_network}\n"), 2)
        for service in ("shop_db", "shop_cache"):
            self.assertIn(f"  {service}:\n", text)

    def test_write_managed_compose_uses_distinct_networks_per_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            requirements = [
                {
                    "compose_service": "db",
                    "image": "postgres:16",
                },
            ]
            shop_path = Path(tmp) / "shop.yml"
            books_path = Path(tmp) / "books.yml"

            ccstack.write_managed_compose(shop_path, requirements, "shop")
            ccstack.write_managed_compose(books_path, requirements, "books")

            shop_text = shop_path.read_text()
            books_text = books_path.read_text()

        # Canonical compose network prefix after the rename.
        self.assertIn("devstack_shop_net", shop_text)
        self.assertNotIn("devstack_books_net", shop_text)
        self.assertIn("devstack_books_net", books_text)
        self.assertNotIn("devstack_shop_net", books_text)

    def test_write_managed_compose_preserves_existing_single_workspace_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            compose_path = Path(tmp) / "docker-compose.devstack.yml"
            requirements = [
                {
                    "compose_service": "shop_db",
                    "image": "mysql:8",
                    "port": 13306,
                    "container_port": 3306,
                    "volumes": ["shop_db_data:/var/lib/mysql"],
                },
            ]

            ccstack.write_managed_compose(compose_path, requirements, "shop")
            text = compose_path.read_text()

        # The generated header and services: section must still come first,
        # the volumes: section must still appear, and the new networks: block
        # must come strictly after the volumes: block.
        self.assertTrue(text.startswith("# Generated by devstack."))
        self.assertEqual(text.count("services:\n"), 1)
        services_idx = text.index("services:\n")
        volumes_idx = text.index("volumes:\n")
        networks_idx = text.index("networks:\n")
        self.assertLess(services_idx, volumes_idx)
        self.assertLess(volumes_idx, networks_idx)
        # The named volume from the requirement must still be declared.
        self.assertIn("  shop_db_data: {}\n", text)
        self.assertTrue(text.endswith("\n"))

    def test_registry_register_rejects_duplicate_manifest_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            shop_root = Path(tmp) / "shop"
            books_root = Path(tmp) / "books"
            shop_root.mkdir()
            books_root.mkdir()
            shared_manifest = Path(tmp) / "shared-manifest.json"
            shared_manifest.write_text("{}\n")

            registry = ccstack.WorkspaceRegistry()
            registry.register("shop", shop_root, shared_manifest)

            with self.assertRaises(ccstack.CcstackError) as error:
                registry.register("books", books_root, shared_manifest)

        message = str(error.exception)
        self.assertIn("shop", message)
        self.assertIn(str(shared_manifest.expanduser().resolve()), message)
        # Failed registration must NOT mutate the registry state.
        self.assertNotIn("books", registry.workspaces)
        self.assertNotIn("books", registry.manifests)
        self.assertEqual(
            registry.manifests["shop"],
            str(shared_manifest.expanduser().resolve()),
        )

    def test_registry_register_is_idempotent_for_same_workspace_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            shop_root = Path(tmp) / "shop"
            shop_root.mkdir()
            manifest = Path(tmp) / "shop-manifest.json"
            manifest.write_text("{}\n")

            registry = ccstack.WorkspaceRegistry()
            registry.register("shop", shop_root, manifest)
            # Re-registering the same workspace with the same manifest must not
            # raise — otherwise standard prepare/save flows would break.
            registry.register("shop", shop_root, manifest)

        self.assertEqual(
            registry.manifests["shop"],
            str(manifest.expanduser().resolve()),
        )
        self.assertEqual(
            registry.workspaces["shop"],
            str(shop_root.expanduser().resolve()),
        )

    # ------------------------------------------------------------------
    # Generalized seed pipeline — relaxed generic_local_seed_statement
    # (ccstack:2887), multi-row defaults, derived REST probes, DDL/DML
    # fingerprint + named-volume recreation opt-in.
    # ------------------------------------------------------------------

    def test_generic_local_seed_statement_emits_seed_for_wide_table_without_state_column(self):
        # 12 columns, no status/deleted/hidden/active flag — previously suppressed
        # by the (now-removed) 8-column cap and the mandatory state-column guard.
        # The relaxed implementation must still emit a deterministic INSERT.
        table = {
            "name": "report_metric",
            "columns": [
                {"name": "metric_id", "type": "long", "id": True},
                {"name": "metric_title", "type": "varchar"},
                {"name": "metric_subtitle", "type": "varchar"},
                {"name": "metric_unit", "type": "varchar"},
                {"name": "metric_summary", "type": "text"},
                {"name": "metric_value", "type": "decimal"},
                {"name": "metric_count", "type": "integer"},
                {"name": "metric_ratio", "type": "double"},
                {"name": "metric_owner", "type": "varchar"},
                {"name": "metric_source", "type": "varchar"},
                {"name": "metric_created_at", "type": "timestamp"},
                {"name": "metric_updated_at", "type": "timestamp"},
            ],
        }

        statement = ccstack.generic_local_seed_statement(table, "postgresql")

        self.assertTrue(statement)
        self.assertIn('INSERT INTO "report_metric"', statement)
        self.assertIn("ON CONFLICT DO NOTHING", statement)

    def test_generic_local_seed_statement_emits_three_rows_by_default(self):
        # The 3-5 default seed-row range is exercised so list/pagination smoke
        # probes have data to return. Default count = 3.
        table = {
            "name": "article",
            "columns": [
                {"name": "article_id", "type": "long", "id": True},
                {"name": "title", "type": "varchar"},
                {"name": "body", "type": "text"},
                {"name": "display_yn", "type": "varchar"},
            ],
        }

        statement = ccstack.generic_local_seed_statement(table, "postgresql")

        # Count rows by looking at distinct primary key values 1, 2, 3.
        # The statement is a single multi-row INSERT for non-mssql engines.
        self.assertIn("(1, ", statement)
        self.assertIn("(2, ", statement)
        self.assertIn("(3, ", statement)
        self.assertNotIn("(4, ", statement)
        # Textual columns on rows 2+ carry a row suffix so display values
        # are distinguishable. (Row 1 uses the unsuffixed base value.)
        self.assertIn("devstack local article", statement)
        self.assertIn("devstack local article 2", statement)
        self.assertIn("devstack local article 3", statement)

    def test_generic_local_seed_statement_clamps_row_count_to_three_to_five(self):
        # row_count is clamped to the 3-5 contract: requests <3 floor to 3,
        # requests >5 ceiling to 5.
        table = {
            "name": "article",
            "columns": [
                {"name": "article_id", "type": "long", "id": True},
                {"name": "title", "type": "varchar"},
            ],
        }

        below = ccstack.generic_local_seed_statement(table, "postgresql", row_count=1)
        above = ccstack.generic_local_seed_statement(table, "postgresql", row_count=99)

        # `below` (clamped up to 3): 3 distinct PK rows.
        self.assertIn("(1, ", below)
        self.assertIn("(2, ", below)
        self.assertIn("(3, ", below)
        self.assertNotIn("(4, ", below)

        # `above` (clamped down to 5): 5 distinct PK rows.
        self.assertIn("(1, ", above)
        self.assertIn("(5, ", above)
        self.assertNotIn("(6, ", above)

    def test_generic_local_seed_statement_skips_id_only_table(self):
        # Tables with only the primary key (no non-key seedable columns) are
        # still suppressed — these are typically pure id registries / lookup
        # stubs where a synthetic seed row carries no smoke-test value.
        table = {
            "name": "item",
            "columns": [
                {"name": "id", "type": "long", "id": True},
            ],
        }

        statement = ccstack.generic_local_seed_statement(table, "postgresql")

        self.assertEqual(statement, "")

    def test_discover_controller_routes_extracts_get_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = Path(tmp) / "users-api"
            source_dir = module / "src" / "main" / "kotlin" / "com" / "example"
            source_dir.mkdir(parents=True)
            (source_dir / "UserController.kt").write_text(
                '''
package com.example

import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RestController

@RestController
@RequestMapping("/users")
class UserController {
    @GetMapping
    fun list() = listOf("alice")

    @GetMapping("/{id}")
    fun get(id: Long) = "alice"

    @GetMapping("/active")
    fun active() = listOf("alice")
}
'''.strip()
                + "\n"
            )

            routes = ccstack.discover_controller_routes(module)

        paths = {entry["path"] for entry in routes}
        methods = {entry["method"] for entry in routes}
        self.assertIn("/users", paths)
        self.assertIn("/users/active", paths)
        self.assertIn("/users/{id}", paths)
        self.assertEqual(methods, {"GET"})

    def test_derive_rest_probe_urls_filters_parametric_paths(self):
        service = {
            "port": 18099,
            "controller_routes": [
                {"method": "GET", "path": "/users"},
                {"method": "GET", "path": "/users/{id}"},
                {"method": "POST", "path": "/users"},
                {"method": "GET", "path": "/users/active"},
            ],
        }

        probes = ccstack.derive_rest_probe_urls(service)
        urls = [url for _, url in probes]

        self.assertIn("http://localhost:18099/users", urls)
        self.assertIn("http://localhost:18099/users/active", urls)
        self.assertNotIn("http://localhost:18099/users/{id}", urls)
        # POST is filtered out — only GET/ANY methods get probed.
        self.assertEqual(len(urls), 2)

    def test_verify_service_api_dummy_data_uses_derived_rest_routes(self):
        # Newly-introduced service with auto-derived controller_routes can be
        # verified without adding a service-name-specific branch.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = ccstack.Workspace(
                Path(tmp),
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        "apps.example.users": {
                            "port": 18099,
                            "controller_routes": [
                                {"method": "GET", "path": "/users"},
                            ],
                        }
                    },
                },
            )

            class Response:
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return False

                def read(self):
                    return json.dumps([{"userId": 1, "name": "ccstack local"}]).encode("utf-8")

            with mock.patch.object(ccstack.urllib.request, "urlopen", return_value=Response()):
                verified, detail = workspace.verify_service_api_dummy_data("apps.example.users")

        self.assertTrue(verified, detail)
        self.assertIn("/users", detail)

    def test_verify_service_api_dummy_data_preserves_samplegoods_fallback(self):
        # Regression — with the shipped shopping overrides loaded, services
        # whose name matches the "samplegoods" keyword inherit the historical REST
        # probes (label "REST samplegoods ...") from the overrides JSON.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deploy_shopping_overrides(root)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {"apps.fixity-samplegoods.samplegoods": {"port": 8099}},
                },
            )

            class Response:
                status = 200

                def __init__(self, body):
                    self.body = body

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return False

                def read(self):
                    return json.dumps(self.body).encode("utf-8")

            def fake_urlopen(request, timeout):
                url = request if isinstance(request, str) else request.full_url
                return Response({"data": [{"goodsSeq": 1}]})

            with mock.patch.object(ccstack.urllib.request, "urlopen", side_effect=fake_urlopen):
                verified, detail = workspace.verify_service_api_dummy_data("apps.fixity-samplegoods.samplegoods")

        self.assertTrue(verified, detail)
        self.assertIn("REST samplegoods", detail)

    def test_verify_service_api_dummy_data_no_overrides_uses_controller_route_probes_only(self):
        """Without any overrides loaded, verify_service_api_dummy_data must
        produce REST probes ONLY from the service's persisted
        controller_routes. The service-name keyword path is silent (no
        samplegoods literal branch). Proves the engine has no baked-in shopping
        domain knowledge when no overrides file is present."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = ccstack.Workspace(
                Path(tmp),
                {
                    "workspace": "test-shop",
                    "profiles": {},
                    "services": {
                        # Service name contains "samplegoods" — but with NO overrides
                        # loaded, the engine must NOT inject samplegoods probes.
                        "apps.fixity-samplegoods.samplegoods": {
                            "port": 8099,
                            "controller_routes": [
                                {"method": "GET", "path": "/health/ping"},
                            ],
                        }
                    },
                },
            )

            class Response:
                status = 200

                def __init__(self, body):
                    self.body = body

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return False

                def read(self):
                    return json.dumps(self.body).encode("utf-8")

            requested_urls = []

            def fake_urlopen(request, timeout):
                url = request if isinstance(request, str) else request.full_url
                requested_urls.append(url)
                return Response([{"id": 1}])

            with mock.patch.object(ccstack.urllib.request, "urlopen", side_effect=fake_urlopen):
                verified, detail = workspace.verify_service_api_dummy_data("apps.fixity-samplegoods.samplegoods")

        self.assertTrue(verified, detail)
        self.assertIn("/health/ping", detail)
        # Verify NO samplegoods literal URL was probed — the no-overrides engine
        # never injects keyword-triggered probes.
        for url in requested_urls:
            self.assertNotIn("/sample-goods", url, f"unexpected samplegoods literal URL probed: {url}")
            self.assertNotIn("/sample-main", url, f"unexpected samplegoods literal URL probed: {url}")

    def test_compute_seed_fingerprint_changes_when_schema_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            schema_path = tmp_path / "schema.sql"
            dml_path = tmp_path / "dml.sql"

            schema_path.write_text("CREATE TABLE foo (id INT);\n")
            dml_path.write_text("INSERT INTO foo VALUES (1);\n")
            first = ccstack.compute_seed_fingerprint({"postgresql": schema_path}, {"postgresql": dml_path})

            # Same content yields same fingerprint (deterministic).
            again = ccstack.compute_seed_fingerprint({"postgresql": schema_path}, {"postgresql": dml_path})
            self.assertEqual(first, again)

            # Schema edit changes the fingerprint.
            schema_path.write_text("CREATE TABLE foo (id INT, name VARCHAR(64));\n")
            changed = ccstack.compute_seed_fingerprint({"postgresql": schema_path}, {"postgresql": dml_path})

        self.assertNotEqual(first, changed)
        self.assertEqual(len(first), 16)
        self.assertEqual(len(changed), 16)

    def test_workspace_apply_records_seed_fingerprint_and_detects_schema_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            module = root / "contents-api"
            resources = module / "src" / "main" / "resources"
            source_dir = module / "src" / "main" / "kotlin" / "com" / "example"
            resources.mkdir(parents=True)
            source_dir.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':contents-api'\n")
            (module / "build.gradle").write_text("plugins { id 'org.springframework.boot' version '3.2.0' }\n")
            (resources / "application-local.yml").write_text(
                """
server:
  port: 18081
spring:
  datasource:
    url: jdbc:postgresql://localhost:5432/contentdb
    username: remote
    password: secret
""".strip()
                + "\n"
            )
            (source_dir / "ArticleRepository.kt").write_text(
                '''
package com.example

import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate

class ArticleRepository(private val jdbc: NamedParameterJdbcTemplate) {
    fun findArticles() = jdbc.query(
        """
        SELECT article_id, title, body, deleted, hidden, created_at
          FROM article
         WHERE deleted = false
           AND hidden = false
         ORDER BY created_at DESC
        """.trimIndent(),
    ) { rs, _ -> rs.getString("title") }
}
'''.strip()
                + "\n"
            )
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                draft_result = ccstack.execute_ui_action(
                    workspace,
                    {"action": ["workspace_analyze"], "workspace_root": [str(root)]},
                )
                apply_result_first = ccstack.execute_ui_action(
                    workspace,
                    {"action": ["workspace_apply"], "draft_id": [draft_result["draft_id"]]},
                )
                fingerprint_path = ccstack.seed_fingerprint_path("shopping")
                fingerprint_first = fingerprint_path.read_text().strip() if fingerprint_path.exists() else ""

                # Edit the entity so the next analyze sees a different table shape,
                # then re-analyze + re-apply.
                (source_dir / "ArticleRepository.kt").write_text(
                    '''
package com.example

import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate

class ArticleRepository(private val jdbc: NamedParameterJdbcTemplate) {
    fun findArticles() = jdbc.query(
        """
        SELECT article_id, title, body, deleted, hidden, created_at, updated_at, author_id
          FROM article
         WHERE deleted = false
           AND hidden = false
         ORDER BY created_at DESC
        """.trimIndent(),
    ) { rs, _ -> rs.getString("title") }
}
'''.strip()
                    + "\n"
                )
                draft_result_second = ccstack.execute_ui_action(
                    workspace,
                    {"action": ["workspace_analyze"], "workspace_root": [str(root)]},
                )
                apply_result_second = ccstack.execute_ui_action(
                    workspace,
                    {"action": ["workspace_apply"], "draft_id": [draft_result_second["draft_id"]]},
                )
                fingerprint_second = fingerprint_path.read_text().strip() if fingerprint_path.exists() else ""

        self.assertTrue(apply_result_first["ok"])
        self.assertTrue(apply_result_second["ok"])
        # First apply has no previous fingerprint to compare against — not flagged.
        self.assertFalse(apply_result_first.get("seed_changed", False))
        self.assertTrue(fingerprint_first)
        self.assertEqual(len(fingerprint_first), 16)
        # Schema edit drives a new fingerprint AND the change flag.
        self.assertNotEqual(fingerprint_first, fingerprint_second)
        self.assertTrue(apply_result_second.get("seed_changed", False))
        self.assertIn("seed change detected", apply_result_second["output"])

    def test_workspace_apply_recreate_volumes_is_opt_in(self):
        # The destructive named-volume recreation must NOT fire unless the
        # caller explicitly opts in. Default workspace_apply is warn-only.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            module = root / "contents-api"
            resources = module / "src" / "main" / "resources"
            entity_dir = module / "src" / "main" / "java" / "com" / "example"
            resources.mkdir(parents=True)
            entity_dir.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':contents-api'\n")
            (module / "build.gradle").write_text("plugins { id 'org.springframework.boot' version '3.2.0' }\n")
            (resources / "application.yml").write_text(
                """
server:
  port: 18081
spring:
  datasource:
    url: jdbc:postgresql://localhost:5432/shop
    username: app
    password: app
""".strip()
                + "\n"
            )
            (entity_dir / "Item.java").write_text(
                """
package com.example;

import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

@Entity
@Table(name = "item")
public class Item {
    @Id
    private Long id;
    private String title;
    private Integer displayYn;
}
""".strip()
                + "\n"
            )
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )

            recorded_calls = []

            def fake_run(cmd, *args, **kwargs):
                recorded_calls.append(list(cmd))

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"), \
                    mock.patch.object(ccstack, "run", side_effect=fake_run):
                draft_result = ccstack.execute_ui_action(
                    workspace,
                    {"action": ["workspace_analyze"], "workspace_root": [str(root)]},
                )
                # Default apply (no opt-in) MUST NOT touch docker volumes.
                default_apply = ccstack.execute_ui_action(
                    workspace,
                    {"action": ["workspace_apply"], "draft_id": [draft_result["draft_id"]]},
                )
                default_calls = list(recorded_calls)
                recorded_calls.clear()

                # Explicit opt-in via recreate_volumes=1 — destructive ops allowed.
                draft_result_again = ccstack.execute_ui_action(
                    workspace,
                    {"action": ["workspace_analyze"], "workspace_root": [str(root)]},
                )
                optin_apply = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_apply"],
                        "draft_id": [draft_result_again["draft_id"]],
                        "recreate_volumes": ["1"],
                    },
                )
                optin_calls = list(recorded_calls)

        # Default path: no docker volume rm invocation.
        self.assertFalse(
            any("volume" in call and "rm" in call for call in default_calls if "docker" in call),
            default_calls,
        )
        self.assertFalse(default_apply.get("volume_recreation", {}).get("performed", False))

        # Opt-in path: at least one `docker volume rm` invocation.
        rm_calls = [
            call for call in optin_calls
            if len(call) >= 3 and call[0] == "docker" and call[1] == "volume" and call[2] == "rm"
        ]
        self.assertTrue(rm_calls, optin_calls)
        self.assertTrue(optin_apply.get("volume_recreation", {}).get("performed", False))

    def test_workspace_recreate_volumes_action_runs_docker_volume_rm(self):
        # The dedicated workspace_recreate_volumes UI action always triggers
        # the destructive path; existence of a separate action makes the
        # opt-in boundary explicit and observable in the UI surface.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            module = root / "contents-api"
            resources = module / "src" / "main" / "resources"
            entity_dir = module / "src" / "main" / "java" / "com" / "example"
            resources.mkdir(parents=True)
            entity_dir.mkdir(parents=True)
            (root / "gradlew").write_text("#!/bin/sh\n")
            (root / "settings.gradle").write_text("include ':contents-api'\n")
            (module / "build.gradle").write_text("plugins { id 'org.springframework.boot' version '3.2.0' }\n")
            (resources / "application.yml").write_text(
                """
server:
  port: 18081
spring:
  datasource:
    url: jdbc:postgresql://localhost:5432/shop
    username: app
    password: app
""".strip()
                + "\n"
            )
            (entity_dir / "Item.java").write_text(
                """
package com.example;

import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

@Entity
@Table(name = "item")
public class Item {
    @Id
    private Long id;
    private String title;
}
""".strip()
                + "\n"
            )
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )

            recorded_calls = []

            def fake_run(cmd, *args, **kwargs):
                recorded_calls.append(list(cmd))

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"), \
                    mock.patch.object(ccstack, "run", side_effect=fake_run):
                draft_result = ccstack.execute_ui_action(
                    workspace,
                    {"action": ["workspace_analyze"], "workspace_root": [str(root)]},
                )
                result = ccstack.execute_ui_action(
                    workspace,
                    {"action": ["workspace_recreate_volumes"], "draft_id": [draft_result["draft_id"]]},
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "recreated")
        rm_calls = [
            call for call in recorded_calls
            if len(call) >= 3 and call[0] == "docker" and call[1] == "volume" and call[2] == "rm"
        ]
        self.assertTrue(rm_calls, recorded_calls)

    # ------------------------------------------------------------------
    # Seed-drift DEGRADED readiness state
    #
    # When `apply_workspace_draft` detects `seed_changed=True` and the operator
    # declines the destructive named-volume recreation, the drift fact must
    # be persisted to `STATE_HOME/{name}/seed-drift.json` so later, independent
    # processes can render the workspace as DEGRADED until drift is cleared.
    # Boot is never blocked; the state is observability-only. A follow-up
    # status-counter task aggregates `code` / `level` from the warnings array,
    # so the schema below is the stable contract.
    # ------------------------------------------------------------------

    def _build_shopping_workspace_root(self, root: Path) -> None:
        """Helper: build a minimal shopping-shaped workspace root that drives
        the same managed-postgres infra + DDL/DML seed pipeline used by the
        existing seed-fingerprint tests. Reused across the seed-drift tests."""
        module = root / "contents-api"
        resources = module / "src" / "main" / "resources"
        source_dir = module / "src" / "main" / "kotlin" / "com" / "example"
        resources.mkdir(parents=True)
        source_dir.mkdir(parents=True)
        (root / "gradlew").write_text("#!/bin/sh\n")
        (root / "settings.gradle").write_text("include ':contents-api'\n")
        (module / "build.gradle").write_text("plugins { id 'org.springframework.boot' version '3.2.0' }\n")
        (resources / "application-local.yml").write_text(
            """
server:
  port: 18081
spring:
  datasource:
    url: jdbc:postgresql://localhost:5432/contentdb
    username: remote
    password: secret
""".strip()
            + "\n"
        )
        (source_dir / "ArticleRepository.kt").write_text(
            '''
package com.example

import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate

class ArticleRepository(private val jdbc: NamedParameterJdbcTemplate) {
    fun findArticles() = jdbc.query(
        """
        SELECT article_id, title, body, deleted, hidden, created_at
          FROM article
         WHERE deleted = false
           AND hidden = false
         ORDER BY created_at DESC
        """.trimIndent(),
    ) { rs, _ -> rs.getString("title") }
}
'''.strip()
            + "\n"
        )

    def _drive_drift_apply(self, root: Path, workspace: "ccstack.Workspace") -> tuple:
        """Helper: perform first apply (baseline fingerprint) + schema edit +
        second apply (drift detected). Returns (first_apply_result,
        second_apply_result, schema_path).

        Caller must already be inside the REGISTRY_PATH / STATE_HOME mock
        context so the workspace state lands in the tempdir.
        """
        draft_first = ccstack.execute_ui_action(
            workspace,
            {"action": ["workspace_analyze"], "workspace_root": [str(root)]},
        )
        first_apply = ccstack.execute_ui_action(
            workspace,
            {"action": ["workspace_apply"], "draft_id": [draft_first["draft_id"]]},
        )
        # Edit the entity so the next analyze sees a different table shape.
        source_dir = root / "contents-api" / "src" / "main" / "kotlin" / "com" / "example"
        (source_dir / "ArticleRepository.kt").write_text(
            '''
package com.example

import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate

class ArticleRepository(private val jdbc: NamedParameterJdbcTemplate) {
    fun findArticles() = jdbc.query(
        """
        SELECT article_id, title, body, deleted, hidden, created_at, updated_at, author_id
          FROM article
         WHERE deleted = false
           AND hidden = false
         ORDER BY created_at DESC
        """.trimIndent(),
    ) { rs, _ -> rs.getString("title") }
}
'''.strip()
            + "\n"
        )
        draft_second = ccstack.execute_ui_action(
            workspace,
            {"action": ["workspace_analyze"], "workspace_root": [str(root)]},
        )
        second_apply = ccstack.execute_ui_action(
            workspace,
            {"action": ["workspace_apply"], "draft_id": [draft_second["draft_id"]]},
        )
        return first_apply, second_apply

    def test_apply_persists_seed_drift_when_recreate_declined(self):
        # Drift fact must survive the apply: a later, independent process
        # (status command, UI meta endpoint, follow-up status-counter task)
        # reads STATE_HOME/{name}/seed-drift.json instead of recomputing.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            self._build_shopping_workspace_root(root)
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )
            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                first_apply, second_apply = self._drive_drift_apply(root, workspace)
                drift_path = ccstack.seed_drift_state_path("shopping")
                self.assertTrue(drift_path.exists(), drift_path)
                payload = json.loads(drift_path.read_text())

        # First apply has no previous fingerprint to compare against — NOT degraded.
        self.assertFalse(first_apply.get("seed_changed", False))
        self.assertEqual(first_apply.get("warnings", []), [])
        # Second apply detects drift; warn-only mode persists the state file.
        self.assertTrue(second_apply.get("seed_changed", False))
        self.assertEqual(len(second_apply.get("warnings", [])), 1)
        warning = second_apply["warnings"][0]
        self.assertEqual(warning["code"], "seed-drift")
        self.assertEqual(warning["level"], "degraded")
        self.assertEqual(warning["workspace"], "shopping")
        # Remediation string is stable and exact — UI / counter task may
        # compare verbatim.
        self.assertEqual(
            warning["remediation"],
            "devstack workspace apply shopping --recreate-volumes",
        )
        # Affected services include the managed postgres compose service.
        self.assertTrue(
            any("postgres" in name or "db" in name for name in warning["affected_services"]),
            warning["affected_services"],
        )
        # Persisted record carries the same canonical fields.
        self.assertEqual(payload["workspace"], "shopping")
        self.assertEqual(
            payload["remediation"],
            "devstack workspace apply shopping --recreate-volumes",
        )
        self.assertTrue(payload["fingerprint_previous"])
        self.assertTrue(payload["fingerprint_current"])
        self.assertNotEqual(payload["fingerprint_previous"], payload["fingerprint_current"])
        self.assertEqual(len(payload["fingerprint_previous"]), 16)
        self.assertEqual(len(payload["fingerprint_current"]), 16)
        self.assertIn("recorded_at", payload)
        self.assertEqual(
            sorted(warning["affected_services"]),
            sorted(payload["affected_services"]),
        )

    def test_apply_clears_seed_drift_when_recreate_volumes_succeeds(self):
        # Opt-in destructive recreate path must remove the persisted drift
        # record AND emit an empty warnings array, so a subsequent status
        # render reports the workspace as fully green again.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            self._build_shopping_workspace_root(root)
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )

            def fake_run(cmd, *args, **kwargs):
                # Both `docker compose down` and `docker volume rm` must
                # succeed silently in this test — no errors recorded.
                return None

            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"), \
                    mock.patch.object(ccstack, "run", side_effect=fake_run):
                _, second_apply = self._drive_drift_apply(root, workspace)
                drift_path = ccstack.seed_drift_state_path("shopping")
                self.assertTrue(
                    drift_path.exists(),
                    "drift state must be present before recreate",
                )
                # Now run the dedicated workspace_recreate_volumes action.
                draft_after = ccstack.execute_ui_action(
                    workspace,
                    {"action": ["workspace_analyze"], "workspace_root": [str(root)]},
                )
                recreate_result = ccstack.execute_ui_action(
                    workspace,
                    {
                        "action": ["workspace_recreate_volumes"],
                        "draft_id": [draft_after["draft_id"]],
                    },
                )
                drift_after = drift_path.exists()

        self.assertTrue(second_apply.get("seed_changed", False))
        self.assertEqual(len(second_apply.get("warnings", [])), 1)
        self.assertTrue(recreate_result["ok"])
        self.assertEqual(recreate_result["status"], "recreated")
        # Drift cleared by the destructive recreate.
        self.assertFalse(drift_after, "seed-drift.json must be removed after recreate")
        self.assertEqual(recreate_result.get("warnings", []), [])

    def test_apply_clears_seed_drift_when_fingerprint_matches_again(self):
        # If a third apply produces a fingerprint that matches the persisted
        # one (drift resolved upstream, e.g. operator reverted the schema
        # change), seed_changed becomes False on that apply and the
        # warn-only path must clear the persisted drift file. No destructive
        # recreate needed.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            self._build_shopping_workspace_root(root)
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )
            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                _, second_apply = self._drive_drift_apply(root, workspace)
                drift_path = ccstack.seed_drift_state_path("shopping")
                self.assertTrue(drift_path.exists())
                # Third apply with the SAME edited schema — fingerprint of
                # the current apply now matches the persisted fingerprint
                # (which was updated to the current value by the second
                # apply's `write_seed_fingerprint`), so seed_changed=False.
                draft_third = ccstack.execute_ui_action(
                    workspace,
                    {"action": ["workspace_analyze"], "workspace_root": [str(root)]},
                )
                third_apply = ccstack.execute_ui_action(
                    workspace,
                    {"action": ["workspace_apply"], "draft_id": [draft_third["draft_id"]]},
                )
                drift_after = drift_path.exists()

        self.assertTrue(second_apply.get("seed_changed", False))
        self.assertFalse(third_apply.get("seed_changed", False))
        self.assertEqual(third_apply.get("warnings", []), [])
        self.assertFalse(drift_after, "seed-drift.json must be removed on fingerprint match")

    def test_workspace_meta_emits_seed_drift_warning_signal(self):
        # Machine-readable surface: workspace_meta carries a `warnings` array
        # so the follow-up status-counter task aggregates by `code` / `level`
        # without parsing free-text status output.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            self._build_shopping_workspace_root(root)
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )
            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                _, second_apply = self._drive_drift_apply(root, workspace)
                # Reload workspace from the now-applied managed config — that
                # is what a fresh `status` invocation would see.
                manifest_path = ccstack.generated_manifest_path("shopping")
                applied_config = ccstack.load_json(manifest_path)
                bound_workspace = ccstack.Workspace(root, applied_config)
                meta = ccstack.workspace_meta(bound_workspace)

        self.assertTrue(second_apply.get("seed_changed", False))
        self.assertIn("warnings", meta)
        self.assertEqual(len(meta["warnings"]), 1)
        entry = meta["warnings"][0]
        self.assertEqual(entry["code"], "seed-drift")
        self.assertEqual(entry["level"], "degraded")
        self.assertEqual(entry["workspace"], "shopping")
        self.assertEqual(
            entry["remediation"],
            "devstack workspace apply shopping --recreate-volumes",
        )
        self.assertTrue(entry["affected_services"])
        # Schema stability — these keys ARE the contract for the follow-up
        # status-counter aggregator; do not rename without coordinating.
        for key in (
            "code",
            "level",
            "workspace",
            "affected_services",
            "named_volumes",
            "remediation",
            "fingerprint_previous",
            "fingerprint_current",
        ):
            self.assertIn(key, entry)

    def test_print_status_marks_affected_services_degraded(self):
        # `ccstack status` output must visibly mark the affected DB services
        # as DEGRADED with the exact remediation command, AND emit a single
        # workspace-level state line so the user sees the workspace is not
        # fully green. Boot is not blocked — the per-service rows above the
        # annotation continue to render the live runtime state unchanged.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "shopping"
            self._build_shopping_workspace_root(root)
            workspace = ccstack.Workspace(
                Path(tmp),
                ccstack.setup_workspace_config(Path(tmp)),
            )
            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                _, second_apply = self._drive_drift_apply(root, workspace)
                manifest_path = ccstack.generated_manifest_path("shopping")
                applied_config = ccstack.load_json(manifest_path)
                bound_workspace = ccstack.Workspace(root, applied_config)
                drift_state = ccstack.read_seed_drift_state("shopping")
                affected = list(drift_state.get("affected_services", []) or [])
                self.assertTrue(affected, "test fixture must produce at least one affected DB service")

                # Capture print_status output for the affected services only;
                # the assertion target is the DEGRADED annotation, not the
                # upstream runtime state of those services.
                import io
                buffer = io.StringIO()
                with mock.patch.object(ccstack.sys, "stdout", buffer):
                    bound_workspace.print_status(affected)
                output = buffer.getvalue()

        self.assertTrue(second_apply.get("seed_changed", False))
        # Every affected service gets a DEGRADED annotation row.
        for name in affected:
            self.assertIn(f"{name:28} DEGRADED", output, output)
        self.assertIn("reason=seed-drift", output)
        # Exact remediation command must be surfaced verbatim.
        self.assertIn(
            "devstack workspace apply shopping --recreate-volumes",
            output,
        )
        # Single workspace-level state line, machine-friendly prefix.
        self.assertIn("WORKSPACE STATE: degraded - seed drift", output)


    # ------------------------------------------------------------------
    # Non-JVM command-service datasource handshake regression tests
    # ------------------------------------------------------------------

    def test_command_service_datasource_env_detects_database_url(self):
        service = {
            "type": "command",
            "command": "node server.js",
            "env": {
                "DATABASE_URL": "postgres://app:secret@localhost:5432/app",
                "PORT": "3000",
                "NODE_ENV": "development",
            },
        }
        entries = ccstack.command_service_datasource_env(service)

        self.assertEqual(
            entries,
            [("DATABASE_URL", "postgres://app:secret@localhost:5432/app")],
        )

    def test_command_service_datasource_env_detects_multiple_shapes(self):
        service = {
            "type": "command",
            "env": {
                "DATABASE_URL": "postgres://app:secret@localhost:5432/app",
                "ANALYTICS_DB_URL": "mysql://reader@127.0.0.1:3306/analytics",
                "SPRING_DATASOURCE_URL": "jdbc:postgresql://localhost:5432/legacy",
                "LOG_LEVEL": "debug",
            },
        }

        entries = ccstack.command_service_datasource_env(service)
        keys = sorted(key for key, _ in entries)

        self.assertEqual(
            keys,
            ["ANALYTICS_DB_URL", "DATABASE_URL", "SPRING_DATASOURCE_URL"],
        )

    def test_command_service_datasource_env_skips_non_datasource_entries(self):
        service = {
            "type": "command",
            "env": {
                "REDIS_URL_NOT_DETECTED_KEY": "not-a-url",
                "PLAIN": "x",
            },
        }
        self.assertEqual(ccstack.command_service_datasource_env(service), [])

    def test_command_service_datasource_env_empty_when_no_env(self):
        self.assertEqual(ccstack.command_service_datasource_env({"type": "command"}), [])
        self.assertEqual(ccstack.command_service_datasource_env({}), [])

    def test_parse_datasource_endpoint_jdbc_with_explicit_port(self):
        result = ccstack.parse_datasource_endpoint(
            "jdbc:postgresql://localhost:5432/app"
        )
        self.assertEqual(result, ("localhost", 5432))

    def test_parse_datasource_endpoint_jdbc_defaults_port_for_known_engine(self):
        # JDBC postgres without an explicit port falls back to the engine default.
        result = ccstack.parse_datasource_endpoint(
            "jdbc:postgresql://db-host/legacy"
        )
        self.assertEqual(result, ("db-host", 5432))

    def test_parse_datasource_endpoint_non_jdbc_postgres(self):
        result = ccstack.parse_datasource_endpoint(
            "postgres://app:secret@localhost:5432/app"
        )
        self.assertEqual(result, ("localhost", 5432))

    def test_parse_datasource_endpoint_non_jdbc_mysql_default_port(self):
        result = ccstack.parse_datasource_endpoint(
            "mysql://reader@127.0.0.1/analytics"
        )
        self.assertEqual(result, ("127.0.0.1", 3306))

    def test_parse_datasource_endpoint_redis_default_port(self):
        result = ccstack.parse_datasource_endpoint("redis://localhost/0")
        self.assertEqual(result, ("localhost", 6379))

    def test_parse_datasource_endpoint_symbolic_host_returns_none(self):
        # ${DB_HOST}-style placeholders cannot be probed; refuse to return
        # a literal "${" host that would mislead the TCP probe.
        result = ccstack.parse_datasource_endpoint(
            "postgres://app@${DB_HOST}:5432/app"
        )
        self.assertIsNone(result)

    def test_parse_datasource_endpoint_unknown_scheme_returns_none(self):
        self.assertIsNone(
            ccstack.parse_datasource_endpoint("smb://localhost/share")
        )
        self.assertIsNone(ccstack.parse_datasource_endpoint(""))
        self.assertIsNone(ccstack.parse_datasource_endpoint("not-a-url"))

    def test_find_managed_database_service_matches_loopback_aliases(self):
        services = {
            "ccstack.infra.postgres": {
                "type": "compose",
                "engine": "postgresql",
                "port": 5432,
                "container": "ccstack-postgres",
            },
            "ccstack.infra.mysql": {
                "type": "compose",
                "engine": "mysql",
                "port": 3306,
            },
            "apps.api": {
                "type": "command",
                "command": "node server.js",
            },
        }

        match_loopback = ccstack.find_managed_database_service(
            services, "127.0.0.1", 5432
        )
        match_localhost = ccstack.find_managed_database_service(
            services, "localhost", 5432
        )
        match_mysql = ccstack.find_managed_database_service(
            services, "localhost", 3306
        )
        no_match_other_port = ccstack.find_managed_database_service(
            services, "localhost", 9999
        )
        no_match_non_loopback = ccstack.find_managed_database_service(
            services, "remote-db.example.com", 5432
        )

        self.assertIsNotNone(match_loopback)
        # The function returns the matched service-key as stored in ``services``;
        # this fixture uses the legacy ``ccstack.infra.*`` prefix to exercise
        # Shim 5's read-side alias recognition.
        self.assertEqual(match_loopback[0], "ccstack.infra.postgres")
        self.assertEqual(match_localhost[0], "ccstack.infra.postgres")
        self.assertEqual(match_mysql[0], "ccstack.infra.mysql")
        self.assertIsNone(no_match_other_port)
        self.assertIsNone(no_match_non_loopback)

    def test_find_managed_database_service_ignores_non_engine_compose(self):
        services = {
            "infra.redis": {
                "type": "compose",
                # No `engine` key — Redis compose services are not in
                # MANAGED_DATABASES so they must not be matched as
                # ccstack-managed databases.
                "port": 6379,
            },
        }
        self.assertIsNone(
            ccstack.find_managed_database_service(services, "localhost", 6379)
        )

    def test_managed_database_alive_requires_container_running_when_present(self):
        db_service = {
            "type": "compose",
            "engine": "postgresql",
            "port": 5432,
            "container": "ccstack-postgres",
        }
        with mock.patch.object(ccstack, "docker_container_status", return_value="running"), \
                mock.patch.object(ccstack, "port_open", return_value=True):
            self.assertTrue(
                ccstack.managed_database_alive(db_service, "localhost", 5432)
            )
        with mock.patch.object(ccstack, "docker_container_status", return_value="exited"), \
                mock.patch.object(ccstack, "port_open", return_value=True):
            self.assertFalse(
                ccstack.managed_database_alive(db_service, "localhost", 5432)
            )
        with mock.patch.object(ccstack, "docker_container_status", return_value="running"), \
                mock.patch.object(ccstack, "port_open", return_value=False):
            self.assertFalse(
                ccstack.managed_database_alive(db_service, "localhost", 5432)
            )

    def test_managed_database_alive_without_container_uses_tcp_only(self):
        db_service = {"type": "compose", "engine": "mysql", "port": 3306}
        with mock.patch.object(ccstack, "port_open", return_value=True):
            self.assertTrue(
                ccstack.managed_database_alive(db_service, "localhost", 3306)
            )
        with mock.patch.object(ccstack, "port_open", return_value=False):
            self.assertFalse(
                ccstack.managed_database_alive(db_service, "localhost", 3306)
            )

    def test_add_service_status_note_appends_and_dedupes(self):
        service: dict = {}
        ccstack.add_service_status_note(service, "app_binding_unverified", True)
        ccstack.add_service_status_note(service, "app_binding_unverified", True)
        ccstack.add_service_status_note(
            service,
            "datasource_tcp_unreachable",
            "localhost:5432",
            detail="DATABASE_URL",
        )

        notes = service["devstack_status_notes"]
        self.assertEqual(len(notes), 2)
        keys = sorted(n["key"] for n in notes)
        self.assertEqual(
            keys,
            ["app_binding_unverified", "datasource_tcp_unreachable"],
        )
        # All entries carry an ISO timestamp.
        for entry in notes:
            self.assertIn("ts", entry)
            self.assertTrue(entry["ts"].endswith("+00:00") or "T" in entry["ts"])
        # Detail is preserved when provided.
        unreachable = next(
            n for n in notes if n["key"] == "datasource_tcp_unreachable"
        )
        self.assertEqual(unreachable["detail"], "DATABASE_URL")

    def test_wait_for_ready_command_service_with_datasource_env_passes_on_tcp_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "default_timeout": 5,
                    "profiles": {},
                    "services": {
                        "apps.api": {
                            "type": "command",
                            "command": "node server.js",
                            "env": {
                                "DATABASE_URL": "postgres://app:secret@localhost:5432/app",
                            },
                        },
                    },
                },
            )
            workspace.pid_path("apps.api").write_text("12345")

            with mock.patch.object(ccstack, "process_running", return_value=True), \
                    mock.patch.object(ccstack, "port_open", return_value=True):
                # Must return cleanly (no raise) when the promised host:port is open.
                workspace.wait_for_ready(
                    "apps.api",
                    workspace.service("apps.api"),
                )

            notes = workspace.service("apps.api").get("devstack_status_notes") or []
            keys = [n.get("key") for n in notes]
            self.assertIn("app_binding_unverified", keys)
            self.assertEqual(
                next(n for n in notes if n["key"] == "app_binding_unverified")["value"],
                True,
            )

    def test_wait_for_ready_command_service_fails_when_db_tcp_never_opens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "default_timeout": 5,
                    "profiles": {},
                    "services": {
                        "apps.api": {
                            "type": "command",
                            "command": "node server.js",
                            "env": {
                                "DATABASE_URL": "postgres://app@localhost:5432/app",
                            },
                        },
                    },
                },
            )
            workspace.pid_path("apps.api").write_text("12345")

            # Shrink the wall-clock deadline so the loop runs once (proving
            # the handshake fired and recorded the unreachable note) then
            # times out and raises. Without the time patch the test would
            # sleep ~5s waiting for the real deadline.
            now = [0.0, 0.1, 99.0]

            def fake_time():
                if len(now) > 1:
                    return now.pop(0)
                return now[0]

            with mock.patch.object(ccstack, "process_running", return_value=True), \
                    mock.patch.object(ccstack, "port_open", return_value=False), \
                    mock.patch.object(ccstack.time, "time", side_effect=fake_time), \
                    mock.patch.object(ccstack.time, "sleep", return_value=None):
                with self.assertRaisesRegex(
                    ccstack.CcstackError,
                    "datasource handshake did not complete",
                ):
                    workspace.wait_for_ready(
                        "apps.api",
                        workspace.service("apps.api"),
                    )

            notes = workspace.service("apps.api").get("devstack_status_notes") or []
            keys = [n.get("key") for n in notes]
            # Transient unreachability is recorded for the follow-up status-counter.
            self.assertIn("datasource_tcp_unreachable", keys)
            # The unverified note must NOT be added when the handshake never succeeded.
            self.assertNotIn("app_binding_unverified", keys)

    def test_wait_for_ready_command_service_without_datasource_env_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "default_timeout": 0,
                    "profiles": {},
                    "services": {
                        "apps.worker": {
                            "type": "command",
                            "command": "node worker.js",
                            "env": {"LOG_LEVEL": "info"},
                        },
                    },
                },
            )

            # No health, no port, no datasource env keys → wait_for_ready
            # must early-return without raising and without producing any
            # status notes.
            workspace.wait_for_ready(
                "apps.worker", workspace.service("apps.worker")
            )

            self.assertNotIn(
                "devstack_status_notes", workspace.service("apps.worker")
            )

    def test_wait_for_ready_command_service_probes_managed_db_liveness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "default_timeout": 5,
                    "profiles": {},
                    "services": {
                        "apps.api": {
                            "type": "command",
                            "command": "node server.js",
                            "env": {
                                "DATABASE_URL": "postgres://app@localhost:5432/app",
                            },
                        },
                        "ccstack.infra.postgres": {
                            "type": "compose",
                            "engine": "postgresql",
                            "port": 5432,
                            "container": "ccstack-postgres",
                        },
                    },
                },
            )
            workspace.pid_path("apps.api").write_text("12345")

            # TCP is open BUT the managed container reports "exited" → the
            # managed-DB liveness branch must veto readiness and keep
            # looping. The 0s deadline (default_timeout=5 with one
            # iteration sleep) flips the loop into the timeout path so we
            # can observe the failure deterministically.
            with mock.patch.object(ccstack, "process_running", return_value=True), \
                    mock.patch.object(ccstack, "port_open", return_value=True), \
                    mock.patch.object(
                        ccstack, "docker_container_status", return_value="exited"
                    ):
                # Shrink the deadline so the test doesn't sleep for 5s. We
                # patch time.time so the first iteration succeeds-then-fails.
                originals = {"now": [0.0, 0.0, 1.0, 99.0]}

                def fake_time():
                    if len(originals["now"]) > 1:
                        return originals["now"].pop(0)
                    return originals["now"][0]

                with mock.patch.object(ccstack.time, "time", side_effect=fake_time), \
                        mock.patch.object(ccstack.time, "sleep", return_value=None):
                    with self.assertRaises(ccstack.CcstackError):
                        workspace.wait_for_ready(
                            "apps.api",
                            workspace.service("apps.api"),
                        )

            # Shim 6 — writer uses canonical ``devstack_status_notes`` key.
            notes = workspace.service("apps.api").get("devstack_status_notes") or []
            keys = [n.get("key") for n in notes]
            self.assertIn("managed_database_not_alive", keys)
            # The unverified note must NOT be present — the handshake never succeeded.
            self.assertNotIn("app_binding_unverified", keys)

    def test_verify_command_service_datasources_records_unparsed_endpoint_note(self):
        service = {
            "type": "command",
            "env": {
                # Symbolic host is unparseable for TCP probing.
                "DATABASE_URL": "postgres://app@${DB_HOST}:5432/app",
            },
        }
        entries = ccstack.command_service_datasource_env(service)
        result = ccstack.verify_command_service_datasources(
            "apps.api", service, entries, services={}
        )

        # Only unparseable entries → no required probes succeeded, so the
        # handshake reports failure rather than silently approving.
        self.assertFalse(result)
        notes = service.get("devstack_status_notes") or []
        keys = [n.get("key") for n in notes]
        self.assertIn("datasource_endpoint_unparsed", keys)
        self.assertNotIn("app_binding_unverified", keys)

    # ------------------------------------------------------------------
    # Grey-zone status counters
    #
    # The four reliability features (managed datasource probe signals,
    # persisted seed-drift state, mockserver-routed external_http
    # candidates, ccstack_status_notes app_binding_unverified) emit
    # machine-readable signals that the status command must aggregate
    # into named counters: degraded, low_confidence, mock_fallback,
    # unverified_binding. Non-zero counters render prominently; an
    # all-zero workspace stays quiet (only the always-printable
    # parseable line is emitted).
    # ------------------------------------------------------------------

    def _make_grey_zone_workspace(
        self,
        root,
        *,
        services=None,
        managed_environment=None,
    ):
        """Build a bound in-memory Workspace whose config carries the bits
        the grey-zone aggregator looks at. ``workspace_setup`` is omitted
        so the workspace is treated as bound.
        """
        config = {
            "workspace": "grey",
            "profiles": {"default": {"services": list((services or {}).keys()) or ["api"]}},
            "services": services or {
                "api": {
                    "type": "gradle",
                    "task": ":api:bootRun",
                    "port": 18181,
                },
            },
        }
        if managed_environment is not None:
            config["ccstack_managed_environment"] = managed_environment
        return ccstack.Workspace(root, config)

    def _capture_print_status(self, workspace, service_names):
        import io
        buffer = io.StringIO()
        with mock.patch.object(ccstack.sys, "stdout", buffer):
            workspace.print_status(service_names)
        return buffer.getvalue()

    def test_peek_managed_datasource_probe_signals_does_not_drain(self):
        # Contract: the new peek helper returns a copy without clearing
        # the underlying module-level list. Calling consume after peek
        # must still drain the original signals — otherwise other
        # readiness consumers would be silently broken.
        ccstack.consume_managed_datasource_probe_signals()
        ccstack.MANAGED_DATASOURCE_PROBE_SIGNALS.append(
            {"service": "apps.a", "confidence": "low", "ok": False}
        )
        ccstack.MANAGED_DATASOURCE_PROBE_SIGNALS.append(
            {"service": "apps.b", "confidence": "high", "ok": True}
        )

        try:
            peeked = ccstack.peek_managed_datasource_probe_signals()
            self.assertEqual(len(peeked), 2)
            # Returned list is independent — mutating it does not touch
            # the module-level signal list.
            peeked.clear()
            self.assertEqual(len(ccstack.MANAGED_DATASOURCE_PROBE_SIGNALS), 2)

            drained = ccstack.consume_managed_datasource_probe_signals()
            self.assertEqual(len(drained), 2)
            self.assertEqual(len(ccstack.MANAGED_DATASOURCE_PROBE_SIGNALS), 0)
        finally:
            ccstack.MANAGED_DATASOURCE_PROBE_SIGNALS.clear()

    def test_count_external_http_mock_fallback_filters_sibling_and_generator(self):
        # The aggregator must count only per-candidate requirements whose
        # reason starts with the mockserver-fallback prefix — sibling
        # routes and the workspace-wide mock generator must be excluded
        # to keep the counter accurate.
        requirements = [
            {
                "id": "external-http-mock",
                "kind": "external_http",
                "technology": "mockserver",
                "reason": "External HTTP URLs were detected; ccstack will provide a local mock endpoint.",
            },
            {
                "id": "external-http-1",
                "kind": "external_http",
                "reason": "External HTTP URL will be pointed at the local ccstack mock endpoint.",
            },
            {
                "id": "external-http-2",
                "kind": "external_http",
                "reason": "External HTTP URL routed to sibling app at localhost:8081 (workspace-managed gradle service).",
            },
            {
                "id": "external-http-3",
                "kind": "external_http",
                "reason": "External HTTP URL will be pointed at the local ccstack mock endpoint.",
            },
            # Non-external_http kinds must be ignored, no matter the reason.
            {
                "id": "redis-local",
                "kind": "redis",
                "reason": "External HTTP URL will be pointed at the local ccstack mock endpoint.",
            },
        ]
        self.assertEqual(
            ccstack.count_external_http_mock_fallback_requirements(requirements),
            2,
        )
        self.assertEqual(ccstack.count_external_http_mock_fallback_requirements([]), 0)
        self.assertEqual(ccstack.count_external_http_mock_fallback_requirements(None), 0)

    def test_count_workspace_grey_zone_returns_full_contract_keys_when_empty(self):
        # Even a freshly bound workspace with no grey-zone signal at all
        # must return the five contract keys initialized to 0. Counter
        # consumers depend on the fixed key set — never omit keys. The
        # set grew from 4 to 5 when the unmanaged_dependency surface was
        # added (analysis-time classification of datasources whose engine
        # is outside MANAGED_DATABASES — Altibase, Oracle, DB2, ...).
        ccstack.consume_managed_datasource_probe_signals()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_grey_zone_workspace(Path(tmp))
            counters = ccstack.count_workspace_grey_zone(workspace)
        self.assertEqual(
            sorted(counters.keys()),
            sorted([
                "degraded",
                "low_confidence",
                "mock_fallback",
                "unverified_binding",
                "unmanaged_dependency",
            ]),
        )
        for key, value in counters.items():
            self.assertEqual(value, 0, f"{key} should default to 0 on a clean workspace")

    def test_count_workspace_grey_zone_counts_low_confidence_probe_signals(self):
        # Probe signals tagged confidence=low or confidence=degraded count.
        # Confidence=high probe signals are PROVEN readiness — they MUST
        # NOT inflate the low_confidence counter.
        ccstack.consume_managed_datasource_probe_signals()
        ccstack.MANAGED_DATASOURCE_PROBE_SIGNALS.extend([
            {"service": "apps.a", "confidence": "low", "ok": True, "method": "tcp_only"},
            {"service": "apps.b", "confidence": "degraded", "ok": False, "method": "none"},
            {"service": "apps.c", "confidence": "high", "ok": True, "method": "tcp+greeting"},
        ])
        try:
            with tempfile.TemporaryDirectory() as tmp:
                workspace = self._make_grey_zone_workspace(Path(tmp))
                counters = ccstack.count_workspace_grey_zone(workspace)
            self.assertEqual(counters["low_confidence"], 2)
            self.assertEqual(counters["degraded"], 0)
            # Aggregator must be non-destructive — the signals are still
            # in the module-level list afterwards.
            self.assertEqual(len(ccstack.MANAGED_DATASOURCE_PROBE_SIGNALS), 3)
        finally:
            ccstack.MANAGED_DATASOURCE_PROBE_SIGNALS.clear()

    def test_count_workspace_grey_zone_counts_unverified_binding_notes(self):
        # Each service whose ccstack_status_notes carries
        # app_binding_unverified=True increments the unverified_binding
        # counter once — multiple notes on the same service still count
        # as one (the helper dedupes by service).
        ccstack.consume_managed_datasource_probe_signals()
        services = {
            "apps.a": {
                "type": "gradle",
                "port": 18101,
                "ccstack_status_notes": [
                    {"key": "app_binding_unverified", "value": True},
                    {"key": "datasource_endpoint_unparsed", "value": "DATABASE_URL"},
                ],
            },
            "apps.b": {
                "type": "command",
                "ccstack_status_notes": [
                    {"key": "app_binding_unverified", "value": True},
                ],
            },
            "apps.c": {
                "type": "gradle",
                "port": 18103,
                "ccstack_status_notes": [
                    # value=False or missing must NOT count.
                    {"key": "app_binding_unverified", "value": False},
                ],
            },
            "apps.d": {
                "type": "gradle",
                "port": 18104,
                # No status notes — must NOT count.
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_grey_zone_workspace(Path(tmp), services=services)
            counters = ccstack.count_workspace_grey_zone(workspace)
        self.assertEqual(counters["unverified_binding"], 2)

    def test_count_workspace_grey_zone_reads_persisted_mock_fallback_routes(self):
        # The aggregator reads the persisted ccstack_managed_environment
        # ["mock_fallback_routes"] integer; older manifests lacking the
        # key gracefully default to 0 instead of raising.
        ccstack.consume_managed_datasource_probe_signals()
        with tempfile.TemporaryDirectory() as tmp:
            workspace_with = self._make_grey_zone_workspace(
                Path(tmp),
                managed_environment={"mock_fallback_routes": 3, "services": []},
            )
            counters_with = ccstack.count_workspace_grey_zone(workspace_with)

            workspace_without = self._make_grey_zone_workspace(
                Path(tmp),
                managed_environment={"services": []},
            )
            counters_without = ccstack.count_workspace_grey_zone(workspace_without)

            workspace_none = self._make_grey_zone_workspace(Path(tmp))
            counters_none = ccstack.count_workspace_grey_zone(workspace_none)

        self.assertEqual(counters_with["mock_fallback"], 3)
        self.assertEqual(counters_without["mock_fallback"], 0)
        self.assertEqual(counters_none["mock_fallback"], 0)

    def test_count_workspace_grey_zone_counts_persisted_seed_drift(self):
        # When the workspace has a persisted seed-drift state record, the
        # degraded counter increments by one. Unbound workspaces (those
        # in the `workspace_setup` mode) must contribute 0 — they have no
        # readiness story to grade yet.
        ccstack.consume_managed_datasource_probe_signals()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                ccstack.write_seed_drift_state(
                    "grey",
                    {
                        "workspace": "grey",
                        "affected_services": ["mysql"],
                        "named_volumes": ["grey-mysql-data"],
                        "remediation": "devstack workspace apply grey --recreate-volumes",
                        "fingerprint_previous": "abc",
                        "fingerprint_current": "def",
                    },
                )
                workspace = self._make_grey_zone_workspace(Path(tmp))
                counters_bound = ccstack.count_workspace_grey_zone(workspace)

                unbound = ccstack.Workspace(
                    Path(tmp),
                    ccstack.setup_workspace_config(Path(tmp)),
                )
                counters_unbound = ccstack.count_workspace_grey_zone(unbound)

        self.assertEqual(counters_bound["degraded"], 1)
        self.assertEqual(counters_unbound["degraded"], 0)

    def test_format_grey_zone_counters_line_is_stable_and_parseable(self):
        # The parseable line must use the fixed field order so external
        # tooling can pattern-match on a stable layout. Missing keys
        # silently fall back to 0; numeric coercion must not raise.
        # The contract was extended additively with unmanaged_dependency
        # — every prior key keeps its position; the new key appends at
        # the end so pre-existing prefix-based parsers still match.
        line = ccstack.format_grey_zone_counters_line({
            "degraded": 1,
            "low_confidence": 2,
            "mock_fallback": 0,
            "unverified_binding": 3,
            "unmanaged_dependency": 4,
        })
        self.assertEqual(
            line,
            "CCSTACK STATUS COUNTERS: degraded=1 low_confidence=2 "
            "mock_fallback=0 unverified_binding=3 unmanaged_dependency=4",
        )

        empty = ccstack.format_grey_zone_counters_line({})
        self.assertEqual(
            empty,
            "CCSTACK STATUS COUNTERS: degraded=0 low_confidence=0 "
            "mock_fallback=0 unverified_binding=0 unmanaged_dependency=0",
        )

    def test_format_grey_zone_summary_line_quiet_when_all_zero(self):
        # All-zero counters must render the empty string so an
        # all-green workspace stays quiet (no extra summary line).
        self.assertEqual(ccstack.format_grey_zone_summary_line({}), "")
        self.assertEqual(
            ccstack.format_grey_zone_summary_line({
                "degraded": 0,
                "low_confidence": 0,
                "mock_fallback": 0,
                "unverified_binding": 0,
            }),
            "",
        )

    def test_format_grey_zone_summary_line_lists_non_zero_in_contract_order(self):
        # Non-zero counters are listed in stable contract order; zero
        # counters are omitted from the human summary so the line stays
        # focused on what actually needs operator attention.
        summary = ccstack.format_grey_zone_summary_line({
            "unverified_binding": 4,
            "degraded": 0,
            "mock_fallback": 2,
            "low_confidence": 1,
        })
        self.assertEqual(
            summary,
            "WORKSPACE STATE: grey-zone - low_confidence=1, mock_fallback=2, unverified_binding=4",
        )

    def test_print_status_renders_counters_line_even_when_all_zero(self):
        # The always-printable parseable line is the machine-readable
        # surface for tooling — it must render on every status call,
        # including the all-green case, so tooling can grep for a
        # deterministic prefix without first checking if any counter is
        # non-zero. The human WORKSPACE STATE line stays quiet. After
        # the unmanaged_dependency extension, the all-zero tail now
        # carries five key=value pairs (still in stable contract order).
        ccstack.consume_managed_datasource_probe_signals()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_grey_zone_workspace(Path(tmp))
            output = self._capture_print_status(workspace, ["api"])
        self.assertIn(
            "CCSTACK STATUS COUNTERS: degraded=0 low_confidence=0 "
            "mock_fallback=0 unverified_binding=0 unmanaged_dependency=0",
            output,
        )
        # No grey-zone WORKSPACE STATE line — all counters are 0.
        self.assertNotIn("WORKSPACE STATE: grey-zone", output)
        # No per-service GREY-ZONE annotation either.
        self.assertNotIn("GREY-ZONE", output)

    def test_print_status_emits_grey_zone_workspace_summary_when_non_zero(self):
        # When any counter is non-zero (and seed-drift did not already
        # emit its own WORKSPACE STATE line), the human-readable
        # WORKSPACE STATE: grey-zone summary must render so the user
        # cannot miss that the workspace is not fully green.
        ccstack.consume_managed_datasource_probe_signals()
        ccstack.MANAGED_DATASOURCE_PROBE_SIGNALS.append(
            {"service": "apps.x", "confidence": "low", "ok": True, "method": "tcp_only"},
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                workspace = self._make_grey_zone_workspace(Path(tmp))
                output = self._capture_print_status(workspace, ["api"])
        finally:
            ccstack.MANAGED_DATASOURCE_PROBE_SIGNALS.clear()
        self.assertIn(
            "CCSTACK STATUS COUNTERS: degraded=0 low_confidence=1 "
            "mock_fallback=0 unverified_binding=0 unmanaged_dependency=0",
            output,
        )
        self.assertIn("WORKSPACE STATE: grey-zone - low_confidence=1", output)

    def test_print_status_emits_per_service_grey_zone_annotation_for_unverified_binding(self):
        # Per-service annotation rows surface the unverified_binding
        # signal next to the service it belongs to, so operators can
        # see which service is grey-zoned without re-running the probe.
        ccstack.consume_managed_datasource_probe_signals()
        services = {
            "apps.a": {
                "type": "gradle",
                "port": 18091,
                "ccstack_status_notes": [
                    {"key": "app_binding_unverified", "value": True, "detail": "actuator env unreachable"},
                ],
            },
            "apps.b": {
                "type": "gradle",
                "port": 18092,
                # No grey-zone note — must NOT get a GREY-ZONE row.
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_grey_zone_workspace(Path(tmp), services=services)
            output = self._capture_print_status(workspace, ["apps.a", "apps.b"])

        self.assertIn(
            f"{'apps.a':28} {'GREY-ZONE':10} reason=app_binding_unverified",
            output,
        )
        self.assertIn("detail=\"actuator env unreachable\"", output)
        # apps.b has no grey-zone row.
        self.assertNotIn(f"{'apps.b':28} GREY-ZONE", output)
        self.assertIn(
            "CCSTACK STATUS COUNTERS: degraded=0 low_confidence=0 "
            "mock_fallback=0 unverified_binding=1 unmanaged_dependency=0",
            output,
        )
        self.assertIn("WORKSPACE STATE: grey-zone - unverified_binding=1", output)

    def test_print_status_grey_zone_does_not_duplicate_seed_drift_workspace_state(self):
        # When seed-drift already emits its own
        # `WORKSPACE STATE: degraded - seed drift` line, the grey-zone
        # human summary must NOT also fire — that would print two
        # WORKSPACE STATE lines for what the operator perceives as the
        # same condition. The parseable counters line still fires (it
        # always does), and the degraded counter still reflects the
        # drift signal for tooling consumers.
        ccstack.consume_managed_datasource_probe_signals()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                ccstack.write_seed_drift_state(
                    "grey",
                    {
                        "workspace": "grey",
                        "affected_services": ["api"],
                        "named_volumes": ["grey-data"],
                        "remediation": "devstack workspace apply grey --recreate-volumes",
                        "fingerprint_previous": "x",
                        "fingerprint_current":  "y",
                    },
                )
                workspace = self._make_grey_zone_workspace(Path(tmp))
                output = self._capture_print_status(workspace, ["api"])

        # Seed-drift's own WORKSPACE STATE line MUST appear once.
        self.assertEqual(
            output.count("WORKSPACE STATE: degraded - seed drift"),
            1,
        )
        # And the grey-zone alternate summary must NOT also fire.
        self.assertNotIn("WORKSPACE STATE: grey-zone", output)
        # But the parseable line is still emitted and reflects the
        # drift in the degraded counter.
        self.assertIn(
            "CCSTACK STATUS COUNTERS: degraded=1 low_confidence=0 "
            "mock_fallback=0 unverified_binding=0 unmanaged_dependency=0",
            output,
        )

    def test_workspace_meta_exposes_counters_dict_with_contract_keys(self):
        # Tooling-friendly JSON mirror: workspace_meta must expose the
        # same four counters in a `counters` dict so a UI client (or any
        # JSON consumer) can pick them up without parsing the text
        # status. Unbound workspaces return all-zero counters because
        # there is no readiness story to grade yet.
        ccstack.consume_managed_datasource_probe_signals()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with mock.patch.object(ccstack, "REGISTRY_PATH", home / ".ccstack" / "workspaces.json"), \
                    mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                bound_workspace = self._make_grey_zone_workspace(Path(tmp))
                bound_meta = ccstack.workspace_meta(bound_workspace)

                unbound_workspace = ccstack.Workspace(
                    Path(tmp),
                    ccstack.setup_workspace_config(Path(tmp)),
                )
                unbound_meta = ccstack.workspace_meta(unbound_workspace)

        self.assertIn("counters", bound_meta)
        self.assertEqual(
            sorted(bound_meta["counters"].keys()),
            sorted([
                "degraded",
                "low_confidence",
                "mock_fallback",
                "unverified_binding",
                "unmanaged_dependency",
            ]),
        )
        for value in bound_meta["counters"].values():
            self.assertEqual(value, 0)

        self.assertIn("counters", unbound_meta)
        self.assertEqual(
            sorted(unbound_meta["counters"].keys()),
            sorted([
                "degraded",
                "low_confidence",
                "mock_fallback",
                "unverified_binding",
                "unmanaged_dependency",
            ]),
        )
        for value in unbound_meta["counters"].values():
            self.assertEqual(value, 0)

    def test_workspace_meta_counters_reflect_aggregator_outputs(self):
        # workspace_meta["counters"] must be the same dict the CLI uses,
        # so a UI and a tooling grep on the text line never disagree.
        ccstack.consume_managed_datasource_probe_signals()
        ccstack.MANAGED_DATASOURCE_PROBE_SIGNALS.append(
            {"service": "apps.y", "confidence": "degraded", "ok": False, "method": "none"},
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                services = {
                    "apps.y": {
                        "type": "gradle",
                        "port": 18201,
                        "ccstack_status_notes": [
                            {"key": "app_binding_unverified", "value": True},
                        ],
                    },
                }
                workspace = self._make_grey_zone_workspace(
                    Path(tmp),
                    services=services,
                    managed_environment={"mock_fallback_routes": 2, "services": []},
                )
                meta = ccstack.workspace_meta(workspace)
                counters_direct = ccstack.count_workspace_grey_zone(workspace)
        finally:
            ccstack.MANAGED_DATASOURCE_PROBE_SIGNALS.clear()

        self.assertEqual(meta["counters"], counters_direct)
        self.assertEqual(meta["counters"]["low_confidence"], 1)
        self.assertEqual(meta["counters"]["mock_fallback"], 2)
        self.assertEqual(meta["counters"]["unverified_binding"], 1)
        self.assertEqual(meta["counters"]["degraded"], 0)

    def test_materialize_managed_environment_persists_mock_fallback_routes(self):
        # The mock_fallback counter survives across status invocations
        # via the additive ccstack_managed_environment["mock_fallback_routes"]
        # field. Verify the persistence happens with a stub analysis
        # whose `environment.requirements` carries one mockserver-fallback
        # candidate, one sibling-routed candidate, and the workspace-wide
        # mock generator entry. Expected count: 1 (only the per-candidate
        # fallback route, not the generator or the sibling route).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            config = {
                "services": {
                    "apps.api": {
                        "type": "gradle",
                        "task": ":apps:api:bootRun",
                        "path": "apps/api",
                        "port": 18301,
                    },
                },
                "profiles": {"default": {"services": ["apps.api"]}},
            }
            analysis = {
                "environment": {
                    "requirements": [
                        {
                            "id": "external-http-mock",
                            "kind": "external_http",
                            "technology": "mockserver",
                            "status": "generate",
                            "service_name": "ccstack-managed.mock-http",
                            "compose_service": "mock-http",
                            "image": "mockserver/mockserver:5.15.0",
                            "port": 1080,
                            "reason": "External HTTP URLs were detected; ccstack will provide a local mock endpoint.",
                        },
                        {
                            "id": "external-http-1",
                            "kind": "external_http",
                            "technology": "http",
                            "status": "configure",
                            "reason": "External HTTP URL will be pointed at the local ccstack mock endpoint.",
                        },
                        {
                            "id": "external-http-2",
                            "kind": "external_http",
                            "technology": "http",
                            "status": "configure",
                            "reason": "External HTTP URL routed to sibling app at localhost:18301 (workspace-managed gradle service).",
                        },
                    ],
                    "generated": [
                        {
                            "id": "external-http-mock",
                            "kind": "external_http",
                            "technology": "mockserver",
                            "service_name": "ccstack-managed.mock-http",
                            "compose_service": "mock-http",
                            "image": "mockserver/mockserver:5.15.0",
                            "port": 1080,
                        },
                    ],
                    "provided": [],
                    "manual": [],
                    "env": [],
                    "args": [],
                },
                "persistence": {"datasources": [], "tables": []},
            }
            home = Path(tmp) / "home"
            with mock.patch.object(ccstack, "STATE_HOME", home / ".ccstack" / "state"):
                managed_config, managed = ccstack.materialize_managed_environment(
                    "ws-greyzone", root, config, analysis,
                )

        self.assertIn("mock_fallback_routes", managed)
        self.assertEqual(managed["mock_fallback_routes"], 1)
        # Round-trip via the persisted config block, mimicking what a fresh
        # status invocation sees after load. The writer uses the canonical
        # devstack key (Shim 6 — legacy ccstack key is read-only).
        self.assertEqual(
            managed_config["devstack_managed_environment"]["mock_fallback_routes"],
            1,
        )

    # ------------------------------------------------------------------
    # Unmanaged-datasource classification (`unmanaged_dependency` counter)
    # ------------------------------------------------------------------
    # Contract source: context/analysis.md "Contract" section.
    # The aggregator surfaces analysis-time classifications of datasource
    # engines outside MANAGED_DATABASES (Altibase, Oracle, DB2, Tibero, ...)
    # via the existing grey-zone counter system as a 5th key. The boundary
    # rule is pinned by T1; multi-source dedupe by T2; manifest round-trip
    # by T4; h2 exclusion by T5; per-service annotation by T8; graceful
    # invariant no-op (no false failure for unmanaged engines) by T11.

    def test_count_unmanaged_datasource_dependencies_filters_managed_and_h2_and_unknown(self):
        # The pure helper must accept only entries that satisfy ALL of:
        # kind=database AND status=manual_review AND technology is a
        # non-empty string AND technology not in MANAGED_DATABASES AND
        # technology not in {"h2", "unknown"}. Anything else is silently
        # filtered (it is not the unmanaged-dependency grey zone — it is
        # either managed, embedded, missing-info, or a different kind).
        managed_engines = list(ccstack.MANAGED_DATABASES.keys())
        entries = [
            # Managed engines — must NOT count.
            *[
                {
                    "kind": "database",
                    "status": "manual_review",
                    "technology": engine,
                    "source": f"src/main/resources/application-{engine}.yml",
                }
                for engine in managed_engines
            ],
            # Embedded H2 — must NOT count (separate first-class reason).
            {
                "kind": "database",
                "status": "manual_review",
                "technology": "h2",
                "source": "src/main/resources/application.yml",
            },
            # "unknown" technology — must NOT count (missing-info, not an
            # unmanaged engine).
            {
                "kind": "database",
                "status": "manual_review",
                "technology": "unknown",
                "source": "persistence model",
            },
            # Non-database kinds — must NOT count even if status matches.
            {
                "kind": "external_http",
                "status": "manual_review",
                "technology": "altibase",
                "source": "src/main/java/Foo.java",
            },
            # Non-manual_review statuses — must NOT count.
            {
                "kind": "database",
                "status": "generate",
                "technology": "altibase",
                "source": "src/main/resources/application-altibase.yml",
            },
            {
                "kind": "database",
                "status": "configure",
                "technology": "altibase",
                "source": "src/main/resources/application-altibase.yml",
            },
            # Empty technology — must NOT count.
            {
                "kind": "database",
                "status": "manual_review",
                "technology": "",
                "source": "src/main/resources/application-altibase.yml",
            },
            # Non-dict garbage — must NOT crash and must NOT count.
            "not a dict",
            None,
            123,
            # The unmanaged engine that SHOULD count.
            {
                "kind": "database",
                "status": "manual_review",
                "technology": "altibase",
                "source": "src/main/resources/application-altibase.yml",
            },
            # A different unmanaged engine that should ALSO count (proves
            # the helper is generic, not Altibase-specific — standing
            # user feedback: generic solution over case fix).
            {
                "kind": "database",
                "status": "manual_review",
                "technology": "oracle",
                "source": "modules/foo/src/main/resources/application.yml",
            },
        ]
        self.assertEqual(
            ccstack.count_unmanaged_datasource_dependencies(entries),
            2,
        )
        self.assertEqual(ccstack.count_unmanaged_datasource_dependencies([]), 0)
        self.assertEqual(ccstack.count_unmanaged_datasource_dependencies(None), 0)

    def test_count_unmanaged_datasource_dependencies_dedupes_by_engine_and_source(self):
        # Three distinct (technology, source) pairs count as 3. Two
        # entries with identical (technology, source) count as 1. The
        # real-world shopping workspace has six distinct *-altibase
        # services with six distinct source paths — those count as 6.
        entries = [
            {"kind": "database", "status": "manual_review",
             "technology": "altibase",
             "source": "shopping/partnershop-altibase/application.yml"},
            {"kind": "database", "status": "manual_review",
             "technology": "altibase",
             "source": "shopping/attribute-altibase/application.yml"},
            {"kind": "database", "status": "manual_review",
             "technology": "altibase",
             "source": "shopping/catalog-altibase/application.yml"},
            # Duplicate of the first entry — must collapse.
            {"kind": "database", "status": "manual_review",
             "technology": "altibase",
             "source": "shopping/partnershop-altibase/application.yml"},
        ]
        self.assertEqual(
            ccstack.count_unmanaged_datasource_dependencies(entries),
            3,
        )

        # Mixed engines with overlapping sources: distinctness is by the
        # pair, so two engines pointing at the same source still count
        # as two distinct unmanaged dependencies.
        mixed = [
            {"kind": "database", "status": "manual_review",
             "technology": "altibase", "source": "ds/master.yml"},
            {"kind": "database", "status": "manual_review",
             "technology": "oracle", "source": "ds/master.yml"},
        ]
        self.assertEqual(
            ccstack.count_unmanaged_datasource_dependencies(mixed),
            2,
        )

    def test_count_workspace_grey_zone_counts_unmanaged_dependency_from_manifest(self):
        # End-to-end via the workspace aggregator. The persisted
        # `ccstack_managed_environment.manual_review` array is the
        # status-time source of truth (built by materialize_managed_environment).
        # Two unmanaged-engine entries -> counters["unmanaged_dependency"] == 2;
        # the other four counter keys stay at 0 (no other signal source).
        ccstack.consume_managed_datasource_probe_signals()
        manual_review = [
            {"id": "database-altibase-manual-1", "kind": "database",
             "status": "manual_review", "technology": "altibase",
             "confidence": "medium",
             "source": "shopping/partnershop-altibase/application.yml",
             "reason": "altibase datasource detected, but ccstack has no Docker-managed template for this database engine.",
             "url": "jdbc:altibase://localhost:20300/db"},
            {"id": "database-altibase-manual-2", "kind": "database",
             "status": "manual_review", "technology": "altibase",
             "confidence": "medium",
             "source": "shopping/attribute-altibase/application.yml",
             "reason": "altibase datasource detected, but ccstack has no Docker-managed template for this database engine.",
             "url": "jdbc:altibase://localhost:20300/db2"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_grey_zone_workspace(
                Path(tmp),
                managed_environment={
                    "services": [],
                    "manual_review": manual_review,
                },
            )
            counters = ccstack.count_workspace_grey_zone(workspace)
        self.assertEqual(counters["unmanaged_dependency"], 2)
        # Other counters MUST stay at 0 — the new arm contributes only
        # to its own key.
        self.assertEqual(counters["degraded"], 0)
        self.assertEqual(counters["low_confidence"], 0)
        self.assertEqual(counters["mock_fallback"], 0)
        self.assertEqual(counters["unverified_binding"], 0)

    def test_count_workspace_grey_zone_ignores_h2_manual_review(self):
        # Risk-4 regression pin (analysis.md). The existing first-class
        # "Embedded H2 datasource detected" manual_review reason has
        # technology=="h2" and must NOT inflate the new unmanaged_dependency
        # counter. The h2 case is a separate concern with its own design
        # rationale (ccstack does not replace Docker-backed DBs with H2).
        ccstack.consume_managed_datasource_probe_signals()
        manual_review = [
            {"id": "database-h2", "kind": "database",
             "status": "manual_review", "technology": "h2",
             "confidence": "high",
             "source": "src/main/resources/application.yml",
             "reason": "Embedded H2 datasource detected; ccstack does not replace Docker-backed database runtime with H2.",
             "url": "jdbc:h2:mem:test"},
            # An "unknown" classification (couldn't parse URL) is ALSO
            # excluded — that is a missing-info item, not an unmanaged
            # engine. Pin both exclusions together.
            {"id": "database-unknown-foo", "kind": "database",
             "status": "manual_review", "technology": "unknown",
             "confidence": "low",
             "source": "modules/foo/application.yml",
             "reason": "Datasource URL could not be classified",
             "url": "jdbc:weird:foo"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_grey_zone_workspace(
                Path(tmp),
                managed_environment={
                    "services": [],
                    "manual_review": manual_review,
                },
            )
            counters = ccstack.count_workspace_grey_zone(workspace)
        self.assertEqual(counters["unmanaged_dependency"], 0)

    def test_print_status_emits_per_service_grey_zone_annotation_for_unmanaged_dependency(self):
        # Real-world scenario fixture (shopping workspace). A service
        # named `partnershop-altibase` whose manifest's manual_review
        # array contains a matching unmanaged Altibase entry must emit:
        #   1. The per-service annotation row `GREY-ZONE reason=unmanaged_dependency engine=altibase`
        #   2. The parseable counters tail with unmanaged_dependency=1
        #   3. The human WORKSPACE STATE line listing unmanaged_dependency=1
        ccstack.consume_managed_datasource_probe_signals()
        services = {
            "partnershop-altibase": {
                "type": "gradle",
                "task": ":partnershop-altibase:bootRun",
                "path": "shopping/partnershop-altibase",
                "port": 18181,
            },
        }
        managed_environment = {
            "services": [],
            "manual_review": [
                {"id": "database-altibase-manual-1", "kind": "database",
                 "status": "manual_review", "technology": "altibase",
                 "confidence": "medium",
                 "source": "shopping/partnershop-altibase/src/main/resources/application.yml",
                 "reason": "altibase datasource detected, but ccstack has no Docker-managed template for this database engine.",
                 "url": "jdbc:altibase://localhost:20300/db"},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._make_grey_zone_workspace(
                Path(tmp),
                services=services,
                managed_environment=managed_environment,
            )
            output = self._capture_print_status(workspace, ["partnershop-altibase"])

        self.assertIn(
            f"{'partnershop-altibase':28} {'GREY-ZONE':10} "
            f"reason=unmanaged_dependency engine=altibase",
            output,
        )
        self.assertIn(
            "CCSTACK STATUS COUNTERS: degraded=0 low_confidence=0 "
            "mock_fallback=0 unverified_binding=0 unmanaged_dependency=1",
            output,
        )
        self.assertIn(
            "WORKSPACE STATE: grey-zone - unmanaged_dependency=1",
            output,
        )

    def test_verify_managed_datasource_is_noop_for_unmanaged_engine_service(self):
        # Contract: invariants for unmanaged targets degrade gracefully.
        # When a service has no SPRING_DATASOURCE_*_URL entries on its
        # `env` (the unmanaged case — no managed JDBC URL was planted
        # because the requirement was status=manual_review and the
        # build_local_environment_plan loop skipped env-injection), the
        # invariant must NOT raise. This is the "graceful invariant
        # behavior" clause of the contract — the new counter expresses
        # honesty about the unmanaged classification; it MUST NOT cause
        # ccstack to start firing CcstackError for engines it never
        # tried to provision in the first place.
        service_without_managed_env = {
            "type": "gradle",
            "task": ":partnershop-altibase:bootRun",
            "path": "shopping/partnershop-altibase",
            "port": 18181,
            "env": {
                # Plenty of env vars, but none of them are
                # SPRING_DATASOURCE_*_URL — because the unmanaged
                # requirement never produced an application_env.
                "JAVA_TOOL_OPTIONS": "-Xmx512m",
                "SPRING_PROFILES_ACTIVE": "local",
            },
        }
        # Must return None (the no-op return) and must NOT raise.
        result = ccstack.verify_managed_datasource(
            "partnershop-altibase",
            service_without_managed_env,
        )
        self.assertIsNone(result)

        # Empty env dict — same no-op guarantee.
        service_with_empty_env = {
            "type": "gradle",
            "task": ":x:bootRun",
            "port": 18182,
            "env": {},
        }
        self.assertIsNone(
            ccstack.verify_managed_datasource("x", service_with_empty_env)
        )

        # Missing env entirely — must still no-op.
        service_without_env = {
            "type": "gradle",
            "task": ":y:bootRun",
            "port": 18183,
        }
        self.assertIsNone(
            ccstack.verify_managed_datasource("y", service_without_env)
        )

    # ---------- Non-JVM infra dependency signal scanning ----------
    #
    # The JVM scanner inspect_runtime_environment_signals reads Java/Kotlin
    # sources and Spring application*.properties|yml. A separate scanner
    # inspect_command_service_runtime_signals reads non-JVM manifests
    # (package.json, pyproject.toml, requirements.txt, go.mod, Cargo.toml)
    # and emits signals in the SAME shape so the existing
    # runtime_signal_requirement dispatcher consumes them uniformly.

    def test_inspect_command_service_runtime_signals_detects_node_ioredis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({
                "name": "node-app",
                "dependencies": {"ioredis": "^5.0.0", "express": "^4.0.0"},
            }))

            signals = ccstack.inspect_command_service_runtime_signals(root)

        self.assertIn("redis", signals)
        self.assertTrue(any("package.json" in s.get("path", "") for s in signals["redis"]))
        self.assertTrue(any("ioredis" in s.get("marker", "") for s in signals["redis"]))

    def test_inspect_command_service_runtime_signals_detects_node_kafka_mongo_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({
                "name": "node-app",
                "dependencies": {
                    "kafkajs": "^2.0.0",
                    "mongoose": "^7.0.0",
                    "@opensearch-project/opensearch": "^2.0.0",
                },
            }))

            signals = ccstack.inspect_command_service_runtime_signals(root)

        self.assertTrue(signals["kafka"], "kafkajs must yield a kafka signal")
        self.assertTrue(signals["mongo"], "mongoose must yield a mongo signal")
        self.assertTrue(signals["search"], "opensearch client must yield a search signal")

    def test_inspect_command_service_runtime_signals_detects_python_pyproject(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                "[project]\n"
                "name = \"py-app\"\n"
                "dependencies = [\n"
                "  \"redis>=5\",\n"
                "  \"pymongo>=4\",\n"
                "  \"confluent-kafka>=2\",\n"
                "  \"opensearch-py>=2\",\n"
                "]\n"
            )

            signals = ccstack.inspect_command_service_runtime_signals(root)

        self.assertTrue(signals["redis"], "redis pkg must yield a redis signal")
        self.assertTrue(signals["mongo"], "pymongo must yield a mongo signal")
        self.assertTrue(signals["kafka"], "confluent-kafka must yield a kafka signal")
        self.assertTrue(signals["search"], "opensearch-py must yield a search signal")

    def test_inspect_command_service_runtime_signals_detects_python_requirements_txt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text(
                "redis==5.0.0\n"
                "motor>=3.0\n"
                "aiokafka>=0.10\n"
                "elasticsearch>=8.0\n"
            )

            signals = ccstack.inspect_command_service_runtime_signals(root)

        self.assertTrue(signals["redis"])
        self.assertTrue(signals["mongo"], "motor must yield a mongo signal")
        self.assertTrue(signals["kafka"], "aiokafka must yield a kafka signal")
        self.assertTrue(signals["search"], "elasticsearch must yield a search signal")

    def test_inspect_command_service_runtime_signals_detects_go_mod(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text(
                "module example.com/go-app\n"
                "\n"
                "go 1.22\n"
                "\n"
                "require (\n"
                "  github.com/redis/go-redis/v9 v9.0.0\n"
                "  go.mongodb.org/mongo-driver v1.13.0\n"
                "  github.com/IBM/sarama v1.42.0\n"
                "  github.com/opensearch-project/opensearch-go/v2 v2.3.0\n"
                ")\n"
            )

            signals = ccstack.inspect_command_service_runtime_signals(root)

        self.assertTrue(signals["redis"], "go-redis must yield a redis signal")
        self.assertTrue(signals["mongo"], "mongo-driver must yield a mongo signal")
        self.assertTrue(signals["kafka"], "sarama must yield a kafka signal")
        self.assertTrue(signals["search"], "opensearch-go must yield a search signal")

    def test_inspect_command_service_runtime_signals_detects_cargo_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Cargo.toml").write_text(
                "[package]\n"
                "name = \"rust-app\"\n"
                "version = \"0.1.0\"\n"
                "\n"
                "[dependencies]\n"
                "redis = \"0.24\"\n"
                "mongodb = \"2.7\"\n"
                "rdkafka = \"0.36\"\n"
            )

            signals = ccstack.inspect_command_service_runtime_signals(root)

        self.assertTrue(signals["redis"])
        self.assertTrue(signals["mongo"])
        self.assertTrue(signals["kafka"], "rdkafka must yield a kafka signal")

    def test_inspect_command_service_runtime_signals_returns_empty_for_unrelated_deps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({
                "name": "plain-app",
                "dependencies": {"chalk": "^5.0.0", "yargs": "^17.0.0"},
            }))

            signals = ccstack.inspect_command_service_runtime_signals(root)

        for kind in ("redis", "kafka", "mongo", "search"):
            self.assertEqual(signals[kind], [], f"{kind} must be empty for unrelated deps")

    def test_inspect_command_service_runtime_signals_tolerates_malformed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text("{ not valid json")
            (root / "pyproject.toml").write_text("[malformed\nnot toml")
            (root / "Cargo.toml").write_text("not toml either]")
            (root / "go.mod").write_text("")  # empty

            # Must not crash; must return the standard empty-per-kind shape.
            signals = ccstack.inspect_command_service_runtime_signals(root)

        self.assertEqual(set(signals.keys()), {"redis", "kafka", "mongo", "search"})
        for kind in signals:
            self.assertEqual(signals[kind], [])

    def test_inspect_command_service_runtime_signals_ignores_node_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({"name": "outer"}))
            nested = root / "node_modules" / "fake-redis-pkg"
            nested.mkdir(parents=True)
            (nested / "package.json").write_text(json.dumps({
                "name": "fake-redis-pkg",
                "dependencies": {"ioredis": "^5.0.0"},
            }))

            signals = ccstack.inspect_command_service_runtime_signals(root)

        # The nested transitive dep inside node_modules must not contribute.
        self.assertEqual(signals["redis"], [])

    def test_inspect_command_service_runtime_signals_polyglot_unions_kinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            web = root / "services" / "web"
            api = root / "services" / "api"
            worker = root / "services" / "worker"
            for d in (web, api, worker):
                d.mkdir(parents=True)
            (web / "package.json").write_text(json.dumps({
                "name": "web", "dependencies": {"ioredis": "^5.0.0"},
            }))
            (api / "pyproject.toml").write_text(
                "[project]\nname = \"api\"\ndependencies = [\"pymongo>=4\"]\n"
            )
            (worker / "go.mod").write_text(
                "module example.com/worker\n\ngo 1.22\n\nrequire github.com/IBM/sarama v1.42.0\n"
            )

            signals = ccstack.inspect_command_service_runtime_signals(root)

        self.assertTrue(signals["redis"])
        self.assertTrue(signals["mongo"])
        self.assertTrue(signals["kafka"])

    # ---------- Command-service port + health inference ----------

    def test_inspect_command_services_infers_node_port_from_app_listen_literal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "node-app"
            root.mkdir()
            (root / "package.json").write_text(json.dumps({
                "name": "node-app",
                "scripts": {"start": "node server.js"},
            }))
            (root / "server.js").write_text(
                "const express = require('express');\n"
                "const app = express();\n"
                "app.listen(4000, () => console.log('up'));\n"
            )

            config = ccstack.inspect_workspace(root, "node-app")

        service = config["services"]["node-app"]
        self.assertEqual(service.get("port"), 4000)

    def test_inspect_command_services_infers_node_port_from_PORT_env_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "node-app"
            root.mkdir()
            (root / "package.json").write_text(json.dumps({
                "name": "node-app",
                "scripts": {"start": "node server.js"},
            }))
            # No literal — use a PORT env declaration in a .env file.
            (root / ".env").write_text("PORT=3001\nLOG_LEVEL=info\n")
            (root / "server.js").write_text(
                "const app = require('express')();\n"
                "app.listen(process.env.PORT, () => {});\n"
            )

            config = ccstack.inspect_workspace(root, "node-app")

        service = config["services"]["node-app"]
        self.assertEqual(service.get("port"), 3001)

    def test_inspect_command_services_infers_python_uvicorn_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "fastapi-app"
            root.mkdir()
            (root / "pyproject.toml").write_text(
                "[project]\nname = \"fastapi-app\"\n"
            )
            (root / "app.py").write_text(
                "import uvicorn\n"
                "from fastapi import FastAPI\n"
                "app = FastAPI()\n"
                "if __name__ == '__main__':\n"
                "    uvicorn.run(app, host='0.0.0.0', port=8123)\n"
            )

            config = ccstack.inspect_workspace(root, "fastapi-app")

        service = config["services"]["fastapi-app"]
        self.assertEqual(service.get("port"), 8123)

    def test_inspect_command_services_infers_django_runserver_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "django-app"
            root.mkdir()
            (root / "requirements.txt").write_text("django\n")
            (root / "manage.py").write_text(
                "# manage.py shim\n"
                "if __name__ == '__main__':\n"
                "    sys.argv = ['manage.py', 'runserver', '0.0.0.0:9101']\n"
            )

            config = ccstack.inspect_workspace(root, "django-app")

        service = config["services"]["django-app"]
        self.assertEqual(service.get("port"), 9101)

    def test_inspect_command_services_infers_go_listen_and_serve_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "go-app"
            root.mkdir()
            (root / "go.mod").write_text("module example.com/go-app\n\ngo 1.22\n")
            (root / "main.go").write_text(
                "package main\n"
                "import \"net/http\"\n"
                "func main() {\n"
                "  http.HandleFunc(\"/health\", func(w http.ResponseWriter, r *http.Request) {})\n"
                "  http.ListenAndServe(\":7070\", nil)\n"
                "}\n"
            )

            config = ccstack.inspect_workspace(root, "go-app")

        service = config["services"]["go-app"]
        self.assertEqual(service.get("port"), 7070)
        # /health route literal is present → health field set.
        self.assertEqual(service.get("health"), "http://localhost:7070/health")

    def test_inspect_command_services_infers_node_health_when_route_literal_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "node-app"
            root.mkdir()
            (root / "package.json").write_text(json.dumps({
                "name": "node-app",
                "scripts": {"start": "node server.js"},
            }))
            (root / "server.js").write_text(
                "const app = require('express')();\n"
                "app.get('/health', (req, res) => res.send('ok'));\n"
                "app.listen(8080);\n"
            )

            config = ccstack.inspect_workspace(root, "node-app")

        service = config["services"]["node-app"]
        self.assertEqual(service.get("port"), 8080)
        self.assertEqual(service.get("health"), "http://localhost:8080/health")

    def test_inspect_command_services_omits_health_when_no_route_literal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "node-app"
            root.mkdir()
            (root / "package.json").write_text(json.dumps({
                "name": "node-app",
                "scripts": {"start": "node server.js"},
            }))
            (root / "server.js").write_text(
                "const app = require('express')();\n"
                "app.get('/', (req, res) => res.send('hi'));\n"
                "app.listen(8080);\n"
            )

            config = ccstack.inspect_workspace(root, "node-app")

        service = config["services"]["node-app"]
        self.assertEqual(service.get("port"), 8080)
        # No /health route → health field MUST NOT be set (avoids false positive).
        self.assertNotIn("health", service)

    # ---------- Integration: non-JVM signals flow into the dispatcher ----------

    def test_inspect_workspace_node_ioredis_yields_redis_signal_present(self):
        """End-to-end: a Node project with ioredis surfaces a runtime
        signal that inspect_workspace's caller would see — even though
        the project has no Spring sources."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "node-app"
            root.mkdir()
            (root / "package.json").write_text(json.dumps({
                "name": "node-app",
                "dependencies": {"ioredis": "^5.0.0"},
            }))

            signals_jvm = ccstack.inspect_runtime_environment_signals(root)
            signals_cmd = ccstack.inspect_command_service_runtime_signals(root)

        # JVM scanner alone finds nothing (no Spring source); command scanner
        # supplies the redis signal that would feed runtime_signal_requirement.
        self.assertEqual(signals_jvm.get("redis", []), [])
        self.assertTrue(signals_cmd["redis"])


class CcstackUiBugFixTest(unittest.TestCase):
    """Regression tests for the devstack UI/UX P0/P1 bug audit (UIB-1..UIB-6,
    plus bundled UIB-9 engine half and UIB-10 background routing).

    All tests follow the established CcstackUiTest pattern:
      - HTML/JS assertions render via `ccstack.ui_page(workspace, registry, False)`
        and look for substrings in the returned source.
      - Server-side action tests call `ccstack.execute_ui_action(...)` and mock
        the subprocess boundary so they don't shell out to a real `ccstack`.
    """

    def _make_minimal_workspace(self) -> "ccstack.Workspace":
        return ccstack.Workspace(
            Path("/tmp/shop"),
            {
                "workspace": "shop",
                "profiles": {"default": {"services": ["api"]}},
                "services": {
                    "api": {
                        "type": "gradle",
                        "task": ":api:bootRun",
                        "port": 18081,
                        "health": "http://localhost:18081/actuator/health",
                    },
                },
            },
        )

    # ---------- UIB-3: action=down dispatches via background path ----------

    def test_ui_action_down_runs_in_background_like_up(self):
        workspace = self._make_minimal_workspace()
        with mock.patch.object(ccstack, "run_devstack_command_background") as bg, \
                mock.patch.object(ccstack, "run_devstack_command") as sync_run:
            bg.return_value = {
                "ok": True,
                "returncode": 0,
                "output": "devstack UI: shutdown is running in the background.\npid: 12345\nStop it with: kill 12345",
                "elapsed": 0,
                "pid": 12345,
            }
            sync_run.return_value = {"ok": True, "returncode": 0, "output": "", "elapsed": 0}
            result = ccstack.execute_ui_action(
                workspace,
                {"action": ["down"], "target": ["default"]},
            )
        self.assertTrue(bg.called, "down must dispatch via run_devstack_command_background")
        self.assertFalse(
            sync_run.called,
            "down must NOT use synchronous run_devstack_command — UI hangs otherwise",
        )
        self.assertTrue(result["ok"])
        self.assertIn("running in the background", result["output"])
        # Verify the background label is meaningful for the action.
        call_kwargs = bg.call_args.kwargs if bg.call_args.kwargs else {}
        if "label" in call_kwargs:
            self.assertEqual(call_kwargs["label"], "shutdown")
        else:
            # positional: (workspace, command, label)
            args = bg.call_args.args
            if len(args) >= 3:
                self.assertEqual(args[2], "shutdown")

    # ---------- UIB-10 (partial): restart also runs in background ----------

    def test_ui_action_restart_runs_in_background(self):
        workspace = self._make_minimal_workspace()
        with mock.patch.object(ccstack, "run_devstack_command_background") as bg, \
                mock.patch.object(ccstack, "run_devstack_command") as sync_run:
            bg.return_value = {
                "ok": True,
                "returncode": 0,
                "output": "devstack UI: restart is running in the background.\npid: 22222",
                "elapsed": 0,
                "pid": 22222,
            }
            sync_run.return_value = {"ok": True, "returncode": 0, "output": "", "elapsed": 0}
            result = ccstack.execute_ui_action(
                workspace,
                {"action": ["restart"], "service": ["api"]},
            )
        self.assertTrue(bg.called, "restart must dispatch via run_devstack_command_background")
        self.assertFalse(sync_run.called, "restart must NOT use synchronous run_devstack_command")
        self.assertTrue(result["ok"])
        self.assertIn("running in the background", result["output"])

    # ---------- UIB-9 (engine half): structured service_states in status ----------

    def test_ui_action_status_returns_structured_service_states_map(self):
        workspace = self._make_minimal_workspace()
        # Fake status text that exercises the parser the same way the
        # original client-side `updateServiceBoardFromStatusOutput` parses.
        status_text = "api running 18081 healthy /tmp/shop/.ccstack/logs/api.log\n"
        with mock.patch.object(ccstack, "run_devstack_command") as sync_run:
            sync_run.return_value = {
                "ok": True,
                "returncode": 0,
                "output": status_text,
                "elapsed": 0,
            }
            result = ccstack.execute_ui_action(
                workspace,
                {"action": ["status"], "target": ["all"]},
            )
        self.assertTrue(result["ok"])
        self.assertIn("output", result)
        # New additive field — the contract this regression test pins.
        self.assertIn(
            "service_states",
            result,
            "status action must include the additive service_states map",
        )
        states = result["service_states"]
        self.assertIsInstance(states, dict)
        self.assertIn("api", states)
        self.assertEqual(states["api"].get("state"), "running")

    # ---------- UIB-1: setLoading writes title + detail ----------

    def test_ui_set_loading_renders_title_and_detail(self):
        workspace = self._make_minimal_workspace()
        page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry(), False)
        self.assertIn('id="globalActivity"', page)
        self.assertIn('id="globalActivityTitle"', page)
        self.assertIn('id="globalActivityDetail"', page)
        # The setLoading body must assign title/detail somewhere; check both
        # textContent writes are present.
        self.assertIn("function setLoading(isLoading", page)
        # Confirm the function body is no longer the original no-op shape.
        set_loading_start = page.index("function setLoading(isLoading")
        # Window large enough to span the function body even with future
        # inline comments.
        body_slice = page[set_loading_start:set_loading_start + 1400]
        self.assertIn("globalActivityTitle", body_slice)
        self.assertIn("globalActivityDetail", body_slice)
        self.assertIn("textContent", body_slice)

    # ---------- UIB-2: workspace change tears down live state ----------

    def test_ui_workspace_change_handler_stops_live_logs_and_doctor_polls_before_navigation(self):
        workspace = self._make_minimal_workspace()
        page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry(), False)
        # Locate the change handler.
        anchor = page.index('workspace.addEventListener("change"')
        handler_slice = page[anchor:anchor + 1500]
        self.assertIn("stopLiveLogs(", handler_slice)
        self.assertIn("stopDoctorStatusPolling(", handler_slice)
        # And the teardown must precede the navigation.
        nav_offset = handler_slice.index("window.location.search")
        live_offset = handler_slice.index("stopLiveLogs(")
        poll_offset = handler_slice.index("stopDoctorStatusPolling(")
        self.assertLess(live_offset, nav_offset)
        self.assertLess(poll_offset, nav_offset)

    # ---------- UIB-4: doctor heartbeat surface ----------

    def test_ui_apply_doctor_status_renders_runtime_and_heartbeat_near_status_pill(self):
        workspace = self._make_minimal_workspace()
        page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry(), False)
        self.assertIn('id="doctorHeartbeat"', page)
        # Heartbeat must be in the same DOM container as the pill (proximity
        # check: within ~400 chars of the doctorStatusPill markup).
        pill_offset = page.index('id="doctorStatusPill"')
        heartbeat_offset = page.index('id="doctorHeartbeat"')
        self.assertLess(
            abs(heartbeat_offset - pill_offset),
            500,
            "doctorHeartbeat must be rendered near doctorStatusPill",
        )
        # applyDoctorStatus must reference the runtime / heartbeat fields.
        apply_start = page.index("function applyDoctorStatus(data)")
        apply_slice = page[apply_start:apply_start + 1800]
        self.assertIn("data.runtime_seconds", apply_slice)
        self.assertIn("data.last_heartbeat_at", apply_slice)

    # ---------- UIB-5: stopDoctorStatusPolling accepts clearRunId ----------

    def test_ui_stop_doctor_status_polling_clears_run_id_on_terminal_state(self):
        workspace = self._make_minimal_workspace()
        page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry(), False)
        # Function signature must accept an options object.
        self.assertIn("function stopDoctorStatusPolling(options", page)
        # Body must clear doctorCurrentRunId when clearRunId is set.
        stop_start = page.index("function stopDoctorStatusPolling(options")
        stop_slice = page[stop_start:stop_start + 1000]
        self.assertIn('doctorCurrentRunId = ""', stop_slice)
        self.assertIn("clearRunId", stop_slice)
        # applyDoctorStatus's terminal branch must request clearRunId.
        apply_start = page.index("function applyDoctorStatus(data)")
        apply_slice = page[apply_start:apply_start + 2200]
        self.assertIn("stopDoctorStatusPolling({ clearRunId: true })", apply_slice)

    # ---------- UIB-6: doctor cancel button — three coverage tests ----------

    def test_ui_doctor_cancel_button_is_rendered(self):
        workspace = self._make_minimal_workspace()
        page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry(), False)
        self.assertIn('id="doctorCancelButton"', page)
        self.assertIn(">Cancel diagnosis<", page)
        # Sanity: it is initially hidden + disabled.
        btn_offset = page.index('id="doctorCancelButton"')
        btn_slice = page[btn_offset:btn_offset + 200]
        self.assertIn("hidden", btn_slice)
        self.assertIn("disabled", btn_slice)

    def test_ui_doctor_cancel_click_handler_posts_doctor_cancel(self):
        workspace = self._make_minimal_workspace()
        page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry(), False)
        # The click handler should reference doctorCurrentRunId and POST the
        # doctor_cancel action. We do not pin a specific fetch construction.
        self.assertIn('doctorCancelButton.addEventListener("click"', page)
        click_start = page.index('doctorCancelButton.addEventListener("click"')
        click_slice = page[click_start:click_start + 1500]
        self.assertIn("doctor_cancel", click_slice)
        self.assertIn("doctorCurrentRunId", click_slice)

    def test_ui_doctor_apply_status_toggles_cancel_button_on_active_states(self):
        workspace = self._make_minimal_workspace()
        page = ccstack.ui_page(workspace, ccstack.WorkspaceRegistry(), False)
        apply_start = page.index("function applyDoctorStatus(data)")
        apply_slice = page[apply_start:apply_start + 1800]
        # Active state must cover spawned + running.
        self.assertIn('state === "spawned"', apply_slice)
        self.assertIn('state === "running"', apply_slice)
        # Toggle pattern must use !isActive on both hidden and disabled.
        self.assertIn("doctorCancelButton.hidden = !isActive", apply_slice)
        self.assertIn("doctorCancelButton.disabled = !isActive", apply_slice)


class ParseJdbcUrlSpringPlaceholderTest(unittest.TestCase):
    """Regression tests for parse_jdbc_url with Spring ${…} placeholder syntax.

    Issue #1: when a JDBC URL is wrapped in ${name:defaultValue} form and the
    default value starts with jdbc:, the function must extract and parse that
    default value instead of returning {}.
    """

    def test_plain_jdbc_url_is_unaffected(self):
        result = ccstack.parse_jdbc_url("jdbc:postgresql://localhost:5432/mydb")
        self.assertEqual(result["engine"], "postgresql")
        self.assertEqual(result["host"], "localhost")
        self.assertEqual(result["port"], 5432)
        self.assertEqual(result["database"], "mydb")

    def test_spring_placeholder_with_jdbc_default_is_parsed(self):
        url = "${SPRING_DATASOURCE_URL:jdbc:mysql://localhost:3306/shopdb}"
        result = ccstack.parse_jdbc_url(url)
        self.assertNotEqual(result, {}, "parse_jdbc_url returned {} for a Spring placeholder URL with jdbc: default")
        self.assertEqual(result["engine"], "mysql")
        self.assertEqual(result["host"], "localhost")
        self.assertEqual(result["port"], 3306)
        self.assertEqual(result["database"], "shopdb")

    def test_spring_placeholder_with_postgresql_default(self):
        url = "${DS_URL:jdbc:postgresql://db-host:5432/analytics}"
        result = ccstack.parse_jdbc_url(url)
        self.assertEqual(result["engine"], "postgresql")
        self.assertEqual(result["host"], "db-host")
        self.assertEqual(result["port"], 5432)
        self.assertEqual(result["database"], "analytics")

    def test_spring_placeholder_without_jdbc_default_returns_empty(self):
        # A placeholder whose default value is not a JDBC URL should return {}
        url = "${SPRING_DATASOURCE_URL:some-other-value}"
        result = ccstack.parse_jdbc_url(url)
        self.assertEqual(result, {})

    def test_spring_placeholder_without_default_returns_empty(self):
        url = "${SPRING_DATASOURCE_URL}"
        result = ccstack.parse_jdbc_url(url)
        self.assertEqual(result, {})

    def test_spring_placeholder_with_sqlserver_default(self):
        url = "${APP_JDBC_URL:jdbc:sqlserver://sqlserver-host:1433;databaseName=AppDB}"
        result = ccstack.parse_jdbc_url(url)
        self.assertEqual(result["engine"], "sqlserver")
        self.assertEqual(result["host"], "sqlserver-host")
        self.assertEqual(result["database"], "AppDB")

class AttachSchemaFilesNonMssqlTest(unittest.TestCase):
    """Regression tests for GitHub issue #2: attach_schema_files_to_generated_databases
    must set init_schema / init_dml for all database types, not just mssql.

    The bug caused /docker-entrypoint-initdb.d bind mounts to be dropped on every
    re-apply for MySQL and PostgreSQL because existing_managed_infra_requirement
    only re-adds the bind mount when init_schema or init_dml is truthy.
    """

    def _make_requirement(self, engine: str) -> dict:
        return {
            "technology": engine,
            "volumes": [],
        }

    def test_attach_schema_sets_init_schema_for_mysql(self):
        """MySQL requirements must have init_schema set after attach so
        existing_managed_infra_requirement can re-add the bind mount on re-apply."""
        requirement = self._make_requirement("mysql")
        schema_file = Path("/workspace/.devstack/infra/mysql-schema.sql")
        ccstack.attach_schema_files_to_generated_databases(
            [requirement],
            {"mysql": schema_file},
        )
        self.assertEqual(
            requirement.get("init_schema"),
            "/docker-entrypoint-initdb.d/010-devstack-local-schema.sql",
            "MySQL requirement must have init_schema set for re-apply bind-mount survival",
        )

    def test_attach_schema_sets_init_dml_for_mysql(self):
        """MySQL requirements must have init_dml set after attach."""
        requirement = self._make_requirement("mysql")
        schema_file = Path("/workspace/.devstack/infra/mysql-schema.sql")
        dml_file = Path("/workspace/.devstack/infra/mysql-dml.sql")
        ccstack.attach_schema_files_to_generated_databases(
            [requirement],
            {"mysql": schema_file},
            {"mysql": dml_file},
        )
        self.assertEqual(
            requirement.get("init_dml"),
            "/docker-entrypoint-initdb.d/020-devstack-local-dml.sql",
            "MySQL requirement must have init_dml set for re-apply bind-mount survival",
        )

    def test_attach_schema_sets_init_schema_for_postgresql(self):
        """PostgreSQL requirements must have init_schema set after attach."""
        requirement = self._make_requirement("postgresql")
        schema_file = Path("/workspace/.devstack/infra/pg-schema.sql")
        ccstack.attach_schema_files_to_generated_databases(
            [requirement],
            {"postgresql": schema_file},
        )
        self.assertEqual(
            requirement.get("init_schema"),
            "/docker-entrypoint-initdb.d/010-devstack-local-schema.sql",
            "PostgreSQL requirement must have init_schema set for re-apply bind-mount survival",
        )

    def test_attach_schema_sets_init_dml_for_postgresql(self):
        """PostgreSQL requirements must have init_dml set after attach."""
        requirement = self._make_requirement("postgresql")
        schema_file = Path("/workspace/.devstack/infra/pg-schema.sql")
        dml_file = Path("/workspace/.devstack/infra/pg-dml.sql")
        ccstack.attach_schema_files_to_generated_databases(
            [requirement],
            {"postgresql": schema_file},
            {"postgresql": dml_file},
        )
        self.assertEqual(
            requirement.get("init_dml"),
            "/docker-entrypoint-initdb.d/020-devstack-local-dml.sql",
            "PostgreSQL requirement must have init_dml set for re-apply bind-mount survival",
        )

    def test_mssql_init_schema_path_unchanged(self):
        """MSSQL must still use /devstack-init/ path (not /docker-entrypoint-initdb.d/)."""
        requirement = self._make_requirement("mssql")
        schema_file = Path("/workspace/.devstack/infra/mssql-schema.sql")
        ccstack.attach_schema_files_to_generated_databases(
            [requirement],
            {"mssql": schema_file},
        )
        self.assertEqual(
            requirement.get("init_schema"),
            "/devstack-init/010-devstack-local-schema.sql",
        )

    def test_mssql_init_dml_path_unchanged(self):
        """MSSQL must still use /devstack-init/ path for DML (not /docker-entrypoint-initdb.d/)."""
        requirement = self._make_requirement("mssql")
        schema_file = Path("/workspace/.devstack/infra/mssql-schema.sql")
        dml_file = Path("/workspace/.devstack/infra/mssql-dml.sql")
        ccstack.attach_schema_files_to_generated_databases(
            [requirement],
            {"mssql": schema_file},
            {"mssql": dml_file},
        )
        self.assertEqual(
            requirement.get("init_dml"),
            "/devstack-init/020-devstack-local-dml.sql",
        )

    def test_bind_mount_added_to_volumes_for_mysql(self):
        """Bind-mount must be added to the volumes list for MySQL."""
        requirement = self._make_requirement("mysql")
        schema_file = Path("/workspace/.devstack/infra/mysql-schema.sql")
        ccstack.attach_schema_files_to_generated_databases(
            [requirement],
            {"mysql": schema_file},
        )
        self.assertIn(
            f"{schema_file}:/docker-entrypoint-initdb.d/010-devstack-local-schema.sql:ro",
            requirement["volumes"],
        )

    def test_bind_mount_not_duplicated_on_reattach(self):
        """Calling attach twice must not duplicate the bind-mount entry."""
        requirement = self._make_requirement("mysql")
        schema_file = Path("/workspace/.devstack/infra/mysql-schema.sql")
        ccstack.attach_schema_files_to_generated_databases(
            [requirement],
            {"mysql": schema_file},
        )
        ccstack.attach_schema_files_to_generated_databases(
            [requirement],
            {"mysql": schema_file},
        )
        self.assertEqual(
            requirement["volumes"].count(
                f"{schema_file}:/docker-entrypoint-initdb.d/010-devstack-local-schema.sql:ro"
            ),
            1,
        )


class InitializeManagedInfraMySQLPostgresTest(unittest.TestCase):
    """Regression tests for: initialize_managed_infra must apply DDL/DML for
    MySQL and PostgreSQL (not just MSSQL), and must warn the user when a
    stale named data volume is silently bypassing /docker-entrypoint-initdb.d/.

    Root cause history:
      1. initialize_managed_infra used to early-return for engine != "mssql",
         so MySQL/PostgreSQL DDL never ran via that code path.
      2. MySQL/Postgres images silently skip /docker-entrypoint-initdb.d/
         when the named data volume already contains an initialized database
         from a prior run, so the bind-mount alone is not sufficient.
    """

    def _workspace_with_schema_files(self, tmp_root: Path, engine: str, with_dml: bool = True):
        """Build a Workspace plus seeded schema/dml files on disk for `engine`."""
        schema_dir = ccstack.managed_schema_dir("test-shop")
        schema_dir.mkdir(parents=True, exist_ok=True)
        schema_path = ccstack.managed_schema_path("test-shop", engine)
        dml_path = ccstack.managed_dml_path("test-shop", engine)
        schema_path.write_text("CREATE TABLE t (id INT);\n")
        if with_dml:
            dml_path.write_text("INSERT INTO t VALUES (1);\n")
        workspace = ccstack.Workspace(
            tmp_root,
            {
                "workspace": "test-shop",
                "default_timeout": 5,
                "profiles": {},
                "services": {},
            },
        )
        return workspace, schema_path, dml_path

    # --- MSSQL behaviour must remain unchanged --------------------------------

    def test_initialize_mssql_path_still_runs_sqlcmd(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _, _ = self._workspace_with_schema_files(Path(tmp), "mssql")
            service = {
                "type": "compose",
                "engine": "mssql",
                "compose": "compose.yml",
                "service": "mssql",
                "database": "shop",
            }
            with mock.patch.object(workspace, "run_mssql_sqlcmd") as sqlcmd, \
                 mock.patch.object(ccstack, "managed_data_volume_exists", return_value=False):
                workspace.initialize_managed_infra(service)
        # At minimum the CREATE DATABASE call must have fired.
        self.assertGreaterEqual(sqlcmd.call_count, 1)

    # --- MySQL: DDL must be applied ------------------------------------------

    def test_initialize_mysql_applies_schema_via_compose_exec(self):
        """MySQL services must run init_schema via `docker compose exec` so
        the SQL file is applied even when the named data volume already
        existed and the image silently skipped /docker-entrypoint-initdb.d/."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, schema_path, dml_path = self._workspace_with_schema_files(
                Path(tmp), "mysql"
            )
            service = {
                "type": "compose",
                "engine": "mysql",
                "compose": str(Path(tmp) / "compose.yml"),
                "service": "mysql",
                "database": "shop",
                "username": "ccstack",
                "password": "ccstack",
                "init_schema": "/docker-entrypoint-initdb.d/010-devstack-local-schema.sql",
                "init_dml": "/docker-entrypoint-initdb.d/020-devstack-local-dml.sql",
            }
            with mock.patch.object(ccstack, "run") as run_mock, \
                 mock.patch.object(
                     workspace,
                     "run_mysql_apply_sql_with_retry",
                 ) as apply_mock, \
                 mock.patch.object(ccstack, "managed_data_volume_exists", return_value=False):
                run_mock.return_value = None
                workspace.initialize_managed_infra(service)

            # Schema and DML must both be applied via the mysql helper.
            applied = [c.args[1] for c in apply_mock.call_args_list]
            self.assertIn(str(schema_path), applied)
            self.assertIn(str(dml_path), applied)

    def test_initialize_mysql_skips_when_no_schema_files(self):
        """No init_schema / init_dml means no apply attempt — must not crash
        and must not call the apply helper."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _, _ = self._workspace_with_schema_files(
                Path(tmp), "mysql", with_dml=False
            )
            # Remove both SQL files — simulating "no DDL configured".
            # DML may exist on disk from a prior test run, so remove it too.
            ccstack.managed_schema_path("test-shop", "mysql").unlink()
            ccstack.managed_dml_path("test-shop", "mysql").unlink(missing_ok=True)
            service = {
                "type": "compose",
                "engine": "mysql",
                "compose": str(Path(tmp) / "compose.yml"),
                "service": "mysql",
                "database": "shop",
                # No init_schema / init_dml keys at all.
            }
            with mock.patch.object(
                workspace, "run_mysql_apply_sql_with_retry"
            ) as apply_mock, \
                mock.patch.object(ccstack, "managed_data_volume_exists", return_value=False):
                workspace.initialize_managed_infra(service)
            apply_mock.assert_not_called()

    # --- PostgreSQL: DDL must be applied -------------------------------------

    def test_initialize_postgresql_applies_schema_via_compose_exec(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, schema_path, dml_path = self._workspace_with_schema_files(
                Path(tmp), "postgresql"
            )
            service = {
                "type": "compose",
                "engine": "postgresql",
                "compose": str(Path(tmp) / "compose.yml"),
                "service": "postgres",
                "database": "shop",
                "username": "ccstack",
                "password": "ccstack",
                "init_schema": "/docker-entrypoint-initdb.d/010-devstack-local-schema.sql",
                "init_dml": "/docker-entrypoint-initdb.d/020-devstack-local-dml.sql",
            }
            with mock.patch.object(ccstack, "run") as run_mock, \
                 mock.patch.object(
                     workspace,
                     "run_postgres_apply_sql_with_retry",
                 ) as apply_mock, \
                 mock.patch.object(ccstack, "managed_data_volume_exists", return_value=False):
                run_mock.return_value = None
                workspace.initialize_managed_infra(service)

            applied = [c.args[1] for c in apply_mock.call_args_list]
            self.assertIn(str(schema_path), applied)
            self.assertIn(str(dml_path), applied)

    # --- Stale named volume detection ----------------------------------------

    def test_managed_data_volume_exists_returns_true_on_rc_zero(self):
        """managed_data_volume_exists must shell out to `docker volume inspect`
        and return True when the volume already exists (rc=0)."""
        completed = mock.Mock()
        completed.returncode = 0
        with mock.patch.object(ccstack.subprocess, "run", return_value=completed) as sub:
            self.assertTrue(ccstack.managed_data_volume_exists("mysql", "mysql"))
        sub.assert_called_once()
        # First arg must be the docker volume inspect command for the right name.
        args = sub.call_args.args[0]
        self.assertEqual(args[:3], ["docker", "volume", "inspect"])
        self.assertEqual(args[3], "devstack-mysql-data")

    def test_managed_data_volume_exists_returns_false_on_rc_nonzero(self):
        completed = mock.Mock()
        completed.returncode = 1
        with mock.patch.object(ccstack.subprocess, "run", return_value=completed):
            self.assertFalse(ccstack.managed_data_volume_exists("mysql", "mysql"))

    def test_managed_data_volume_exists_returns_false_when_docker_missing(self):
        """If docker is not installed the helper must not crash; return False."""
        with mock.patch.object(ccstack.subprocess, "run", side_effect=FileNotFoundError("docker")):
            self.assertFalse(ccstack.managed_data_volume_exists("mysql", "mysql"))

    def test_managed_data_volume_exists_returns_false_for_engine_without_volume(self):
        """Engines outside MANAGED_DATABASES have no managed data volume —
        the helper must return False without shelling out."""
        with mock.patch.object(ccstack.subprocess, "run") as sub:
            self.assertFalse(ccstack.managed_data_volume_exists("oracle", "oracle"))
        sub.assert_not_called()

    def test_initialize_mysql_warns_when_data_volume_already_exists(self):
        """When the named data volume already exists, devstack must print a
        clear warning to stderr explaining that /docker-entrypoint-initdb.d/
        is silently skipped and pointing to the --recreate-volumes remediation."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _, _ = self._workspace_with_schema_files(Path(tmp), "mysql")
            service = {
                "type": "compose",
                "engine": "mysql",
                "compose": str(Path(tmp) / "compose.yml"),
                "service": "mysql",
                "database": "shop",
                "username": "ccstack",
                "password": "ccstack",
                "init_schema": "/docker-entrypoint-initdb.d/010-devstack-local-schema.sql",
            }
            from io import StringIO
            buf = StringIO()
            with mock.patch.object(ccstack.sys, "stderr", buf), \
                 mock.patch.object(ccstack, "managed_data_volume_exists", return_value=True), \
                 mock.patch.object(workspace, "run_mysql_apply_sql_with_retry"):
                workspace.initialize_managed_infra(service)
            stderr_text = buf.getvalue()
            self.assertIn("devstack-mysql-data", stderr_text)
            self.assertIn("docker-entrypoint-initdb.d", stderr_text)
            self.assertIn("--recreate-volumes", stderr_text)

    def test_initialize_mysql_does_not_warn_when_data_volume_absent(self):
        """First-run scenario: no existing named volume → no stale-volume warning."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _, _ = self._workspace_with_schema_files(Path(tmp), "mysql")
            service = {
                "type": "compose",
                "engine": "mysql",
                "compose": str(Path(tmp) / "compose.yml"),
                "service": "mysql",
                "database": "shop",
                "username": "ccstack",
                "password": "ccstack",
                "init_schema": "/docker-entrypoint-initdb.d/010-devstack-local-schema.sql",
            }
            from io import StringIO
            buf = StringIO()
            with mock.patch.object(ccstack.sys, "stderr", buf), \
                 mock.patch.object(ccstack, "managed_data_volume_exists", return_value=False), \
                 mock.patch.object(workspace, "run_mysql_apply_sql_with_retry"):
                workspace.initialize_managed_infra(service)
            stderr_text = buf.getvalue()
            self.assertNotIn("docker-entrypoint-initdb.d", stderr_text)

    def test_initialize_postgresql_warns_when_data_volume_already_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _, _ = self._workspace_with_schema_files(Path(tmp), "postgresql")
            service = {
                "type": "compose",
                "engine": "postgresql",
                "compose": str(Path(tmp) / "compose.yml"),
                "service": "postgres",
                "database": "shop",
                "username": "ccstack",
                "password": "ccstack",
                "init_schema": "/docker-entrypoint-initdb.d/010-devstack-local-schema.sql",
            }
            from io import StringIO
            buf = StringIO()
            with mock.patch.object(ccstack.sys, "stderr", buf), \
                 mock.patch.object(ccstack, "managed_data_volume_exists", return_value=True), \
                 mock.patch.object(workspace, "run_postgres_apply_sql_with_retry"):
                workspace.initialize_managed_infra(service)
            stderr_text = buf.getvalue()
            self.assertIn("devstack-postgres-data", stderr_text)
            self.assertIn("--recreate-volumes", stderr_text)

    # --- run_mysql_apply_sql command shape -----------------------------------

    def test_run_mysql_apply_sql_invokes_docker_compose_exec(self):
        """The mysql apply helper must build a `docker compose exec` command
        that pipes the SQL file into the mysql CLI inside the container."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, schema_path, _ = self._workspace_with_schema_files(Path(tmp), "mysql")
            service = {
                "type": "compose",
                "engine": "mysql",
                "compose": str(Path(tmp) / "compose.yml"),
                "service": "mysql",
                "database": "shop",
                "username": "ccstack",
                "password": "ccstack",
            }
            with mock.patch.object(ccstack, "compose_command", return_value=["docker", "compose"]), \
                 mock.patch.object(ccstack.subprocess, "run") as subprocess_mock:
                subprocess_mock.return_value = mock.MagicMock(returncode=0)
                workspace.run_mysql_apply_sql(service, str(schema_path))
        args = subprocess_mock.call_args.args[0]
        # Must invoke compose exec on the right service with stdin = SQL file
        self.assertIn("exec", args)
        self.assertIn("-T", args)
        self.assertIn("mysql", args)
        self.assertTrue(
            "shop" in " ".join(args),
            f"database name 'shop' must appear in args: {args}",
        )

    def test_run_postgres_apply_sql_invokes_docker_compose_exec(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, schema_path, _ = self._workspace_with_schema_files(Path(tmp), "postgresql")
            service = {
                "type": "compose",
                "engine": "postgresql",
                "compose": str(Path(tmp) / "compose.yml"),
                "service": "postgres",
                "database": "shop",
                "username": "ccstack",
                "password": "ccstack",
            }
            with mock.patch.object(ccstack, "compose_command", return_value=["docker", "compose"]), \
                 mock.patch.object(ccstack.subprocess, "run") as subprocess_mock:
                subprocess_mock.return_value = mock.MagicMock(returncode=0)
                workspace.run_postgres_apply_sql(service, str(schema_path))
        args = subprocess_mock.call_args.args[0]
        self.assertIn("exec", args)
        self.assertIn("-T", args)
        self.assertIn("postgres", args)
        self.assertTrue(
            "shop" in " ".join(args),
            f"database name 'shop' must appear in args: {args}",
        )

    def test_run_mysql_apply_sql_retries_until_ready(self):
        """run_mysql_apply_sql_with_retry must retry on DevstackError until
        the configured timeout elapses, matching the mssql helper's contract."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, schema_path, _ = self._workspace_with_schema_files(Path(tmp), "mysql")
            service = {
                "type": "compose",
                "engine": "mysql",
                "compose": str(Path(tmp) / "compose.yml"),
                "service": "mysql",
                "database": "shop",
                "username": "ccstack",
                "password": "ccstack",
            }
            with mock.patch.object(
                workspace,
                "run_mysql_apply_sql",
                side_effect=[ccstack.CcstackError("mysql not ready"), None],
            ) as apply_mock, mock.patch.object(ccstack.time, "sleep") as sleep_mock:
                workspace.run_mysql_apply_sql_with_retry(service, str(schema_path))
        self.assertEqual(apply_mock.call_count, 2)
        sleep_mock.assert_called_once_with(2)

    def test_run_postgres_apply_sql_retries_until_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, schema_path, _ = self._workspace_with_schema_files(Path(tmp), "postgresql")
            service = {
                "type": "compose",
                "engine": "postgresql",
                "compose": str(Path(tmp) / "compose.yml"),
                "service": "postgres",
                "database": "shop",
                "username": "ccstack",
                "password": "ccstack",
            }
            with mock.patch.object(
                workspace,
                "run_postgres_apply_sql",
                side_effect=[ccstack.CcstackError("postgres not ready"), None],
            ) as apply_mock, mock.patch.object(ccstack.time, "sleep") as sleep_mock:
                workspace.run_postgres_apply_sql_with_retry(service, str(schema_path))
        self.assertEqual(apply_mock.call_count, 2)
        sleep_mock.assert_called_once_with(2)

    # --- Engines outside the managed-databases set are no-ops ----------------

    def test_initialize_non_database_compose_service_is_noop(self):
        """A compose service that is not a managed database (e.g. redis) must
        still be a no-op after the engine gate is broadened."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = ccstack.Workspace(
                root,
                {
                    "workspace": "test-shop",
                    "default_timeout": 5,
                    "profiles": {},
                    "services": {},
                },
            )
            service = {
                "type": "compose",
                "engine": "redis",
                "compose": "compose.yml",
                "service": "redis",
            }
            with mock.patch.object(workspace, "run_mssql_sqlcmd") as sqlcmd, \
                 mock.patch.object(ccstack, "run") as run_mock, \
                 mock.patch.object(ccstack, "managed_data_volume_exists", return_value=False):
                workspace.initialize_managed_infra(service)
        sqlcmd.assert_not_called()
        run_mock.assert_not_called()


class ExistingManagedInfraRequirementBindMountTest(unittest.TestCase):
    """Regression test for the compose-regeneration bug where
    existing_managed_infra_requirement dropped the schema/DML init-script
    bind-mounts whenever the persisted service entry in
    devstack.generated.json had empty init_schema / init_dml — even though
    the SQL files materialised under managed_schema_path / managed_dml_path
    still existed on disk. The result was that the next compose `up` on a
    fresh named data volume initialised an empty database.

    The fix: append the engine-correct bind-mount whenever the host-side
    SQL file exists, regardless of init_schema / init_dml being set on the
    persisted entry, matching the target-path convention from
    attach_schema_files_to_generated_databases.
    """

    def _materialize_sql(self, state_home: Path, workspace: str, engine: str):
        schema_p = state_home / workspace / "infra" / "schema" / f"devstack-local-schema-{engine}.sql"
        dml_p = state_home / workspace / "infra" / "schema" / f"devstack-local-dml-{engine}.sql"
        schema_p.parent.mkdir(parents=True, exist_ok=True)
        schema_p.write_text("CREATE TABLE x(id INT);\n")
        dml_p.write_text("INSERT INTO x VALUES (1);\n")
        return schema_p, dml_p

    def _run_case(self, engine: str, expected_target_schema: str, expected_target_dml: str):
        with tempfile.TemporaryDirectory() as tmp:
            state_home = Path(tmp) / "state"
            workspace = "demo"
            with mock.patch.object(ccstack, "STATE_HOME", state_home):
                # Confirm the helpers resolve to a path inside our temp state home.
                self.assertEqual(
                    ccstack.managed_schema_path(workspace, engine).parent.parent,
                    state_home / workspace / "infra",
                )
                schema_p, dml_p = self._materialize_sql(state_home, workspace, engine)

                # Persisted service entry — bug shape: init_schema / init_dml empty,
                # volumes list only contains the named data volume (the bind-mount
                # was lost on the previous compose regeneration).
                persisted = {
                    "type": "compose",
                    "compose": str(ccstack.managed_infra_compose_path(workspace)),
                    "project": ccstack.managed_compose_project_name(workspace),
                    "service": ccstack.MANAGED_DATABASES[engine]["label"],
                    "engine": engine,
                    "port": ccstack.MANAGED_DATABASES[engine]["port"],
                    "image": ccstack.MANAGED_DATABASES[engine]["image"],
                    "container_port": ccstack.MANAGED_DATABASES[engine]["container_port"],
                    "environment": {},
                    "command": None,
                    "volumes": ccstack.database_data_volumes(engine, ccstack.MANAGED_DATABASES[engine]["label"]),
                    "database": "demo",
                    "username": "ccstack",
                    "password": "ccstack",
                    "init_schema": None,
                    "init_dml": None,
                }

                req = ccstack.existing_managed_infra_requirement(
                    workspace, f"devstack.infra.{persisted['service']}", persisted
                )

        self.assertIsNotNone(req, f"existing_managed_infra_requirement returned None for {engine}")
        volumes = req.get("volumes") or []
        schema_bind = f"{schema_p}:{expected_target_schema}:ro"
        dml_bind = f"{dml_p}:{expected_target_dml}:ro"
        self.assertIn(
            schema_bind, volumes,
            f"{engine}: schema bind-mount missing from regenerated volumes (got {volumes!r})",
        )
        self.assertIn(
            dml_bind, volumes,
            f"{engine}: DML bind-mount missing from regenerated volumes (got {volumes!r})",
        )
        # No duplicate entries — fix must be idempotent across repeated regenerations.
        self.assertEqual(volumes.count(schema_bind), 1, f"{engine}: schema bind-mount duplicated")
        self.assertEqual(volumes.count(dml_bind), 1, f"{engine}: DML bind-mount duplicated")

    def test_mysql_bind_mounts_restored_when_persisted_init_paths_empty(self):
        self._run_case(
            "mysql",
            "/docker-entrypoint-initdb.d/010-devstack-local-schema.sql",
            "/docker-entrypoint-initdb.d/020-devstack-local-dml.sql",
        )

    def test_postgresql_bind_mounts_restored_when_persisted_init_paths_empty(self):
        self._run_case(
            "postgresql",
            "/docker-entrypoint-initdb.d/010-devstack-local-schema.sql",
            "/docker-entrypoint-initdb.d/020-devstack-local-dml.sql",
        )

    def test_mssql_bind_mounts_restored_when_persisted_init_paths_empty(self):
        # MSSQL uses /devstack-init/ (sqlcmd applies the scripts), not initdb.d.
        # The bind-mount still belongs on the compose file so sqlcmd can read the
        # host-materialised SQL from inside the container.
        self._run_case(
            "mssql",
            "/devstack-init/010-devstack-local-schema.sql",
            "/devstack-init/020-devstack-local-dml.sql",
        )

    def test_mysql_no_duplicate_when_persisted_volumes_already_have_bind_mount(self):
        """Idempotency: if the persisted volumes list already contains the
        bind-mount strings, the reconstruction must not duplicate them."""
        engine = "mysql"
        with tempfile.TemporaryDirectory() as tmp:
            state_home = Path(tmp) / "state"
            workspace = "demo"
            with mock.patch.object(ccstack, "STATE_HOME", state_home):
                schema_p, dml_p = self._materialize_sql(state_home, workspace, engine)
                schema_bind = f"{schema_p}:/docker-entrypoint-initdb.d/010-devstack-local-schema.sql:ro"
                dml_bind = f"{dml_p}:/docker-entrypoint-initdb.d/020-devstack-local-dml.sql:ro"
                persisted = {
                    "type": "compose",
                    "compose": str(ccstack.managed_infra_compose_path(workspace)),
                    "service": "mysql",
                    "engine": "mysql",
                    "port": 3306,
                    "image": "mysql:8.0",
                    "container_port": 3306,
                    "environment": {},
                    "volumes": [
                        "devstack-mysql-data:/var/lib/mysql",
                        schema_bind,
                        dml_bind,
                    ],
                    "database": "demo",
                    "init_schema": "/docker-entrypoint-initdb.d/010-devstack-local-schema.sql",
                    "init_dml": "/docker-entrypoint-initdb.d/020-devstack-local-dml.sql",
                }
                req = ccstack.existing_managed_infra_requirement(
                    workspace, "devstack.infra.mysql", persisted
                )

        volumes = req.get("volumes") or []
        self.assertEqual(volumes.count(schema_bind), 1, f"schema bind-mount duplicated: {volumes!r}")
        self.assertEqual(volumes.count(dml_bind), 1, f"DML bind-mount duplicated: {volumes!r}")


if __name__ == "__main__":
    unittest.main()
