"""skill versions: immutable SKILL.md + files snapshot per create/update/restore/install

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-27 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skill_versions",
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("skill_md", sa.Text(), nullable=False),
        sa.Column(
            "files",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        sa.Column("author", sa.String(length=320), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["skill_id"], ["skills.id"], name=op.f("fk_skill_versions_skill_id_skills"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["author_id"], ["users.id"], name=op.f("fk_skill_versions_author_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_versions")),
        sa.UniqueConstraint("skill_id", "number", name=op.f("uq_skill_versions_skill_id")),
    )
    with op.batch_alter_table("skill_versions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_skill_versions_skill_id"), ["skill_id"], unique=False)
    # Backfill: every existing user skill gets its current content as version 1.
    skills = sa.table(
        "skills",
        sa.column("id", sa.Uuid()),
        sa.column("owner_id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("version", sa.String()),
        sa.column("body", sa.Text()),
        sa.column("allowed_tools", sa.JSON()),
        sa.column("meta", sa.JSON()),
        sa.column("files", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    versions = sa.table(
        "skill_versions",
        sa.column("id", sa.Uuid()),
        sa.column("skill_id", sa.Uuid()),
        sa.column("number", sa.Integer()),
        sa.column("skill_md", sa.Text()),
        sa.column("files", sa.JSON()),
        sa.column("author_id", sa.Uuid()),
        sa.column("author", sa.String()),
        sa.column("note", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    import uuid

    from argus.skills.package import SkillPackage

    bind = op.get_bind()
    rows = []
    for r in bind.execute(sa.select(skills)).mappings():
        pkg = SkillPackage(
            name=r["name"],
            description=r["description"],
            body=r["body"],
            version=r["version"],
            allowed_tools=list(r["allowed_tools"] or []),
            metadata=dict(r["meta"] or {}),
        )
        rows.append(
            {
                "id": uuid.uuid4(),
                "skill_id": r["id"],
                "number": 1,
                "skill_md": pkg.render(),
                "files": dict(r["files"] or {}),
                "author_id": r["owner_id"],
                "author": "",
                "note": "baseline",
                "created_at": r["created_at"],
            }
        )
    if rows:
        op.bulk_insert(versions, rows)


def downgrade() -> None:
    with op.batch_alter_table("skill_versions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_skill_versions_skill_id"))
    op.drop_table("skill_versions")
