---
name: web-development
description: "Build and deploy web applications (frontend and/or backend) to Vercel. Use the web_dev tool to scaffold a project, edit it, set environment variables, deploy via the Vercel CLI, and return the live URL to the user."
version: 1.0.0
---

# Web Development & Deployment

Use this skill whenever a user asks you to **build a website / web app** (frontend,
backend, or full-stack) and/or **deploy it and give them a link**. It pairs the
agent's normal code-writing abilities with a `web_dev` tool that ships projects
to Vercel non-interactively.

## When to use it

* "build me a landing page", "create a website", "make a web app"
* "add a backend / API", "full-stack app"
* "deploy it", "host it", "put it online", "send me the link to my site"
* "set this env/secret/API key for the app"

## Workflow

1. **Understand what to build.** Clarify the type of project if it is not obvious:
   a static frontend, a backend/API, or a full-stack app. Keep the scope reasonable.

2. **Scaffold (optional).** Use `web_dev` with `action=scaffold`,
   `project=<name>`, and `type=frontend|backend|fullstack` to create a starter
   directory. You can also build files by hand with the normal filesystem tools
   (`write_file`/`edit_file`/`apply_patch`) in a new directory under the workspace.

3. **Write / edit the code.** Use the filesystem tools to add your HTML/CSS/JS
   (frontend) or your server/API code (backend). For a full-stack app provide an
   `index.html` plus a server entrypoint; the scaffold's defaults are a safe start.

4. **Set environment variables BEFORE deploying** if the app needs secrets or
   config (API keys, database URLs). Use `web_dev` with `action=set_env`,
   `project=<dir>`, `name=<VAR>`, `value=<value>`, `environment=production`
   (or `preview`/`development`). Deploying after setting env means the build can
   read them.

5. **Deploy.** Use `web_dev` with `action=deploy`, `project=<dir>`. It runs the
   Vercel CLI against that directory and returns the **live URL**.

6. **Give the user the link.** After a successful deploy, tell the user the
   public https URL(s) from the tool output. For full-stack projects, list the
   frontend URL and the API route(s) explicitly.

## Environment variables

The Vercel CLI authenticates with an operator-configured `VERCEL_TOKEN`
(the same way `novita_sandbox` uses `NOVITA_API_KEY`). When it is missing the
`web_dev` tool is disabled — tell the user an admin must set `VERCEL_TOKEN`.

Common env vars to set per app (as asked by the user): `DATABASE_URL`,
`OPENAI_API_KEY`, `STRIPE_SECRET`, `PUBLIC_*` build vars, etc. Prefer
`environment=production` so production deployments see them.

## Deploying again after a change

If the user asks for changes after a deploy, edit the files, then `action=deploy`
again. The tool reports the refreshed deployment URL(s). Setting env vars also
requires a redeploy for the running app to pick them up.

## Notes

* Keep secrets private: never echo raw env values back verbatim in chat unless the user
  explicitly asked; the CLI hides sensitive values anyway.
* The tool deploys the project directory you name — always point it at the same
  directory you wrote the code into.
* `web_dev` `action=status` lists a project's deployments and env vars; `inspect`
  prints its live URLs. Use them to confirm or debug.