---
name: literature-review
description: Run a structured literature review on a research question - search strategy, screening, evidence table and synthesis with citations. Use for academic or technical surveys where coverage and traceability matter more than speed.
version: 1.0.0
allowed-tools: [web_search, web_fetch, notes_write, notes_read, artifact_write]
metadata:
  tags: [research, academic, writing]
---

# Literature review

## 1. Protocol (write it down before searching)
- Research question in PICO or a similar frame (population, intervention/topic, comparison, outcome).
- Inclusion/exclusion criteria: years, languages, study types, venues.
- 3-5 search strings with synonyms; record them with `notes_write` for reproducibility.

## 2. Search and screen
- Run every search string; prefer scholarly indexes, publisher pages, arXiv, standards bodies.
- Screen by title/abstract first, then full text. Log each excluded item with a one-line reason.
- Follow references backwards and "cited by" forwards for the most relevant 3-5 papers.

## 3. Extract
Fill one row per included source in the evidence table (`templates/evidence-table.md`):
citation, method, sample/data, key finding, limitations, quality (high/med/low).

## 4. Synthesise
- Group by theme, not by paper. For each theme state the consensus, the disagreements and why
  they might differ (method, population, era).
- Distinguish strong evidence (replicated, high quality) from suggestive findings.
- End with explicit gaps and the studies that would close them.

## Rules
- Every claim carries a numbered citation; never cite a paper you did not open.
- Report the search date and the number of items found, screened and included.
