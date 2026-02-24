import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv('.env')

async def main():
    dsn = os.getenv('DATABASE_URL').replace('postgresql+asyncpg', 'postgresql')
    conn = await asyncpg.connect(dsn)
    
    print("--- PIPELINE ---")
    rows = await conn.fetch('SELECT id, email_id, org_name, current_state FROM pipeline ORDER BY created_at DESC LIMIT 5')
    for r in rows:
        print(dict(r))
        
    print("\n--- AUDIT LOG ---")
    audit_rows = await conn.fetch('SELECT pipeline_id, event_type, from_state, to_state FROM audit_log ORDER BY created_at DESC LIMIT 5')
    for r in audit_rows:
        print(dict(r))
        
    await conn.close()

if __name__ == '__main__':
    asyncio.run(main())
