# .agents

Tool-neutral instructions for coding agents working on OpenMarketer.

- `../AGENTS.md` is the project brief every agent reads. `CLAUDE.md` imports it.
- `agents/` holds one Markdown file per specialised agent: YAML front matter (`name`, `description`, optional `tools`) followed by the instructions. `.claude/agents` is a symlink to this folder, so Claude Code picks them up as subagents.

To add an agent, add a file to `agents/`. Keep it about this project: what the agent owns, what already exists, how to work, and what done means.
