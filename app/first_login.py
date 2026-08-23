"""
Run this ONCE, interactively, if your Garmin account has MFA/2FA enabled.
It logs in, prompts you for the MFA code on the terminal, then saves the
session tokens to disk so the background service never has to do an
interactive login again.

Usage (from the app/ directory, with your .env's values exported, or edit
the two lines below directly):

    GARMIN_EMAIL=you@example.com GARMIN_PASSWORD=yourpassword python3 first_login.py

If your account has no MFA, you don't need this - main.py's first sync
will log in on its own.
"""

import os
from garminconnect import Garmin

TOKENSTORE = os.getenv("GARMIN_TOKENSTORE", "/data/.garminconnect")


def prompt_mfa():
    return input("Enter the MFA code Garmin just sent you: ").strip()


def main():
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        raise SystemExit("Set GARMIN_EMAIL and GARMIN_PASSWORD in your environment first.")

    client = Garmin(email, password, prompt_mfa=prompt_mfa)
    client.login(TOKENSTORE)
    print(f"Login successful. Session saved to {TOKENSTORE}.")
    print("You can now start the main service normally (docker compose up -d).")


if __name__ == "__main__":
    main()
