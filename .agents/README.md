# .agents

Tool-neutral instructions for coding agents working on OpenMarketer.

- `../AGENTS.md` is the project brief every agent reads. `CLAUDE.md` imports it.
- `agents/` holds one Markdown file per specialised agent: YAML front matter (`name`, `description`, optional `tools`) followed by the instructions. `.claude/agents` is a symlink to this folder, so Claude Code picks them up as subagents.
- `skills/` holds one folder per procedure that crosses the agents' areas, with a `SKILL.md` (YAML front matter `name` and `description`, then the steps). `.claude/skills` is a symlink to this folder. `new-agent-workflow` adds a product agent and its Temporal workflow.

To add an agent, add a file to `agents/`. Keep it about this project: what the agent owns, what already exists, how to work, and what done means. A skill gives the order of a task and who does each step; it points at the agents' rules instead of repeating them.
