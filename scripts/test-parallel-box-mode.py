#!/usr/bin/env python3
"""pbox must show how a box is bought. With parallel_box_spot=false the launch template carries no spot options and
every launch is ON-DEMAND (~3x); pbox used to announce Spot capacity and print only the spot price regardless.
Offline: the real scripts/parallel-box.sh against a fake `aws` and `ssh` on PATH."""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "parallel-box.sh"

FAKE_AWS = r'''#!/usr/bin/env python3
import os, sys
a = " ".join(sys.argv[1:])
mode = os.environ["FAKE_MARKET"]          # spot | none | broken
running = os.environ.get("FAKE_RUNNING") == "1"
life = os.environ.get("FAKE_LIFECYCLE", "None")
if "describe-launch-template-versions" in a:
    if mode == "broken":
        sys.exit(254)
    print("spot" if mode == "spot" else "None")
elif "describe-instances" in a and "InstanceLifecycle" in a:
    print(life)
elif "describe-instances" in a and "PublicIpAddress" in a:
    print("203.0.113.9" if running else "None")
elif "describe-instances" in a and "InstanceType" in a:
    print("c8g.48xlarge")
elif "describe-instances" in a:
    print("None")
elif "describe-volumes" in a and "AvailabilityZone" in a:
    print("us-west-2d")
elif "describe-volumes" in a:
    print("vol-1\t500\tavailable")
elif "describe-instance-types" in a:
    print("192")
elif "describe-spot-price-history" in a:
    print("2.2198")
elif "run-instances" in a:
    print("An error occurred (InsufficientInstanceCapacity) when calling the RunInstances operation")
    sys.exit(254)
else:
    print("None")
'''


class PboxMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for name, body in (("aws", FAKE_AWS), ("ssh", "#!/bin/sh\nexit 255\n")):
            p = Path(self.tmp) / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IXUSR)

    def pbox(self, *args, market="spot", running=False, lifecycle="None"):
        env = dict(os.environ, PATH=self.tmp + ":" + os.environ["PATH"], FAKE_MARKET=market,
                   FAKE_RUNNING="1" if running else "0", FAKE_LIFECYCLE=lifecycle, PARALLEL_BOX_TYPES="c8g.48xlarge",
                   HOME=self.tmp)
        r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)
        return r.stdout + r.stderr

    def test_up_on_demand_says_so_and_does_not_present_the_spot_price_as_the_cost(self):
        out = self.pbox("up", market="none")
        self.assertIn("PURCHASE MODE: ON-DEMAND", out)
        self.assertIn("about 3x spot", out)
        self.assertIn("spot reference", out)
        self.assertIn("NOT what you pay", out)
        self.assertNotIn("Spot capacity for 192-core", out)
        self.assertIn("no on-demand capacity", out)
        self.assertNotIn("no spot capacity", out)

    def test_up_spot_is_unchanged_in_spirit(self):
        out = self.pbox("up", market="spot")
        self.assertIn("PURCHASE MODE: spot", out)
        self.assertNotIn("ON-DEMAND", out)
        self.assertIn("no spot capacity", out)
        self.assertNotIn("spot reference", out)

    def test_up_says_unknown_rather_than_guessing_when_the_template_cannot_be_read(self):
        out = self.pbox("up", market="broken")
        self.assertIn("PURCHASE MODE: unknown", out)
        self.assertNotIn("PURCHASE MODE: spot", out)

    def test_status_of_a_running_box_says_how_it_was_bought(self):
        self.assertIn("(c8g.48xlarge, spot)", self.pbox("status", "1", running=True, lifecycle="spot"))
        out = self.pbox("status", "1", running=True, lifecycle="None", market="none")
        self.assertIn("ON-DEMAND (about 3x spot)", out)

    def test_status_always_shows_how_the_next_launch_will_be_bought(self):
        down_od = self.pbox("status", "1", market="none")
        self.assertIn("state:  down", down_od)
        self.assertIn("next launch: ON-DEMAND", down_od)
        self.assertIn("parallel_box_spot=true", down_od)
        self.assertIn("next launch: spot", self.pbox("status", "1", market="spot"))
        self.assertIn("next launch: unknown", self.pbox("status", "1", market="broken"))

    def test_status_of_both_boxes_labels_each(self):
        out = self.pbox("status", market="none")
        self.assertEqual(out.count("next launch: ON-DEMAND"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
