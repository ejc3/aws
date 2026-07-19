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
  if [ -n "$resume" ]; then cmd="claude --resume $resume --dangerously-skip-permissions"
  else cmd="claude --resume --dangerously-skip-permissions"; fi

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
  if [ -z "$win" ]; then
    if tmux has-session -t "=$session" 2>/dev/null; then
      win="$(tmux new-window -d -P -F '#{window_id}' -t "=$session" -n "$winname" -c "$folder" "$cmd")"
    else
      tmux new-session -d -s "$session" -n "$winname" -c "$folder" "$cmd"
      win="$(tmux list-windows -t "=$session" -F '#{window_id}' | head -1)"
    fi
    tmux set-option -w -t "$win" @tclaude_key "$key"
  fi

  tmux select-window -t "$win"
  if [ -n "${TMUX-}" ]; then tmux switch-client -t "=$session"; else tmux attach -t "=$session"; fi
}
