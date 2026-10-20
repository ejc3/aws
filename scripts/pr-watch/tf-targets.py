#!/usr/bin/env python3
"""The PR watch's targeted apply (scripts/pr-watch/SKILL.md, step 4): which Terraform addresses a merge changed.

    python3 -I tf-targets.py <ejc3/aws checkout> <commit before the merge> <merge commit>

Prints one `-target=ADDRESS` per line and exits 0, or prints `FULL: <reason>` and exits 3 when only a full plan is safe,
or prints nothing and exits 0 when the merge touched nothing Terraform reads. Both sides of the diff count, so a deleted
block is targeted too (Terraform plans the destroy of a targeted address that left the configuration).

What maps to targets, and how:
  - a changed top-level `resource`, `data` or `module` block in a root *.tf file: that block;
  - a changed attribute of a `locals` block, or a changed `variable` block: every block that reads `local.NAME` /
    `var.NAME`, following locals that read other locals;
  - a changed file that a block reads by path (`file()`, `templatefile()`, `filesha256()`, ...): the blocks naming it;
  - a changed file under `modules/NAME/`: every `module` block whose source is `./modules/NAME`;
  - then every block that reads one of those addresses (its dependents), since -target takes only dependencies.
An `ephemeral` block is walked like a resource (a changed one, or one reading a changed address, brings every block that
reads `ephemeral.TYPE.NAME`) but is never printed itself: it has no state, and targeting its readers plans it.
Anything else Terraform reads is FULL: `provider`, `terraform`, `moved`, `import`, `removed` and `check` blocks, a root
`output` block (a targeted apply would leave its value in state as it was), the lock file, a change outside any block, a
reference the parser cannot follow, and a changed file that fileset(), a source_dir or an interpolated path may read, or
that no block names literally (NEVER_READ lists the tests, docs and CI files that need no plan). A file named in a module
block's own directory (modules/NAME/...) targets that module.
"""
import re
import subprocess
import sys

BLOCK = re.compile(r'^(resource|data|ephemeral|module|locals|variable|output|provider|terraform|moved|import|removed|check)\b'
                   r'(?:\s+"([^"]+)")?(?:\s+"([^"]+)")?\s*\{\s*$')
FULL_KINDS = {"provider", "terraform", "moved", "import", "removed", "check"}
NODE_KINDS = ("resource", "data", "ephemeral", "module")  # blocks with an address that others read
READS_FULL = {"provider", "terraform", "check"}  # a reader of this kind makes the change a full plan; moved, import and
# removed blocks only name addresses (from, to, id), they do not read them
MAX_TARGETS = 80  # past this a targeted plan is no longer the quick path; plan everything


# Files Terraform never reads, by this repository's layout: tests, documentation, CI and review settings. A changed file
# that is neither named literally by a block nor on this list makes the plan FULL rather than a guess.
# Checked after a literal match and before the indirect patterns (metal-box-tools.tf's `scripts/${each.value}` would
# otherwise cover every test). A file that matches neither is FULL.
NEVER_READ = [r"scripts/test-[^/]*\.py", r"[^/]*\.md", r"\.github/.*", r"\.greptile/.*", r"\.claude/.*",
              r"\.gitignore", r"LICENSE"]


class Full(Exception):
    pass


