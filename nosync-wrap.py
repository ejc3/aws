#!/usr/bin/env python3
"""Spawn a child (Claude Code) on a pty and strip synchronized-output-mode
sequences (CSI ?2026h / CSI ?2026l) from its output before the parent terminal
(tmux) sees them.

Why: Claude wraps its repaints in synchronized-update mode. tmux buffers all grid
updates while sync mode is active and emits only a final viewport redraw, so lines
that scroll past never reach the host terminal's native scrollback. Removing the
wrappers makes tmux apply each write immediately and scroll with plain linefeeds,
which the terminal saves. Cost: Claude's repaints are no longer atomic, so brief
tearing is possible; acceptable for gaining scrollback on a phone.
"""
import os, pty, re, sys, select, fcntl, termios, struct, signal, tty as ttymod

RE_SYNC   = re.compile(rb"\x1b\[\?2026[hl]")
RE_TAIL   = re.compile(rb"\x1b(\[\??2?0?2?6?)?$")   # possible split sequence at chunk end
carry = b""

def filt(data):
    global carry
    data = carry + data
    carry = b""
    m = RE_TAIL.search(data)
    if m and m.group(0) != b"":
        carry = data[m.start():]
        data = data[:m.start()]
    return RE_SYNC.sub(b"", data)

def main():
    argv = sys.argv[1:]
    if not argv:
        sys.stderr.write("usage: nosync-wrap CMD [ARGS...]\n"); sys.exit(2)
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(argv[0], argv)
    def winch(*_):
        try:
            sz = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0"*8)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, sz)
        except Exception: pass
    signal.signal(signal.SIGWINCH, winch); winch()
    try: old = termios.tcgetattr(0); ttymod.setraw(0); restore = True
    except Exception: restore = False
    try:
        while True:
            try: r,_,_ = select.select([0, fd], [], [])
            except InterruptedError: continue
            if fd in r:
                try: out = os.read(fd, 65536)
                except OSError: break
                if not out: break
                d = filt(out)
                if d: os.write(1, d)
            if 0 in r:
                try: data = os.read(0, 65536)
                except OSError: break
                if not data: break
                os.write(fd, data)
    finally:
        if restore:
            try: termios.tcsetattr(0, termios.TCSAFLUSH, old)
            except Exception: pass
    try: os.waitpid(pid, 0)
    except Exception: pass

if __name__ == "__main__":
    main()
