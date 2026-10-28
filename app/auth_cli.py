"""
The one way a setup link's token is made (auth.py): run in the container,

    python auth_cli.py setup-link

it makes a token, stores only its SHA-256 and an expiry 24 hours on in
/data/auth.json - replacing any earlier unused one - and prints one JSON
line, {"token": "...", "expires_at": "..."}. The link is
http://<host>:<port>/setup#<token>: the token in the fragment, so it
never reaches a server's log. The Admin runs this through `docker exec`
(its Setup link button); without the Admin, by hand:

    docker exec <container> python auth_cli.py setup-link

The same link sets a first password and resets a forgotten one.
$OPENFIT_AUTH_PATH points it elsewhere than /data/auth.json (for running
outside the container).
"""

import json
import os
import sys

import auth

USAGE = "usage: python auth_cli.py setup-link"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["setup-link"]:
        print(USAGE, file=sys.stderr)
        return 2
    path = os.getenv("OPENFIT_AUTH_PATH") or auth.DEFAULT_AUTH_PATH
    token, expires_at = auth.make_setup_token(path)
    print(json.dumps({"token": token, "expires_at": expires_at}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