def glob_regex(glob):
    """A path glob as a regex: `**` crosses directories, `*` and `?` do not."""
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif glob[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif glob[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return re.compile(out)


def indirect_patterns(tree):
    """Paths read through fileset(), a source_dir, or a path with an interpolation in it, as (glob, regex). A path with
    no literal part (`${path.module}/${src}`) is skipped: its value comes from another of these patterns."""
    globs = set()
    for text in tree.values():
        for m in re.finditer(r'fileset\(\s*(?:path\.module|"\$\{path\.module\}(?:/([^"]*))?")\s*,\s*"([^"]+)"', text):
            globs.add(((m.group(1) + "/") if m.group(1) else "") + m.group(2))
        for m in re.finditer(r'source_dir\s*=\s*"\$\{path\.module\}/([^"]+)"', text):
            globs.add(m.group(1).rstrip("/") + "/**")
        for m in re.finditer(r'"\$\{path\.module\}/([^"]*\$\{[^"]*)"', text):
            g = re.sub(r"\$\{[^}]*\}", "*", m.group(1))
            if g.strip("*/"):
                globs.add(g)
    return [(g, glob_regex(g)) for g in sorted(globs)]


def git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=True).stdout


HEREDOC = re.compile(r"<<-?([A-Za-z_][A-Za-z0-9_]*)\s*$")


def heredoc_lines(lines):
    """1-based numbers of the lines inside heredocs (their end marker included): a column-zero `}` or `  name =` there is
    text, not Terraform (nextjs-ct-user-data.tf's user data closes shell functions at column zero inside a locals block)."""
    inside, marker = set(), None
    for n, line in enumerate(lines, 1):
        if marker is not None:
            inside.add(n)
            if line.strip() == marker:
                marker = None
        elif (m := HEREDOC.search(line)):
            marker = m.group(1)
    return inside


def blocks(text):
    """Top-level blocks of a terraform-fmt'ed file: (kind, label1, label2, first line, last line), 1-based."""
    out, cur = [], None
    lines = text.splitlines()
    skip = heredoc_lines(lines)
    for n, line in enumerate(lines, 1):
        if n in skip:
            continue
        if cur is None:
            m = BLOCK.match(line)
            if m:
                cur = [m.group(1), m.group(2), m.group(3), n]
        elif line == "}":
            out.append((*cur, n))
            cur = None
    return out


def address(kind, a, b):
    return {"resource": f"{a}.{b}", "data": f"data.{a}.{b}", "ephemeral": f"ephemeral.{a}.{b}",
            "module": f"module.{a}"}.get(kind)


def local_names(text, first, last):
    """Each attribute of a locals block with its line range: {name: (start, end)}. A range stops at its last line of
    value: the blank lines and attribute-level comments (`  # ...`) after it introduce the next local, not this one."""
    lines = text.splitlines()
    skip = heredoc_lines(lines)
    starts = [(n, m.group(1)) for n in range(first + 1, last)
              if n not in skip and (m := re.match(r"^  ([A-Za-z_][A-Za-z0-9_-]*)\s*=", lines[n - 1]))]
    out = {}
    for i, (n, name) in enumerate(starts):
        end = (starts[i + 1][0] - 1) if i + 1 < len(starts) else last - 1
        while end > n and (lines[end - 1].strip() == "" or re.match(r"^  #", lines[end - 1])):
            end -= 1
        out[name] = (n, end)
    return out


def changed_lines(root, base, head, path):
    """Old-side and new-side changed line numbers of one file."""
    old, new = set(), set()
    for line in git(root, "diff", "--no-renames", "-U0", base, head, "--", path).splitlines():
        m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if m:
            o, oc, nn, nc = int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1)
            # A pure insertion has no old-side lines and a pure deletion no new-side ones: the line next to it did not
            # change, and counting it would pull in the neighbouring block (or local).
            old |= set(range(o, o + oc))
            new |= set(range(nn, nn + nc))
    return old, new


def show(root, rev, path):
    try:
        return git(root, "show", f"{rev}:{path}")
    except subprocess.CalledProcessError:
        return ""


