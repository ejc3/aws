#!/usr/bin/env python3
"""Offline pin for .greptile/: detects a PR that changes how Greptile reviews this repo.

Greptile documents that greptile.json is read from the PR's source branch and autoApprove
from the base branch. Its first review here, on #135, quoted .greptile/config.json from the
PR's head commit, so a PR changes its own review, and nothing else notices when one of these
flips:

- triggerOnUpdates or triggerOnDrafts true: every push or draft spends a review credit.
- shouldUpdateDescription true: Greptile writes its summary into the hand-written PR body.
- updateSummaryOnly true: no inline comments, so required conversation resolution on main
  never blocks on a finding.
- statusCheck false: the per-commit "Greptile Review" check becomes a top-level
  "files reviewed" comment.
- autoApprove.enabled true: an APPROVED review would read as coverage. Greptile reads this
  one from the base branch and applies the strictest of it and the dashboard, so a PR
  cannot enable it for itself; this pin catches the PR that would put it on main.
- keys that skip, filter or hide reviews (skipReview, fileChangeLimit, the branch, author,
  label and keyword filters, disabledRules, statusCommentsEnabled, hideFooter), a wider
  ignorePatterns, another strictness or commentTypes, and a removed, renamed or disabled rule.
- an edit to the review content: a rule's severity, scope or text, the instructions, a
  files.json scope or description, or rules.md. A narrowed rule is still well-formed, so the
  APPROVED_* pins below hold what was last reviewed, and a PR that means to change one updates
  its pin in the same diff. The documents that files.json attaches are not pinned.

This detects; it does not enforce. Runner Lambda Tests is not a required check on main, so
a PR that fails here can still merge.

Rule scopes and files.json scopes must each match a tracked file: a glob that matches
nothing points its rule at an empty set, and the rule then reviews clean forever.

Stdlib only, no credentials or network: runs under `python3 -S -B`.
"""
import copy
import hashlib
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GREPTILE = ROOT / ".greptile"

