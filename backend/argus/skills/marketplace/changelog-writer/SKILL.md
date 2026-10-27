---
name: changelog-writer
description: Turn commits, merged pull requests or a diff into a clear user-facing changelog / release notes entry in Keep a Changelog format with semantic-version guidance. Use when preparing a release or summarising what changed for users.
version: 1.0.0
allowed-tools: [fs_read, fs_list, fs_search, shell, artifact_write]
metadata:
  tags: [engineering, release, writing]
---

# Changelog writer

## 1. Collect
Gather changes since the last release: `git log --oneline <last-tag>..HEAD` via `shell` if
available, otherwise the provided list. Read `CHANGELOG.md` (if any) to match its style.

## 2. Classify
Group each user-visible change under exactly one heading:
**Added**, **Changed**, **Deprecated**, **Removed**, **Fixed**, **Security**.
Drop internal-only changes (refactors, CI, tests) unless they affect users or operators.

## 3. Write each entry
- Lead with the user benefit in plain language, not the implementation: "Exports now include
  speaker notes" rather than "refactor exporter pipeline".
- One line each; link the PR or issue when known.
- Breaking changes get a **BREAKING** prefix and a one-line migration instruction.

## 4. Version
Recommend the next semantic version: major for breaking changes, minor for additions, patch for
fixes only. Put the entry at the top under `## [x.y.z] - YYYY-MM-DD`.
Never invent changes that are not in the source material.
