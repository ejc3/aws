#!/usr/bin/env python3
"""Secrets fetched by boot and admin scripts stay out of xtrace output, logs and command lines.

Three leaks shaped these checks:
  * dev-hop-key.tf and jumpbox2-user-data.tf fetched private keys under `set -x`, and the
    trace carried them into cloud-init-output.log, the journal, dev-selfupdate.log and the
    EC2 serial console.
  * dev-selfupdate.log and setup-sync.log were created 0644, so every local account could
    read those traces.
  * scripts/dev-box-triage.sh put the Cloudflare token on curl's command line, readable by
    any local user through /proc/<pid>/cmdline.

THE STATIC CHECK executes nothing. It reads every Terraform heredoc and every scripts/*.sh
except the test-*.sh harnesses as shell; a harness holds fetch text as data (an awk program
that looks for `--with-decryption`, a fake's case pattern) and fetches no real secret.
Each assignment from a secret fetch -- `aws secretsmanager get-secret-value`,
`--with-decryption`, the IMDS `/api/token`, `gh auth token` -- needs tracing definitely off
at that point in the same shell: an unconditional `set +x` earlier in that shell that no
later `set -x`, not even a conditional restore, has undone. A heredoc body or a function
body is a separate shell and starts out "maybe traced", because it can be run with
`bash -x` or inherit an exported SHELLOPTS. A `set +x` inside an if, case or loop only
counts inside that block. The check also fails on a secret passed as an argument
(`--token "$REG_TOKEN"`) or an Authorization header built on a command line (`Bearer $T`).

IMDS token headers (`X-aws-ec2-metadata-token: $TOKEN`) on curl's command line are not
flagged: that token only works from inside the instance, where any local user can mint one.

The other tests run the real log-permission and token blocks against stubs in temp dirs.

Run from the repo root:  python3 -S -B scripts/test-boot-secret-hygiene.py
"""
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Known sites that are not fixed yet. Each is still reported on every run; the test fails if a
# new finding appears or if one of these disappears (delete the entry then, so the list cannot
# silently outlive the fix). #128 fixed the three runner_user_data sites this list started
# with: IMDSv2 TOKEN6 and TOKEN, and `--token "$REG_TOKEN"` on sudo's command line.
EXPECTED_TO_FIX_IN_128 = {}

# Reviewed sites that are safe without an explicit `set +x`, with the reason.
JUSTIFIED = {
    "mac-dev.tf:user_data:traced-fetch:PW":
        "the macOS user data never enables xtrace and runs once under ec2-macos-init; the "
        "instance is disabled by default (enable_mac_dev) and ignores user_data changes",
}

# --------------------------------------------------------------------------------- scanner
FETCH = re.compile(r"secretsmanager\s+get-secret-value|--with-decryption\b|/api/token\b|\bgh\s+auth\s+token\b")
ASSIGNMENT = re.compile(r"(?:(?:export|local|readonly|declare(?:\s+-[A-Za-z]+)*)\s+)?"
                        r"([A-Za-z_][A-Za-z0-9_]*)=(?:\"?\$\(|`)")
SECRET_ARG = re.compile(r"(--(?:[a-z0-9]+-)*(?:token|password|passwd|secret))(?:=|\s+)[\"']?\$\{?([A-Za-z_][A-Za-z0-9_]*)")
BEARER = re.compile(r"(?:\bBearer|\bAuthorization:\s*token)\s+\$\{?([A-Za-z_][A-Za-z0-9_]*)", re.I)
# An xtrace switch hidden in quotes or a substitution (eval "set -x", exported SHELLOPTS).
HIDDEN_ENABLE = re.compile(r"\bset\s+(?:[-+][A-Za-z]+\s+)*-[A-Za-z]*x|\bset\s+-o\s+xtrace|\bSHELLOPTS\b|\bBASH_XTRACEFD\b")
FUNCTION = re.compile(r"\s*(?:function\s+)?[A-Za-z_][\w:.-]*\s*\(\s*\)\s*\{|\s*function\s+[A-Za-z_][\w:.-]*\s*\{")
LOOP_OPEN = re.compile(r"(?:^|[;&|]|\bdo\b|\bthen\b)\s*(?:for|while|until)\s")
LOOP_CLOSE = re.compile(r"(?:^|[;&|])\s*done\b")

Finding = namedtuple("Finding", "id line context")
Site = namedtuple("Site", "unit line var off context")


class Lexed:
    def __init__(self, masked, ops, heredocs, open_):
        self.masked, self.ops, self.heredocs, self.open = masked, ops, heredocs, open_


