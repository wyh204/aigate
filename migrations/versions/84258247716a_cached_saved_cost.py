"""cached saved cost

Revision ID: 84258247716a
Revises: 51f592f77192
Create Date: 2026-11-02 12:46:18.225191

Adds ``requests.saved_cost_usd``: what a cache hit saved. The server default
matters — the table already has rows in a running deployment, and a NOT NULL
column without one cannot be added to a non-empty table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "84258247716a"
down_revision: str | Sequence[str] | None = "51f592f77192"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "requests",
        sa.Column(
            "saved_cost_usd",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("requests") as batch:
        batch.drop_column("saved_cost_usd")
