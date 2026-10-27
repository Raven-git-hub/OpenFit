"""
The OpenFit Admin's web server: one page and its JSON API, on port 8090.

Every /api route but setup and login needs a logged-in session (401),
and every request that isn't a GET must be JSON and carry the header
X-OpenFit-Admin: 1 (403) - a cross-site page can't send either. Nothing
runs on import: serve() reads the environment, prints the setup code if
there's no password yet, starts the weekly check and the server.
"""

import os
import sys

from flask import Flask, jsonify, render_template, request, session

from admin_auth import Auth, AuthError, load_secret_key
from admin_ops import Admin, OpError, Refused

PORT = 8090
OPEN_ENDPOINTS = {"index", "api_setup", "api_login", "static"}


def _fail(message, status, **extra):
    return jsonify(ok=False, error=message, **extra), status


def create_app(admin, auth, secret_key):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret_key,
        # Its own name: cookies aren't kept apart by port, and the user
        # system on :8080 is the same host. Plain HTTP on the LAN, so not
        # Secure.
        SESSION_COOKIE_NAME="openfit_admin",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=False,
    )

    def logged_in():
        tag = auth.session_tag()
        return tag is not None and session.get("admin") == tag

    @app.before_request
    def guard():
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("X-OpenFit-Admin") != "1" or not request.is_json:
                return _fail("This request didn't come from the Admin's page.", 403)
        if request.path.startswith("/api/") and request.endpoint not in OPEN_ENDPOINTS and not logged_in():
            return _fail("Log in first.", 401)
        return None

    def body():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    @app.get("/")
    def index():
        state = "setup" if not auth.password_set() else ("app" if logged_in() else "login")
        return render_template("index.html", state=state)

    @app.post("/api/setup")
    def api_setup():
        data = body()
        try:
            auth.setup(data.get("code"), data.get("password"))
        except AuthError as e:
            return _fail(e.message, e.status)
        session.clear()
        session["admin"] = auth.session_tag()
        return jsonify(ok=True)

    @app.post("/api/login")
    def api_login():
        try:
            auth.login(request.remote_addr or "", body().get("password"))
        except AuthError as e:
            return _fail(e.message, e.status)
        session.clear()
        session["admin"] = auth.session_tag()
        return jsonify(ok=True)

    @app.post("/api/logout")
    def api_logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/status")
    def api_status():
        return jsonify(admin.status())

    @app.get("/api/op")
    def api_op():
        return jsonify(op=admin.current_op())

    @app.get("/api/backups")
    def api_backups():
        return jsonify(backups=admin.list_backups())

    @app.post("/api/check")
    def api_check():
        try:
            return jsonify(ok=True, check=admin.check())
        except Refused as e:
            return _fail(e.message, e.status)
        except OpError as e:
            return _fail(e.message, 502, check=admin.last_check())

    @app.post("/api/update")
    def api_update():
        try:
            return jsonify(ok=True, op=admin.start_update()), 202
        except Refused as e:
            return _fail(e.message, e.status)

    @app.post("/api/undo")
    def api_undo():
        try:
            return jsonify(ok=True, op=admin.start_undo()), 202
        except Refused as e:
            return _fail(e.message, e.status)

    @app.post("/api/users")
    def api_add_user():
        data = body()
        try:
            return jsonify(ok=True, op=admin.start_add_user(data.get("name"), data.get("port"), request.host)), 202
        except Refused as e:
            return _fail(e.message, e.status)

    def done(action):
        """A user action that runs in the request: 200, its refusal, or 502
        with the output behind a failed step."""
        try:
            action()
        except Refused as e:
            return _fail(e.message, e.status)
        except OpError as e:
            return _fail(e.message, 502, detail=e.detail)
        return jsonify(ok=True)

    @app.post("/api/users/<name>/start")
    def api_start_user(name):
        return done(lambda: admin.start_user(name))

    @app.post("/api/users/<name>/stop")
    def api_stop_user(name):
        return done(lambda: admin.stop_user(name))

    @app.post("/api/users/<name>/remove")
    def api_remove_user(name):
        return done(lambda: admin.remove_user(name))

    @app.post("/api/removed/<name>/delete-data")
    def api_delete_data(name):
        return done(lambda: admin.delete_data(name, body().get("confirm")))

    return app


def serve():
    repo = os.environ.get("OPENFIT_REPO", "").rstrip("/")
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        sys.exit(f"[admin] OPENFIT_REPO must be the OpenFit repo's path (got {repo!r})")
    data_dir = os.environ.get("ADMIN_DATA", "/data")
    os.makedirs(data_dir, exist_ok=True)
    admin = Admin(repo, data_dir)
    auth = Auth(data_dir)
    if auth.setup_code:
        print(f"[admin] No password yet. Open http://<server IP>:{PORT} and use this setup code: {auth.setup_code}", flush=True)
    admin.start_auto_checks()
    create_app(admin, auth, load_secret_key(data_dir)).run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    serve()
