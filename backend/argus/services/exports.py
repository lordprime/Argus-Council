"""Turn a run's Markdown deliverable into the requested output formats."""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

from argus.exporters import FORMATS, export
from argus.runtime.context import Workspace


def slugify(text: str, fallback: str = "deliverable") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-")
    return slug or fallback


async def export_markdown(
    workspace: Workspace,
    markdown: str,
    fmt: str,
    *,
    title: str,
    run_meta: dict[str, Any],
    by: str = "Exporter",
) -> dict[str, Any]:
    if fmt not in FORMATS:
        raise ValueError(f"Unsupported format '{fmt}'")
    export_id = f"exp_{uuid.uuid4().hex[:10]}"
    sink = workspace.sink
    await sink.emit("export.status", {"export_id": export_id, "format": fmt, "status": "generating"})
    try:
        # PDF/DOCX rendering is CPU-bound; keep the event loop responsive.
        data, mime, ext = await asyncio.to_thread(export, markdown, fmt, title=title, metadata=run_meta)
        path = f"exports/{slugify(title)}.{ext}"
        if mime.startswith("text/") or fmt in ("latex", "json"):
            art = await workspace.write(path, data.decode(), by=by, kind="export", mime=mime)
        else:
            art = await workspace.write_binary(path, data, by=by, mime=mime)
    except Exception as exc:
        await sink.emit(
            "export.status",
            {"export_id": export_id, "format": fmt, "status": "failed", "error": str(exc)[:300]},
        )
        raise
    await sink.emit(
        "export.status",
        {
            "export_id": export_id,
            "format": fmt,
            "status": "ready",
            "artifact_id": str(art.id),
            "path": art.path,
        },
    )
    return {
        "export_id": export_id,
        "format": fmt,
        "path": art.path,
        "version": art.version,
        "size": art.size,
        "mime": art.mime,
    }
