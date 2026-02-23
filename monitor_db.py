import asyncio
import asyncpg
import os
import time
from tabulate import tabulate

DB_URL = "postgresql://postgres:pg-charityai@localhost:5432/postgres"

async def monitor():
    conn = await asyncpg.connect(DB_URL)
    from datetime import datetime
    last_time = datetime.min
    
    print("Monitoring audit_log... waiting for new events...")
    
    try:
        while True:
            records = await conn.fetch('''
                SELECT id, created_at, from_state, to_state, actor, details
                FROM audit_log 
                WHERE created_at > $1 
                ORDER BY created_at ASC
            ''', last_time)
            
            if records:
                table_data = []
                for r in records:
                    table_data.append([
                        r['created_at'].strftime("%H:%M:%S"),
                        r['actor'],
                        f"{r['from_state']} -> {r['to_state']}",
                        str(r['details'])[:50]
                    ])
                    last_time = max(last_time, r['created_at'].replace(tzinfo=None) if r['created_at'].tzinfo else r['created_at'])
                
                print(tabulate(table_data, headers=["Time", "Actor", "Transition", "Details"]))
                print("-" * 80)
            
            await asyncio.sleep(2)
    except KeyboardInterrupt:
        print("Monitoring stopped.")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(monitor())
