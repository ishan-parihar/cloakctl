# cloakctl (skill)

Agent-facing skill for using and operationalizing the
[cloakctl](https://github.com/ishan-parihar/cloakctl) system: one persistent
stealth browser per profile over CLI verbs, plus a registry where agents
compound reusable automations (skill macros vs typed `wf` modules).

Install:

```bash
npx skills add ishan-parihar/cloakctl
# or: install this skill package into your agent's skills dir
```

Layout: `SKILL.md` (doctrine + core loop) · `references/` (page actuation,
workflows+skills, remote VPS, troubleshooting) · `scripts/smoke.sh`
(end-to-end install check on an isolated state dir) · `evals/evals.json`
(test prompts).

The product itself lives at `internet/cloakctl/` in the agentic-utility
project; its README carries the full command reference and guarantees.
