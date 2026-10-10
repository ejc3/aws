# Instructions for coding agents

Session values stay out of the repository. A value copied from a working session (a shell, an environment variable, a launch command or a transcript) is not useful to the repository and goes stale: profile names, account names, one-off paths, model names, local ports. Don't put one in a file, a comment, a commit message, or a pull request title, description or review comment. Use a variable or a made-up placeholder (`example-profile`, `example-name`). Where something specific has to be named, use its publicly documented identifier. Check the diff, the commit messages and the PR text before every push.

## Cheaper models for straightforward work

Claude Code here can run three current models. Opus 5.5 is the default. Sonnet 5.5 and Haiku 5.5 are faster and
much cheaper (list price per million tokens, input/output: Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Haiku 5.5 $0.10/$0.50),
so hand them the simple parts and keep Opus for the thinking.

- **Haiku 5.5 (`haiku`)**: mechanical work that is easy to check. Finding and reading files and summarizing them,
  extraction and classification, renames, formatting, lookups, running a command and reporting what it printed, simple
  browser checks. Anthropic: "high-volume, latency-sensitive tasks such as classification, extraction, and routing".
- **Sonnet 5.5 (`sonnet`)**: well-scoped everyday engineering. A bug whose cause is known, a contained change with a
  clear done condition, tests for behaviour that already exists, docs. Anthropic: "the best combination of speed and
  intelligence".
- **Opus 5.5 (stay on it)**: planning and design, debugging where the cause is unknown, security, anything touching
  shared infrastructure, money or credentials, reviewing other agents' work, and the final check before you say done.

How to use them:

- Give a subagent its model on the Agent tool (`model: "haiku"` or `model: "sonnet"`); the main session keeps its own.
  For a long run of simple work, `/model sonnet` switches the whole session; switch back when the work gets hard.
- Delegate down, never up. A subagent runs on the session's own model or a cheaper one: Haiku from a Sonnet session,
  Haiku or Sonnet from an Opus session. A Sonnet session never starts an Opus subagent; if the work needs Opus, the
  session itself goes back to Opus (`/model opus`) first.
- A subagent starts with no context: give it the goal, the files, the constraints and what "done" looks like.
- One task per subagent. Check what it did yourself (run the tests, read the diff) before relying on it.
- If a subagent gets it wrong once, do that piece in the main session instead; do not retry the same model.
- Never hand a cheaper model an action that cannot be undone: a deploy, a delete, a push to main, a credential.
