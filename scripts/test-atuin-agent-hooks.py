"""Offline checks for scripts/atuin-agent-hooks.py, using the hook shapes Atuin writes."""
import copy
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

spec = importlib.util.spec_from_file_location("atuin_agent_hooks", Path(__file__).with_name("atuin-agent-hooks.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

BIN = "/home/colton/.atuin/bin/atuin"
EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")


def codex_doc(command, copies=1):
    return {"hooks": {event: [{"hooks": [{"command": command, "type": "command"}], "matcher": "^Bash$"}
                              for _ in range(copies)] for event in EVENTS}}


def commands(doc, event):
    return [hook["command"] for entry in doc["hooks"][event] for hook in entry["hooks"]]


class NormalizeTests(unittest.TestCase):
    def test_duplicates_collapse_to_one_absolute_hook_per_event(self):
        doc = codex_doc(BIN + " hook codex", copies=30)
        self.assertTrue(module.normalize(doc, "codex", BIN))
        for event in EVENTS:
            self.assertEqual(doc["hooks"][event],
                             [{"hooks": [{"command": BIN + " hook codex", "type": "command"}], "matcher": "^Bash$"}])

    def test_bare_hook_is_pointed_at_the_absolute_binary(self):
        doc = codex_doc("atuin hook codex")
        self.assertTrue(module.normalize(doc, "codex", BIN))
        self.assertEqual(commands(doc, "PreToolUse"), [BIN + " hook codex"])

    def test_fresh_bare_copy_next_to_a_normalized_one_is_dropped(self):
        doc = codex_doc(BIN + " hook codex")
        for event in EVENTS:
            doc["hooks"][event].append({"hooks": [{"command": "atuin hook codex", "type": "command"}],
                                        "matcher": "^Bash$"})
        self.assertTrue(module.normalize(doc, "codex", BIN))
        for event in EVENTS:
            self.assertEqual(commands(doc, event), [BIN + " hook codex"])

    def test_normalizing_twice_changes_nothing_the_second_time(self):
        doc = codex_doc("atuin hook codex", copies=3)
        module.normalize(doc, "codex", BIN)
        snapshot = copy.deepcopy(doc)
        self.assertFalse(module.normalize(doc, "codex", BIN))
        self.assertEqual(doc, snapshot)

    def test_other_hooks_settings_and_matchers_are_preserved(self):
        doc = {"model": "opus", "permissions": {"allow": ["Bash"]}, "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "atuin hook claude-code"},
                                              {"type": "command", "command": "/usr/local/bin/audit"}]},
                {"matcher": "Edit", "hooks": [{"type": "command", "command": "prettier --check"}]},
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "atuin hook claude-code"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "notify-done"}]}]}}
        self.assertTrue(module.normalize(doc, "claude-code", BIN))
        self.assertEqual(doc["model"], "opus")
        self.assertEqual(doc["permissions"], {"allow": ["Bash"]})
        self.assertEqual(doc["hooks"]["PreToolUse"], [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": BIN + " hook claude-code"},
                                          {"type": "command", "command": "/usr/local/bin/audit"}]},
            {"matcher": "Edit", "hooks": [{"type": "command", "command": "prettier --check"}]}])
        self.assertEqual(doc["hooks"]["Stop"], [{"hooks": [{"type": "command", "command": "notify-done"}]}])

    def test_without_the_binary_only_atuin_hooks_are_removed(self):
        doc = codex_doc("atuin hook codex", copies=2)
        doc["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "notify-done"}]}]
        self.assertTrue(module.normalize(doc, "codex", None))
        self.assertEqual(doc["hooks"], {"Stop": [{"hooks": [{"type": "command", "command": "notify-done"}]}]})

    def test_another_agents_hook_and_unusual_shapes_are_left_alone(self):
        doc = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "atuin hook codex"}]},
                                        "not-an-object", {"hooks": "not-a-list"}],
                         "Empty": [], "Odd": "not-a-list"}}
        snapshot = copy.deepcopy(doc)
        self.assertFalse(module.normalize(doc, "claude-code", BIN))
        self.assertEqual(doc, snapshot)
        self.assertFalse(module.normalize({"hooks": {}}, "codex", BIN))
        self.assertFalse(module.normalize({"model": "opus"}, "codex", BIN))


class MainTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, ".claude"))
        os.makedirs(os.path.join(self.home, ".codex"))

    def write(self, relative, content, mode=0o600):
        path = os.path.join(self.home, relative)
        with open(path, "w") as fh:
            fh.write(content)
        os.chmod(path, mode)
        return path

    def binary(self):
        path = os.path.join(self.home, ".atuin", "bin", "atuin")
        os.makedirs(os.path.dirname(path))
        self.write(".atuin/bin/atuin", "#!/bin/sh\n", 0o755)
        return path

    def run_main(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(module.main(["atuin-agent-hooks.py", self.home]), 0)
        return out.getvalue()

    def test_rewrites_both_configs_keeping_permissions(self):
        binary = self.binary()
        settings = self.write(".claude/settings.json", json.dumps({"model": "opus", "hooks": {
            "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "atuin hook claude-code"}]}]}}))
        hooks = self.write(".codex/hooks.json", json.dumps(codex_doc("atuin hook codex", copies=4)), 0o644)
        output = self.run_main()
        self.assertIn("normalized .claude/settings.json", output)
        self.assertIn("normalized .codex/hooks.json", output)
        self.assertEqual(commands(json.load(open(settings)), "PostToolUse"), [binary + " hook claude-code"])
        self.assertEqual(commands(json.load(open(hooks)), "PreToolUse"), [binary + " hook codex"])
        self.assertEqual(stat.S_IMODE(os.stat(settings).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(hooks).st_mode), 0o644)
        self.assertEqual(self.run_main(), "")

    def test_missing_or_malformed_configs_are_skipped_untouched(self):
        self.binary()
        broken = self.write(".codex/hooks.json", "{not json")
        self.assertEqual(self.run_main(), "")
        self.assertEqual(open(broken).read(), "{not json")
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "settings.json")))

    def test_without_atuin_installed_the_hooks_are_removed(self):
        hooks = self.write(".codex/hooks.json", json.dumps(codex_doc("atuin hook codex")))
        self.run_main()
        self.assertEqual(json.load(open(hooks)), {"hooks": {}})


if __name__ == "__main__":
    unittest.main()
