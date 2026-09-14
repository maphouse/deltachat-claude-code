# deltachat-claude-code

Delta Chat bot fronting real Claude Code sessions on **thinkpad**; runs as `emory`,
`bypassPermissions`, systemd `agentbot.service`. See parent `dc-bots/CLAUDE.md` for
architecture, running, and editing conventions.

## Publishing playbook

This project is the reference for publishing open-source code. Follow these practices
when preparing any project for public release.

### GitHub repo setup

- **Description**: one-line pitch (shows in search results and embeds)
- **Website**: link to PyPI page or docs, not the repo itself
- **Topics**: 5-10 lowercase tags for discoverability (e.g. `claude-code`, `delta-chat`,
  `chatbot`, `cli`, `encrypted-chat`). Check what similar projects use
- **Social preview**: upload a 1280x640 image (`social-preview.png` in repo)

### README structure

1. **Title + badges** — name, logo/icon, then shield.io badges (PyPI version, Python
   versions, license)
2. **One-line pitch** — bold, no jargon, says what it does and for whom
3. **Expanded opener** — 2-3 sentences explaining what this actually is, what makes it
   different from the obvious alternative
4. **Contents** — TOC linking to sections below
5. **Why** — motivation, not features. Answer "why would I use this instead of X?"
6. **Screenshots** — table layout, 3-4 images showing real usage, captioned
7. **Architecture** — short code-block diagram of modules and their roles
8. **System requirements** — prerequisites, resource usage table (RAM, CPU)
9. **How to setup** — `pip install` first, then source install, then configure + provision
10. **Usage** — command tables grouped by category (session control, settings, info)
11. **Security** — honest about the threat model and what's load-bearing
12. **Known limitations** — things that don't work, framed as facts not apologies
13. **License**

### llms.txt

Add `llms.txt` at repo root — a plain-text elevator pitch for LLM discovery. One paragraph,
no markdown, explains what the project does and how to use it. This is what an LLM reads
when deciding whether to recommend the project.

### PyPI packaging (Python projects)

- **Package layout**: source in a subdirectory (`agentbot/`), `pyproject.toml` at repo root.
  Runtime data (config, db, accounts) resolves from CWD via `Path.cwd()`, not
  `Path(__file__).parent`
- **pyproject.toml fields**: name, version, description, readme, license (SPDX expression
  e.g. `"MIT"`), license-files, requires-python, authors, keywords, classifiers,
  dependencies, optional-dependencies, project.urls (Homepage, Repository), project.scripts
  (entry points)
- **License**: use PEP 639 SPDX expression (`license = "MIT"`) with `license-files`.
  Do NOT add a `License ::` classifier — setuptools 84+ rejects mixing the two
- **Build & publish**: `python -m build` then `twine upload dist/*`. Store PyPI token in
  `~/.pypi-token` (mode 600), pass via `TWINE_USERNAME=__token__ TWINE_PASSWORD="$(cat ~/.pypi-token)"`
- **Badges**: add to README top — `shields.io/pypi/v/`, `shields.io/pypi/pyversions/`,
  license badge
- **Version bumps**: edit `version` in pyproject.toml, rebuild, upload. PyPI won't accept
  a re-upload of the same version

### config.example.toml

Ship a `config.example.toml` with safe defaults and comments. The real `config.toml` is
gitignored. README should show how to copy and edit it.
