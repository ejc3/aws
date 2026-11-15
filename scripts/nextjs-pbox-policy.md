## Shared Next.js host: run heavy work on pbox

**RAM comes first. Run memory-heavy or CPU-heavy work on your own pbox, not on this shared host.**
CPU work can queue. Running out of physical RAM stalls or kills other people's dev servers and
agent sessions. Swap and throttling are not substitutes for RAM.

- Estimate the aggregate peak RAM of all simultaneous workers and child processes, including
  compilers, test workers and browsers. Add headroom for the OS and run-to-run variation. Use a
  prior CI/pbox measurement; if you don't know the peak, measure it on pbox. Pick the smallest
  flavor that fits the RAM budget, then size CPU and disk. Use a memory flavor for RAM-heavy work.
- Keep edits, small targeted checks, and the existing dev servers here. Run heavy builds,
  browser suites, large tests and parallel jobs on pbox or in the project's existing CI.
- Check `pbox status` and `pbox flavors`. The owner chooses an assigned/free slot N, for example
  `pbox up N memory-32`. Save the printed instance ID, then use `pbox ssh N --instance ID`
  and `pbox down N --instance ID`. Members have one box: use
  `pbox up memory-32`, `pbox ssh` and `pbox down`. Use pbox's direct SSH path from this dev
  account, never a jumpbox. Copy results back and stop your box when done. If you cannot get
  a pbox, report the blocker; don't run the heavy job here instead.