def lex(text):
    """Mask quoted text, substitutions and comments; record operators and heredoc tags.

    `open` is true while a quote, substitution or ( group is unclosed or the text ends in a
    line continuation, i.e. the command carries on onto the next physical line.
    """
    masked = list(text)
    ops, heredocs, stack = [], [], []
    group, i, n = 0, 0, len(text)

    def hide(a, b):
        for k in range(a, min(b, n)):
            if masked[k] != "\n":
                masked[k] = "_"

    def word_start(k):
        return k == 0 or text[k - 1] in " \t\n;&|()"

    while i < n:
        c = text[i]
        top = stack[-1] if stack else None
        if top == "sq":
            if c == "'":
                stack.pop()
            else:
                hide(i, i + 1)
            i += 1
            continue
        if top in ("dq", "bt", "brace"):
            if c == "\\":
                hide(i, i + 2)
                i += 2
            elif (top == "dq" and c == '"') or (top == "bt" and c == "`") or (top == "brace" and c == "}"):
                stack.pop()
                i += 1
            elif top != "bt" and text.startswith("$(", i):
                stack.append("cmd")
                hide(i, i + 2)
                i += 2
            elif top != "bt" and text.startswith("${", i):
                stack.append("brace")
                hide(i, i + 2)
                i += 2
            elif top == "dq" and c == "`":
                stack.append("bt")
                hide(i, i + 1)
                i += 1
            else:
                hide(i, i + 1)
                i += 1
            continue
        inner = top in ("cmd", "paren")
        if c == "\\":
            if inner:
                hide(i, i + 2)
            i += 2
            continue
        if c == "#" and word_start(i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            for k in range(i, end):
                masked[k] = " "
            i = end
            continue
        if c in "'\"`":
            stack.append({"'": "sq", '"': "dq", "`": "bt"}[c])
            if inner:
                hide(i, i + 1)
            i += 1
            continue
        if text.startswith("$(", i) or text.startswith("${", i):
            stack.append("cmd" if text[i + 1] == "(" else "brace")
            if inner:
                hide(i, i + 2)
            i += 2
            continue
        if text.startswith("<<", i) and not text.startswith("<<<", i) and (i == 0 or text[i - 1] != "<"):
            m = re.match(r"<<-?\s*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z_][A-Za-z0-9_]*))", text[i:])
            if m:
                heredocs.append(m.group(1) or m.group(2) or m.group(3))
                if inner:
                    hide(i, i + m.end())
                i += m.end()
                continue
        if inner:
            if c == "(":
                stack.append("paren")
            elif c == ")":
                stack.pop()
            hide(i, i + 1)
            i += 1
            continue
        two = text[i:i + 2]
        if two in (";;", "&&", "||"):
            ops.append((i, i + 2, two))
            i += 2
        elif c == ";":
            ops.append((i, i + 1, ";"))
            i += 1
        elif c == "|":
            width = 2 if two == "|&" else 1
            ops.append((i, i + width, "|"))
            i += width
        elif c == "&" and not ((i > 0 and text[i - 1] in "<>") or two == "&>"):
            ops.append((i, i + 1, "&"))
            i += 1
        elif c == "\n":
            ops.append((i, i + 1, "nl"))
            i += 1
        elif c == "(":
            group += 1
            ops.append((i, i + 1, "("))
            i += 1
        elif c == ")":
            if group:
                group -= 1
                ops.append((i, i + 1, ")"))
            else:
                ops.append((i, i + 1, "pattern)"))
            i += 1
        elif c in "{}" and word_start(i) and (i + 1 == n or text[i + 1] in " \t\n;"):
            ops.append((i, i + 1, c))
            i += 1
        else:
            i += 1
    masked = "".join(masked)
    last = masked.split("\n")[-1]
    continued = (len(last) - len(last.rstrip("\\"))) % 2 == 1
    return Lexed(masked, ops, heredocs, bool(stack) or group > 0 or continued)


def xtrace_change(command):
    """'on', 'off' or None: what this simple command does to xtrace in the current shell."""
    words = command.split()
    if not words or words[0] != "set":
        return None
    result, k = None, 1
    while k < len(words):
        word = words[k]
        if word == "--":
            break
        if word in ("-o", "+o") and k + 1 < len(words):
            if words[k + 1] == "xtrace":
                result = "on" if word == "-o" else "off"
            k += 2
            continue
        if re.fullmatch(r"[-+][A-Za-z]+", word) and "x" in word:
            result = "on" if word[0] == "-" else "off"
        k += 1
    return result


class Frame:
    def __init__(self, kind, entry):
        self.kind, self.entry, self.branches, self.has_else, self.pattern = kind, entry, [], False, False


class Shell:
    """Abstract xtrace state of one shell: `off` is True only when tracing is definitely off."""

    def __init__(self):
        self.off = False
        self.frames = []

    def top(self, kind):
        return self.frames[-1] if self.frames and self.frames[-1].kind == kind else None

    def branch(self, kind, is_else=False):
        frame = self.top(kind)
        if frame is None:
            self.off = False
            return
        frame.branches.append(self.off)
        self.off = frame.entry
        frame.has_else = frame.has_else or is_else

    def close(self, kind):
        idx = next((k for k in range(len(self.frames) - 1, -1, -1) if self.frames[k].kind == kind), None)
        if idx is None:
            self.off = False
            return
        if idx != len(self.frames) - 1:
            self.off = False
            del self.frames[idx + 1:]
        frame = self.frames.pop()
        if kind == "if":
            states = frame.branches + [self.off] + ([] if frame.has_else else [frame.entry])
        elif kind == "case":
            states = frame.branches + [self.off, frame.entry]
        else:
            states = [frame.entry, self.off]
        self.off = all(states)


class Sink:
    def __init__(self):
        self.findings, self.sites, self.counts = [], [], {}

    def finding(self, unit, kind, what, line, context):
        base = f"{unit}:{kind}:{what}"
        self.counts[base] = self.counts.get(base, 0) + 1
        n = self.counts[base]
        self.findings.append(Finding(base if n == 1 else f"{base}#{n}", line, context))


def loop_enables_tracing(rest_of_line, lines, next_index):
    depth = 1
    for chunk in [rest_of_line] + lines[next_index:]:
        code = "" if chunk.lstrip().startswith("#") else chunk
        if HIDDEN_ENABLE.search(code):
            return True
        depth += len(LOOP_OPEN.findall(code)) - len(LOOP_CLOSE.findall(code))
        if depth <= 0:
            return False
    return False


def process(shell, text, lexed, line, lines, next_index, unit, context, sink):
    code = lexed.masked
    raw_code = "".join(" " if (m == " " and t not in " \t") else t for t, m in zip(text, code))
    # A `set -x` hidden in quotes or a substitution, or any use of SHELLOPTS or BASH_XTRACEFD,
    # can change tracing without being a plain `set` command in this shell.
    if ((HIDDEN_ENABLE.search(raw_code) and not HIDDEN_ENABLE.search(code))
            or re.search(r"\bSHELLOPTS\b|\bBASH_XTRACEFD\b", raw_code)):
        shell.off = False
    for m in SECRET_ARG.finditer(raw_code):
        sink.finding(unit, "secret-arg", f"{m.group(1)} ${m.group(2)}", line, context)
    for m in BEARER.finditer(raw_code):
        sink.finding(unit, "bearer-arg", f"${m.group(1)}", line, context)

    bounds = lexed.ops + [(len(text), len(text), "end")]
    start, before, depth = 0, "start", 0
    for op_start, op_end, after in bounds:
        seg_code, seg_raw = code[start:op_start], text[start:op_start]
        seg_before, start, before = before, op_end, after
        if seg_before == "(":
            depth += 1
        elif seg_before == ")":
            depth = max(0, depth - 1)
        if seg_before == ";;" and shell.top("case"):
            shell.branch("case")
            shell.top("case").pattern = True
        tokens = list(re.finditer(r"\S+", seg_code))
        if not tokens:
            continue
        case = shell.top("case")
        if case and case.pattern and after == "pattern)" and tokens[0].group() != "esac":
            case.pattern = False
            continue
        k, header_only = 0, False
        while k < len(tokens):
            word = tokens[k].group()
            if word in ("then", "do", "!", "{", "}", "time"):
                k += 1
            elif word == "else":
                shell.branch("if", is_else=True)
                k += 1
            elif word == "elif":
                shell.branch("if")
                k += 1
            elif word == "if":
                shell.frames.append(Frame("if", shell.off))
                k += 1
            elif word in ("fi", "esac", "done"):
                shell.close({"fi": "if", "esac": "case", "done": "loop"}[word])
                header_only = True
                break
            elif word in ("for", "while", "until"):
                shell.frames.append(Frame("loop", shell.off))
                if loop_enables_tracing(seg_raw[tokens[k].end():] + text[op_end:], lines, next_index):
                    shell.off = False
                if word == "for":
                    header_only = True
                    break
                k += 1
            elif word == "case":
                frame = Frame("case", shell.off)
                frame.pattern = after != "pattern)"
                shell.frames.append(frame)
                header_only = True
                break
            else:
                break
        if header_only or k >= len(tokens):
            continue
        command_code = seg_code[tokens[k].start():].strip()
        command_raw = seg_raw[tokens[k].start():].strip()
        change = xtrace_change(command_code)
        if change == "on":
            shell.off = False
        elif change == "off" and depth == 0 and seg_before not in ("&&", "||", "|") and after not in ("|", "&"):
            shell.off = True
        assignment = ASSIGNMENT.match(command_raw)
        if assignment and FETCH.search(command_raw):
            var = assignment.group(1)
            sink.sites.append(Site(unit, line, var, shell.off, context))
            if not shell.off:
                sink.finding(unit, "traced-fetch", var, line, context)


def analyze(lines, first_line, unit, context, sink):
    shell = Shell()
    i = 0
    while i < len(lines):
        begin = i
        text = lines[i]
        i += 1
        handled = 0
        while True:
            lexed = lex(text)
            for tag in lexed.heredocs[handled:]:
                body_start = i
                while i < len(lines) and lines[i].strip() != tag:
                    i += 1
                analyze(lines[body_start:i], first_line + body_start, unit, f"heredoc {tag}", sink)
                i = min(i + 1, len(lines))
                handled += 1
            if lexed.open and i < len(lines):
                text += "\n" + lines[i]
                i += 1
                continue
            break
        if not lexed.masked.strip():
            continue
        line = first_line + begin
        if FUNCTION.match(lexed.masked):
            opener = lexed.masked.index("{")
            closers = [s for s, _, op in lexed.ops if op == "}" and s > opener]
            if closers:
                body = [text[opener + 1:closers[-1]]]
            else:
                depth, j = 1, i
                while j < len(lines):
                    stripped = lines[j].strip()
                    if not stripped.startswith("#"):
                        depth += (1 if stripped.endswith("{") else 0) - (1 if stripped.startswith("}") else 0)
                    if depth <= 0:
                        break
                    j += 1
                body, i = lines[i:j], j + 1
            analyze(body, line + (0 if closers else 1), unit, "function body", sink)
            continue
        process(shell, text, lexed, line, lines, i, unit, context, sink)


def render_terraform(body):
    """Terraform's rendering, near enough to read the result as shell, line numbers kept."""
    body = body.replace("$${", "\0DOLLAR").replace("%%{", "\0PERCENT")
    body = re.sub(r"(?m)^[ \t]*[$%]\{[^{}\n]*(?:\{[^{}\n]*\}[^{}\n]*)*\}[ \t]*$", ":", body)
    body = re.sub(r"\$\{[^{}\n]*(?:\{[^{}\n]*\}[^{}\n]*)*\}", "TFVALUE", body)
    body = re.sub(r"%\{[^{}\n]*\}", "", body)
    return body.replace("\0DOLLAR", "${").replace("\0PERCENT", "%{")


TF_HEREDOC = re.compile(r"^\s*(?:([A-Za-z_][A-Za-z0-9_-]*)\s*=)?[^#\n]*?<<-?([A-Za-z_][A-Za-z0-9_]*)\s*$")


def terraform_units():
    paths = sorted(ROOT.glob("*.tf")) + sorted(ROOT.glob("modules/*/*.tf"))
    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        lines = path.read_text().split("\n")
        names, i = {}, 0
        while i < len(lines):
            m = None if lines[i].lstrip().startswith(("#", "//")) else TF_HEREDOC.match(lines[i])
            if not m:
                i += 1
                continue
            j = i + 1
            while j < len(lines) and lines[j].strip() != m.group(2):
                j += 1
            name = m.group(1) or "heredoc"
            names[name] = names.get(name, 0) + 1
            unit = f"{rel}:{name}" if names[name] == 1 else f"{rel}:{name}#{names[name]}"
            yield unit, render_terraform("\n".join(lines[i + 1:j])).split("\n"), i + 2
            i = j + 1


def script_units(root=ROOT):
    # The test-*.sh harnesses stay out: they match fetch text in awk and grep programs and
    # fake case patterns, and none of that text fetches anything on a host.
    for path in sorted((root / "scripts").glob("*.sh")):
        if not path.name.startswith("test-"):
            yield path.relative_to(root).as_posix(), path.read_text().split("\n"), 1


def scan(units):
    sink = Sink()
    for unit, lines, first_line in units:
        analyze(lines, first_line, unit, "top level", sink)
    return sink


def scan_text(text, unit="fixture"):
    return scan([(unit, text.split("\n"), 1)])


UNITS = {unit: (lines, first) for unit, lines, first in list(terraform_units()) + list(script_units())}
REPO = scan((unit, lines, first) for unit, (lines, first) in UNITS.items())


def unit_text(unit):
    return "\n".join(UNITS[unit][0])


def ids(sink):
    return sorted(f.id for f in sink.findings)


# A line that only turns tracing off, bare or silenced: `set +x`, `{ set +x; } 2>/dev/null`.
PAUSE = re.compile(r"^\s*(?:set\s+\+x|\{\s*set\s+\+x\s*;\s*\}(?:\s*2>\s*/dev/null)?)\s*$")


# ------------------------------------------------------------------------ repository scan
class RepositoryScanTests(unittest.TestCase):
    def test_the_repository_has_no_unreviewed_secret_exposure(self):
        found = {f.id: f for f in REPO.findings}
        known = sorted(set(found) & set(EXPECTED_TO_FIX_IN_128))
        if known:
            print("\n  KNOWN OPEN, expected-to-fix-in-#128 (still reported, not passing silently):", file=sys.stderr)
            for fid in known:
                print(f"    {fid} (line {found[fid].line}): {EXPECTED_TO_FIX_IN_128[fid]}", file=sys.stderr)
        unexpected = [f"{fid} (line {f.line}, {f.context})" for fid, f in sorted(found.items())
                      if fid not in EXPECTED_TO_FIX_IN_128 and fid not in JUSTIFIED]
        self.assertEqual(unexpected, [], "a secret can reach xtrace output or a command line")
        stale = sorted((set(EXPECTED_TO_FIX_IN_128) | set(JUSTIFIED)) - set(found))
        self.assertEqual(stale, [], "these allowlist entries no longer match a finding; delete them")

    def test_every_known_secret_fetch_is_found_and_judged(self):
        # Proves the extraction still reaches the real sites, so a clean result means something.
        found = {(s.unit.split(":")[0], s.var) for s in REPO.sites}
        for site in (("dev-hop-key.tf", "HOPJSON"), ("dev-instance-common.tf", "GH_TOKEN"),
                     ("jumpbox2-user-data.tf", "FCVM_KEY"), ("scripts/dev-box-triage.sh", "CF_TOKEN"),
                     ("runner-autoscale.tf", "REG_TOKEN"), ("runner-autoscale.tf", "TOKEN6"),
                     ("runner-autoscale.tf", "TOKEN"), ("mac-dev.tf", "PW")):
            self.assertIn(site, found)
        untraced = {(s.unit, s.var) for s in REPO.sites if s.off}
        for site in (("dev-hop-key.tf:dev_hop_setup", "HOPJSON"), ("dev-instance-common.tf:gh_auth_script", "GH_TOKEN"),
                     ("jumpbox2-user-data.tf:jumpbox_2_user_data", "FCVM_KEY"),
                     ("scripts/dev-box-triage.sh", "CF_TOKEN"), ("runner-autoscale.tf:runner_user_data", "REG_TOKEN")):
            self.assertIn(site, untraced)

    def test_test_harnesses_are_left_out_of_the_scan(self):
        # A harness checks user data with an awk program like this one; it is a pattern the
        # scanner alone would read as a traced fetch, not a fetch.
        harness = ("#!/bin/bash\nset -uo pipefail\nUNPAUSED=$(awk '\n"
                   "  /latest\\/api\\/token|--with-decryption/ && !paused { print NR }\n' \"$USERDATA\")\n")
        self.assertEqual(ids(scan_text(harness)), ["fixture:traced-fetch:UNPAUSED"])
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "scripts").mkdir()
            (Path(tmp) / "scripts" / "test-runner-userdata.sh").write_text(harness)
            (Path(tmp) / "scripts" / "admin.sh").write_text(f"#!/bin/bash\nset -x\n{FETCH_LINE}\n")
            self.assertEqual(ids(scan(script_units(Path(tmp)))), ["scripts/admin.sh:traced-fetch:X"])

    def without_pause_before_fetch(self, unit, var):
        """The unit with the xtrace pause nearest above its first untraced fetch of `var` deleted.

        Not the unit's first `set +x`: a unit can pause more than once, and a fetch inside a
        heredoc of its own (REG_TOKEN can sit in one) is guarded by the pause nearest above it.
        """
        text = unit_text(unit)
        lines = text.split("\n")
        site = next((s for s in scan_text(text, unit).sites if s.var == var and s.off), None)
        self.assertIsNotNone(site, f"{unit} has no untraced fetch of {var}")
        fetch = site.line - 1
        self.assertRegex(lines[fetch], rf"\b{var}=")
        pause = next((k for k in range(fetch - 1, -1, -1) if PAUSE.match(lines[k])), None)
        self.assertIsNotNone(pause, f"{unit} has no xtrace pause above its {var} fetch")
        return "\n".join(lines[:pause] + lines[pause + 1:])

    def test_real_blocks_are_reported_once_their_protection_is_removed(self):
        for unit, var in (("dev-hop-key.tf:dev_hop_setup", "HOPJSON"),
                          ("jumpbox2-user-data.tf:jumpbox_2_user_data", "FCVM_KEY"),
                          ("dev-instance-common.tf:gh_auth_script", "GH_TOKEN"),
                          ("runner-autoscale.tf:runner_user_data", "REG_TOKEN"),
                          ("scripts/dev-box-triage.sh", "CF_TOKEN")):
            with self.subTest(unit=unit, kind=f"traced-fetch:{var}", old="the pause nearest above the fetch"):
                finding = f"{unit}:traced-fetch:{var}"
                self.assertNotIn(finding, ids(scan_text(unit_text(unit), unit)))
                self.assertIn(finding, ids(scan_text(self.without_pause_before_fetch(unit, var), unit)))
        cases = (
            ("dev-hop-key.tf:dev_hop_setup", "\nHOPJSON=$(", '\nif [ -n "$HOP_XTRACE" ]; then set -x; fi\nHOPJSON=$(',
             "traced-fetch:HOPJSON"),
            ("scripts/dev-box-triage.sh", "printf 'Authorization: Bearer %s\\n' \"$CF_TOKEN\" | curl -s --max-time 15 -H @-",
             'curl -s --max-time 15 -H "Authorization: Bearer $CF_TOKEN"', "bearer-arg:$CF_TOKEN"),
        )
        for unit, old, new, kind in cases:
            with self.subTest(unit=unit, kind=kind, old=old.strip()):
                text = unit_text(unit)
                self.assertIn(old, text)
                self.assertNotIn(f"{unit}:{kind}", ids(scan_text(text, unit)))
                self.assertIn(f"{unit}:{kind}", ids(scan_text(text.replace(old, new, 1), unit)))


