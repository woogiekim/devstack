import importlib.util
from importlib.machinery import SourceFileLoader
import argparse
import base64
import os
import socket
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
# Pick the renamed devstack file when present (post git mv);
# fall back to ccstack for any in-progress checkout.
DEVSTACK_PATH = ROOT / "devstack" if (ROOT / "devstack").exists() else ROOT / "ccstack"
CCSTACK_PATH = DEVSTACK_PATH  # legacy alias for older tests


def load_ccstack():
    loader = SourceFileLoader("ccstack_module", str(CCSTACK_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ccstack = load_ccstack()


def _smoke_env(name: str, default: str = "") -> str:
    """Read a smoke-test env var, preferring ``DEVSTACK_SMOKE_<name>`` over
    the legacy ``CCSTACK_SMOKE_<name>`` (rename Shim 1 mirror on the test
    side — the smoke tests run before the tool's env_read helper is
    invoked, so we re-implement the precedence locally)."""
    new_key = f"DEVSTACK_SMOKE_{name}"
    legacy_key = f"CCSTACK_SMOKE_{name}"
    value = os.environ.get(new_key)
    if value is None:
        value = os.environ.get(legacy_key)
    if value is None:
        return default
    return value


def _read_http(url: str, body: Optional[str] = None, method: str = "GET", timeout: int = 5) -> Tuple[int, str, str]:
    payload = None
    headers = {}
    if body is not None:
        payload = body.encode("utf-8")
        headers["Content-Type"] = "application/json"
    basic_auth = _smoke_env("BASIC_AUTH").strip()
    if basic_auth:
        token = base64.b64encode(basic_auth.encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    req = urllib.request.Request(url, data=payload, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.getcode(), "", response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, str(error.reason), error.read().decode("utf-8", errors="replace")
    except OSError as error:
        return 0, "connection_error", str(error)


class CcstackWorkspaceSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _smoke_env("ENABLED").strip() != "1":
            raise unittest.SkipTest(
                "set DEVSTACK_SMOKE_ENABLED=1 (or legacy CCSTACK_SMOKE_ENABLED) "
                "to run live devstack workspace smoke tests"
            )

        env_workspace = _smoke_env("WORKSPACE").strip()
        env_root = _smoke_env("WORKSPACE_ROOT").strip()

        if not env_workspace and not env_root:
            raise unittest.SkipTest(
                "set DEVSTACK_SMOKE_WORKSPACE (or DEVSTACK_SMOKE_WORKSPACE_ROOT; "
                "legacy CCSTACK_SMOKE_* aliases also accepted) to run live devstack "
                "workspace smoke tests"
            )

        if env_root:
            root = Path(env_root).expanduser().resolve()
            if not root.exists() or not root.is_dir():
                raise unittest.SkipTest(f"smoke root not found: {root}")
            cls.workspace = ccstack.Workspace(root, ccstack.setup_workspace_config(root))
            return

        registry = ccstack.WorkspaceRegistry.load()
        known = registry.entries()
        for entry in known:
            if entry["name"] == env_workspace:
                cls.workspace = ccstack.Workspace.load(argparse.Namespace(root=None, workspace=env_workspace))
                return
        if not known:
            raise unittest.SkipTest("no registered workspace found")

        raise unittest.SkipTest(f"smoke workspace not found: {env_workspace}")

    def _service_port(self, workspace, service_name: str) -> int:
        service = workspace.service(service_name)
        port = service.get("port")
        if not port:
            self.skipTest(f"service port not defined: {service_name}")
        return int(port)

    def test_workspace_metadata_is_visible(self):
        workspace = self.workspace
        services = workspace.services

        if not services:
            self.fail("workspace has no parsed services")

        self.assertIn("apps.fixity-contents.content", services)
        self.assertIn("apps.fixity-contents.shorturl", services)
        self.assertIn("apps.proxy.contents", services)
        self.assertIn("apps.proxy.redirect", services)

        self.assertIn("ccstack.infra.mssql", services)
        self.assertIn("ccstack.infra.redis", services)
        self.assertIn("ccstack.infra.opensearch", services)
        self.assertIn("ccstack.infra.mock-http", services)

    def test_health_endpoints_are_reachable(self):
        workspace = self.workspace
        health_checks = [
            ("apps.fixity-contents.admin", "/actuator/health"),
            ("apps.fixity-contents.auction", "/actuator/health"),
            ("apps.fixity-contents.banner", "/actuator/health"),
            ("apps.fixity-contents.comment", "/actuator/health"),
            ("apps.fixity-contents.config", "/actuator/health"),
            ("apps.fixity-contents.content", "/actuator/health"),
            ("apps.fixity-contents.interaction", "/actuator/health"),
            ("apps.fixity-contents.member", "/actuator/health"),
            ("apps.fixity-contents.moderation", "/actuator/health"),
            ("apps.fixity-contents.qr", "/actuator/health"),
            ("apps.fixity-contents.seo", "/actuator/health"),
            ("apps.fixity-contents.shorturl", "/actuator/health"),
            ("apps.proxy.contents", "/actuator/health"),
            ("apps.proxy.redirect", "/actuator/health"),
        ]

        for service_name, path in health_checks:
            with self.subTest(service=service_name):
                if service_name not in workspace.services:
                    self.skipTest(f"service not configured: {service_name}")
                port = self._service_port(workspace, service_name)
                host = _smoke_env("HOST", "127.0.0.1")
                url = f"http://{host}:{port}{path}"
                status, _, body = _read_http(url)
                self.assertEqual(status, 200, f"{service_name} health failed: {status}, body={body[:200]}")

    def test_api_calls_and_status_surface(self):
        workspace = self.workspace
        host = _smoke_env("HOST", "127.0.0.1")

        service_endpoints = [
            ("apps.fixity-contents.content", "GET", "/internal/contents/list", None),
            ("apps.fixity-contents.content", "GET", "/internal/contents/detail?contentId=1", None),
            ("apps.fixity-contents.content", "GET", "/internal/contents/count", None),
            ("apps.fixity-contents.member", "GET", "/internal/members/batch?memberIds=1,2", None),
            ("apps.proxy.contents", "POST", "/graphql", '{"query":"query { contentsCount(input: { excludeDeleted: true, excludeHidden: true }) }"}'),
        ]
        expected_body_fragments = {
            ("apps.fixity-contents.content", "/internal/contents/list"): "ccstack local content",
            ("apps.fixity-contents.content", "/internal/contents/detail?contentId=1"): "ccstack local content",
            ("apps.fixity-contents.content", "/internal/contents/count"): "1",
            ("apps.proxy.contents", "/graphql"): '"contentsCount":1',
        }

        for service_name, method, path, body in service_endpoints:
            with self.subTest(service=service_name, path=path, method=method):
                if service_name not in workspace.services:
                    self.skipTest(f"service not configured: {service_name}")
                port = self._service_port(workspace, service_name)
                url = f"http://{host}:{port}{path}"
                status, _, body_text = _read_http(url, body=body, method=method)
                auth_hint = ""
                if status == 401 and not _smoke_env("BASIC_AUTH").strip():
                    auth_hint = "; set DEVSTACK_SMOKE_BASIC_AUTH=user:password (or legacy CCSTACK_SMOKE_BASIC_AUTH) for Basic Auth protected endpoints"
                self.assertNotEqual(
                    status,
                    0,
                    f"{service_name} API call failed (unreachable): {url}, error={body_text[:200]}",
                )
                self.assertGreaterEqual(
                    status,
                    200,
                    f"{service_name} API call returned non-2xx/3xx status {status}: {body_text[:200]}",
                )
                self.assertLessEqual(
                    status,
                    399,
                    f"{service_name} API call returned non-2xx/3xx status {status}{auth_hint}: {body_text[:200]}",
                )
                expected_fragment = expected_body_fragments.get((service_name, path))
                if expected_fragment:
                    self.assertIn(
                        expected_fragment,
                        body_text,
                        f"{service_name} API call returned an unexpected empty/local-data response: {body_text[:300]}",
                    )

    def test_ports_are_bound(self):
        workspace = self.workspace
        host = _smoke_env("HOST", "127.0.0.1")
        required_ports = [
            18084,
            18081,
            18082,
            18083,
            8080,
            8081,
        ]

        for port in required_ports:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                result = sock.connect_ex((host, port))
                self.assertEqual(result, 0, f"port not open: {host}:{port}")
