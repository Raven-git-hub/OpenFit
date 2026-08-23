"""
Run this ONCE to grant OpenFit access to your Google Health data (steps,
sleep, resting heart rate - sourced from your Pixel Watch/Fitbit).

Prereqs (see README for the full walkthrough):
  1. Create a Google Cloud project, enable the Google Health API.
  2. Configure the OAuth consent screen (Testing mode is fine - add
     yourself as a test user, no Google review needed for personal use).
  3. Create an OAuth Client ID of type "Desktop app". Note the client ID
     and secret.

Usage:
    GOOGLE_HEALTH_CLIENT_ID=... GOOGLE_HEALTH_CLIENT_SECRET=... python3 authorize.py

This starts a tiny local web server on http://127.0.0.1:8765, opens (or
prints) the Google consent URL, and once you approve access, captures the
authorization code and exchanges it for a refresh token - saved to
GOOGLE_HEALTH_TOKENSTORE (defaults to /data/.google_health_token.json) so
the background sync never needs interactive login again.
"""

import base64
import hashlib
import json
import os
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT_URI = "http://127.0.0.1:8765/"

SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
]

_captured_code = {}


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            _captured_code["code"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>Authorized. You can close this tab and return "
                              b"to the terminal.</body></html>")
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # keep the console quiet


def main():
    client_id = os.getenv("GOOGLE_HEALTH_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_HEALTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit("Set GOOGLE_HEALTH_CLIENT_ID and GOOGLE_HEALTH_CLIENT_SECRET first.")

    # PKCE
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode(params)

    print("\nOpen this URL in a browser and approve access:\n")
    print(auth_url)
    print("\nWaiting for you to complete the consent screen...")

    server = HTTPServer(("127.0.0.1", 8765), _CallbackHandler)
    while "code" not in _captured_code:
        server.handle_request()

    code = _captured_code["code"]
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
        timeout=15,
    )
    resp.raise_for_status()
    tokens = resp.json()

    if "refresh_token" not in tokens:
        raise SystemExit(
            "No refresh_token in response - if you'd already authorized this app before, "
            "revoke access at https://myaccount.google.com/permissions and try again "
            "(Google only issues a refresh token on first consent, or when prompt=consent "
            "forces re-consent)."
        )

    tokenstore = os.getenv("GOOGLE_HEALTH_TOKENSTORE", "/data/.google_health_token.json")
    os.makedirs(os.path.dirname(tokenstore), exist_ok=True)
    with open(tokenstore, "w") as f:
        json.dump(tokens, f)

    print(f"\nDone. Refresh token saved to {tokenstore}.")
    print("You can now start the main service normally (docker compose up -d).")


if __name__ == "__main__":
    main()
