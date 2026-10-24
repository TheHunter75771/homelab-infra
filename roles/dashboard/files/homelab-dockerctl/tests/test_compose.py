"""Pinned images from the deployed compose files (rendered from roles/stacks/templates; the
fixtures are those templates rendered with placeholder identifiers)."""

from __future__ import annotations

import os
import textwrap
import unittest

from fakes import FIXTURES, IMAGES, Env, dockerctl

try:
    import yaml  # only for the cross-check against a real YAML parser; the helper never uses it
except ImportError:  # pragma: no cover - CI installs pyyaml, the host does not need it
    yaml = None

parse = dockerctl.parse_compose


class Fixtures(unittest.TestCase):
    def test_media_stack_pins_come_through_anchors_and_merge_keys(self) -> None:
        cf = parse((FIXTURES / "stacks/media/compose.yaml").read_text())
        self.assertEqual(cf.project, "media")
        self.assertEqual(sorted(cf.services), ["jellyfin", "jellyseerr", "prowlarr", "radarr", "sabnzbd", "sonarr"])
        self.assertEqual(cf.services["sonarr"].image, IMAGES["sonarr"])
        for name, svc in cf.services.items():
            with self.subTest(service=name):
                self.assertEqual(svc.container_name, name)
                self.assertFalse(svc.build)
                self.assertEqual(svc.labels.get("homelab.watch"), "true", "the label comes from <<: *common")
                self.assertRegex(svc.image, r"@sha256:[0-9a-f]{64}$")

    def test_local_build_is_detected_and_build_labels_are_not_service_labels(self) -> None:
        cf = parse((FIXTURES / "stacks/utils/compose.yaml").read_text())
        svc = cf.services["wol-relay"]
        self.assertTrue(svc.build)
        self.assertEqual(svc.image, IMAGES["wol-relay"])
        self.assertEqual(svc.labels, {"homelab.watch": "true"})
        self.assertNotIn("homelab.source-hash", svc.labels)

    def test_edge_stack(self) -> None:
        cf = parse((FIXTURES / "stacks/edge/compose.yaml").read_text())
        self.assertEqual(cf.services["cloudflared"].image, IMAGES["cloudflared"])
        self.assertEqual(cf.services["cloudflared"].labels, {"homelab.watch": "true"})

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_agrees_with_a_real_yaml_parser(self) -> None:
        for path in sorted(FIXTURES.glob("stacks/*/compose.yaml")):
            with self.subTest(path=path.parent.name):
                text = path.read_text()
                ref = yaml.safe_load(text)
                ours = parse(text)
                self.assertEqual(ours.project, ref.get("name"))
                self.assertEqual(set(ours.services), set(ref["services"]))
                for name, svc in ref["services"].items():
                    labels = svc.get("labels") or {}
                    if isinstance(labels, list):
                        labels = dict(entry.split("=", 1) for entry in labels)
                    self.assertEqual(ours.services[name].image, svc.get("image"))
                    self.assertEqual(ours.services[name].container_name, svc.get("container_name"))
                    self.assertEqual(ours.services[name].build, "build" in svc)
                    self.assertEqual(ours.services[name].labels, {k: str(v) for k, v in labels.items()})


class Syntax(unittest.TestCase):
    def doc(self, text: str):
        return parse(textwrap.dedent(text))

    def test_explicit_keys_beat_merged_ones_and_lists_of_merges(self) -> None:
        cf = self.doc("""\
            x-a: &a
              image: "a:1"
              labels:
                homelab.watch: "true"
            x-b: &b
              container_name: from-b
              image: "b:1"
            services:
              app:
                <<: [*a, *b]
                image: "own:2" # explicit wins
        """)
        svc = cf.services["app"]
        self.assertEqual(svc.image, "own:2")
        self.assertEqual(svc.container_name, "from-b")
        self.assertEqual(svc.labels, {"homelab.watch": "true"})

    def test_label_forms(self) -> None:
        cf = self.doc("""\
            services:
              listed:
                image: x:1
                labels:
                  - "homelab.watch=true"
                  - other=1
              compact:
                image: x:1
                labels:
                - homelab.watch=true
              flow-map:
                image: x:1
                labels: { homelab.watch: "true", a: b }
              flow-list:
                image: x:1
                labels: ["homelab.watch=true"]
              plain-bool:
                image: x:1
                labels:
                  homelab.watch: true
              unwatched:
                image: x:1
                labels:
                  homelab.watch: "false"
        """)
        for name in ("listed", "compact", "flow-map", "flow-list", "plain-bool"):
            with self.subTest(name=name):
                self.assertEqual(cf.services[name].labels.get("homelab.watch"), "true")
        self.assertEqual(cf.services["listed"].labels["other"], "1")
        self.assertEqual(cf.services["unwatched"].labels["homelab.watch"], "false")

    def test_comments_quotes_and_documents(self) -> None:
        cf = self.doc("""\
            ---
            # image: "commented:out"
            name: 'demo'
            services:
              a:
                image: "repo/a:1.0@sha256:abc" # trailing comment
                container_name: 'it''s-fine'
              b:
                # image: nope
                image: repo/b:2 # plain with comment
                command: ["sh", "-c", "echo '# not a comment'"]
              "c":
                image: "with # hash"
        """)
        self.assertEqual(cf.project, "demo")
        self.assertEqual(cf.services["a"].image, "repo/a:1.0@sha256:abc")
        self.assertEqual(cf.services["a"].container_name, "it's-fine")
        self.assertEqual(cf.services["b"].image, "repo/b:2")
        self.assertEqual(cf.services["c"].image, "with # hash")

    def test_tabs_are_refused(self) -> None:
        with self.assertRaises(dockerctl.ComposeError):
            parse("services:\n\tapp:\n\t\timage: x\n")

    def test_unknown_alias_is_refused(self) -> None:
        with self.assertRaises(dockerctl.ComposeError):
            parse("services:\n  app:\n    <<: *missing\n    image: x\n")


