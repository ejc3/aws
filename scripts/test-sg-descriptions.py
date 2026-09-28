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
    # Every Terraform file in the repository, local modules included (not provider caches).
    for path in sorted(p for p in ROOT.rglob("*.tf") if ".terraform" not in p.parts):
        name = str(path.relative_to(ROOT))
        text = path.read_text()
        for m in re.finditer(r'resource "(%s)" "([^"]+)" \{(.*?)\n\}' % "|".join(TYPES), text, re.S):
            body = m.group(3)
            for d in re.findall(r'description\s*=\s*"((?:[^"\\]|\\.)*)"', body):
                for rendered in render(body, d):
                    yield name, m.group(2), rendered
            for expr in re.findall(r'description\s*=\s*([^"\s][^\n]*?)\s*$', body, re.M):
                keys = dynamic_keys(body, expr)
                if keys is None:
                    yield name, m.group(2), None
                for key in keys or []:
                    yield name, m.group(2), key


def render(body, text):
    """Each value the quoted description `text` can take, decoded as Terraform decodes it.
    ${each.key} takes every top-level key of the resource's inline for_each map; any other
    interpolation, or an escape this cannot decode, yields None (unresolvable)."""
    pieces = [hcl_unescape(p) for p in text.split("${each.key}")]
    if None in pieces:
        return [None]
    if len(pieces) == 1:
        return pieces
    keys = for_each_keys(body)
    if keys is None:
        return [None]
    return [key.join(pieces) for key in keys]


HCL_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}


def hcl_unescape(text):
    """A quoted HCL string's value: \\n \\r \\t \\" \\\\ \\uNNNN \\UNNNNNNNN, and $${ / %%{ for a
    literal ${ / %{. Any other escape, or an interpolation or directive, is None."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\":
            nxt = text[i + 1:i + 2]
            if nxt in HCL_ESCAPES:
                out.append(HCL_ESCAPES[nxt])
                i += 2
                continue
            width = {"u": 4, "U": 8}.get(nxt)
            digits = text[i + 2:i + 2 + width] if width else ""
            if not width or not re.fullmatch(r"[0-9A-Fa-f]{%d}" % width, digits):
                return None
            out.append(chr(int(digits, 16)))
            i += 2 + width
            continue
        if text.startswith(("$${", "%%{"), i):
            out.append(text[i + 1:i + 3])
            i += 3
            continue
        if text.startswith(("${", "%{"), i):
            return None
        out.append(text[i])
        i += 1
    return "".join(out)


def for_each_keys(body):
    """Top-level keys of the resource's `for_each = { ... }` map literal (it may span lines)."""
    m = re.search(r"^\s*for_each\s*=\s*\{", body, re.M)
    if not m:
        return None
    depth, i, start = 1, m.end(), m.end()
    while depth and i < len(body):
        depth += {"{": 1, "}": -1}.get(body[i], 0)
        i += 1
    return map_keys(top_level(body[start:i - 1]))


def top_level(inner):
    """The text of a map literal at nesting depth 0 (nested {...} and [...] values removed)."""
    top, depth = [], 0
    for ch in inner:
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif depth == 0:
            top.append(ch)
    return "".join(top)


def dynamic_keys(body, expr):
    """The keys `expr` (`<block>.key`) takes, from its dynamic block's inline for_each map."""
    k = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\.key", expr)
    if not k:
        return None
    each = re.search(r'dynamic "%s" \{\s*for_each\s*=\s*\{([^\n]*)\}' % k.group(1), body)
    if not each:
        return None
    return map_keys(top_level(each.group(1)))


KEY = re.compile(r'\s*(?:"((?:[^"\\]|\\.)*)"|([A-Za-z_][A-Za-z0-9_-]*))\s*=')


def map_keys(top):
    """Every key of a map literal's top level (nested values already removed), decoded as
    Terraform decodes it. All or nothing: an entry that does not parse as `key = ...` makes the
    whole map unresolvable (None), so no key can slip past unchecked."""
    keys = []
    for entry in re.split(r"[,\n]", top):
        if not entry.strip():
            continue
        m = KEY.match(entry)
        if not m:
            return None
        quoted, bare = m.groups()
        key = bare if quoted is None else hcl_unescape(quoted)
        if key is None:
            return None
        keys.append(key)
    return keys


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

    def test_map_keys_decode_escapes_and_refuse_partial_parses(self):
        self.assertEqual(map_keys('https = "x", "http\\"4" = "y"'), ["https", 'http"4'])
        self.assertIsNone(map_keys('https = "x", ??? = "y"'), "an unparsable entry makes the map unresolvable")
        self.assertEqual(map_keys('"bad\\u0027key" = 1'), ["bad'key"], "a " + "\\u escape decodes to its character")
        self.assertEqual(map_keys(r'"a\nb" = 1'), ["a\nb"], r"\n is a newline, not n")
        self.assertIsNone(map_keys(r'"a\qb" = 1'), "an unknown escape is unresolvable")
        self.assertEqual(render("", "x\\u0027y"), ["x'y"], "literal descriptions are decoded too")

    def test_modules_are_scanned(self):
        names = {name for name, _, _ in descriptions()}
        self.assertTrue(any(n.startswith("modules/") for n in names) or
                        not any("aws_security_group" in p.read_text() for p in (ROOT / "modules").rglob("*.tf")),
                        "security groups in local modules must be scanned")

    def test_the_check_catches_an_apostrophe(self):
        self.assertIsNone(ALLOWED.match("engine's own IP"))


if __name__ == "__main__":
    unittest.main()
