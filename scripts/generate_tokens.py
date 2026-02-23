#!/usr/bin/env python3
"""
generate_tokens.py — OAuth2 Flow for Gmail + Google Calendar

Generates:
  auth/gmail_token.json       — Read/send Gmail permissions
  auth/gcalendar_token.json   — Read/write Google Calendar permissions

Prerequisites:
  1. Create OAuth2 credentials in Google Cloud Console:
       APIs & Services → Credentials → Create OAuth Client ID → Desktop app
  2. Download the JSON → save as auth/gmail_oauth.json
  3. Run this script: python scripts/generate_tokens.py

Both tokens are generated in the same flow from the same OAuth client,
requesting all scopes at once to minimize browser pop-ups.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
AUTH_DIR = ROOT / "auth"
AUTH_DIR.mkdir(exist_ok=True)

OAUTH_JSON    = AUTH_DIR / "gmail_oauth.json"
GMAIL_TOKEN   = AUTH_DIR / "gmail_token.json"
GCAL_TOKEN    = AUTH_DIR / "gcalendar_token.json"

SCOPES = [
    # Gmail: read + send (modify labels on processed emails)
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    # Calendar: read/write events
    "https://www.googleapis.com/auth/calendar",
]


def main() -> None:
    if not OAUTH_JSON.exists():
        print("❌  OAuth credentials not found at:", OAUTH_JSON)
        print()
        print("Steps to create them:")
        print("  1. Go to Google Cloud Console → APIs & Services → Credentials")
        print("  2. Create OAuth 2.0 Client ID → Application type: Desktop app")
        print("  3. Download JSON → save as: auth/gmail_oauth.json")
        print("  4. Re-run this script")
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.oauth2.credentials import Credentials
    except ImportError:
        print("❌  Missing google-auth-oauthlib. Run:")
        print("    pip install google-auth-oauthlib google-auth-httplib2")
        sys.exit(1)

    print("🔐  Starting OAuth2 flow for Gmail + Google Calendar")
    print("    Requesting scopes:")
    for s in SCOPES:
        print(f"    - {s}")
    print()
    print("    A browser window will open — sign in with the Google account")
    print("    that owns the Gmail inbox and Calendar to be managed.\n")

    flow = InstalledAppFlow.from_client_secrets_file(str(OAUTH_JSON), SCOPES)
    creds = flow.run_local_server(port=0)

    token_data = creds.to_json()

    # Save the same token for both Gmail and GCal (same account, same OAuth client)
    GMAIL_TOKEN.write_text(token_data)
    print(f"✅  Gmail token saved:    {GMAIL_TOKEN}")

    GCAL_TOKEN.write_text(token_data)
    print(f"✅  Calendar token saved: {GCAL_TOKEN}")
    print()
    print("🎉  All tokens generated successfully!")
    print("    Tokens auto-refresh when expired — no need to re-run.")
    print()
    print("    ⚠️  IMPORTANT: Add auth/ to .gitignore (already done).")
    print("    These tokens grant access to Gmail and Calendar — keep them secret!")


if __name__ == "__main__":
    main()
