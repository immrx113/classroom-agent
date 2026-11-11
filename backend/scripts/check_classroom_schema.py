"""Read-only startup check: the configured classroom DB must match Alembic heads."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from app.config import settings
from app.db.session import engine


async def main():
    if make_url(settings.DATABASE_URL).database != "yuan_classroom_local":
        raise RuntimeError("Startup requires yuan_classroom_local")
    expected = set(ScriptDirectory.from_config(Config("alembic.ini")).get_heads())
    try:
        async with engine.connect() as conn:
            current = set(await conn.run_sync(
                lambda sync: MigrationContext.configure(sync).get_current_heads()))
        if current != expected:
            raise RuntimeError(
                f"Database migration required: current={sorted(current)}, expected={sorted(expected)}. "
                "In backend run .\\.venv\\Scripts\\python.exe -m alembic upgrade head")
        print("Classroom database schema is current.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
