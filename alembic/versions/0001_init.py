"""users и attempts

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("tg_id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("username", sa.Text),
        sa.Column("full_name", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "attempts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.tg_id", ondelete="CASCADE"), nullable=False),
        sa.Column("mode", sa.Text, nullable=False),
        sa.Column("topic", sa.Text),
        sa.Column("score", sa.Integer, nullable=False),
        sa.Column("total", sa.Integer, nullable=False),
        sa.Column("pct", sa.Integer, nullable=False),
        sa.Column("mistakes", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_attempts_user_created", "attempts", ["user_id", "created_at"])


def downgrade():
    op.drop_table("attempts")
    op.drop_table("users")