PINNED = {
    "triggerOnUpdates": False,
    "triggerOnDrafts": False,
    "statusCheck": True,
    "shouldUpdateDescription": False,
    "updateSummaryOnly": False,
    "autoApprove": {"enabled": False},
    "strictness": 2,
    "commentTypes": ["logic", "syntax"],
    "ignorePatterns": "browser-manager/package-lock.json\nbrowser-manager/next-env.d.ts",
}
ALLOWED_KEYS = set(PINNED) | {"instructions", "rules"}
# Documented keys that skip, filter or hide reviews. Allow one only with a reviewed reason.
NARROWING_KEYS = {
    "skipReview": "skips automatic reviews",
    "fileChangeLimit": "skips pull requests above a file count",
    "labels": "reviews only labelled pull requests",
    "disabledLabels": "skips labelled pull requests",
    "includeAuthors": "reviews only the listed authors",
    "excludeAuthors": "skips the listed authors",
    "includeBranches": "reviews only the listed base branches, and stacked PRs target feature branches",
    "excludeBranches": "skips the listed base branches",
    "includeKeywords": "reviews only pull requests with a keyword",
    "ignoreKeywords": "skips pull requests with a keyword",
    "disabledRules": "turns rules off by id",
    "statusCommentsEnabled": "can remove the summary comment that carries Comments Outside Diff",
    "hideFooter": "removes the last reviewed commit and the Re-trigger button",
}
# The review content as last approved. Rules: id -> (severity, scope, sha256 of the rule text).
# A scope of None means the rule has no scope and applies to every file.
APPROVED_RULES = {
    "aws-dev-box-never-reaches-jumpbox": (
        "high",
        ["*.tf", "modules/**/*.tf", "scripts/*.sh", "scripts/*.py", ".claude/**", "AGENTS.md", "README.md"],
        "1bb74b8fe47d230a872efb6e808d6e3f72abba950a4856dfd59a7c88809c734a",
    ),
    "aws-iam-no-silent-widening": (
        "high",
        ["*.tf", "modules/**/*.tf"],
        "16e4eed06c085ffdd40b1cf46bb3512367eeeebe32a966605a8dc819015eeb4e",
    ),
    "aws-secrets-never-xtraced-or-published": (
        "high",
        ["*.tf", "modules/**/*.tf", "scripts/*.sh", "scripts/*.py", ".claude/hooks/verify-consistent.sh"],
        "8ea3e0b323144ef55f9ecf7188cc2e45b99aadeeeea860b07465bf25e4604233",
    ),
    "aws-no-committed-credentials": (
        "high",
        None,
        "44173e3e831c94ed2d5362be4e046ad03d6eb9e0fd39b5db0806a544b2491ac3",
    ),
    "aws-persistent-box-replacement-safety": (
        "high",
        ["*.tf", "modules/**/*.tf"],
        "2d96ceb8fdb47dbd5ca10ab64724d7eda5668aeb8166905d3370a56b2a8285de",
    ),
    "aws-ci-stays-credential-free": (
        "high",
        [".github/**", "github-actions.tf", "github-ami-builder.tf", "dev-staging-bootstrap.tf",
         "codeartifact.tf", "scripts/test-ci-security.py"],
        "1f8885d2ce86146b950b12f64b11eda16285ec42ef2f4cba9b3ac898efc5f2f0",
    ),
    "aws-embedded-code-has-a-failing-case": (
        "medium",
        ["runner-*.tf", "backup-*.tf", "security-*.tf", "modules/**/*.tf", "jumpbox.tf", "jumpbox2.tf",
         "jumpbox2-user-data.tf", "nextjs-user-data.tf", "codex-remote-control.tf",
         "dev-instance-common.tf", "dev-ebs.tf", "nextjs-dev.tf", "ssm-managed-instance.tf",
         "cloudflare.tf", "github-actions.tf", "github-ami-builder.tf", "dev-staging-bootstrap.tf",
         "codeartifact.tf", "scripts/*.py", "scripts/*.sh", ".github/workflows/lambda-tests.yml",
         ".github/workflows/drift.yml", ".github/workflows/runner-release-freshness.yml"],
        "292b27319a7ab41a3640ef2ec6e91ed4743a69360d6419d400e4625c149c8a7f",
    ),
    "aws-apply-time-limits-validate-cannot-catch": (
        "medium",
        ["*.tf", "modules/**/*.tf"],
        "4fb8a760d52a0db1b7defb661ebd97a167708f6090fb2fd8b819cb435318c3d0",
    ),
    "aws-unattended-boot-script-robustness": (
        "medium",
        ["*-user-data.tf", "dev-instance-common.tf", "dev-hop-key.tf", "dev-selfupdate.tf",
         "runner-autoscale.tf", "firecracker-dev.tf", "x86-dev.tf", "io-box.tf", "jumpbox.tf",
         "jumpbox2.tf", "nextjs-dev.tf", "parallel-box*.tf", "mac-dev.tf", "codex-remote-control.tf",
         "claude-remote-control.tf"],
        "94caca43349802633a629bb30259c44443b88e8e11c1a2bc5b0c00a18afeb690",
    ),
    "aws-rollout-gates-match-their-runbook": (
        "medium",
        ["security-monitoring.tf", "backup-security.tf", "cloudflare.tf", "mac-dev.tf",
         "nextjs-user-data.tf", "scripts/parallel-box.sh"],
        "df7c214ed3e926378a54ea4290a56b210852fe70ab8cedf524a4ab0b0456d13e",
    ),
}
APPROVED_INSTRUCTIONS_SHA256 = "58e23369c6b9027fce88dc985534249a7fa1ff5ef303d998c942b9ae6475c809"
# files.json path -> (scope, sha256 of the description). AGENTS.md has no scope: every review reads it.
APPROVED_FILES = {
    "AGENTS.md": (None, "bbeaf1fc3d40f71ee57bb6287ed11dab6afe469138a5bd59bb1f3582c7b72b47"),
    "GITHUB-RUNNERS.md": (
        ["runner-*.tf", "github-ami-builder.tf", ".github/**", "scripts/*runner*"],
        "f3d39ac1036d66a217da8f82e31b5765955b2b20b247c36d5262ef7250eb640c",
    ),
    "README.md": (
        ["security-monitoring.tf", "backup-security.tf", "cloudflare.tf", "mac-dev.tf",
         "nextjs-user-data.tf", "scripts/parallel-box.sh"],
        "8626513d453e09d47c0ba169df315dfbda701f841b3b234f864b2bc601c84819",
    ),
}
APPROVED_RULES_MD_SHA256 = "bee096e7439030046e7aa63203df589012fc80245b4acefe942892a5fb60bef9"
REPIN = "If the change is intended, update its pin in scripts/test-greptile-config.py in the same PR"
RULE_IDS = set(APPROVED_RULES)
# A rule without scope applies to every file; only these may omit it.
UNSCOPED_RULE_IDS = {rule_id for rule_id, (_, scope, _) in APPROVED_RULES.items() if scope is None}
RULE_KEYS = {"id", "rule", "scope", "severity", "enabled"}
SEVERITIES = {"low", "medium", "high"}
# files.json path -> whether the entry must carry a scope.
CONTEXT_FILES = {path: scope is not None for path, (scope, _) in APPROVED_FILES.items()}
FILE_KEYS = {"path", "description", "scope"}
RULE_ID = re.compile(r"aws-[a-z0-9]+(?:-[a-z0-9]+)*")
RULE_ID_IN_MARKDOWN = re.compile(r"`(aws-[a-z0-9]+(?:-[a-z0-9]+)*)`")