# ------------------------------------------------------------------------ scanner fixtures
FETCH_LINE = "X=$(aws secretsmanager get-secret-value --secret-id s --query SecretString --output text)"


class ScannerFixtureTests(unittest.TestCase):
    def assertFlags(self, text, expected):
        self.assertEqual(ids(scan_text(text)), expected, text)

    def test_the_deliberately_leaky_fixture_fails(self):
        self.assertFlags(f"#!/bin/bash\nset -euxo pipefail\n{FETCH_LINE}\n", ["fixture:traced-fetch:X"])

    def test_a_fetch_is_fine_only_with_tracing_definitely_off(self):
        self.assertFlags(f"set -x\nset +x\n{FETCH_LINE}\n", [])
        self.assertFlags(f"set -x\n{{ set +x; }} 2>/dev/null\n{FETCH_LINE}\n", [])
        self.assertFlags(f"set -euo pipefail\n{FETCH_LINE}\n", ["fixture:traced-fetch:X"])
        self.assertFlags(f"case $- in *x*) R=1 ;; *) R= ;; esac\nset +x\n{FETCH_LINE}\nif [ -n \"$R\" ]; then set -x; fi\n", [])

    def test_every_fetch_shape_is_recognised(self):
        for fetch in ('T=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")',
                      'T="$(aws ssm get-parameter --name /p --with-decryption --query Parameter.Value --output text)"',
                      "local T=$(gh auth token)",
                      "export T=`aws secretsmanager get-secret-value --secret-id s`",
                      "if ! T=$(aws ssm get-parameter --name /p --with-decryption); then exit 1; fi",
                      "T=$(aws secretsmanager get-secret-value \\\n  --secret-id s --output text)"):
            with self.subTest(fetch=fetch):
                self.assertFlags(f"set -x\n{fetch}\n", ["fixture:traced-fetch:T"])

    def test_a_disable_that_might_not_run_does_not_count(self):
        for guard in ('[ -n "$Q" ] && set +x', "true || set +x", "( set +x )", "set +x | cat", "set +x &",
                      "if [ -n \"$Q\" ]; then\n  set +x\nfi", "if a; then :; else set +x; fi",
                      "case $a in\n  y) set +x ;;\nesac", "for i in 1; do set +x; done",
                      "f() {\n  set +x\n}\nf"):
            with self.subTest(guard=guard):
                self.assertFlags(f"set -x\n{guard}\n{FETCH_LINE}\n", ["fixture:traced-fetch:X"])

    def test_a_disable_covers_its_own_branch(self):
        self.assertFlags(f"set -x\nif a; then\n  :\nelse\n  set +x\n  {FETCH_LINE}\nfi\n", [])
        self.assertFlags(f"set -x\ncase $a in\n  y) set +x; {FETCH_LINE} ;;\nesac\n", [])
        self.assertFlags(f"set +x\nif a; then\n  :\nfi\n{FETCH_LINE}\n", [])

    def test_any_restore_before_the_fetch_counts(self):
        for restore in ('if [ -n "$R" ]; then set -x; fi', "set -o xtrace", "eval 'set -x'", "export SHELLOPTS",
                        "case $a in\n  y) set -x ;;\nesac"):
            with self.subTest(restore=restore):
                self.assertFlags(f"set +x\n{restore}\n{FETCH_LINE}\n", ["fixture:traced-fetch:X"])

    def test_a_loop_body_that_turns_tracing_back_on_traces_the_next_iteration(self):
        self.assertFlags(f"set +x\nfor i in 1 2; do\n  {FETCH_LINE}\n  set -x\ndone\n", ["fixture:traced-fetch:X"])
        self.assertFlags(f"set +x\nfor i in 1 2; do\n  {FETCH_LINE}\ndone\n", [])

    def test_heredoc_and_function_bodies_are_separate_shells(self):
        self.assertFlags(f"set +x\nsudo -u ubuntu bash <<'EOF'\n{FETCH_LINE}\nEOF\n", ["fixture:traced-fetch:X"])
        self.assertFlags(f"set -x\nsudo -u ubuntu bash <<'EOF'\nset +x\n{FETCH_LINE}\nEOF\n", [])
        self.assertFlags(f"set +x\nfetch() {{\n  {FETCH_LINE}\n}}\n", ["fixture:traced-fetch:X"])
        self.assertFlags(f"set +x\nfetch() {{ {FETCH_LINE}; }}\n", ["fixture:traced-fetch:X"])
        # The parent keeps its own state across a child body that turns tracing on.
        self.assertFlags(f"set +x\nbash <<'EOF'\nset -x\nEOF\n{FETCH_LINE}\n", [])

    def test_non_shell_text_does_not_confuse_the_block_tracking(self):
        python = "python3 - <<'PY'\nif x:\n    for y in z:\n        pass\nPY\n"
        inline = "python3 -c '\nif x:\n    for y in z: pass\n'\n"
        for body in (python, inline, "cat > /etc/x.conf <<EOF\ncase \"it's\"\nEOF\n"):
            with self.subTest(body=body[:14]):
                self.assertFlags(f"set -x\n{body}set +x\n{FETCH_LINE}\n", [])

    def test_secrets_on_a_command_line_are_reported(self):
        self.assertFlags('sudo -u ubuntu ./config.sh --url u --token "$REG_TOKEN"\n', ["fixture:secret-arg:--token $REG_TOKEN"])
        self.assertFlags("tool --api-password=$PW\n", ["fixture:secret-arg:--api-password $PW"])
        self.assertFlags('curl -H "Authorization: Bearer ${CF_TOKEN}" https://x\n', ["fixture:bearer-arg:$CF_TOKEN"])
        self.assertFlags('curl -H "Authorization: token $PAT" https://x\n', ["fixture:bearer-arg:$PAT"])

    def test_safe_command_lines_are_not_reported(self):
        for line in ('printf \'%s\' "$REG_TOKEN" | sudo -u ubuntu bash -c \'exec ./config.sh --token "$(cat)"\'',
                     "printf 'Authorization: Bearer %s\\n' \"$T\" | curl -s -H @- https://x",
                     'curl -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/mac',
                     "aws secretsmanager get-secret-value --secret-id s --output text > /etc/creds.json",
                     '# curl -H "Authorization: Bearer $T" --token "$T"'):
            with self.subTest(line=line):
                self.assertFlags(f"set -x\n{line}\n", [])


