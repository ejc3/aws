#!/usr/bin/env python3
"""Redact possible AWS account IDs before publishing PR, issue, or review text."""
import re
import sys


def public_text(body: str) -> str:
    return re.sub(r"(?<![0-9])[0-9]{12}(?![0-9])", "(account hidden)", body)


if __name__ == "__main__":
    sys.stdout.write(public_text(sys.stdin.read()))
