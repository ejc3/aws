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
                for rendered in render(body, d):
                    yield path.name, m.group(2), rendered
            for expr in re.findall(r'description\s*=\s*([^"\s][^\n]*?)\s*$', body, re.M):
                keys = dynamic_keys(body, expr)
                if keys is None:
                    yield path.name, m.group(2), None
                for key in keys or []:
                    yield path.name, m.group(2), key


def render(body, text):
    """Each description `text` can become. ${each.key} takes every top-level key of the
    resource's inline for_each map; any other interpolation yields None (unresolvable)."""
    if "${" not in text:
        return [text]
    if re.sub(r"\$\{each\.key\}", "", text).count("${"):
        return [None]
    keys = for_each_keys(body)
    if keys is None:
        return [None]
    return [text.replace("${each.key}", key) for key in keys]


def for_each_keys(body):
    """Top-level keys of the resource's `for_each = { ... }` map literal (it may span lines)."""
    m = re.search(r"^\s*for_each\s*=\s*\{", body, re.M)
    if not m:
        return None
    depth, i, start = 1, m.end(), m.end()
    while depth and i < len(body):
        depth += {"{": 1, "}": -1}.get(body[i], 0)
        i += 1
    inner, top, depth = body[start:i - 1], [], 0
    for j, ch in enumerate(inner):   # keep only the text at nesting depth 0
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif depth == 0:
            top.append(ch)
    # A key is a quoted string (any characters, which is the point: they are what gets checked)
    # or a bare identifier.
    return [q or b for q, b in re.findall(r'(?:^|[,\n{])\s*(?:"([^"]*)"|([A-Za-z0-9_-]+))\s*=', "".join(top))]


def dynamic_keys(body, expr):
    """The keys `expr` (`<block>.key`) takes, from its dynamic block's inline for_each map."""
    k = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\.key", expr)
    if not k:
        return None
    each = re.search(r'dynamic "%s" \{\s*for_each\s*=\s*\{([^\n]*)\}' % k.group(1), body)
    if not each:
        return None
    return [q or b for q, b in re.findall(r'(?:^|,)\s*(?:"([^"]*)"|([A-Za-z0-9_-]+))\s*=', each.group(1))]


class SecurityGroupDescriptionTests(unittest.TestCase):
    def test_every_description_uses_only_characters_ec2_accepts(self):
        seen = 0
        for name, resource, text in descriptions():
            seen += 1
            self.assertIsNotNone(text, f"{name} {resource}: a computed description (or interpolation) this test cannot resolve; "
                                       "make it a literal or teach descriptions() its values")
            self.assertRegex(text, ALLOWED, f"{name} {resource}: {text!r}")
            self.assertLess(len(text), 256, f"{name} {resource}")
        self.assertGreater(seen, 20, "the scan found too few descriptions to be looking in the right place")

    def test_dynamic_block_keys_are_resolved_and_checked(self):
        body = 'dynamic "egress" {\n    for_each = { https = ["tcp", 443], dns-udp = ["udp", 53] }\n    content {\n      description = egress.key\n'
        self.assertEqual(dynamic_keys(body, "egress.key"), ["https", "dns-udp"])
        self.assertIsNone(dynamic_keys(body, "var.something"))
        keys = [d for name, resource, d in descriptions() if name == "gpu-box.tf" and resource == "gpu_box"]
        self.assertIn("dns-udp", keys, "the gpu box's computed egress descriptions are checked")

    def test_each_key_interpolations_are_rendered_for_every_key(self):
        body = 'for_each = {\n    v4 = { cidr4 = "0.0.0.0/0" }\n    v6 = { cidr6 = "::/0" }\n  }\n  description = "x"\n'
        self.assertEqual(render(body, "HTTPS (${each.key})"), ["HTTPS (v4)", "HTTPS (v6)"])
        self.assertEqual(render(body, "${var.other}"), [None])
        rendered = [d for name, r, d in descriptions() if name == "games-multiplayer.tf" and r == "games_engine"]
        self.assertIn("HTTPS only (v4): Vercel callbacks, ECR, logs", rendered)

    def test_the_check_catches_an_apostrophe(self):
        self.assertIsNone(ALLOWED.match("engine's own IP"))


if __name__ == "__main__":
    unittest.main()
