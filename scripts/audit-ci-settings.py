#!/usr/bin/env python3
"""Audit the GitHub Actions settings of every repository we run CI for, against the owner's policy (#466):
our self-hosted runners are fine anywhere, as long as no drive-by non-contributor can make them run.

    scripts/audit-ci-settings.py [OWNER ...]        default: ejc3 dolphin-labs-hq

Prints one line per repository and item: `pass`, `fail`, `warn` (needs a person to look) or `n/a`, with the reason.
Read-only: every call is a GET. Tokens come from scripts/github-app-token.py (the administration GitHub App), one per
owner, held in memory and never printed. A repository the app cannot read is reported, not skipped silently.

Items (numbered as in #466):
  1 fork PR approval        public repos: approval required for all external contributors
  2 private fork PRs        private repos: fork PR workflows off, or approval required, never secrets or a write token
  3 runner groups           org runner groups: limited to selected repositories, public repos only where intended
  4 default token           GITHUB_TOKEN read-only, and Actions may not approve pull requests
  5 pull_request_target     no pull_request_target / workflow_run job checks out the PR head
  6 deploy secrets          no deploy-shaped secret is a repository secret (a PR workflow could read it)
  7 draft-skipped jobs      a job skipped for drafts is also triggered on ready_for_review
  8 branch protection       main requires the CI and `review decision` checks where the repository has them
  9 pinned actions          third-party actions are pinned to a commit SHA
"""
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.github.com"
DEPLOY_SECRET = re.compile(r"(CLOUDFLARE|DEPLOY|VERCEL|AWS_|WRANGLER|FLY_|NETLIFY|NPM_TOKEN|PYPI)", re.I)
FIRST_PARTY = ("actions/", "github/")
SHA = re.compile(r"^[0-9a-f]{40}$")


def token(owner):
    out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "github-app-token.py"), owner],
                         capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def get(tok, path):
    """(status, body) for a GET; body is parsed JSON or None."""
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {tok}",
                                                      "Accept": "application/vnd.github+json",
                                                      "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, None
    except (urllib.error.URLError, OSError, ValueError) as e:
        # A timeout, a refused connection or a truncated body: status 0, which every caller reports as not readable.
        return 0, None


def paged(tok, path, key):
    page, out = 1, []
    while True:
        sep = "&" if "?" in path else "?"
        st, body = get(tok, f"{path}{sep}per_page=100&page={page}")
        if st != 200 or not body:
            return out, st
        items = body.get(key, []) if isinstance(body, dict) else body
        out += items
        if len(items) < 100:
            return out, st
        page += 1


def workflows(tok, full):
    """({path: text} of the repository's workflow files, [what could not be read]). No directory is no files."""
    st, listing = get(tok, f"/repos/{full}/contents/.github/workflows")
    if st == 404:
        return {}, []
    if st != 200 or not isinstance(listing, list):
        return {}, [f".github/workflows ({st})"]
    files, unread = {}, []
    for f in listing:
        if f.get("type") == "file" and f["name"].endswith((".yml", ".yaml")):
            st, blob = get(tok, f"/repos/{full}/contents/{f['path']}")
            if st == 200 and isinstance(blob, dict) and blob.get("encoding") == "base64":
                files[f["path"]] = base64.b64decode(blob["content"]).decode("utf-8", "replace")
            else:
                unread.append(f"{f['path']} ({st})")
    return files, unread


def triggers(text):
    """The workflow's trigger names, from the `on:` key (inline list, map or single word)."""
    # [ \t]*, not \s*: \s would cross the newline and read a block's first key as the inline form.
    m = re.search(r"^(?:on|\"on\"|'on'|true):[ \t]*(.*)$", text, re.M)
    if not m:
        return set()
    inline = m.group(1).strip()
    if inline and not inline.startswith("#"):
        return set(re.findall(r"[a-z_]+", inline))
    block = text[m.end():]
    names = set()
    for line in block.splitlines():
        if line.strip() == "" or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            break
        k = re.match(r"^  ([a-z_]+):", line)
        if k:
            names.add(k.group(1))
    return names


# What `pull_request` runs on when a workflow names no types.
DEFAULT_PR_TYPES = {"opened", "synchronize", "reopened"}


def pr_types(text, event):
    """The event's activity types; GitHub's default set when the workflow names none."""
    m = re.search(rf"^  {event}:\s*\n((?:    .*\n|\s*\n)*)", text, re.M)
    if not m:
        return DEFAULT_PR_TYPES
    t = re.search(r"types:\s*\[([^\]]*)\]", m.group(1))
    if t:
        return {x.strip() for x in t.group(1).split(",")}
    t = re.search(r"types:\s*\n((?:\s+-\s*\S+\s*\n)+)", m.group(1))
    return {x.strip("- \n") for x in t.group(1).splitlines()} if t else DEFAULT_PR_TYPES