# --------------------------------------------------------------------------- log permissions
SELFUPDATE_TF = (ROOT / "dev-selfupdate.tf").read_text()
SELFUPDATE_SETUP = SELFUPDATE_TF.split("  selfupdate_setup = <<-EOT\n", 1)[1].split("\n  EOT\n", 1)[0]
SELFUPDATE_SH = render_terraform(SELFUPDATE_SETUP.split("<<'SELFUPD'\n", 1)[1].split("\nSELFUPD\n", 1)[0]) + "\n"
LOGROTATE = SELFUPDATE_SETUP.split("cat > /etc/logrotate.d/dev-selfupdate <<'LOGROTATE'\n", 1)[1].split("\nLOGROTATE\n", 1)[0]
NEXTJS_TF = (ROOT / "nextjs-user-data.tf").read_text()
SETUP_SYNC = render_terraform(NEXTJS_TF.split("<<'SETUPSYNC'\n", 1)[1].split("\nSETUPSYNC\n", 1)[0]) + "\n"

INSTALL_STUB = """#!/bin/bash
printf 'install %s\\n' "$*" >> "$FAKE_CALLS"
mode=""; args=()
while [ $# -gt 0 ]; do
  case "$1" in
    -m) mode=$2; shift 2 ;;
    -o|-g) shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
: > "${args[-1]}"
[ -z "$mode" ] || chmod "$mode" "${args[-1]}"
"""
CHGRP_STUB = "#!/bin/sh\nprintf 'chgrp %s\\n' \"$*\" >> \"$FAKE_CALLS\"\n"


