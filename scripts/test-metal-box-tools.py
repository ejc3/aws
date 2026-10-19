#!/usr/bin/env python3
"""metal-box-tools.tf: the metal boxes' box tools are kept equal to main by a daily association, not only at boot."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "metal-box-tools.tf").read_text()
SELFUPDATE = (ROOT / "dev-selfupdate.tf").read_text()


class MetalBoxTools(unittest.TestCase):
    def test_every_tool_the_boot_script_installs_is_synced(self):
        tools = dict(re.findall(r'^\s+([a-z-]+)\s+=\s+"([a-z-]+\.sh)"$', TF.split("metal_box_tools = {", 1)[1].split("}", 1)[0], re.M))
        self.assertEqual(tools, {"pbox": "parallel-box.sh", "gbox": "gpu-box.sh", "wbox": "wbox.sh",
                                 "codex-restart": "codex-restart.sh"})
        for name, script in tools.items():
            # the same script the boot path embeds into /usr/local/bin/<name>
            self.assertIn('base64 -d > /usr/local/bin/%s ' % name, SELFUPDATE)
            self.assertIn('scripts/%s"' % script, SELFUPDATE)
            self.assertTrue((ROOT / "scripts" / script).is_file(), script)

    def test_the_association_is_daily_checks_sha256_and_renames_into_place(self):
        assoc = TF.split('resource "aws_ssm_association" "metal_box_tools"', 1)[1]
        self.assertIn('schedule_expression = "rate(1 day)"', assoc)
        self.assertIn("values = local.metal_update_guard_instances", assoc)
        self.assertIn('filesha256("${path.module}/scripts/${script}")', assoc)
        self.assertIn("sha256sum -c --quiet", assoc)
        install = assoc.index('"install -m 755 \\"$dir/${name}\\" /usr/local/bin/${name}.new"')
        self.assertLess(assoc.index("sha256sum -c --quiet"), install, "every tool is checked before any is replaced")
        self.assertIn('"mv -f /usr/local/bin/${name}.new /usr/local/bin/${name}"', assoc)
        self.assertNotIn("&& mv", assoc, "install and mv are separate lines, so set -e stops on either")
        self.assertNotRegex(assoc, r"systemctl|reboot|restart ", "it writes files only")


if __name__ == "__main__":
    unittest.main(verbosity=2)