TAGS = {}


def report(full, item, status, detail=""):
    print(f"{full:42} {TAGS.get(full, ''):11} {item:22} {status:5} {detail}".rstrip(), flush=True)


def audit_repo(tok, repo, rulesets_main):
    full, private = repo["full_name"], repo["private"]
    files, unread = workflows(tok, full)
    if unread:
        # Every workflow-derived verdict below would be about a partial set: say so, and still audit the settings.
        report(full, "- workflows", "warn", "not readable: " + ", ".join(unread))
    texts = "\n".join(files.values())
    # Which repositories matter most: those whose workflows ask for our self-hosted runners. A fork of an upstream
    # project that only uses GitHub-hosted runners is tagged so it can be read last.
    ours = bool(re.search(r"runs-on:.*self-hosted|runs-on:\s*\n\s*-\s*self-hosted|\[\s*self-hosted", texts))
    TAGS[full] = "SELF-HOSTED" if ours else ("fork" if repo.get("fork") else "")
    if not files and not unread:
        report(full, "- workflows", "n/a", "no workflow files; settings audited anyway")

    # 1 / 2: who may make a fork PR run workflows.
    if private:
        report(full, "1 fork PR approval", "n/a", "private repository")
        st, fp = get(tok, f"/repos/{full}/actions/permissions/fork-pr-workflows-private-repos")
        if st != 200:
            report(full, "2 private fork PRs", "warn", f"not readable ({st})")
        elif not fp.get("run_workflows_from_fork_pull_requests"):
            report(full, "2 private fork PRs", "pass", "fork PR workflows off")
        elif fp.get("send_secrets_and_variables") or fp.get("send_write_tokens_to_workflows"):
            report(full, "2 private fork PRs", "fail", "fork PRs get secrets or a write token")
        elif not fp.get("require_approval_for_fork_pr_workflows"):
            report(full, "2 private fork PRs", "fail", "fork PR workflows run without approval")
        else:
            report(full, "2 private fork PRs", "pass", "on, approval required, no secrets or write token")
    else:
        report(full, "2 private fork PRs", "n/a", "public repository")
        st, ap = get(tok, f"/repos/{full}/actions/permissions/fork-pr-contributor-approval")
        policy = (ap or {}).get("approval_policy")
        if st != 200:
            report(full, "1 fork PR approval", "warn", f"not readable ({st})")
        elif policy == "all_external_contributors":
            report(full, "1 fork PR approval", "pass", policy)
        else:
            report(full, "1 fork PR approval", "fail", f"{policy} (want all_external_contributors)")

    # 4: the default token.
    st, wp = get(tok, f"/repos/{full}/actions/permissions/workflow")
    if st != 200:
        report(full, "4 default token", "warn", f"not readable ({st})")
    else:
        bad = []
        if wp.get("default_workflow_permissions") != "read":
            bad.append(f"default {wp.get('default_workflow_permissions')}")
        if wp.get("can_approve_pull_request_reviews"):
            bad.append("Actions may approve PRs")
        report(full, "4 default token", "fail" if bad else "pass", ", ".join(bad) or "read, no PR approval")

    # 5: privileged triggers that check out the PR head.
    risky, seen = [], False
    for path, text in files.items():
        t = triggers(text)
        if t & {"pull_request_target", "workflow_run"}:
            seen = True
            if re.search(r"github\.event\.pull_request\.head\.(sha|ref)|refs/pull/|github\.head_ref"
                         r"|github\.event\.workflow_run\.head_(sha|branch)", text):
                risky.append(path.rsplit("/", 1)[-1])
    if not seen:
        report(full, "5 pull_request_target", "n/a", "no pull_request_target or workflow_run trigger")
    elif risky:
        report(full, "5 pull_request_target", "fail", "checks out the PR head: " + ", ".join(risky))
    else:
        report(full, "5 pull_request_target", "pass", "privileged trigger present, no PR-head checkout")

    # 6: deploy-shaped repository secrets.
    secrets, st = paged(tok, f"/repos/{full}/actions/secrets", "secrets")
    if st != 200:
        report(full, "6 deploy secrets", "warn", f"not readable ({st})")
    else:
        deploy = sorted(s["name"] for s in secrets if DEPLOY_SECRET.search(s["name"]))
        report(full, "6 deploy secrets", "warn" if deploy else "pass",
               ("repository secrets: " + ", ".join(deploy) + " (should be main-only environment secrets)") if deploy
               else "none deploy-shaped")

    # 7: jobs skipped for drafts must also run on ready_for_review.
    missing = []
    for path, text in files.items():
        if re.search(r"pull_request\.draft", text) and "pull_request" in triggers(text):
            types = pr_types(text, "pull_request")
            if "ready_for_review" not in types:
                missing.append(path.rsplit("/", 1)[-1])
    drafty = any(re.search(r"pull_request\.draft", t) for t in files.values())
    report(full, "7 draft-skipped jobs", "fail" if missing else ("pass" if drafty else "n/a"),
           ("not triggered on ready_for_review: " + ", ".join(missing)) if missing
           else ("draft-skipped jobs also run on ready_for_review" if drafty else "no draft-skipped jobs"))

    # 8: required checks on main.
    rules, unreadable = rulesets_main.get(full), []
    if rules is None:
        st, rules = get(tok, f"/repos/{full}/rules/branches/{repo.get('default_branch') or 'main'}")
        if st != 200:
            rules, unreadable = [], [f"rulesets not readable ({st})"]
    required = {c["context"] for r in rules if r.get("type") == "required_status_checks"
                for c in r["parameters"]["required_status_checks"]}
    # Classic branch protection is separate from rulesets, and a repository may use either.
    st, prot = get(tok, f"/repos/{full}/branches/{repo.get('default_branch') or 'main'}/protection")
    if st not in (200, 404):  # 404: no classic protection, which is fine when rulesets cover it
        unreadable.append(f"classic protection not readable ({st})")
    if st == 200:
        rsc = prot.get("required_status_checks") or {}
        required |= set(rsc.get("contexts") or []) | {c.get("context") for c in rsc.get("checks") or [] if c.get("context")}
    # Item 8 says only whether something is required; whether the set covers every CI job is read by a person.
    gaps = list(unreadable)
    if "review-decision" in texts or "review decision" in texts:
        if "review decision" not in required:
            gaps.append("review decision")
    if not required:
        gaps.append("no required checks")
    report(full, "8 branch protection", "warn" if gaps else "pass",
           ("missing: " + ", ".join(gaps)) if gaps else "required: " + ", ".join(sorted(required)))

    # 9: third-party actions pinned to a SHA.
    loose = set()
    for text in files.values():
        for uses in re.findall(r"uses:\s*['\"]?([^\s'\"#]+)", text):
            if uses.startswith(("./", "docker://")) or uses.startswith(FIRST_PARTY) or "@" not in uses:
                continue
            ref = uses.rsplit("@", 1)[1]
            if not SHA.match(ref):
                loose.add(uses)
    report(full, "9 pinned actions", "fail" if loose else "pass",
           ("not pinned: " + ", ".join(sorted(loose))) if loose else "third-party actions pinned (or none)")


