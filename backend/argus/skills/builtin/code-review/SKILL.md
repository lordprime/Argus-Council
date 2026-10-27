---
name: code-review
description: Review code for correctness, security and maintainability with a severity rubric. Use when asked to review, audit or critique code, or before accepting code produced by the council.
version: 1.0.0
allowed-tools: [fs_read, fs_list, artifact_read, artifact_list, notes_write]
---

# Code review

Review in this order and stop to report anything critical immediately.

1. **Correctness**: does it do what the task requires? Trace one realistic input end to end.
2. **Security**: injection (SQL, shell, path), secrets in code, unsafe deserialization, missing authz checks,
   SSRF, unbounded resource use.
3. **Error handling**: failures surfaced, not swallowed; resources released.
4. **Tests**: meaningful assertions; edge cases (empty, large, unicode, concurrent).
5. **Maintainability**: naming, duplication, dead code, unclear abstractions.

For every finding report: `severity` (see `references/severity.md`), `file:line`, what goes wrong with a
concrete input, and the smallest fix. Do not report style preferences as bugs.
