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
    """Every description of a security group or rule, as the text EC2 will receive. A quoted
    literal is itself; `<block>.key` inside `dynamic "<block>"` whose for_each is an inline map
    yields each key. Any other expression is yielded as None, so the test fails until it is
    either made a literal or taught here how to resolve it."""
    for path in sorted(ROOT.glob("*.tf")):
        text = path.read_text()
        for m in re.finditer(r'resource "(%s)" "([^"]+)" \{(.*?)\n\}' % "|".join(TYPES), text, re.S):
            body = m.group(3)
            for d in re.findall(r'description\s*=\s*"((?:[^"\\]|\\.)*)"', body):
                yield path.name, m.group(2), d
            for expr in re.findall(r'description\s*=\s*([^"\s][^\n]*?)\s*$', body, re.M):
                keys = dynamic_keys(body, expr)
                if keys is None:
                    yield path.name, m.group(2), None
                for key in keys or []:
                    yield path.name, m.group(2), key


def dynamic_keys(body, expr):
    """The keys `expr` (`<block>.key`) takes, from its dynamic block's inline for_each map."""
    k = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\.key", expr)
    if not k:
        return None
    each = re.search(r'dynamic "%s" \{\s*for_each\s*=\s*\{([^\n]*)\}' % k.group(1), body)
    if not each:
        return None
    return re.findall(r'(?:^|,)\s*"?([^"=,\s]+)"?\s*=', each.group(1))


class SecurityGroupDescriptionTests(unittest.TestCase):
    def test_every_description_uses_only_characters_ec2_accepts(self):
        seen = 0
        for name, resource, text in descriptions():
            seen += 1
            self.assertIsNotNone(text, f"{name} {resource}: a computed description this test cannot resolve; "
                                       "make it a literal or teach descriptions() its values")
            literal = re.sub(r"\$\{[^}]*\}", "", text)   # interpolations are checked where they are set
            self.assertRegex(literal, ALLOWED, f"{name} {resource}: {text!r}")
            self.assertLess(len(text), 256, f"{name} {resource}")
        self.assertGreater(seen, 20, "the scan found too few descriptions to be looking in the right place")

    def test_dynamic_block_keys_are_resolved_and_checked(self):
        body = 'dynamic "egress" {\n    for_each = { https = ["tcp", 443], dns-udp = ["udp", 53] }\n    content {\n      description = egress.key\n'
        self.assertEqual(dynamic_keys(body, "egress.key"), ["https", "dns-udp"])
        self.assertIsNone(dynamic_keys(body, "var.something"))
        keys = [d for name, resource, d in descriptions() if name == "gpu-box.tf" and resource == "gpu_box"]
        self.assertIn("dns-udp", keys, "the gpu box's computed egress descriptions are checked")

    def test_the_check_catches_an_apostrophe(self):
        self.assertIsNone(ALLOWED.match("engine's own IP"))


if __name__ == "__main__":
    unittest.main()
