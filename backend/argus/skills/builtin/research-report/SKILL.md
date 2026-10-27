---
name: research-report
description: Plan, evidence and write a rigorous research report with inline citations. Use for any research-mode deliverable, literature review, market scan or technical investigation.
version: 1.0.0
allowed-tools: [web_search, web_fetch, notes_write, notes_read, artifact_write]
---

# Research report

Follow these phases in order. Keep everything you learn in shared notes so other members can build on it.

## 1. Frame
- Restate the question in one sentence and list 3-6 sub-questions that, answered together, answer it.
- Write explicit inclusion criteria (time range, geography, source types) and what "done" looks like.

## 2. Gather
- For each sub-question, search with at least two differently-worded queries.
- Prefer primary sources (papers, standards, official statistics, filings) over commentary.
- Record every useful finding with `notes_write`, including `source` (URL) and a short verbatim quote.
- Score sources with the `source-evaluation` skill when reliability matters.

## 3. Analyse
- Group findings by sub-question. Mark each claim as **supported** (2+ independent sources),
  **single-source**, or **contested** (sources disagree; say why).
- Quantify where possible. State uncertainty plainly.
- List open gaps instead of filling them with guesses.

## 4. Write
Use `templates/report.md` as the skeleton. Rules:
- Every factual sentence that came from a source ends with a numbered citation like `[3]`.
- The References section lists `[n] Title — Publisher/Author, Year. URL (accessed YYYY-MM-DD)`.
- Lead with the answer (executive summary), then the evidence.
- Never cite a source you did not read in this run.

## Quality bar
Before handing over, check every item in `references/checklist.md`.
