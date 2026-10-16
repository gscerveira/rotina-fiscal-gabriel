"""Alembic environment.

The app hands over its own connection (see fiscal.db.migrate); the `alembic` CLI falls back
to the local database file.
"""

from alembic import context
from sqlalchemy import Connection, create_engine

from fiscal.db import DATA_DIR, DEFAULT_URL, Base


def run(connection: Connection) -> None:
    # Batch mode: SQLite cannot ALTER most things in place.
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


shared = context.config.attributes.get("connection")
if shared is not None:
    run(shared)
else:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with create_engine(DEFAULT_URL).begin() as own:
        run(own)
