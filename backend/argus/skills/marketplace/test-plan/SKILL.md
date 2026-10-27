---
name: test-plan
description: Produce a risk-based test plan for a feature or release - scope, test levels, prioritised cases including edge and failure cases, environments and exit criteria. Use before implementing or shipping a change that needs verification.
version: 1.0.0
allowed-tools: [fs_read, fs_list, fs_search, notes_write, artifact_write]
metadata:
  tags: [engineering, quality, testing]
---

# Test plan

## 1. Understand the change
Read the spec or diff. List the user-visible behaviours and the components touched. Note anything
that changes data, permissions, money or external integrations: those carry the most risk.

## 2. Risk map
For each behaviour estimate likelihood of a defect and impact if it ships broken. Test effort
follows risk: high-risk paths get unit, integration and end-to-end coverage; low-risk get one level.

## 3. Cases
Write cases in `templates/test-plan.md` format: id, level (unit / integration / e2e / manual),
preconditions, steps, expected result, priority. Always include:
- the happy path and the main alternative paths;
- boundaries (empty, one, max, max+1), invalid input, unicode, time zones;
- failure modes (dependency down, timeout, partial write, retry, concurrency);
- authorization (wrong user, wrong role, expired session);
- accessibility and mobile layout for UI changes.

## 4. Exit criteria
State what must be true to ship: all P0 cases pass, no open blocker bugs, coverage of changed code,
performance budget met. Name the cases that must be automated as regression tests.
