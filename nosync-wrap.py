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

Also implements Ctrl-Z (suspend). Because this shim puts the outer tty in RAW mode,
the line discipline no longer generates SIGTSTP -- Ctrl-Z would otherwise arrive at
the child as an inert 0x1A byte (the observed "Ctrl-Z hangs"). So we watch the input
stream for the suspend character and perform the job-control dance by hand:
restore the tty, stop the child's process GROUP, then stop ourselves so the hosting
shell regains control. SIGCONT reverses it. SIGSTOP is used for the child rather
than SIGTSTP because the child is a session leader in its own (orphaned) session,
and POSIX discards stop signals sent to an orphaned process group.
"""
import os, pty, re, sys, select, fcntl, termios, struct, signal, tty as ttymod

SUSP = b"\x1a"          # Ctrl-Z (VSUSP); raw mode means we must handle it ourselves

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


def _tty_guard():
    """SIGTTOU must be ignored around termios changes: if we are not (yet) the
    foreground process group, tcsetattr raises SIGTTOU and stops this process --
    observed as the wrapper sitting in state T before it ever ran."""
    try: return signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    except Exception: return None

def _tty_unguard(prev):
    if prev is not None:
        try: signal.signal(signal.SIGTTOU, prev)
        except Exception: pass

def set_raw(fd0):
    p = _tty_guard()
    try: ttymod.setraw(fd0)
    except Exception: pass
    finally: _tty_unguard(p)

def set_mode(fd0, mode):
    p = _tty_guard()
    try: termios.tcsetattr(fd0, termios.TCSAFLUSH, mode)
    except Exception: pass
    finally: _tty_unguard(p)

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
    try: old = termios.tcgetattr(0); set_raw(0); restore = True
    except Exception: old = None; restore = False

    def child_pgid():
        try: return os.getpgid(pid)
        except Exception: return None

    def suspend():
        """Ctrl-Z: stop the child job, then stop ourselves so the shell returns."""
        # Hand the terminal back in its original mode, or the shell inherits raw.
        if restore: set_mode(0, old)
        g = child_pgid()
        try:
            # SIGSTOP, not SIGTSTP: the child leads its own orphaned session, and
            # POSIX discards stop signals delivered to an orphaned process group.
            if g: os.killpg(g, signal.SIGSTOP)
            else: os.kill(pid, signal.SIGSTOP)
        except Exception: pass
        # Suspend self with the default handler so the shell actually sees us stop.
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTSTP)

    def resumed(*_):
        """SIGCONT: re-arm raw mode, wake the child, force a repaint."""
        if restore: set_raw(0)
        g = child_pgid()
        try:
            if g: os.killpg(g, signal.SIGCONT)
            else: os.kill(pid, signal.SIGCONT)
        except Exception: pass
        winch()                       # nudge the child to redraw its UI
        signal.signal(signal.SIGTSTP, tstp)

    def tstp(*_):
        """Someone sent US SIGTSTP directly -- take the child down with us."""
        suspend()

    signal.signal(signal.SIGCONT, resumed)
    signal.signal(signal.SIGTSTP, tstp)
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
                if SUSP in data:
                    # forward everything typed before Ctrl-Z, swallow the Ctrl-Z
                    head, _, tail = data.partition(SUSP)
                    if head: os.write(fd, head)
                    suspend()
                    if tail: os.write(fd, tail)
                    continue
                os.write(fd, data)
    finally:
        if restore: set_mode(0, old)
    try: os.waitpid(pid, 0)
    except Exception: pass

if __name__ == "__main__":
    main()
