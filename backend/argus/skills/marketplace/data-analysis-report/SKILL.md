---
name: data-analysis-report
description: Analyse a dataset (CSV, JSON or query output) and write a reproducible report - data checks, descriptive statistics, the analysis that answers the question, charts and caveats. Use when asked to analyse, explore or explain data.
version: 1.0.0
allowed-tools: [fs_read, fs_list, fs_write, shell, notes_write, artifact_write]
metadata:
  tags: [data, analysis, writing]
---

# Data analysis report

## 1. Question first
Restate the question and the decision it informs. Define the metric(s) precisely
(numerator, denominator, time window, unit).

## 2. Inspect the data
- Row count, columns, types, date range, unique keys.
- Missing values, duplicates, impossible values, outliers. Decide and record how each is handled.
- Never silently drop rows; report how many were excluded and why.

## 3. Analyse
- Start with descriptive statistics (count, mean, median, p10/p90) per relevant segment.
- Then the analysis the question needs: comparison, trend, correlation, cohort, or a simple model.
- Report effect sizes with uncertainty (confidence interval or bootstrap range), not only p-values.
- Correlation is not causation: name plausible confounders.
- Keep the code you ran (`fs_write` an `analysis.py` or notebook-style script) so results reproduce.

## 4. Write
Use `templates/report.md`. Lead with the answer in one or two sentences, then evidence.
Every chart has a title that states the takeaway, labelled axes with units, and a source line.
List limitations and what additional data would change the conclusion.
