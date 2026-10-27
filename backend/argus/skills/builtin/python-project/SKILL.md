---
name: python-project
description: Scaffold and build a well-structured Python project with pyproject.toml, a src layout, pytest tests and a README. Use when the coding task is a Python application, library or CLI.
version: 1.0.0
allowed-tools: [fs_write, fs_read, fs_list, shell, artifact_write]
---

# Python project

Layout:
```
pyproject.toml        # from templates/pyproject.toml
README.md
src/<package>/__init__.py
src/<package>/...
tests/test_<module>.py
```

Steps:
1. Fill `templates/pyproject.toml` (name, description, dependencies kept minimal and pinned to compatible ranges).
2. Implement in small modules with type hints and docstrings on public functions.
3. Write pytest tests next to each behaviour, including one failure-path test per public function.
4. Run `python -m pytest -q` and report the exact output. Fix failures before finishing.
5. README: what it does, install, usage example that you actually ran.

Never vendor secrets; read configuration from environment variables.
