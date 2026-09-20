"""baseline

Revision ID: 0001_baseline
Create Date: 2026-09-20 19:38:28.135298
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Baseline: establishes alembic_version so later revisions have a parent.

    No schema yet. The nine tables from spec section 5 arrive in their own
    revision, which will name this one as down_revision.
    """


def downgrade() -> None:
    """Nothing to undo."""
