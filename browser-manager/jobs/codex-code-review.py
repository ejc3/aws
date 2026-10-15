"""Job for bin/browser-job.py (browserctl job codex codex-code-review.py ...): Codex's automatic code review settings (chatgpt.com/codex/settings/code-review).

    list                         every repository with its review switches, as shown
    enable <owner/repo> [...]    turn automatic review on for each named repository
"""
import os

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


def run(page, args):
    state = _load(page)
    if state != "ok":
        page.screenshot(path=os.path.join(os.environ.get("TMPDIR", "/tmp"), "codex-code-review.png"), full_page=True)
        return {"state": state, "url": page.url, "screenshot": os.path.join(os.environ.get("TMPDIR", "/tmp"), "codex-code-review.png")}
    if not args or args[0] == "list":
        page.screenshot(path=os.path.join(os.environ.get("TMPDIR", "/tmp"), "codex-code-review.png"), full_page=True)
        return {"state": "ok", "text": " ".join(page.inner_text("body").split())[:4000], "switches": _switches(page)}
    if args[0] == "enable":
        changed = []
        for repo in args[1:]:
            for s in _switches(page):
                if repo.lower() in s["label"].lower() and not s["on"]:
                    page.locator("[role=switch]").nth(s["index"]).click()
                    page.wait_for_timeout(1500)
                    changed.append(repo)
                    break
        return {"state": "ok", "changed": changed, "switches": _switches(page)}
    return {"error": "unknown command " + args[0]}
