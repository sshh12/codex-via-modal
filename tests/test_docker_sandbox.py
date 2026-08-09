"""Tests for the local Docker sandbox mode. None of these start a container."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

from codex_modal.codex_config import ModelSettings
from codex_modal.container_entry import (
    ENV_DOC_BEGIN,
    ENV_DOC_END,
    _environment_doc,
    _write_environment_doc,
    build_command,
)
from codex_modal.docker import SandboxOptions, SandboxSpec
from codex_modal.docker.sandbox import ASSETS, _write_run_spec
from codex_modal.options import codex_sets_own_policy, parse_arguments


def _load_egress_proxy():
    path = ASSETS / "egress_proxy.py"
    spec = importlib.util.spec_from_file_location("codex_modal_egress_proxy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


egress = _load_egress_proxy()


class AddressPolicyTests(unittest.TestCase):
    def test_public_addresses_are_allowed(self) -> None:
        for address in ("1.1.1.1", "140.82.116.5", "2606:4700:4700::1111"):
            self.assertTrue(
                egress.Policy.address_is_public(address), f"{address} should be public"
            )

    def test_host_lan_loopback_and_container_ranges_are_refused(self) -> None:
        blocked = (
            "127.0.0.1",  # loopback
            "192.168.68.58",  # the operator's own wifi
            "192.168.1.1",
            "10.160.210.1",  # the sandbox's own bridge gateway
            "172.17.0.1",  # the default Docker bridge / host gateway
            "169.254.169.254",  # cloud metadata
            "100.64.0.1",  # carrier-grade NAT
            "0.0.0.0",
            "224.0.0.1",  # multicast
            "::1",
            "fdc4:f303:9324::254",  # what host.docker.internal resolves to here
            "fe80::1",
            "::ffff:192.168.68.58",  # IPv4-mapped private address
            "not-an-address",
        )
        for address in blocked:
            self.assertFalse(
                egress.Policy.address_is_public(address), f"{address} must be refused"
            )

    def test_host_allow_list_matches_exact_names_and_suffixes(self) -> None:
        policy = egress.Policy(frozenset({443}), ("api.github.com", ".openai.com"))
        self.assertTrue(policy.host_allowed("api.github.com"))
        self.assertTrue(policy.host_allowed("API.GitHub.com"))
        self.assertTrue(policy.host_allowed("openai.com"))
        self.assertTrue(policy.host_allowed("cdn.openai.com"))
        self.assertFalse(policy.host_allowed("github.com"))
        self.assertFalse(policy.host_allowed("notopenai.com"))
        self.assertFalse(policy.host_allowed("evil.com"))

    def test_empty_allow_list_permits_any_public_host(self) -> None:
        policy = egress.Policy(frozenset({443}), ())
        self.assertTrue(policy.host_allowed("anything.example"))


class RequestRewritingTests(unittest.TestCase):
    def _head(self, extra: list[tuple[str, str]]):
        headers = {name.lower(): value for name, value in extra}
        return egress.RequestHead("POST", "/v1/responses", "HTTP/1.1", headers, extra, b"")

    def test_sandbox_authorization_is_replaced_not_appended(self) -> None:
        head = self._head(
            [
                ("Host", "10.0.0.2:8081"),
                ("Authorization", "Bearer sandbox-forged-token"),
                ("Proxy-Authorization", "Basic abc"),
                ("Content-Type", "application/json"),
            ]
        )
        raw = egress.rebuild_request(
            head,
            target="/v1/responses",
            host_header="inference.us-west.modal.direct",
            extra_headers=(("Authorization", "Bearer wk-real.ws-secret"),),
        ).decode()
        self.assertNotIn("sandbox-forged-token", raw)
        self.assertNotIn("Proxy-Authorization", raw)
        self.assertEqual(raw.count("Authorization:"), 1)
        self.assertIn("Authorization: Bearer wk-real.ws-secret", raw)
        self.assertIn("Host: inference.us-west.modal.direct", raw)
        self.assertIn("Content-Type: application/json", raw)

    def test_connection_close_is_forced_so_injection_applies_per_request(self) -> None:
        head = self._head([("Host", "x"), ("Connection", "keep-alive")])
        raw = egress.rebuild_request(head, target="/", host_header="x").decode()
        self.assertIn("Connection: close", raw)
        self.assertNotIn("keep-alive", raw)

    def test_missing_host_header_is_synthesised(self) -> None:
        head = self._head([("Content-Length", "2")])
        raw = egress.rebuild_request(head, target="/", host_header="upstream.example").decode()
        self.assertIn("Host: upstream.example", raw)


class ModelProxyRoutingTests(unittest.TestCase):
    def test_local_prefix_is_swapped_for_the_upstream_base_path(self) -> None:
        proxy = egress.ModelProxy(
            "https://inference.us-west.modal.direct/v1", "Bearer t", "/v1"
        )
        self.assertEqual(proxy.upstream_target("/v1/responses"), "/v1/responses")
        self.assertEqual(proxy.upstream_target("/v1/models?limit=1"), "/v1/models?limit=1")
        self.assertEqual(proxy.port, 443)
        self.assertEqual(proxy.host, "inference.us-west.modal.direct")

    def test_upstream_with_a_deeper_base_path(self) -> None:
        proxy = egress.ModelProxy("http://upstream.example:9000/openai/v1", None, "/v1")
        self.assertEqual(
            proxy.upstream_target("/v1/responses"), "/openai/v1/responses"
        )
        self.assertEqual(proxy.port, 9000)

    def test_non_http_upstream_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            egress.ModelProxy("ftp://upstream.example/v1", None, "/v1")


class PortParsingTests(unittest.TestCase):
    def test_ports_are_parsed_and_validated(self) -> None:
        self.assertEqual(egress.parse_ports("80, 443 ,8080"), frozenset({80, 443, 8080}))
        with self.assertRaises(ValueError):
            egress.parse_ports("70000")


class DangerFlagPlacementTests(unittest.TestCase):
    PREFIX = ["codex", "--profile", "p", "--strict-config"]

    def test_flag_goes_after_a_subcommand_that_defines_it(self) -> None:
        command = build_command(list(self.PREFIX), ["exec", "do a thing"], danger=True)
        self.assertEqual(
            command[-3:], ["exec", "--dangerously-bypass-approvals-and-sandbox", "do a thing"]
        )

    def test_flag_goes_before_a_bare_prompt(self) -> None:
        command = build_command(list(self.PREFIX), ["write a haiku"], danger=True)
        self.assertEqual(
            command[-2:], ["--dangerously-bypass-approvals-and-sandbox", "write a haiku"]
        )

    def test_config_guards_accompany_the_flag(self) -> None:
        command = build_command(list(self.PREFIX), ["exec", "x"], danger=True)
        self.assertIn('approval_policy="never"', command)
        self.assertIn('sandbox_mode="danger-full-access"', command)

    def test_nothing_is_added_when_the_caller_chose_a_policy(self) -> None:
        command = build_command(list(self.PREFIX), ["-s", "workspace-write"], danger=False)
        self.assertEqual(command, self.PREFIX + ["-s", "workspace-write"])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)


class PolicyDetectionTests(unittest.TestCase):
    def test_explicit_codex_policy_options_are_detected(self) -> None:
        for arguments in (
            ["-s", "read-only"],
            ["--sandbox=workspace-write"],
            ["-a", "never"],
            ["--ask-for-approval", "untrusted"],
            ["--approve-for-me"],
            ["--dangerously-bypass-approvals-and-sandbox"],
        ):
            self.assertTrue(codex_sets_own_policy(arguments), arguments)

    def test_ordinary_arguments_leave_the_default_in_place(self) -> None:
        for arguments in ([], ["exec", "hello"], ["--json"], ["resume", "--last"]):
            self.assertFalse(codex_sets_own_policy(arguments), arguments)


class OptionParsingTests(unittest.TestCase):
    def test_docker_options_require_docker_mode(self) -> None:
        with self.assertRaises(ValueError):
            parse_arguments(["--docker-keep"])
        with self.assertRaises(ValueError):
            parse_arguments(["--docker-allow-port", "8080"])

    def test_upstream_mode_cannot_be_combined_with_modal_provisioning(self) -> None:
        with self.assertRaises(ValueError):
            parse_arguments(
                [
                    "--docker",
                    "--docker-upstream",
                    "http://x/v1",
                    "--modal-self-managed",
                    "--modal-gpu",
                    "H200:2",
                ]
            )

    def test_repeated_options_accumulate(self) -> None:
        options = parse_arguments(
            [
                "--docker",
                "--docker-allow-port",
                "8080",
                "--docker-allow-port",
                "8443",
                "--docker-allow-host",
                ".openai.com",
            ]
        )
        self.assertEqual(options.docker_allow_ports, [8080, 8443])
        self.assertEqual(options.docker_allow_hosts, [".openai.com"])

    def test_modal_options_still_pass_through_in_docker_mode(self) -> None:
        options = parse_arguments(
            ["--docker", "--modal-use-app", "my-app", "--modal-context-window", "262144"]
        )
        self.assertTrue(options.docker)
        self.assertEqual(options.use_app, "my-app")
        self.assertEqual(options.context_window, 262144)

    def test_codex_arguments_are_untouched(self) -> None:
        options = parse_arguments(["--docker", "exec", "--json", "a prompt"])
        self.assertEqual(options.codex_arguments, ["exec", "--json", "a prompt"])


class RunSpecTests(unittest.TestCase):
    def _settings(self) -> ModelSettings:
        return ModelSettings(
            slug="deepseek-ai/model",
            display_model="deepseek-ai/model",
            context_window=262144,
            reasoning_effort="high",
            reasoning_levels=("low", "high"),
            provider_base_url="https://inference.us-west.modal.direct/v1",
            persist_history=True,
        )

    def test_the_sandbox_is_pointed_at_the_broker_not_the_real_upstream(self) -> None:
        import tempfile

        settings = self._settings()
        spec = SandboxSpec(
            settings=settings,
            upstream_url=settings.provider_base_url,
            upstream_authorization="Bearer wk-real.ws-secret",
            codex_arguments=["exec", "hi"],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = _write_run_spec(
                Path(directory), spec, SandboxOptions(), "10.99.0.2"
            )
            document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["provider_base_url"], "http://10.99.0.2:8081/v1")
        self.assertEqual(document["workspace"], "/work")
        self.assertTrue(document["danger"])
        # The credential must never appear in anything copied into the sandbox.
        serialized = json.dumps(document)
        self.assertNotIn("wk-real", serialized)
        self.assertNotIn("modal.direct", serialized)


class EnvironmentDocTests(unittest.TestCase):
    def test_env_note_is_included(self) -> None:
        doc = _environment_doc({"env_note": "PREFER_UV_XYZ", "allow_ports": [80, 443]})
        self.assertIn("PREFER_UV_XYZ", doc)
        self.assertIn("Chromium", doc)
        self.assertIn(ENV_DOC_BEGIN, doc)
        self.assertIn(ENV_DOC_END, doc)

    def test_allow_hosts_are_reflected(self) -> None:
        doc = _environment_doc({"allow_hosts": [".openai.com"], "allow_ports": [443]})
        self.assertIn(".openai.com", doc)

    def test_write_creates_agents_md(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            _write_environment_doc(workspace, {"allow_ports": [80, 443]})
            text = (workspace / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn(ENV_DOC_BEGIN, text)

    def test_existing_agents_md_is_preserved_and_our_block_appended(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "AGENTS.md").write_text("USER_RULE_123\n", encoding="utf-8")
            _write_environment_doc(workspace, {"allow_ports": [80, 443]})
            text = (workspace / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("USER_RULE_123", text)
            self.assertIn(ENV_DOC_BEGIN, text)

    def test_rewriting_does_not_duplicate_our_block(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            _write_environment_doc(workspace, {"env_note": "N1", "allow_ports": [80]})
            _write_environment_doc(workspace, {"env_note": "N2", "allow_ports": [80]})
            text = (workspace / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(text.count(ENV_DOC_BEGIN), 1)
            self.assertIn("N2", text)
            self.assertNotIn("N1", text)


class DockerEnvOptionTests(unittest.TestCase):
    def test_env_note_and_disable_flag_parse(self) -> None:
        options = parse_arguments(
            ["--docker", "--docker-env-note", "hello", "--docker-no-env-doc"]
        )
        self.assertEqual(options.docker_env_note, "hello")
        self.assertTrue(options.docker_no_env_doc)

    def test_env_options_require_docker(self) -> None:
        with self.assertRaises(ValueError):
            parse_arguments(["--docker-env-note", "x"])
        with self.assertRaises(ValueError):
            parse_arguments(["--docker-no-env-doc"])


if __name__ == "__main__":
    unittest.main()
