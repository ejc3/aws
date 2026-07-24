# t-claude [SESSION] [--resume <id>]
#   SESSION : tmux session = a named group of related projects ("Apps","Backend",
#             "AWS"). Omit it and the session defaults to "<folder>-<hash>" (unique
#             per directory).
#   WINDOW  : one per project folder, keyed by folder path (hidden @tclaude_key), so
#             two same-named folders never collide.
#   --resume <id> : a NEW window "<folder>-<id>" (claude session id as discriminator).
#   PANES   : never touched — split your own terminal in with Ctrl-b % / ".
# If you pass a SESSION but this folder's claude is already open in a DIFFERENT
# session, t-claude MOVES it here live (move-window keeps claude running) — no prompt,
# since the move is non-destructive.
# Safety: only windows WE create carry @tclaude_key, so manual windows are never
# found, reused, renamed, moved, or closed.
t-claude() {
  local session="" resume="" folder base cmd key winname win hash explicit=0
  folder="$PWD"

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --resume=*) resume="${1#--resume=}"; shift ;;
      --resume)
        if [ -n "${2-}" ] && [ "${2#-}" = "${2-}" ]; then resume="$2"; shift 2; else shift; fi ;;
      -*) shift ;;
      *) [ -z "$session" ] && session="$1"; shift ;;
    esac
  done

  base="${folder##*/}"; [ -z "$base" ] && base="root"
  base="$(printf '%s' "$base" | tr -c 'A-Za-z0-9_-' '_')"

  if [ -n "$session" ]; then
    explicit=1
    session="$(printf '%s' "$session" | tr -c 'A-Za-z0-9_-' '_')"
  else
    hash="$(printf '%s' "$folder" | cksum | awk '{printf "%x", $1}')"
    session="${base}-${hash}"
  fi

  key="$(printf '%s' "$folder" | cksum | awk '{print $1}')_$(printf '%s' "$resume" | cksum | awk '{print $1}')"
  winname="$base"; [ -n "$resume" ] && winname="${base}-$(printf '%s' "$resume" | tr -c 'A-Za-z0-9_-' '_')"
  # nosync-wrap strips Claude's synchronized-output-mode sequences (CSI ?2026h/l)
  # before tmux sees them. tmux buffers all grid updates while sync mode is active and
  # emits only a viewport redraw, so scrolled-off lines never reach the host terminal's
  # native scrollback -- verified by strace on the live process (2026h emitted) and by
  # a 40/40-vs-23/40 scrollback measurement with and without the wrapper. Falls back to
  # bare claude if the wrapper is missing, so t-claude never breaks.
  # Pass --effort here rather than relying on a claude() wrapper in ~/.zshrc: the command
  # runs as `nosync-wrap claude ...`, so the shell runs nosync-wrap and any such function is
  # skipped, leaving the window on the default effort. Exported variables still arrive on
  # their own; a flag cannot, so it belongs in this command.
  local wrap=""; command -v nosync-wrap >/dev/null 2>&1 && wrap="nosync-wrap "
  local flags="--dangerously-skip-permissions --effort ultracode"
  local inner
  if [ -n "$resume" ]; then inner="${wrap}claude --resume $resume $flags"
  else inner="${wrap}claude --resume $flags"; fi

  # Ctrl-Z NOTE: the window is created running your normal interactive shell, and
  # claude is then sent to it as a JOB. Running claude as the pane command directly
  # cannot support Ctrl-Z at all: a pane command is a session leader, so its process
  # group is ORPHANED and POSIX silently discards stop signals. As a shell job it lives
  # in the shell's session, so Ctrl-Z suspends it and `fg` resumes it -- verified.
  # Leading space keeps this out of shell history: ~/.zshrc sets HIST_IGNORE_SPACE, so
  # zsh skips space-prefixed commands. Without it every launch types the full
  # "nosync-wrap claude --resume ..." line into the window's shell and it lands in
  # history, cluttering it and polluting up-arrow / Ctrl-R for the folder you work in.
  cmd=" $inner"

  # already the requested session's window?
  win=""
  if tmux has-session -t "=$session" 2>/dev/null; then
    win="$(tmux list-windows -t "=$session" -F '#{window_id} #{@tclaude_key}' 2>/dev/null | awk -v k="$key" '$2==k {print $1; exit}')"
  fi

  # explicit session, not here yet: if this folder's claude is open in another
  # session, move it here live (non-destructive — claude keeps running).
  if [ -z "$win" ] && [ "$explicit" = 1 ]; then
    local hit osess owin ph
    hit="$(tmux list-windows -a -F '#{session_name} #{window_id} #{@tclaude_key}' 2>/dev/null | awk -v k="$key" '$3==k {print $1" "$2; exit}')"
    if [ -n "$hit" ]; then
      osess="${hit% *}"; owin="${hit#* }"; ph=""
      if ! tmux has-session -t "=$session" 2>/dev/null; then
        tmux new-session -d -s "$session" -c "$folder"
        ph="$(tmux list-windows -t "=$session" -F '#{window_id}' | head -1)"
      fi
      tmux move-window -s "$owin" -t "=$session:"
      [ -n "$ph" ] && tmux kill-window -t "$ph" 2>/dev/null
      win="$owin"
      printf "moved this folder's claude from session '%s' to '%s'\n" "$osess" "$session" >&2
    fi
  fi

  # nothing to reuse/move: add a new window (creating the session if needed)
  local created=0
  if [ -z "$win" ]; then
    # Create the window running the plain interactive shell (no command), then send
    # claude to it as a job -- see the Ctrl-Z note above.
    if tmux has-session -t "=$session" 2>/dev/null; then
      win="$(tmux new-window -d -P -F '#{window_id}' -t "=$session" -n "$winname" -c "$folder")"
    else
      tmux new-session -d -s "$session" -n "$winname" -c "$folder"
      win="$(tmux list-windows -t "=$session" -F '#{window_id}' | head -1)"
    fi
    tmux set-option -w -t "$win" @tclaude_key "$key"
    tmux send-keys -t "$win" "$cmd" Enter
    created=1
  fi

  # Reusing a window whose claude has exited: since the window now runs the shell rather
  # than claude itself, it survives that exit still carrying @tclaude_key, so t-claude
  # would hand you an empty prompt and never start claude again.
  #
  # Look for claude among the window shell's children, not merely for "any child". Running
  # t-claude from inside the very window it is about to reuse -- which is exactly what you
  # do after claude prints "Resume this session with" -- means our own command
  # substitutions are children of that shell, so an any-child test always says claude is
  # alive and silently does nothing. A claude suspended with Ctrl-Z still matches here, so
  # a stopped session never gets a second claude stacked on it.
  if [ "$created" = 0 ]; then
    local pane_pid kid alive=0
    pane_pid="$(tmux display-message -p -t "$win" '#{pane_pid}' 2>/dev/null)"
    if [ -n "$pane_pid" ]; then
      for kid in $(pgrep -P "$pane_pid" 2>/dev/null); do
        case "$(ps -o command= -p "$kid" 2>/dev/null)" in
          *claude*|*nosync-wrap*) alive=1; break ;;
        esac
      done
      if [ "$alive" = 0 ]; then
        tmux send-keys -t "$win" "$cmd" Enter
        printf "relaunched claude in this folder's window -- it had exited\n" >&2
      fi
    fi
  fi

  tmux select-window -t "$win"
  if [ -n "${TMUX-}" ]; then tmux switch-client -t "=$session"; else tmux attach -t "=$session"; fi
}