def glob_regex(glob):
    """Greptile scope glob as a regex: * and ? stay inside one path segment, ** crosses them."""
    if "{" in glob or "}" in glob:
        raise ValueError(f"brace alternation is not checked here, spell the paths out: {glob!r}")
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def same(a, b):
    # json.dumps keeps false apart from 0 and true apart from 1; == does not.
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def normalized(key, value):
    if key == "commentTypes" and isinstance(value, list) and all(isinstance(v, str) for v in value):
        return sorted(value)
    return value


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def glob_list(scope):
    """A list of globs in sorted order, so reordering a scope is not a change to it."""
    if isinstance(scope, list) and all(isinstance(g, str) for g in scope):
        return sorted(scope)
    return scope


def scope_problems(where, scope, tracked):
    if not (isinstance(scope, list) and scope and all(isinstance(g, str) and g for g in scope)):
        return [f"{where}: scope must be a non-empty list of globs"]
    found = []
    for glob in scope:
        if glob.startswith("/") or ".." in glob.split("/"):
            found.append(f"{where}: scope {glob!r} must be relative to the repo root, without ..")
            continue
        try:
            pattern = glob_regex(glob)
        except ValueError as error:
            found.append(f"{where}: {error}")
            continue
        if not any(pattern.match(path) for path in tracked):
            found.append(f"{where}: scope {glob!r} matches no tracked file")
    return found


def approved_rule_problems(where, rule, severity, scope, text_sha256):
    found = []
    if rule.get("severity") != severity:
        found.append(f"{where}: severity is {json.dumps(rule.get('severity'))}, approved "
                     f"{json.dumps(severity)}. {REPIN}")
    if scope is not None and "scope" in rule and not same(glob_list(rule["scope"]), sorted(scope)):
        found.append(f"{where}: scope is {json.dumps(rule['scope'])}, approved {json.dumps(scope)}. {REPIN}")
    text = rule.get("rule")
    if isinstance(text, str) and sha256(text) != text_sha256:
        found.append(f"{where}: text is sha256 {sha256(text)}, approved {text_sha256}. {REPIN}")
    return found


def config_problems(config, tracked):
    if not isinstance(config, dict):
        return ["config.json must be a JSON object"]
    found = []
    for key, want in PINNED.items():
        if key not in config:
            found.append(f"{key} is missing; set it to {json.dumps(want)}")
        elif not same(normalized(key, config[key]), normalized(key, want)):
            found.append(f"{key} is {json.dumps(config[key])}; set it to {json.dumps(want)}")
    for key in sorted(set(config) - ALLOWED_KEYS):
        if key in NARROWING_KEYS:
            found.append(f"{key} {NARROWING_KEYS[key]}; remove it")
        else:
            found.append(f"{key} is not an allowed key; add it to ALLOWED_KEYS only with a documented reason")
    instructions = config.get("instructions")
    if not (isinstance(instructions, str) and instructions.strip()):
        found.append("instructions must be a non-empty string")
    elif sha256(instructions) != APPROVED_INSTRUCTIONS_SHA256:
        found.append(f"instructions text is sha256 {sha256(instructions)}, approved "
                     f"{APPROVED_INSTRUCTIONS_SHA256}. {REPIN}")

    rules = config.get("rules")
    if not isinstance(rules, list):
        return found + ["rules must be a list"]
    seen = []
    for n, rule in enumerate(rules):
        where = f"rules[{n}]"
        if not isinstance(rule, dict):
            found.append(f"{where} must be an object")
            continue
        rule_id = rule.get("id")
        if isinstance(rule_id, str) and RULE_ID.fullmatch(rule_id):
            where = f"rule {rule_id}"
            if rule_id in seen:
                found.append(f"{where}: duplicate id")
            seen.append(rule_id)
        else:
            found.append(f"{where}: id must look like aws-<name> so it can be disabled by id")
        extra = sorted(set(rule) - RULE_KEYS)
        if extra:
            found.append(f"{where}: undocumented fields {extra}")
        if rule.get("enabled", True) is not True:
            found.append(f"{where}: enabled must be absent or true")
        text = rule.get("rule")
        if not (isinstance(text, str) and text.strip()):
            found.append(f"{where}: rule text is empty")
        severity = rule.get("severity")
        if not (isinstance(severity, str) and severity in SEVERITIES):
            found.append(f"{where}: severity must be one of {sorted(SEVERITIES)}, "
                         f"not {json.dumps(severity)}")
        if isinstance(rule_id, str) and rule_id in UNSCOPED_RULE_IDS:
            if "scope" in rule:
                found.append(f"{where}: must stay unscoped so it applies to every file")
        else:
            found.extend(scope_problems(where, rule.get("scope"), tracked))
        if isinstance(rule_id, str) and rule_id in APPROVED_RULES:
            found.extend(approved_rule_problems(where, rule, *APPROVED_RULES[rule_id]))
    missing = sorted(RULE_IDS - set(seen))
    added = sorted(set(seen) - RULE_IDS)
    if missing:
        found.append(f"rules removed or renamed: {missing}")
    if added:
        found.append(f"rules not in APPROVED_RULES: {added}; pin them in the same PR")
    return found


