#!/usr/bin/env python3
"""Offline checks for Security Hub ECS.5 (read-only root filesystems) on this account's ECS task definitions.

Every container of every `aws_ecs_task_definition` in this repository runs with `readonlyRootFilesystem = true`.
A container that needs to write gets a task volume mounted where it writes, never a writable root. imagine's one
writable path is /tmp: Fargate creates the volume root-owned 0755 and the server runs as uid 10001, so a first
container opens it (`chmod 1777 /tmp`, as root, nothing else) and the server waits for it to succeed."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASK_DEFS = {"games_mp_router": "games-multiplayer.tf", "imagine": "imagine.tf"}


def resource(path, name):
    text = (ROOT / path).read_text()
    start = text.index('resource "aws_ecs_task_definition" "%s" {' % name)
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError("unterminated resource %s" % name)


def containers(block):
    """The top-level objects of container_definitions = jsonencode([...]), as text."""
    body = block[block.index("container_definitions = jsonencode(["):]
    out, depth, start = [], 0, None
    for i, ch in enumerate(body):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                out.append(body[start:i + 1])
        elif ch == "]" and depth == 0:
            return out
    raise AssertionError("unterminated container_definitions")


def top_level_name(container):
    return re.search(r'^\s*name\s*=\s*"([^"]+)"', container[1:], re.M).group(1)


class EveryTaskDefinitionIsListed(unittest.TestCase):
    def test_no_task_definition_is_left_out(self):
        found = {}
        for tf in ROOT.glob("*.tf"):
            for name in re.findall(r'^resource "aws_ecs_task_definition" "([^"]+)"', tf.read_text(), re.M):
                found[name] = tf.name
        self.assertEqual(found, TASK_DEFS)


class ReadOnlyRoot(unittest.TestCase):
    def test_every_container_has_a_read_only_root(self):
        for name, path in TASK_DEFS.items():
            cs = containers(resource(path, name))
            self.assertTrue(cs, name)
            for c in cs:
                with self.subTest(task=name, container=top_level_name(c)):
                    self.assertRegex(c, r"\n\s*readonlyRootFilesystem\s*=\s*true\b")

    def test_router_needs_no_writable_mount(self):
        block = resource("games-multiplayer.tf", "games_mp_router")
        self.assertNotIn("mountPoints", block)
        self.assertNotIn("volume {", block)

    def test_imagine_tmp_is_a_task_volume_opened_by_a_first_container(self):
        block = resource("imagine.tf", "imagine")
        self.assertRegex(block, r'volume \{\s*name = "tmp"\s*\}')
        tmp, server = containers(block)
        self.assertEqual(top_level_name(tmp), "tmp")
        self.assertRegex(tmp, r"essential\s*=\s*false")
        self.assertRegex(tmp, r'user\s*=\s*"0"')
        self.assertIn('command                = ["chmod", "1777", "/tmp"]', tmp)
        self.assertEqual(top_level_name(server), "server")
        self.assertRegex(server, r"essential\s*=\s*true")
        self.assertNotRegex(server, r"\buser\s*=")
        self.assertIn('dependsOn              = [{ containerName = "tmp", condition = "SUCCESS" }]', server)
        mount = 'mountPoints            = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]'
        self.assertIn(mount, tmp)
        self.assertIn(mount, server)


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)
