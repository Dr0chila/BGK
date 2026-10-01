"""users.position — должность

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    # без автоподстановки: нынешних админов владелец назначит сам (менеджер или старший — угадать нельзя)
    op.add_column("users", sa.Column("position", sa.Text))


def downgrade():
    op.drop_column("users", "position")
