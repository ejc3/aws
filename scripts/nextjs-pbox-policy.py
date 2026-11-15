#!/usr/bin/env python3
"""Add the common pbox policy in a running Next.js guest without replacing its private instructions."""
import base64
import os
import pwd
import stat
import sys
import tempfile
from pathlib import Path

BEGIN = b"<!-- BEGIN nextjs-pbox-policy (managed) -->\n"
END = b"<!-- END nextjs-pbox-policy (managed) -->\n"
MAX_INSTRUCTIONS_BYTES = 1 << 20
OWNER_POLICY = b"## Host: nextjs-dev \xe2\x80\x94 run heavy work on pbox; browser suites belong in CI"


def render(original, policy):
    """Replace only a complete managed block; preserve every other byte."""
    content = policy.rstrip(b"\n")
    if not content or BEGIN.rstrip() in content or END.rstrip() in content:
        raise ValueError("invalid pbox policy")
    block = BEGIN + content + b"\n" + END
    starts = original.count(BEGIN.rstrip())
    ends = original.count(END.rstrip())
    if not starts and not ends:
        return block + (b"\n" + original if original else b"")
    if starts != 1 or ends != 1:
        raise ValueError("multiple or incomplete pbox policy markers")
    start = original.find(BEGIN)
    end = original.find(END)
    if start < 0 or end < start or (start > 0 and original[start - 1:start] != b"\n") or original[end - 1:end] != b"\n":
        raise ValueError("misplaced pbox policy markers")
    return original[:start] + block + original[end + len(END):]


def effective_codex_file(home, *, create_directory=False):
    directory = home / ".codex"
    if create_directory and not directory.exists():
        directory.mkdir(mode=0o700, exist_ok=True)
    override = directory / "AGENTS.override.md"
    # A symlink or non-regular override is not permission to silently edit a fallback.
    return override if override.exists() or override.is_symlink() else directory / "AGENTS.md"


def fingerprint(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def read_instructions(path):
    if path.parent.resolve(strict=True) != path.parent:
        raise ValueError("will not follow an instructions directory link")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
        old = os.fstat(handle.fileno())
        if not stat.S_ISREG(old.st_mode) or old.st_uid != os.geteuid():
            raise ValueError("instructions must be a regular file owned by the expected account")
        if old.st_size > MAX_INSTRUCTIONS_BYTES:
            raise ValueError("instructions exceed the 1 MiB safety limit")
        content = handle.read(MAX_INSTRUCTIONS_BYTES + 1)
        if len(content) > MAX_INSTRUCTIONS_BYTES:
            raise ValueError("instructions exceed the 1 MiB safety limit")
        return old, content


def owner_status(home):
    """The owner's central distribution owns both files; leave them entirely alone."""
    try:
        _, original = read_instructions(effective_codex_file(home))
    except FileNotFoundError:
        return "pending central owner instructions"
    if OWNER_POLICY not in original.splitlines():
        return "pending central owner instructions"
    body = original.split(OWNER_POLICY, 1)[1].split(b"\n## ", 1)[0]
    if b"**RAM comes first." in body and b"pbox up " in body and b"pbox down " in body:
        return "supplied by the central owner instructions"
    return "pending central owner instructions"


def install_policy(path, policy, *, create=False):
    parent = path.parent
    try:
        old, original = read_instructions(path)
    except FileNotFoundError:
        if not create:
            raise
        old, original = None, b""
    rendered = render(original, policy)
    if len(rendered) > MAX_INSTRUCTIONS_BYTES:
        raise ValueError("updated instructions exceed the 1 MiB safety limit")
    if original == rendered:
        return "unchanged"

    # Publish an identically owned/mode file on the same filesystem. If a user has
    # saved an edit since the read, try again on the next run instead of discarding it.
    fd, temporary = tempfile.mkstemp(prefix=".nextjs-pbox-policy.", dir=parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            if old is not None:
                if os.fstat(handle.fileno()).st_gid != old.st_gid:
                    os.fchown(handle.fileno(), old.st_uid, old.st_gid)
                os.fchmod(handle.fileno(), stat.S_IMODE(old.st_mode))
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        if old is None:
            os.link(temporary, path, follow_symlinks=False)
        else:
            if fingerprint(path.stat(follow_symlinks=False)) != fingerprint(old):
                raise ValueError("instructions changed while reading; will retry without overwriting")
            os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return "updated"


def main(argv):
    mode, account, encoded = argv
    expected_home = Path("/home") / account
    identity = pwd.getpwnam(account)
    # is_mount() misses same-filesystem bind mounts. Use the kernel mount table.
    mountpoints = {row.split()[4] for row in Path("/proc/self/mountinfo").read_bytes().splitlines()}
    if identity.pw_dir != str(expected_home) or os.fsencode(expected_home) not in mountpoints:
        raise ValueError("expected Next.js home is not mounted")
    policy = base64.b64decode(encoded, validate=True)
    if mode == "claude" and os.geteuid() == 0:
        result = install_policy(Path("/etc/claude-code/CLAUDE.md"), policy)
    elif mode == "codex" and os.geteuid() == identity.pw_uid and identity.pw_uid != 0:
        if account == "ejc3":
            result = owner_status(expected_home)
        else:
            result = install_policy(effective_codex_file(expected_home, create_directory=True), policy, create=True)
    else:
        raise ValueError("wrong scope or service user for pbox policy")
    print(f"{account} {mode}: {result}")


if __name__ == "__main__":
    main(sys.argv[1:])
