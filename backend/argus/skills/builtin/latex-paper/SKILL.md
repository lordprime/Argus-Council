---
name: latex-paper
description: Write academic papers, technical reports or theses in LaTeX with BibTeX references. Use when the requested output format is LaTeX or PDF and the content is academic or technical.
version: 1.0.0
allowed-tools: [artifact_write, artifact_read, notes_read]
---

# LaTeX paper

1. Start from `templates/paper.tex` (article class, standard packages only so it compiles anywhere).
2. Put references in `references.bib` and cite with `\cite{key}`; keys are `firstauthorYEARword`.
3. Escape LaTeX special characters in prose: `& % $ # _ { } ~ ^ \`.
4. Use `\section`, `\subsection`; figures/tables need `\caption` and `\label` and are referenced with `\ref`.
5. Math: inline `$...$`, display `\begin{equation}...\end{equation}`.
6. Write files with `artifact_write`: `paper.tex` and `references.bib`.
7. Re-read the file and check: balanced environments, every `\cite` key exists in the .bib, no unescaped `&` or `%`.

See `references/style.md` for writing conventions.
