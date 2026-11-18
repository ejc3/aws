#!/usr/bin/env python3
"""Redact possible AWS account IDs before publishing PR, issue, or review text."""
import re
import sys


# A complete AMI ID can itself contain twelve consecutive decimal digits.
TOKEN = re.compile(r"(?P<ami>\bami-(?:[0-9a-f]{17}|[0-9a-f]{8})\b)|(?<![0-9])[0-9]{12}(?![0-9])")


def public_text(body: str) -> str:
    return TOKEN.sub(lambda match: match.group("ami") or "(account hidden)", body)


if __name__ == "__main__":
    sys.stdout.write(public_text(sys.stdin.read()))
