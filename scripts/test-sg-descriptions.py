#!/usr/bin/env python3
"""EC2 accepts only a-zA-Z0-9, space and ._-:/()#,@[]+=&;{}!$* in security group and rule
descriptions, and rejects anything else at apply time, after the rest of the plan has already
been applied. An apostrophe in one rule's description did exactly that. Check them all here."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TYPES = ("aws_security_group", "aws_vpc_security_group_ingress_rule",
         "aws_vpc_security_group_egress_rule", "aws_security_group_rule")
ALLOWED = re.compile(r"^[a-zA-Z0-9. _\-:/()#,@\[\]+=&;{}!$*]*$")


def descriptions():
    for path in sorted(ROOT.glob("*.tf")):
        text = path.read_text()
        for m in re.finditer(r'resource "(%s)" "([^"]+)" \{(.*?)\n\}' % "|".join(TYPES), text, re.S):
            # description = "..." at any depth (inline ingress/egress blocks included)
            for d in re.findall(r'description\s*=\s*"((?:[^"\\]|\\.)*)"', m.group(3)):
                yield path.name, m.group(2), d


class SecurityGroupDescriptionTests(unittest.TestCase):
    def test_every_description_uses_only_characters_ec2_accepts(self):
        seen = 0
        for name, resource, text in descriptions():
            seen += 1
            literal = re.sub(r"\$\{[^}]*\}", "", text)   # interpolations are checked where they are set
            self.assertRegex(literal, ALLOWED, f"{name} {resource}: {text!r}")
            self.assertLess(len(text), 256, f"{name} {resource}")
        self.assertGreater(seen, 20, "the scan found too few descriptions to be looking in the right place")

    def test_the_check_catches_an_apostrophe(self):
        self.assertIsNone(ALLOWED.match("engine's own IP"))


if __name__ == "__main__":
    unittest.main()
