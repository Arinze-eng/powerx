# nanobot Skills

This directory contains built-in skills that extend nanobot's capabilities.

## Skill Format

Each skill is a directory containing a `SKILL.md` file with:
- YAML frontmatter (name, description, metadata)
- Markdown instructions for the agent

When skills reference large local documentation or logs, prefer nanobot's built-in
`grep` tool to narrow the search space before loading full files.
Use `grep(output_mode="count")` / `files_with_matches` for broad searches first,
use `head_limit` / `offset` to page through large result sets,
and `grep(glob="*.md")` to filter by file name pattern.

## Attribution

These skills are adapted from [OpenClaw](https://github.com/openclaw/openclaw)'s skill system.
The skill format and metadata structure follow OpenClaw's conventions to maintain compatibility.

## Available Skills

| Skill | Description |
|-------|-------------|
| `github` | Interact with GitHub using the `gh` CLI |
| `weather` | Get weather info using wttr.in and Open-Meteo |
| `summarize` | Summarize URLs, files, and YouTube videos |
| `tmux` | Remote-control tmux sessions |
| `clawhub` | Search and install skills from ClawHub registry |
| `skill-creator` | Create new skills |
| `web-development` | Hub for building & deploying web apps end-to-end (routes to the skills below) |
| `web-design` | Expert UI/UX design: layout, typography, color, components, responsive — avoid generic AI-looking pages |
| `frontend-development` | HTML/CSS/JS, React & Next.js, Tailwind, state, data fetching, a11y, performance |
| `backend-development` | Node/Express, Next.js APIs, auth (JWT/sessions/OAuth), validation, security, serverless |
| `database-development` | Schema design, SQL/migrations, Prisma/Drizzle/Supabase, pooling, env credentials |
| `fullstack-development` | Wire frontend + backend + DB into one deployable app; CRUD walkthrough |
| `vercel-deployment` | Deploy via Vercel CLI (`web_dev`): scaffold/deploy/set_env/domains/logs/troubleshooting |
| `poll` | Real-time polling & vigilance: watch anything over time and react — live trading or any repeated non-trading task |
| `alpaca-hackathon` | ICT/SMC + TMA + HMM five-cluster trading strategy playbook (backing the `alpaca_trade` tool) |
