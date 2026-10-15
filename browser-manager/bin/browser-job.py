#!/usr/bin/env python3
"""browser-job: run a scripted job inside one of browser-manager's running, signed-in browsers.

    browserctl job <browser> <job.py> [args...]        (or: python3 bin/browser-job.py <browser> <job.py> [args...])

It asks the manager for that browser's CDP bridge (`browserctl cdp <browser>`, loopback only), attaches with Playwright,
and hands the job a NEW TAB in the person's own session: same cookies, same trusted identity, nothing restarted. The job
is a Python file defining `run(page, args) -> dict`; the dict is printed as JSON. When it ends, only the job's tab is
closed and the connection dropped; the browser and its other tabs are left as they were (and the bridge refuses
Browser.close outright).

A site that shows a human-verification challenge is reported (`page.challenged()`), never solved: a person clears it in
the live browser at its browser-manager URL, and the next run goes through.

Needs Playwright for Python, outside the manager: python3 -m venv ~/pw-venv && ~/pw-venv/bin/pip install playwright
(no browser download needed; it drives the manager's browser). Jobs live in jobs/.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
CHALLENGE_TEXT = ("Verify you are human", "Just a moment", "Checking your browser", "Checking if the site connection is secure")


def endpoint(name):
    ctl = os.environ.get("BROWSERCTL") or str(HERE / "browserctl.mjs")
    out = subprocess.run(["node", ctl, "cdp", name], capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.startswith("ws://127.0.0.1:"):
        sys.exit(f"browser-job: no CDP bridge for {name}: {(out.stderr or out.stdout).strip()}")
    return out.stdout.strip()


def challenged(page):
    try:
        text = page.inner_text("body", timeout=2000)
    except Exception:
        return False
    return any(t in text for t in CHALLENGE_TEXT) or page.locator("iframe[src*='challenges.cloudflare.com']").count() > 0


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    name, job_path, job_args = sys.argv[1], Path(sys.argv[2]), sys.argv[3:]
    if not job_path.exists() and (HERE.parent / "jobs" / job_path).exists():
        job_path = HERE.parent / "jobs" / job_path
    spec = importlib.util.spec_from_file_location("job", job_path)
    job = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(job)
    ws = endpoint(name)
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(ws)
        context = browser.contexts[0]
        page = context.new_page()
        page.challenged = lambda: challenged(page)
        try:
            result = job.run(page, job_args)
        finally:
            try:
                page.close()
            except Exception:
                pass
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
