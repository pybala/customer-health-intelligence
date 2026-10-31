import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Migrations always run as the owner role. The application role never executes DDL.
MIGRATION_URL = os.environ.get("MIGRATION_DATABASE_URL")
if not MIGRATION_URL:
    raise RuntimeError("MIGRATION_DATABASE_URL is required to run migrations")


def run_migrations_offline() -> None:
    context.configure(url=MIGRATION_URL, literal_binds=True, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(MIGRATION_URL, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
