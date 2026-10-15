"""Job for bin/browser-job.py (browserctl job codex codex-code-review.py ...): Codex's automatic code review settings (chatgpt.com/codex/settings/code-review).

    list                         every repository with its review switches, as shown
    enable <owner/repo> [...]    turn automatic review on for each named repository
"""
import os
import re
import tempfile

SETTINGS = "https://chatgpt.com/codex/settings/code-review"


def _load(page):
    page.goto(SETTINGS, wait_until="domcontentloaded", timeout=60000)
    for _ in range(30):
        page.wait_for_timeout(1000)
        if page.challenged():
            return "challenge"
        if page.locator("[role=switch]").count() or "code review" in page.inner_text("body").lower():
            page.wait_for_timeout(2000)
            return "ok"
    return "timeout"


def _switches(page):
    out = []
    sw = page.locator("[role=switch]")
    for i in range(sw.count()):
        el = sw.nth(i)
        label = el.evaluate("e => { let n = e; for (let k = 0; k < 6 && n; k++, n = n.parentElement) {"
                            " const t = (n.innerText || '').trim(); if (t.length > 3) return t.slice(0, 200); } return ''; }")
        out.append({"index": i, "on": el.get_attribute("aria-checked") == "true", "label": " ".join(label.split())})
    return out


def _shot(page):
    # The settings page lists private repositories: keep the screenshot in a directory only this user can read.
    path = os.path.join(tempfile.mkdtemp(prefix="codex-code-review-"), "settings.png")
    page.screenshot(path=path, full_page=True)
    return path


def _names(repo, label):
    # The complete owner/name, not a prefix: example/project must not match example/project-tools.
    return re.search(r"(?<![\w./-])" + re.escape(repo.lower()) + r"(?![\w./-])", label.lower()) is not None


def run(page, args):
    state = _load(page)
    if state != "ok":
        return {"state": state, "url": page.url, "screenshot": _shot(page)}
    if not args or args[0] == "list":
        return {"state": "ok", "screenshot": _shot(page), "text": " ".join(page.inner_text("body").split())[:4000],
                "switches": _switches(page)}
    if args[0] == "enable":
        changed, already, missing = [], [], []
        for repo in args[1:]:
            matches = [s for s in _switches(page) if _names(repo, s["label"])]
            if not matches:
                missing.append(repo)
            elif all(s["on"] for s in matches):
                already.append(repo)
            else:
                for s in matches:
                    if not s["on"]:
                        page.locator("[role=switch]").nth(s["index"]).click()
                        page.wait_for_timeout(1500)
                changed.append(repo)
        return {"state": "ok", "changed": changed, "already_on": already, "not_found": missing, "switches": _switches(page)}
    return {"error": "unknown command " + args[0]}
