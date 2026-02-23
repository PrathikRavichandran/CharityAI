"""
Verify all 3 Ollama models are available before running the pipeline.

Usage:
    python scripts/test_ollama.py
"""

import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
REQUIRED_MODELS = ["phi3", "mistral", "llama3"]


async def main() -> None:
    print(f"🤖  Checking Ollama at {OLLAMA_URL}...")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            available = [m["name"].split(":")[0] for m in resp.json().get("models", [])]
    except Exception as e:
        print(f"❌  Cannot reach Ollama: {e}")
        print("    Make sure Ollama is running: ollama serve")
        sys.exit(1)

    print(f"📦  Available models: {available}")
    all_ok = True
    for model in REQUIRED_MODELS:
        if model in available:
            print(f"    ✅  {model}")
        else:
            print(f"    ❌  {model} — MISSING  →  ollama pull {model}")
            all_ok = False

    if all_ok:
        print("\n🎉  All required models are available. Ready to run!")
    else:
        print("\n⚠️   Some models are missing. Pull them before starting the pipeline.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
