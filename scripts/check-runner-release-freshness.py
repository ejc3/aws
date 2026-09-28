#!/usr/bin/env python3
"""Anonymous, read-only runner release check. Never updates the pin or AWS."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
RELEASES_API = "https://api.github.com/repos/actions/runner/releases?per_page=100"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
VERSION = r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})"


def version(value):
    match = re.fullmatch(VERSION, value)
    if match is None:
        raise ValueError("expected a numeric stable runner version")
    return tuple(int(n) for n in match.groups())


def pinned_version(source):
    body = re.search(r"^  runner_user_data = <<-EOF\n(.*?)^EOF$", source, re.M | re.S)
    pins = re.findall(r'^RUNNER_VERSION="([^"]+)"$', body[1], re.M) if body else []
    if len(pins) != 1:
        raise ValueError("expected exactly one literal RUNNER_VERSION in runner user data")
    version(pins[0])
    return pins[0]


def pinned_app_version(source):
    """The app runners' pin (runner-app/bootstrap.sh). It must equal the fcvm pin, so the one
    freshness verdict covers both fleets."""
    pins = re.findall(r"^RUNNER_VERSION='([^']+)'$", source, re.M)
    if len(pins) != 1:
        raise ValueError("expected exactly one literal RUNNER_VERSION in the app runner bootstrap")
    version(pins[0])
    return pins[0]


def pinned_versions():
    pin = pinned_version((ROOT / "runner-autoscale.tf").read_text())
    if pinned_app_version((ROOT / "runner-app" / "bootstrap.sh").read_text()) != pin:
        raise ValueError("runner-app/bootstrap.sh and runner-autoscale.tf pin different runner versions")
    return pin


def fetch_releases():
    request = urllib.request.Request(RELEASES_API, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ejc3-aws-runner-release-freshness",
    })
    # Intentionally anonymous: no PAT, repository secret, or environment token.
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("release response exceeded the bounded size")
    return json.loads(data)


def assess(pin, releases):
    pinned = version(pin)
    if not isinstance(releases, list) or not 1 <= len(releases) <= 100:
        raise ValueError("expected a nonempty bounded release history")
    stable = {}
    for release in releases:
        if not isinstance(release, dict) or type(release.get("draft")) is not bool or type(release.get("prerelease")) is not bool:
            raise ValueError("release stability metadata is missing or invalid")
        if release["draft"] or release["prerelease"]:
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag.startswith("v"):
            raise ValueError("stable release tag is invalid")
        number = version(tag[1:])
        published = release.get("published_at")
        if not isinstance(published, str):
            raise ValueError("stable release publication date is missing")
        timestamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("stable release publication date has no timezone")
        stable[number] = timestamp.astimezone(timezone.utc)
    if pinned not in stable:
        raise ValueError("pinned release is absent from recent stable history; investigate now")
    newer = {n: published for n, published in stable.items() if n > pinned}
    if not newer:
        return 0, f"Runner {pin} matches the newest stable version in recent public release history."
    newest = ".".join(str(n) for n in max(newer))
    # A second release must not reset the grace period from the first missed one.
    deadline = min(newer.values()) + timedelta(days=30)
    return 1, (
        f"Runner {pin} is behind stable {newest}. Upgrade by "
        f"{deadline.strftime('%Y-%m-%d %H:%M UTC')} at the latest; a required critical "
        "security update can block jobs sooner. Review the version and both official "
        "asset digests, repeat package/wrapper/bootstrap tests, then Terraform-publish "
        "and verify a trusted job/termination. See GITHUB-RUNNERS.md (Instance-bound, "
        f"single-job bootstrap) and https://github.com/actions/runner/releases/tag/v{newest}. "
        "This check does not upgrade or deploy anything."
    )


def main():
    try:
        pin = pinned_versions()
        code, message = assess(pin, fetch_releases())
    except (OSError, ValueError, TypeError, KeyError) as error:
        # Do not echo untrusted API payloads or arbitrary network error strings
        # into GitHub's workflow-command channel.
        print(f"::error title=Runner freshness unknown::Cannot verify runner release freshness ({type(error).__name__}). Inspect the pin/public release history and retry; do not treat this as up to date.")
        return 2
    print(("::error title=Runner update required::" if code else "") + message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
