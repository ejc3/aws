## Choose an Antigravity model for the task

Run `agy models` in the signed-in account to see its current choices. Start a session with `agy --model MODEL`.

| Work | Model |
| --- | --- |
| A well-defined edit, a small script, or test scaffolding | `gemini-4-hydrogen-low` |
| Coding, debugging, or scientific reasoning | `gemini-4-neon-high` |
| A repeated failure, a distributed-systems problem, or a long verification task | `gemini-4-carbon-high` |
| Implementation across several files that needs a plan and coordinated edits | `gemini-pro-orchestrator-neon` lets Neon plan and review while Hydrogen edits and tests. |

For prose, try Neon for a natural, direct first edit and Hydrogen for a short rewrite with clear constraints.
For highly creative work such as a game world, a story, a name or launch copy, use an ensemble. Give Gemini Neon,
Claude and Codex the same brief and a distinct creative direction each. Compare the anonymous drafts for voice,
audience fit, concrete detail and fidelity to the facts. Have one editor revise the strongest draft and a second
check the final text against the brief.

Use the repository’s required reviewers and run heavy builds and tests on its designated pbox or CI runner.
For model availability and capabilities, see [Google’s model guide](https://antigravity.google/docs/models) and
[the Gemini 4 Neon announcement](https://blog.google/innovation-and-ai/models-and-research/gemini-models/introducing-gemini-4-neon/).