class Declared(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Env()

    def tearDown(self) -> None:
        self.env.close()

    def test_declared_services_from_the_stacks_dir(self) -> None:
        declared = {d.name: d for d in self.env.helper.declared()}
        self.assertEqual(sorted(declared), ["cloudflared", "jellyfin", "jellyseerr", "prowlarr", "radarr", "sabnzbd",
                                            "sonarr", "wol-relay"])
        sonarr = declared["sonarr"]
        self.assertEqual((sonarr.stack, sonarr.service, sonarr.pinned, sonarr.local_build, sonarr.watch),
                         ("media", "sonarr", IMAGES["sonarr"], False, True))
        self.assertEqual(sonarr.compose_file, str(self.env.stacks / "media/compose.yaml"))
        self.assertTrue(declared["wol-relay"].local_build)

    def test_default_container_name(self) -> None:
        d = self.env.stacks / "extra"
        d.mkdir()
        (d / "compose.yaml").write_text("services:\n  web:\n    image: nginx:1\n")
        os.chmod(d / "compose.yaml", 0o644)
        names = [x.name for x in self.env.helper.declared()]
        self.assertIn("extra-web-1", names)

    def test_untrusted_files_are_ignored(self) -> None:
        media = self.env.stacks / "media/compose.yaml"
        os.chmod(media, 0o664)
        names = {d.name for d in self.env.helper.declared()}
        self.assertNotIn("sonarr", names)
        self.assertIn("cloudflared", names)
        self.assertIn("not a root-owned regular file", self.env.log)

    def test_symlinked_compose_is_ignored(self) -> None:
        d = self.env.stacks / "linked"
        d.mkdir()
        os.symlink(self.env.stacks / "edge/compose.yaml", d / "compose.yaml")
        stacks = {x.stack for x in self.env.helper.declared()}
        self.assertNotIn("linked", stacks)

    def test_wrong_owner_ignores_everything(self) -> None:
        self.env.close()
        self.env = Env(trusted_owner_uid=os.getuid() + 1)
        self.assertEqual(self.env.helper.declared(), [])
        self.assertIn("not root-owned", self.env.log)

    def test_broken_file_does_not_hide_the_others(self) -> None:
        bad = self.env.stacks / "broken"
        bad.mkdir()
        (bad / "compose.yaml").write_text("services:\n\tapp: x\n")
        os.chmod(bad / "compose.yaml", 0o644)
        names = {d.name for d in self.env.helper.declared()}
        self.assertIn("sonarr", names)
        self.assertIn("skipping", self.env.log)

    def test_odd_stack_dir_names_are_skipped(self) -> None:
        d = self.env.stacks / "Bad Name"
        d.mkdir()
        (d / "compose.yaml").write_text("services:\n  web:\n    image: nginx:1\n    container_name: web\n")
        self.assertNotIn("web", {x.name for x in self.env.helper.declared()})

    def test_ps_reports_running_and_pinned_image_without_inspect_internals(self) -> None:
        outcome, lines = self.env.call({"op": "ps"})
        self.assertEqual(outcome, "ok")
        data = lines[0]["data"]
        self.assertEqual(data["docker"], "active")
        by_name = {c["name"]: c for c in data["containers"]}
        self.assertEqual(sorted(by_name), ["cloudflared", "jellyfin", "sonarr", "wol-relay"], "only labelled containers")
        sonarr = by_name["sonarr"]
        self.assertEqual(set(sonarr), {"name", "state", "health", "started_at", "restart_count", "image", "pinned",
                                       "stack"})
        self.assertEqual(sonarr["image"], IMAGES["sonarr-old"])
        self.assertEqual(sonarr["pinned"], IMAGES["sonarr"])
        self.assertEqual(sonarr["stack"], "media")
        self.assertEqual(sonarr["health"], "healthy")
        self.assertIsNone(by_name["cloudflared"]["health"])
        raw = str(lines)
        for secret in ("do-not-leak", "API_KEY", "Mounts", "/config", "probe output"):
            self.assertNotIn(secret, raw)
        wol = next(d for d in data["declared"] if d["name"] == "wol-relay")
        self.assertEqual(wol, {"name": "wol-relay", "stack": "utils", "service": "wol-relay",
                               "pinned": IMAGES["wol-relay"], "local_build": True})


if __name__ == "__main__":
    unittest.main()