def files_problems(files, tracked):
    if not (isinstance(files, dict) and set(files) == {"files"} and isinstance(files["files"], list)):
        return ['files.json must be {"files": [...]}']
    found, seen = [], []
    tracked_set = set(tracked)
    for n, entry in enumerate(files["files"]):
        where = f"files[{n}]"
        if not isinstance(entry, dict):
            found.append(f"{where} must be an object")
            continue
        path = entry.get("path")
        if isinstance(path, str):
            where = f"files.json {path}"
            seen.append(path)
            if path not in tracked_set:
                found.append(f"{where}: not a tracked file")
        else:
            found.append(f"{where}: path must be a string")
        extra = sorted(set(entry) - FILE_KEYS)
        if extra:
            found.append(f"{where}: undocumented fields {extra}")
        description = entry.get("description")
        if not (isinstance(description, str) and description.strip()):
            found.append(f"{where}: description is empty")
        must_scope = CONTEXT_FILES.get(path) if isinstance(path, str) else None
        if must_scope is False:
            if "scope" in entry:
                found.append(f"{where}: must stay unscoped so every review reads it")
        elif must_scope or "scope" in entry:
            found.extend(scope_problems(where, entry.get("scope"), tracked))
        if isinstance(path, str) and path in APPROVED_FILES:
            scope, description_sha256 = APPROVED_FILES[path]
            if scope is not None and "scope" in entry and not same(glob_list(entry["scope"]), sorted(scope)):
                found.append(f"{where}: scope is {json.dumps(entry['scope'])}, approved "
                             f"{json.dumps(scope)}. {REPIN}")
            if isinstance(description, str) and sha256(description) != description_sha256:
                found.append(f"{where}: description is sha256 {sha256(description)}, approved "
                             f"{description_sha256}. {REPIN}")
    if sorted(seen) != sorted(CONTEXT_FILES):
        found.append(f"files.json paths are {sorted(seen)}; expected {sorted(CONTEXT_FILES)}")
    return found


def rules_md_problems(rules_md):
    if not rules_md.strip():
        return [".greptile/rules.md is empty"]
    named = set(RULE_ID_IN_MARKDOWN.findall(rules_md))
    found = []
    if RULE_IDS - named:
        found.append(f"rules.md does not describe {sorted(RULE_IDS - named)}")
    if named - RULE_IDS:
        found.append(f"rules.md names rules config.json lacks: {sorted(named - RULE_IDS)}")
    if sha256(rules_md) != APPROVED_RULES_MD_SHA256:
        found.append(f"rules.md is sha256 {sha256(rules_md)}, approved {APPROVED_RULES_MD_SHA256}. {REPIN}")
    return found


def problems(config, files, tracked, rules_md):
    return config_problems(config, tracked) + files_problems(files, tracked) + rules_md_problems(rules_md)


def tracked_files():
    listing = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                             capture_output=True, check=True).stdout.decode()
    files = [path for path in listing.split("\0") if path]
    if not files:
        raise AssertionError("git ls-files listed nothing; every scope check would be vacuous")
    return files


class GreptileConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((GREPTILE / "config.json").read_text())
        cls.files = json.loads((GREPTILE / "files.json").read_text())
        cls.rules_md = (GREPTILE / "rules.md").read_text()
        cls.tracked = tracked_files()

    def check(self, config=None, files=None, rules_md=None):
        return problems(self.config if config is None else config,
                        self.files if files is None else files,
                        self.tracked,
                        self.rules_md if rules_md is None else rules_md)

    def mutated(self, change):
        config = copy.deepcopy(self.config)
        change(config)
        return self.check(config=config)

    def mutated_files(self, change):
        files = copy.deepcopy(self.files)
        change(files)
        return self.check(files=files)

    @staticmethod
    def rule(config, rule_id):
        return next(r for r in config["rules"] if r.get("id") == rule_id)

    @staticmethod
    def entry(files, path):
        return next(e for e in files["files"] if e.get("path") == path)

    def assertFlags(self, found, prefix):
        self.assertTrue(any(p.startswith(prefix) for p in found), f"no problem starts with {prefix!r}: {found}")

    def test_committed_config_passes(self):
        self.assertEqual(self.check(), [])

    def test_each_pinned_key_fails_when_removed_or_changed(self):
        changed = {
            "triggerOnUpdates": True,
            "triggerOnDrafts": True,
            "statusCheck": False,
            "shouldUpdateDescription": True,
            "updateSummaryOnly": True,
            "autoApprove": {"enabled": True},
            "strictness": 3,
            "commentTypes": ["logic"],
            "ignorePatterns": PINNED["ignorePatterns"] + "\n*.tf",
        }
        self.assertEqual(set(changed), set(PINNED))
        for key, value in changed.items():
            with self.subTest(key=key, change="removed"):
                self.assertFlags(self.mutated(lambda c: c.pop(key)), key)
            with self.subTest(key=key, change="changed"):
                self.assertFlags(self.mutated(lambda c: c.__setitem__(key, value)), key)

    def test_zero_is_not_false_and_comment_type_order_is_free(self):
        self.assertFlags(self.mutated(lambda c: c.__setitem__("triggerOnUpdates", 0)), "triggerOnUpdates")
        self.assertFlags(self.mutated(lambda c: c.__setitem__("strictness", True)), "strictness")
        self.assertEqual(self.mutated(lambda c: c.__setitem__("commentTypes", ["syntax", "logic"])), [])

    def test_narrowing_and_unknown_keys_fail(self):
        cases = {
            "skipReview": "AUTOMATIC",
            "fileChangeLimit": 1,
            "excludeBranches": ["*"],
            "includeAuthors": ["nobody"],
            "labels": ["greptile"],
            "ignoreKeywords": "wip",
            "disabledRules": ["aws-iam-no-silent-widening"],
            "statusCommentsEnabled": False,
            "hideFooter": True,
            "updateExistingSummaryComment": True,
            "customContext": {"rules": []},
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                self.assertFlags(self.mutated(lambda c: c.__setitem__(key, value)), key)

    def test_wrong_types_fail(self):
        as_list = PINNED["ignorePatterns"].split("\n")
        self.assertFlags(self.mutated(lambda c: c.__setitem__("ignorePatterns", as_list)), "ignorePatterns")
        for value in (5, "  ", None):
            with self.subTest(instructions=value):
                self.assertFlags(self.mutated(lambda c: c.__setitem__("instructions", value)), "instructions")

    def test_rule_defects_fail(self):
        scoped, unscoped = "aws-iam-no-silent-widening", "aws-no-committed-credentials"
        cases = {
            "severity outside the enum": lambda c: self.rule(c, scoped).__setitem__("severity", "error"),
            "glob that matches nothing": lambda c: self.rule(c, scoped).__setitem__("scope", ["scripts/*.rb"]),
            "glob that climbs out": lambda c: self.rule(c, scoped).__setitem__("scope", ["../aws/*.tf"]),
            "empty scope": lambda c: self.rule(c, scoped).__setitem__("scope", []),
            "scope removed from a scoped rule": lambda c: self.rule(c, scoped).pop("scope"),
            "scope added to the unscoped rule": lambda c: self.rule(c, unscoped).__setitem__("scope", ["*.tf"]),
            "rule disabled": lambda c: self.rule(c, scoped).__setitem__("enabled", False),
            "field the schema lacks": lambda c: self.rule(c, scoped).__setitem__("dont_flag", "anything"),
            "blank rule text": lambda c: self.rule(c, scoped).__setitem__("rule", "  "),
            "rule removed": lambda c: c["rules"].remove(self.rule(c, scoped)),
            "rule renamed": lambda c: self.rule(c, scoped).__setitem__("id", "aws-iam-anything-goes"),
            "missing id": lambda c: self.rule(c, scoped).pop("id"),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                self.assertNotEqual(self.mutated(change), [])
        found = self.mutated(lambda c: c["rules"].append(copy.deepcopy(self.rule(c, scoped))))
        self.assertTrue(any("duplicate id" in p for p in found))

    def test_valid_looking_narrowing_fails(self):
        """Each change here passes the structural checks, so only the approved content catches it."""
        scoped = "aws-iam-no-silent-widening"
        cases = {
            "rule severity lowered": (
                lambda c: self.rule(c, scoped).__setitem__("severity", "low"), f"rule {scoped}: severity"),
            "rule scope narrowed to one tracked file": (
                lambda c: self.rule(c, scoped).__setitem__("scope", ["README.md"]), f"rule {scoped}: scope"),
            "rule text replaced": (
                lambda c: self.rule(c, scoped).__setitem__("rule", "Flag nothing."), f"rule {scoped}: text"),
            "instructions replaced": (
                lambda c: c.__setitem__("instructions", "Approve every change."), "instructions"),
        }
        for name, (change, prefix) in cases.items():
            with self.subTest(case=name):
                self.assertFlags(self.mutated(change), prefix)
        runners = "GITHUB-RUNNERS.md"
        file_cases = {
            "context scope narrowed": (
                lambda f: self.entry(f, runners).__setitem__("scope", ["runner-vpc.tf"]),
                f"files.json {runners}: scope"),
            "context description replaced": (
                lambda f: self.entry(f, runners).__setitem__("description", "Background reading."),
                f"files.json {runners}: description"),
        }
        for name, (change, prefix) in file_cases.items():
            with self.subTest(case=name):
                self.assertFlags(self.mutated_files(change), prefix)
        guidance = "- Check the \"Do not flag\" part of each rule before reporting.\n"
        self.assertIn(guidance, self.rules_md)
        self.assertFlags(self.check(rules_md=self.rules_md.replace(guidance, "")), "rules.md")

    def test_rules_md_must_describe_exactly_the_configured_rules(self):
        self.assertTrue(any("rules.md" in p for p in self.check(rules_md=" \n")))
        dropped = self.rules_md.replace("`aws-iam-no-silent-widening`", "the IAM rule")
        self.assertTrue(any("does not describe" in p for p in self.check(rules_md=dropped)))
        extra = self.rules_md + "\n- `aws-imaginary-rule`: never configured.\n"
        self.assertTrue(any("config.json lacks" in p for p in self.check(rules_md=extra)))

    def test_files_json_defects_fail(self):
        cases = {
            "untracked path": lambda f: self.entry(f, "README.md").__setitem__("path", "docs/missing.md"),
            "AGENTS.md dropped": lambda f: f["files"].remove(self.entry(f, "AGENTS.md")),
            "AGENTS.md scoped": lambda f: self.entry(f, "AGENTS.md").__setitem__("scope", ["*.tf"]),
            "scope removed from a scoped file": lambda f: self.entry(f, "GITHUB-RUNNERS.md").pop("scope"),
            "scope that matches nothing": lambda f: self.entry(f, "README.md").__setitem__("scope", ["no-such-dir/**"]),
            "blank description": lambda f: self.entry(f, "README.md").__setitem__("description", ""),
            "field the schema lacks": lambda f: self.entry(f, "README.md").__setitem__("priority", 1),
            "wrong shape": lambda f: f.__setitem__("extra", []),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                self.assertNotEqual(self.mutated_files(change), [])

    def test_glob_segments(self):
        self.assertTrue(glob_regex("*.tf").match("main.tf"))
        self.assertFalse(glob_regex("*.tf").match("modules/security-defaults/main.tf"))
        self.assertTrue(glob_regex("modules/**/*.tf").match("modules/security-defaults/main.tf"))
        self.assertFalse(glob_regex("scripts/*.py").match("scripts/nested/x.py"))
        self.assertTrue(glob_regex(".github/**").match(".github/workflows/drift.yml"))
        self.assertFalse(glob_regex(".github/**").match("github-actions.tf"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
