#!/usr/bin/env python3
"""agent-active-dirs -- the folders that should have a Claude session: interactive in the last 30 days.

A folder qualifies when, within --days (default 30), either:
  * a person prompted a Claude Code conversation there: a user turn typed at the terminal, sent through
    Remote Control or queued (Claude records origin.kind "human"). Tool results, command output, meta and
    subagent turns, background-task notices and SDK-injected turns are not prompts. An entry from a Claude
    Code that did not record an origin counts unless it is a settings command (/model, /effort, ...), which
    tools broadcast to every window; or
  * an agent or person marked it for interactive use: the file `claude-interactive` in its git directory,
    touched within the window. `touch "$(git rev-parse --git-dir)/claude-interactive"` marks a clone or a
    worktree; the git directory is never tracked, so the marker never shows in `git status`.
Each prompt's working directory maps to its git root (a linked worktree is its own root). Folders that no
longer exist, lie outside HOME, or sit under .claude/worktrees (subagent scratch) are left out.

Used by every boot launcher (fcvm-claude-active-repos, nextjs-dev's agents-start) and by agent-session-sync
after a reboot, so they cannot disagree. Prints one folder per line, most recent first; --explain adds why.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime

MARKER = "claude-interactive"
DEFAULT_ROOTS = ("~/*", "~/src/*")
SYNTHETIC = ("<local-command-stdout>", "<local-command-stderr>", "<local-command-caveat>", "Caveat: The messages below")
# Commands that change a session's settings. Tools send them to every window at once, so they say nothing
# about which folders a person works in.
SETTINGS_COMMANDS = ("/model", "/effort", "/remote-control", "/rc", "/login", "/logout", "/status", "/config",
                     "/permissions", "/output-style", "/theme", "/fast")
HUMAN_SOURCES = ("typed", "queued", "remote", "remote-control", "bridge")


def prompt_text(msg):
    c = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        texts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(texts) if texts else None
    return None


def settings_command(text):
    t = text.lstrip()
    if t.startswith("<command-name>"):
        t = t[len("<command-name>"):]
    words = t.split("<", 1)[0].split(None, 1)
    return bool(words) and words[0] in SETTINGS_COMMANDS


def is_prompt(obj):
    """A turn a person sent into the session."""
    if obj.get("type") != "user" or obj.get("isSidechain") or obj.get("isMeta"):
        return False
    msg = obj.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    text = prompt_text(msg)
    if not (text and text.strip()) or text.lstrip().startswith(SYNTHETIC):
        return False
    origin = obj.get("origin")
    if isinstance(origin, dict) and origin.get("kind"):
        return origin.get("kind") == "human"
    if obj.get("turnOrigin"):
        return obj.get("turnOrigin") == "human"
    if obj.get("promptSource"):
        return obj.get("promptSource") in HUMAN_SOURCES
    return not settings_command(text)          # recorded before Claude Code tagged origins


def stamp(obj):
    ts = obj.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def git_root(path, cache):
    if path not in cache:
        try:
            r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, timeout=20)
            cache[path] = r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            cache[path] = None
    return cache[path]


def prompted(claude_dir, cutoff):
    """cwd -> (newest prompt time, prompt count), from transcripts written since the cutoff."""
    seen = {}
    for f in glob.glob(os.path.join(claude_dir, "projects", "*", "*.jsonl")):
        try:
            if os.path.getmtime(f) < cutoff:
                continue
            fh = open(f, "rb")
        except OSError:
            continue
        with fh:
            for raw in fh:
                if b'"type":"user"' not in raw or b'"cwd"' not in raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or not is_prompt(obj):
                    continue
                t, cwd = stamp(obj), obj.get("cwd")
                if t is None or t < cutoff or not isinstance(cwd, str):
                    continue
                last, n = seen.get(cwd, (0, 0))
                seen[cwd] = (max(last, t), n + 1)
    return seen


def marked(roots, cutoff):
    """folder -> marker time, for checkouts under the roots and their linked worktrees (wherever they live)."""
    out = {}
    for pattern in roots:
        for top in glob.glob(os.path.expanduser(pattern)):
            gitdir = os.path.join(top, ".git")
            if not os.path.isdir(gitdir) or os.path.islink(gitdir):
                continue
            try:
                t = os.path.getmtime(os.path.join(gitdir, MARKER))
                if t >= cutoff:
                    out[top] = max(out.get(top, 0), t)
            except OSError:
                pass
            for admin in glob.glob(os.path.join(gitdir, "worktrees", "*")):
                try:
                    t = os.path.getmtime(os.path.join(admin, MARKER))
                    if t < cutoff:
                        continue
                    with open(os.path.join(admin, "gitdir")) as fh:
                        wt = os.path.dirname(fh.read().strip())
                except (OSError, ValueError):
                    continue
                if wt:
                    out[wt] = max(out.get(wt, 0), t)
    return out


def active_dirs(claude_dir, home, days=30, roots=DEFAULT_ROOTS, now=None):
    """[(folder, {"time": t, "prompts": n, "marked": bool})], most recent first."""
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    home_real = os.path.realpath(home)
    roots = [r.replace("~", home, 1) if r.startswith("~") else r for r in roots]
    cache, out = {}, {}
    for cwd, (t, n) in prompted(claude_dir, cutoff).items():
        if not os.path.isdir(cwd):
            continue
        root = git_root(cwd, cache)
        if root:
            e = out.setdefault(root, {"time": 0, "prompts": 0, "marked": False})
            e["time"], e["prompts"] = max(e["time"], t), e["prompts"] + n
    for folder, t in marked(roots, cutoff).items():
        if os.path.isdir(folder):
            e = out.setdefault(folder, {"time": 0, "prompts": 0, "marked": False})
            e["time"], e["marked"] = max(e["time"], t), True
    keep = []
    for folder, e in out.items():
        real = os.path.realpath(folder)
        if not real.startswith(home_real + os.sep) or "/.claude/worktrees/" in real + "/":
            continue
        keep.append((folder, e))
    return sorted(keep, key=lambda kv: -kv[1]["time"])


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--home", default=os.path.expanduser("~"))
    ap.add_argument("--claude-dir", default=None, help="default: $CLAUDE_CONFIG_DIR, else HOME/.claude")
    ap.add_argument("--root", action="append", help="glob of checkouts to look for markers in (default ~/* and ~/src/*)")
    ap.add_argument("--explain", action="store_true")
    a = ap.parse_args(argv)
    claude_dir = a.claude_dir or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(a.home, ".claude")
    now = time.time()
    for folder, e in active_dirs(claude_dir, a.home, a.days, a.root or DEFAULT_ROOTS, now):
        if a.explain:
            why = ", ".join(x for x in ("%d prompts" % e["prompts"] if e["prompts"] else "", "marked" if e["marked"] else "") if x)
            print("%s\t%.1fd ago\t%s" % (folder, (now - e["time"]) / 86400, why))
        else:
            print(folder)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
