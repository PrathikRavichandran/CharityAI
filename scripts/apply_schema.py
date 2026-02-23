#!/usr/bin/env python3
"""
apply_schema.py — Apply infra/schema.sql to the configured database.

Auto-detects local vs RDS:
  - If DATABASE_URL points to localhost/127.0.0.1 → local mode
  - If DATABASE_URL points to RDS → ensure local IP is added to Security Group first!

Usage:
  python scripts/apply_schema.py              # Uses .env (default)
  python scripts/apply_schema.py --local      # Uses .env.local (local Docker Postgres)
"""

import asyncio
import os
import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def load_env(env_file: str = ".env") -> None:
    """Load .env file into os.environ."""
    env_path = ROOT / env_file
    if not env_path.exists():
        print(f"❌  {env_file} not found at {env_path}")
        sys.exit(1)
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


async def main() -> None:
    parser = argparse.ArgumentParser(description="Apply CharityAI schema to PostgreSQL")
    parser.add_argument("--local", action="store_true",
                        help="Use .env.local (local Docker Postgres) instead of .env (RDS)")
    args = parser.parse_args()

    env_file = ".env.local" if args.local else ".env"
    load_env(env_file)

    import asyncpg

    database_url = os.environ.get("DATABASE_URL", "")

    # Strip SQLAlchemy prefix for asyncpg
    url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    if not url:
        print("❌  DATABASE_URL not set in environment")
        sys.exit(1)

    # Friendly display (mask password)
    display = url.split("@")[-1] if "@" in url else url
    print(f"🔗  Connecting to: {url[:30]}...")

    schema_path = ROOT / "infra" / "schema.sql"
    if not schema_path.exists():
        print(f"❌  Schema file not found: {schema_path}")
        sys.exit(1)

    schema_sql = schema_path.read_text(encoding="utf-8")

    try:
        conn = await asyncpg.connect(url, timeout=15)
        print("✅  Connected to database")

        async with conn.transaction():
            await conn.execute(schema_sql)

        await conn.close()
        print("✅  Schema applied successfully!")
        print("    Tables: pipeline, organizations, priority_queue,")
        print("            appointment_history, audit_log, dropped_emails")

    except asyncpg.exceptions.PostgresError as e:
        print(f"❌  PostgreSQL error: {e}")
        sys.exit(1)
    except OSError as e:
        print(f"❌  Connection failed: {e}")
        print()
        if "localhost" not in url and "127.0.0.1" not in url:
            print("  ⚠️  RDS is unreachable from your current IP.")
            print("  Fix: AWS Console → RDS → Security Group → Add inbound rule:")
            print("       Type: PostgreSQL | Port: 5432 | Source: My IP")
            print()
            print("  Or use --local flag to apply schema to local Docker Postgres:")
            print("    1. docker compose -f infra/docker-compose.yml up -d")
            print("    2. python scripts/apply_schema.py --local")
        else:
            print("  ⚠️  Local Postgres is not running.")
            print("  Start it: docker compose -f infra/docker-compose.yml up -d")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
