import asyncio
import httpx

async def main():
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post("http://localhost:8001/poll")
            print("Response:", resp.status_code, resp.text)
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    asyncio.run(main())
