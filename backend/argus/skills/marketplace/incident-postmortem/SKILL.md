---
name: incident-postmortem
description: Write a blameless incident postmortem - timeline, impact, root cause and contributing factors, what went well, and tracked action items. Use after an outage, security incident, data loss or near miss.
version: 1.0.0
allowed-tools: [fs_read, fs_search, notes_write, notes_read, artifact_write]
metadata:
  tags: [operations, reliability, writing]
---

# Incident postmortem

Blameless by default: describe what systems and people did and why it made sense at the time.
Never name individuals as the cause.

## 1. Facts
- Build a timeline in UTC from logs, alerts, chat and deploy history: detection, escalation,
  mitigation, resolution. Mark the gaps where nobody was acting and why.
- Quantify impact: users or requests affected, duration, data lost, money, SLO budget burned.

## 2. Analysis
- Root cause: the smallest change that would have prevented the incident.
- Contributing factors: detection delay, missing safeguards, unclear ownership, runbook gaps.
- Ask "why" until the answer is a system or process property, not a person.
- What went well, and where we got lucky.

## 3. Actions
Each action item is specific, has an owner role, a priority (P0-P2) and a due date, and is one of:
prevent, detect, mitigate, or process. Prefer automated guards over "be more careful".

## 4. Write
Use `templates/postmortem.md`. Keep the summary to five sentences a non-engineer can follow.