def write_stubs(directory, stubs):
    os.makedirs(directory, exist_ok=True)
    for name, body in stubs.items():
        path = os.path.join(directory, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)


class LogPermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "run.log")
        self.calls = os.path.join(self.tmp, "calls")
        self.mode_in_run = os.path.join(self.tmp, "mode-in-run")
        self.mode_at_fetch = os.path.join(self.tmp, "mode-at-fetch")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def env(self, stubs, **extra):
        write_stubs(stubs, {"install": INSTALL_STUB, "chgrp": CHGRP_STUB, "systemctl": "#!/bin/sh\nexit 0\n"})
        return dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}", FAKE_CALLS=self.calls, FAKE_LOG=self.log,
                    FAKE_MODE_IN_RUN=self.mode_in_run, FAKE_MODE_AT_FETCH=self.mode_at_fetch, **extra)

    def existing_log(self, text):
        with open(self.log, "w") as fh:
            fh.write(text)
        os.chmod(self.log, 0o644)

    def mode(self):
        return oct(os.stat(self.log).st_mode & 0o777)

    def read(self, path):
        with open(path) as fh:
            return fh.read()

    def run_selfupdate(self, fail_fetch=False):
        stubs = os.path.join(self.tmp, "stubs")
        env = self.env(stubs, FAKE_FETCH_FAIL="1" if fail_fetch else "")
        write_stubs(stubs, {"aws": """#!/bin/bash
stat -c %a "$FAKE_LOG" > "$FAKE_MODE_AT_FETCH" 2>/dev/null || echo missing > "$FAKE_MODE_AT_FETCH"
[ "$FAKE_FETCH_FAIL" = 1 ] && exit 1
printf '%s\\n' 'echo applied-marker' 'stat -c %a "$FAKE_LOG" > "$FAKE_MODE_IN_RUN"' > "$4"
"""})
        script = (SELFUPDATE_SH.replace("/var/log/dev-selfupdate.log", self.log)
                  .replace("/var/lib/dev-selfupdate", os.path.join(self.tmp, "state")))
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)

    def test_selfupdate_tightens_an_existing_log_before_writing_and_keeps_its_contents(self):
        self.existing_log("two months of earlier runs\n")
        proc = self.run_selfupdate()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(self.mode_at_fetch).strip(), "640")
        self.assertEqual(self.read(self.mode_in_run).strip(), "640")
        self.assertEqual(self.mode(), "0o640")
        content = self.read(self.log)
        self.assertTrue(content.startswith("two months of earlier runs\n"), content)
        self.assertIn("applied-marker", content)
        self.assertIn(f"chgrp adm {self.log}", self.read(self.calls))
        self.assertNotIn("install", self.read(self.calls))

    def test_selfupdate_fetch_failure_is_logged_to_a_tightened_log(self):
        self.existing_log("earlier\n")
        self.run_selfupdate(fail_fetch=True)
        self.assertEqual(self.mode(), "0o640")
        self.assertRegex(self.read(self.log), r"\Aearlier\n.* fetch failed\n\Z")

    def test_selfupdate_creates_a_missing_log_root_adm_0640(self):
        self.run_selfupdate()
        self.assertIn(f"install -m 0640 -o root -g adm /dev/null {self.log}", self.read(self.calls))
        self.assertEqual(self.read(self.mode_at_fetch).strip(), "640")
        self.assertEqual(self.mode(), "0o640")

    def run_setup_sync(self):
        stubs = os.path.join(self.tmp, "stubs")
        env = self.env(stubs)
        write_stubs(stubs, {"aws": """#!/bin/bash
case "$1 $2" in
  "s3api head-object") echo '"etag-2"' ;;
  "s3 cp") printf '%s\\n' 'echo sync-run-output' 'stat -c %a "$FAKE_LOG" > "$FAKE_MODE_IN_RUN"' > "$4" ;;
  *) exit 1 ;;
esac
"""})
        script = (SETUP_SYNC.replace("/var/log/setup-sync.log", self.log)
                  .replace("/var/lib/nextjs-setup", os.path.join(self.tmp, "state"))
                  .replace("/var/lib/ndev", os.path.join(self.tmp, "ndev"))
                  .replace("/tmp/nextjs-setup.", os.path.join(self.tmp, "nextjs-setup."))
                  .replace("/tmp/setup-syntax.err", os.path.join(self.tmp, "setup-syntax.err")))
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)

    def test_setup_sync_tightens_its_log_before_the_run_writes_to_it(self):
        self.existing_log("previous run\n")
        proc = self.run_setup_sync()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("setup-sync: healthy, recorded etag", proc.stdout)
        self.assertEqual(self.read(self.mode_in_run).strip(), "640")
        self.assertEqual(self.mode(), "0o640")
        self.assertEqual(self.read(self.log), "sync-run-output\n")
        self.assertIn(f"chgrp adm {self.log}", self.read(self.calls))

    def test_setup_sync_creates_a_missing_log_root_adm_0640(self):
        self.run_setup_sync()
        self.assertIn(f"install -m 0640 -o root -g adm /dev/null {self.log}", self.read(self.calls))
        self.assertEqual(self.read(self.mode_in_run).strip(), "640")

    def test_logrotate_recreates_the_selfupdate_log_0640_root_adm(self):
        self.assertIn("/var/log/dev-selfupdate.log {", LOGROTATE)
        self.assertRegex(LOGROTATE, r"(?m)^\s+create 0640 root adm$")
        self.assertNotIn("copytruncate", LOGROTATE)
        self.assertEqual((ROOT / "dev-user-data.tf").read_text().count("${local.selfupdate_setup}"), 2)

    def test_the_published_scripts_tighten_old_logs_on_their_first_run(self):
        # The first run of a fixed script happens inside the OLD updater, whose log is 0644.
        outside_body = SELFUPDATE_SETUP.replace(SELFUPDATE_SETUP.split("<<'SELFUPD'\n", 1)[1].split("\nSELFUPD\n", 1)[0], "")
        self.assertIn("chmod 0640 /var/log/dev-selfupdate.log", outside_body)
        nextjs_outside = NEXTJS_TF.split("<<'SETUPSYNC'\n", 1)[0]
        self.assertIn("chmod 0640 /var/log/setup-sync.log", nextjs_outside.split("setup self-update", 1)[1])

    def test_updaters_are_replaced_by_rename_never_rewritten_while_running(self):
        # bash reads a running script from its byte offset; an in-place rewrite by the script
        # it is running makes it resume mid-line in the new text.
        self.assertNotRegex(SELFUPDATE_SETUP, r"cat > /usr/local/bin/dev-selfupdate\.sh <<")
        self.assertIn("mv -f /usr/local/bin/dev-selfupdate.sh.new /usr/local/bin/dev-selfupdate.sh", SELFUPDATE_SETUP)
        self.assertNotRegex(NEXTJS_TF, r"cat > /usr/local/bin/setup-sync <<")
        self.assertIn("mv -f /usr/local/bin/setup-sync.new /usr/local/bin/setup-sync", NEXTJS_TF)