def audit_org_runner_groups(tok, owner):
    groups, st = paged(tok, f"/orgs/{owner}/actions/runner-groups", "runner_groups")
    if st == 404:
        report(owner, "3 runner groups", "n/a", "not an organization")
        return
    if st != 200:
        report(owner, "3 runner groups", "warn", f"not readable ({st})")
        return
    for g in groups:
        name = f"{owner}:{g['name']}"
        problems = []
        if g.get("visibility") == "all":
            problems.append("visible to all repositories")
        elif g.get("visibility") == "private":
            problems.append("visible to every private repository")
        if g.get("allows_public_repositories"):
            problems.append("admits public repositories")
        report(name, "3 runner groups", "warn" if problems else "pass",
               ", ".join(problems) or f"visibility {g.get('visibility')}, no public repositories")


def main():
    owners = sys.argv[1:] or ["ejc3", "dolphin-labs-hq"]
    print(f"{'repository':42} {'runners':11} {'item':22} {'state':5} detail")
    for owner in owners:
        tok = token(owner)
        if not tok:
            report(owner, "-", "warn", "no token for this owner (the app is not installed there)")
            continue
        audit_org_runner_groups(tok, owner)
        repos, st = paged(tok, "/installation/repositories", "repositories")
        if st != 200:
            report(owner, "-", "warn", f"repository list incomplete ({st}): only {len(repos)} audited")
        for repo in sorted(repos, key=lambda r: r["full_name"]):
            if repo["owner"]["login"].lower() != owner.lower() or repo.get("archived"):
                continue
            audit_repo(tok, repo, {})


if __name__ == "__main__":
    main()
