# tmux + Eternal Terminal setup

How dev sessions are organized on the dev boxes and jumpbox, plus the `t-claude`
launcher. All of this is written by terraform (`dev-user-data.tf`) — `~/.tmux.conf`
and the `t-claude` function (`t-claude.zsh`, sourced from `~/.zshrc`) — so it
survives instance recreation.

## Connecting (no disconnects + native scroll)

- **Connection:** In Prompt (Panic), use an **Eternal Terminal (ET)** connection to
  `<box>:2022` with the `fcvm-ec2` key. ET auto-reconnects and survives sleep, IP
  changes, and flaky networks — over one TCP connection on port 2022 (already open in
  the security group). Plain **SSH on :22 is always the fallback** — you can't be locked out.
- **Scrolling — native swipe, no copy-mode.** `~/.tmux.conf` is tuned so a two-finger
  swipe scrolls **Prompt's own scrollback**, as if tmux weren't there. Three settings
  make this work, and all three are load-bearing:
  1. `smcup@/rmcup@` — tmux never enters the alternate screen (which has no scrollback)
  2. `status off` — the single pane is exactly full-screen, so tmux never scrolls
     inside a DECSTBM region (region-scrolled lines are discarded by the terminal)
  3. `indn@` — tmux scrolls with plain linefeeds, never `CSI S` (Prompt drops
     CSI-S-scrolled lines from its scrollback)
  **The invariant: ONE pane, filling the whole terminal.** Splitting panes silently
  stops output from reaching Prompt's scrollback (it comes back when you return to a
  single full-screen pane). Switching windows/sessions interleaves redraws of a
  different buffer into the scrollback — usable, but expect visual seams. `mouse off`
  is deliberate and pinned: with `mouse on`, tmux steals wheel events into copy-mode,
  and tmux 3.8 flips the default to on.
  Do **not** attach with `tmux -CC`: no iOS client implements control mode (verified —
  Prompt included); a plain `tmux attach` is correct here.
  Keyboard fallback that always works: `Ctrl-b [`, then PageUp / arrows, `q` to exit.
- **Rotation / second clients:** every resize (phone rotation, attaching from a
  laptop at a different size) triggers a full redraw and can leave duplicated rows in
  the scrollback, hard-wrapped at the old width. Cosmetic, not data loss. When it gets
  noisy, `clear` starts a clean stretch. Prefer detaching other clients while driving
  from the phone.

## The tmux model (quick reference)

```
tmux SERVER (one per machine)
└── SESSION      a group of related projects — you attach/detach here (this is what persists)
    └── WINDOW   one project (a folder) — a "tab"; one visible at a time, fills the screen
        └── PANE a split running one program (claude, a shell, …)
```

## t-claude — launch / organize claude in tmux

```
t-claude [SESSION] [--resume <id>]
```

| Level | Named / keyed by |
|---|---|
| **session** | the name you pass (`Apps`, `Backend`, `AWS`); omit it → `<folder>-<hash>` (unique per directory) |
| **window** | the **folder** you launch from. Identity is the folder *path* (a hidden `@tclaude_key` window option), so two same-named folders never collide, and the same folder in two sessions is two windows |
| **pane** | t-claude never creates panes. You *can* split (`Ctrl-b %` / `Ctrl-b "`), but **splits break native swipe scrollback** (see above) — prefer separate windows |

Each window runs `claude --resume --dangerously-skip-permissions` (or
`claude --resume <id> --dangerously-skip-permissions` when you pass `--resume`).

### Examples

```
cd ~/web-frontend && t-claude Apps                 # session Apps, window web-frontend
cd ~/mobile-app   && t-claude Apps                 # + window mobile-app (same session)
cd ~/api-gateway  && t-claude Backend              # session Backend, window api-gateway
cd ~/infra        && t-claude AWS                  # session AWS, window infra
cd ~/web-frontend && t-claude Apps --resume conv7  # 2nd window "web-frontend-conv7"
cd ~/scratch      && t-claude                      # session "scratch-<hash>" (per-dir default)
```

Result on one machine:

```
├── SESSION Apps      ├── web-frontend  ├── mobile-app  └── web-frontend-conv7
├── SESSION Backend   └── api-gateway
└── SESSION AWS       └── infra
```

### Guarantees
- **Idempotent** — re-running from the same folder+args reuses that window; no duplicates.
- **Manual-safe** — only windows t-claude creates carry `@tclaude_key`. Windows or sessions
  you create by hand are never found, reused, renamed, or closed — t-claude only selects its
  own or adds a new one.
- Verified with an adversarial suite: bash + zsh, exotic folder names (spaces / dots / colons /
  leading-dash / unicode / `PWD=/`), every argument form, manual-window collisions, and a
  400-op randomized fuzz.

## Relationship to cmux linked-view

cmux (the Mac terminal, `manaflow-ai/cmux`) has a **linked-view** mode that aggregates all of
a host's tmux sessions into one hidden `cmux-view-*` session — using `link-window` — and drives
them over a single `-CC` connection (ejc3 PRs #7021 / #7022 / #7107 / #8428). `t-claude` is the
**server-side organizer** that emits exactly the clean shape linked-view aggregates and renders:
session = project group, window = project, pane = your splits. **t-claude organizes; cmux
linked-view displays.**

## Where it lives
- `t-claude.zsh` — the function itself (this repo, editable).
- `dev-user-data.tf` → `local.shell_setup` writes `~/.tmux.conf` and decodes
  `base64encode(file("t-claude.zsh"))` to `~/.config/t-claude.zsh` (sourced from `~/.zshrc`)
  on every box. `local.tclaude_b64` is the base64 (base64 so terraform never touches the
  function's `${...}`).
- The running jumpbox + dev boxes already have it; recreated boxes inherit it from terraform.