# ------------------------------------------------------------------- dev-box-triage token
TRIAGE = (ROOT / "scripts" / "dev-box-triage.sh").read_text()
TUNNEL_BLOCK = TRIAGE.split('hdr "Cloudflare tunnels (authoritative)"\n', 1)[1].split("\n# ----", 1)[0]
CF_CANARY = "FAKE-CF-TOKEN-CANARY-0123456789"
TUNNEL_JSON = '{"result":{"status":"healthy","connections":[{},{}]}}'


class TriageTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.argv = os.path.join(self.tmp, "curl-argv")
        self.stdin = os.path.join(self.tmp, "curl-stdin")

    def run_block(self, block, trace, curl_stub):
        stubs = os.path.join(self.tmp, "stubs")
        write_stubs(stubs, {"aws": f"#!/bin/sh\nprintf '%s\\n' '{CF_CANARY}'\n", "curl": curl_stub})
        prelude = (f"set -{'x' if trace else ''}uo pipefail\nsay() {{ printf '%s\\n' \"$*\"; }}\n"
                   "REGION=us-west-1; ACCOUNT=fake-account; CC_GAMES_TUNNEL=tunnel-a; DOLPHIN_TUNNEL=tunnel-b; verdict_local=0\n")
        script = prelude + block + '\necho "after-tunnel-block token=${CF_TOKEN-unset}"\n'
        env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
        env.update(PATH=f"{stubs}:{os.environ['PATH']}", FAKE_ARGV=self.argv, FAKE_STDIN=self.stdin, NO_PROXY="127.0.0.1")
        return subprocess.run(["bash", "-c", script], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=90)

    def fake_curl(self):
        return ("#!/bin/bash\nprintf '%s\\n' \"$*\" >> \"$FAKE_ARGV\"\ncat >> \"$FAKE_STDIN\"\n"
                f"printf '%s' '{TUNNEL_JSON}'\n")

    def read(self, path):
        with open(path) as fh:
            return fh.read()

    def test_the_bearer_pattern_is_gone_from_the_script(self):
        self.assertNotIn("Bearer $", TRIAGE)
        self.assertIn("-H @-", TUNNEL_BLOCK)

    def test_the_token_reaches_curl_on_stdin_and_never_argv_or_the_trace(self):
        proc = self.run_block(TUNNEL_BLOCK, trace=True, curl_stub=self.fake_curl())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(CF_CANARY, proc.stdout + proc.stderr)
        self.assertNotIn(CF_CANARY, self.read(self.argv))
        self.assertEqual(self.read(self.stdin), f"Authorization: Bearer {CF_CANARY}\n" * 2)
        self.assertIn("cc-games      status=healthy  connections=2", proc.stdout)
        self.assertIn("+ echo 'after-tunnel-block token=unset'", proc.stderr)
        self.assertIn("after-tunnel-block token=unset", proc.stdout)

    def test_tracing_stays_off_for_an_untraced_caller(self):
        proc = self.run_block(TUNNEL_BLOCK, trace=False, curl_stub=self.fake_curl())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse([line for line in proc.stderr.splitlines() if line.startswith("+")], proc.stderr)

    def test_the_harness_catches_both_leaks(self):
        on_argv = TUNNEL_BLOCK.replace("printf 'Authorization: Bearer %s\\n' \"$CF_TOKEN\" | curl -s --max-time 15 -H @-",
                                       'curl -s --max-time 15 -H "Authorization: Bearer $CF_TOKEN"')
        self.assertNotEqual(on_argv, TUNNEL_BLOCK)
        self.run_block(on_argv, trace=False, curl_stub=self.fake_curl())
        self.assertIn(CF_CANARY, self.read(self.argv))
        traced = TUNNEL_BLOCK.replace("{ set +x; } 2>/dev/null\n", "", 1)
        self.assertNotEqual(traced, TUNNEL_BLOCK)
        self.assertIn(CF_CANARY, self.run_block(traced, trace=True, curl_stub=self.fake_curl()).stderr)

    def test_real_curl_sends_the_header_it_reads_from_stdin(self):
        # A stub cannot prove curl honours `-H @-`; a loopback server can.
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                body = TUNNEL_JSON.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        real_curl = shutil.which("curl")
        self.assertTrue(real_curl, "curl is required for this test")
        wrapper = f"#!/bin/bash\nprintf '%s\\n' \"$*\" >> \"$FAKE_ARGV\"\nexec {real_curl} \"$@\"\n"
        block = TUNNEL_BLOCK.replace("https://api.cloudflare.com", f"http://127.0.0.1:{server.server_address[1]}")
        proc = self.run_block(block, trace=True, curl_stub=wrapper)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sorted(seen), [("/client/v4/accounts/fake-account/cfd_tunnel/tunnel-a", f"Bearer {CF_CANARY}"),
                                        ("/client/v4/accounts/fake-account/cfd_tunnel/tunnel-b", f"Bearer {CF_CANARY}")])
        self.assertNotIn(CF_CANARY, self.read(self.argv) + proc.stdout + proc.stderr)
        self.assertIn("dolphin-labs  status=healthy  connections=2", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