def targets(root, base, head):
    files = [f for f in git(root, "diff", "--no-renames", "--name-only", base, head).splitlines() if f]
    found, locals_hit, vars_hit, paths_hit = set(), set(), set(), set()
    for f in files:
        if f == ".terraform.lock.hcl":
            raise Full("the provider lock file changed")
        if f.startswith("modules/"):
            paths_hit.add("module:" + f.split("/")[1])
            continue
        if not (f.endswith(".tf") and "/" not in f):
            paths_hit.add(f)
            continue
        old_lines, new_lines = changed_lines(root, base, head, f)
        for rev, lines in ((base, old_lines), (head, new_lines)):
            text = show(root, rev, f)
            bs = blocks(text)
            for n in lines:
                if n > len(text.splitlines()):
                    continue  # a pure deletion at the end of the file
                hit = [b for b in bs if b[3] <= n <= b[4]]
                if not hit:
                    if text.splitlines()[n - 1].strip() == "" or text.splitlines()[n - 1].lstrip().startswith("#"):
                        continue
                    raise Full(f"{f}:{n} changed outside any block")
                kind, a, b, first, last = hit[0]
                if kind in FULL_KINDS:
                    raise Full(f"a {kind} block changed ({f})")
                if kind == "output":
                    raise Full(f"output change: output.{a} ({f}); a targeted apply would not record it")
                if kind == "variable":
                    vars_hit.add(a)
                elif kind == "locals":
                    names = [name for name, (s, e) in local_names(text, first, last).items() if s <= n <= e]
                    if not names:
                        # The block's own braces, a blank or a comment line: the named locals carry the change.
                        if n in (first, last) or text.splitlines()[n - 1].strip() == "" or \
                                text.splitlines()[n - 1].lstrip().startswith("#"):
                            continue
                        raise Full(f"{f}:{n} changed inside locals but not inside a named local")
                    locals_hit.update(names)
                else:
                    found.add(address(kind, a, b))
    # Follow references in the merged tree: files to the blocks naming them, then locals and variables to their readers.
    tree = {f: show(root, head, f) for f in git(root, "ls-tree", "--name-only", head).splitlines() if f.endswith(".tf")}
    indirect = indirect_patterns(tree)
    for p in paths_hit:
        if p.startswith("module:"):
            name = p.split(":", 1)[1]
            pat = re.compile(r'source\s*=\s*"\./modules/%s"' % re.escape(name))
        else:
            pat = re.compile(r'[/"]%s"' % re.escape(p))
            if not any(pat.search(text) for text in tree.values()):
                if any(re.fullmatch(rx, p) for rx in NEVER_READ):
                    continue
                hits = [glob for glob, rx in indirect if rx.fullmatch(p)]
                if hits:
                    raise Full(f"{p} may be read through a computed path or fileset ({hits[0]})")
                raise Full(f"{p} changed and no block names it literally; it may be read indirectly")
        for f, text in tree.items():
            for kind, a, b, first, last in blocks(text):
                if not pat.search("\n".join(text.splitlines()[first - 1:last])):
                    continue
                if kind in NODE_KINDS:
                    found.add(address(kind, a, b))
                elif kind == "locals":
                    names = [name for name, (s, e) in local_names(text, first, last).items()
                             if pat.search("\n".join(text.splitlines()[s - 1:e]))]
                    if not names:
                        raise Full(f"{p} is read inside locals but not inside a named local ({f})")
                    locals_hit.update(names)
                elif kind in READS_FULL:
                    raise Full(f"{p} is read by a {kind} block ({f})")
    pending = [("local", x) for x in locals_hit] + [("var", x) for x in vars_hit]
    done = set()
    while pending:
        ref = pending.pop()
        if ref in done:
            continue
        done.add(ref)
        pat = re.compile(r"\b%s\.%s\b" % ref)
        for f, text in tree.items():
            for kind, a, b, first, last in blocks(text):
                body = "\n".join(text.splitlines()[first - 1:last])
                if not pat.search(body):
                    continue
                if kind == "locals":
                    for name, (s, e) in local_names(text, first, last).items():
                        if pat.search("\n".join(text.splitlines()[s - 1:e])):
                            pending.append(("local", name))
                elif kind in NODE_KINDS:
                    found.add(address(kind, a, b))
                elif kind in READS_FULL:
                    raise Full(f"{ref[0]}.{ref[1]} is read by a {kind} block ({f})")
    # Dependents: -target takes what a target depends on, not what depends on it, so a block that reads a changed
    # address (an ECS service reading a replaced task definition) is a target too, through locals as well.
    queue, seen_addr = list(found), set()
    while queue:
        addr = queue.pop()
        if addr in seen_addr:
            continue
        seen_addr.add(addr)
        pat = re.compile(r"(?<![\w.])%s\b" % re.escape(addr))
        for f, text in tree.items():
            for kind, a, b, first, last in blocks(text):
                body = "\n".join(text.splitlines()[first:last - 1])  # the body, not the block's own header
                if not pat.search(body):
                    continue
                if kind in NODE_KINDS:
                    dep = address(kind, a, b)
                    if dep not in found:
                        found.add(dep)
                        queue.append(dep)
                elif kind == "locals":
                    for name, (s, e) in local_names(text, first, last).items():
                        if pat.search("\n".join(text.splitlines()[s - 1:e])):
                            for dep in readers(tree, ("local", name)):
                                if dep not in found:
                                    found.add(dep)
                                    queue.append(dep)
                elif kind in READS_FULL:
                    raise Full(f"{addr} is read by a {kind} block ({f})")
        if len(found) > MAX_TARGETS:
            raise Full(f"more than {MAX_TARGETS} addresses depend on the change")
    return sorted(a for a in found if not a.startswith("ephemeral."))


def readers(tree, ref, done=None):
    """The resource, data and module blocks that read local.NAME or var.NAME, through other locals."""
    done = set() if done is None else done
    if ref in done:
        return set()
    done.add(ref)
    out, pat = set(), re.compile(r"\b%s\.%s\b" % ref)
    for f, text in tree.items():
        for kind, a, b, first, last in blocks(text):
            if not pat.search("\n".join(text.splitlines()[first - 1:last])):
                continue
            if kind == "locals":
                for name, (s, e) in local_names(text, first, last).items():
                    if pat.search("\n".join(text.splitlines()[s - 1:e])):
                        out |= readers(tree, ("local", name), done)
            elif kind in NODE_KINDS:
                out.add(address(kind, a, b))
            elif kind in READS_FULL:
                raise Full(f"{ref[0]}.{ref[1]} is read by a {kind} block ({f})")
    return out


def main(argv):
    root, base, head = argv[1:4]
    try:
        for t in targets(root, base, head):
            print(f"-target={t}")
    except Full as e:
        print(f"FULL: {e}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
