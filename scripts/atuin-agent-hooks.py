"""Keep exactly one Atuin shell-history hook per event for Claude Code and Codex.

Atuin's installer (setup.atuin.sh, since Atuin v18.14.0) adds these hooks whenever it
finds ~/.claude or ~/.codex, and the shared shell setup runs that installer on every boot.
Left alone, two things go wrong:

- Codex runs hooks without ~/.atuin/bin on PATH, so a bare `atuin hook codex` exits 127 on
  every Bash tool call.
- Once a hook is rewritten to the absolute binary, the next install no longer recognises it
  and appends another copy. dolphin reached 30 or more per event.

This points every Atuin hook at the absolute binary, keeps the first one per event (with
its matcher), drops the other Atuin copies, and leaves every other hook and setting alone.
Without the Atuin binary the Atuin hooks are removed instead. Run as the user whose config
it is:

    python3 atuin-agent-hooks.py [HOME]
"""
import json
import os
import re
import sys
import tempfile

AGENTS = ((".claude/settings.json", "claude-code"), (".codex/hooks.json", "codex"))
HOOK = re.compile(r"^(?:\S*/)?atuin hook (claude-code|codex)$")


def normalize(doc, agent, binary):
    """Normalize the agent's Atuin hooks in doc, in place. Returns True if doc changed.

    binary is the absolute Atuin path, or None to remove the Atuin hooks entirely.
    """
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return False
    wanted = None if binary is None else binary + " hook " + agent
    changed = False
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept = False
        result = []
        for entry in entries:
            inner = entry.get("hooks") if isinstance(entry, dict) else None
            if not isinstance(inner, list):
                result.append(entry)
                continue
            new_inner = []
            for hook in inner:
                command = hook.get("command") if isinstance(hook, dict) else None
                match = HOOK.match(command) if isinstance(command, str) else None
                if match is None or match.group(1) != agent:
                    new_inner.append(hook)
                elif wanted is None or kept:
                    changed = True
                else:
                    kept = True
                    if command != wanted:
                        hook = dict(hook, command=wanted)
                        changed = True
                    new_inner.append(hook)
            if new_inner:
                result.append(dict(entry, hooks=new_inner))
            else:
                changed = True
        if result:
            hooks[event] = result
        elif entries:
            del hooks[event]
    return changed


def write_json(path, doc):
    """Replace path atomically, keeping its permission bits."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".atuin-hooks-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv):
    home = argv[1] if len(argv) > 1 else os.path.expanduser("~")
    binary = os.path.join(home, ".atuin", "bin", "atuin")
    if not os.access(binary, os.X_OK):
        binary = None
    for relative, agent in AGENTS:
        path = os.path.join(home, relative)
        try:
            with open(path) as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and normalize(doc, agent, binary):
            write_json(path, doc)
            print("atuin agent hooks: normalized " + relative)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
