## Choose an Antigravity model for the task

Run `agy models` in the signed-in account to see its current choices. Start a session with `agy --model MODEL`.

| Work | Model |
| --- | --- |
| A well-defined edit, a small script, or test scaffolding | `gemini-4-hydrogen-low` |
| Coding, debugging, or scientific reasoning | `gemini-4-neon-high` |
| A repeated failure, a distributed-systems problem, or a long verification task | `gemini-4-carbon-high` |
| Implementation across several files that needs a plan and coordinated edits | `gemini-pro-orchestrator-neon` lets Neon plan and review while Hydrogen edits and tests. |

Use the repository’s required reviewers and run heavy builds and tests on its designated pbox or CI runner.
For model availability and capabilities, see [Google’s model guide](https://antigravity.google/docs/models) and
[the Gemini 4 Neon announcement](https://blog.google/innovation-and-ai/models-and-research/gemini-models/introducing-gemini-4-neon/).
