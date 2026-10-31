"""
The OpenFit Admin's web server: one page and its JSON API, on port 8090.

Every /api route but setup and login needs a logged-in session (401),
and every request that isn't a GET must be JSON and carry the header
X-OpenFit-Admin: 1 (403) - a cross-site page can't send either. The one
exception is a restore's upload (R1), multipart, not JSON: the header,
which another site's form can't add, is its guard. The two downloads are
GETs that change nothing, so a plain link fetches them: the session is all
they need (its cookie is SameSite=Strict, so another site can't send it).
A body is at most 1 MB, an upload's 2 GB. Nothing
runs on import: serve() reads the environment, publishes the app shelf if
it's behind the code, prints the setup code if there's no password yet,
starts the schedule (SU1: the only thing that ever runs by itself) and the
server.
"""

import os
import re
import sys

from flask import Flask, Response, jsonify, render_template, request, session
from werkzeug.exceptions import ClientDisconnected, RequestEntityTooLarge
from werkzeug.formparser import parse_form_data

from admin_auth import Auth, AuthError, load_secret_key
from admin_ops import Admin, BadSchedule, OpError, Refused, timezones

PORT = 8090
OPEN_ENDPOINTS = {"index", "api_setup", "api_login", "static"}
# The one route whose body is multipart, not JSON: a backup uploaded to
# restore as a new user. Its file is streamed to disk as it arrives, and
# stopped (413) past UPLOAD_MAX; the margin is the form's other fields.
UPLOAD_ENDPOINTS = {"api_restore_upload"}
UPLOAD_MAX = 2 * 1024 ** 3
UPLOAD_MARGIN = 64 * 1024
TOO_LARGE = "That file is too large: a backup to restore is at most 2 GB."
# Every other body: the largest JSON the page sends is a few hundred bytes.
BODY_MAX = 1024 * 1024


def _fail(message, status, **extra):
    return jsonify(ok=False, error=message, **extra), status


class _Capped:
    """Where an upload's file part is written: the file new_upload() opened,
    and a 413 the moment it passes UPLOAD_MAX, before the byte past it is
    written. The one file a restore takes: a second file part is refused."""

    def __init__(self, f):
        self.f, self.size, self.taken = f, 0, False

    def factory(self, **_part):
        if self.taken:
            raise ValueError("a restore takes one file")
        self.taken = True
        return self

    def write(self, data):
        self.size += len(data)
        if self.size > UPLOAD_MAX:
            raise RequestEntityTooLarge()
        return self.f.write(data)

    def seek(self, *args):
        return self.f.seek(*args)

    def tell(self):
        return self.f.tell()


def _form_port(value):
    """A port from a form field: digits as a number, anything else as it
    came - for the port rules to refuse."""
    return int(value) if isinstance(value, str) and re.fullmatch(r"\d{1,5}", value) else value


def _form_flag(value):
    """true or false from a form field; anything else as it came."""
    return {"true": True, "false": False}.get(value, value)


def create_app(admin, auth, secret_key):
    app = Flask(__name__)
    app.config.update(
        # An upload's body, for the restore route's sake; every other
        # request is held to BODY_MAX by guard().
        MAX_CONTENT_LENGTH=UPLOAD_MAX + UPLOAD_MARGIN,
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
        upload = request.endpoint in UPLOAD_ENDPOINTS
        request.max_content_length = UPLOAD_MAX + UPLOAD_MARGIN if upload else BODY_MAX
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            form = upload and request.mimetype == "multipart/form-data"
            if request.headers.get("X-OpenFit-Admin") != "1" or not (request.is_json or form):
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
        # The Schedule panel's timezones: the names a schedule can be saved
        # with, written into the page's script as JSON.
        return render_template("index.html", state=state, timezones=list(timezones()))

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

    # The schedule (SU1): saved whole, each field checked (400 naming it);
    # the reply its next run. Dismiss puts a failed or missed run's notice away.
    @app.put("/api/schedule")
    def api_schedule():
        try:
            view = admin.save_schedule(request.get_json(silent=True))
        except BadSchedule as e:
            return _fail(e.message, e.status, field=e.field)
        return jsonify(ok=True, next_run=view["next_run"], next_run_local=view["next_run_local"], schedule=view)

    @app.post("/api/schedule/dismiss")
    def api_schedule_dismiss():
        try:
            return jsonify(ok=True, schedule=admin.dismiss_run())
        except Refused as e:
            return _fail(e.message, e.status)

    @app.post("/api/library/update")
    def api_update_apps():
        try:
            return jsonify(ok=True, op=admin.start_update_apps()), 202
        except Refused as e:
            return _fail(e.message, e.status)

    @app.post("/api/admin/update")
    def api_admin_update():
        try:
            return jsonify(ok=True, op=admin.start_admin_update()), 202
        except Refused as e:
            return _fail(e.message, e.status)

    @app.post("/api/admin/revert")
    def api_admin_revert():
        try:
            return jsonify(ok=True, op=admin.start_admin_revert()), 202
        except Refused as e:
            return _fail(e.message, e.status)

    @app.post("/api/users")
    def api_add_user():
        data = body()
        try:
            return jsonify(ok=True, op=admin.start_add_user(data.get("name"), data.get("port"), request.host)), 202
        except Refused as e:
            return _fail(e.message, e.status)

    @app.post("/api/users/restore")
    def api_restore_upload():
        """A new user from an uploaded backup (R1): multipart - name, port,
        keep_devices ("true" or "false") and the file - the file written to
        /data/restore-uploads/ as it arrives, then the restore started as
        restore-from's is. Refused, cut off or too large, the file is
        deleted here; started, the restore deletes it when it ends."""
        path, f = admin.new_upload()
        capped, started = _Capped(f), False
        try:
            try:
                with f:
                    _stream, form, files = parse_form_data(
                        request.environ, stream_factory=capped.factory, max_content_length=request.max_content_length,
                        max_form_memory_size=4096, max_form_parts=8, silent=False)
            except RequestEntityTooLarge:
                return _fail(TOO_LARGE, 413)
            except ClientDisconnected:
                return _fail("The upload was cut off. Try again.", 400)
            except ValueError:
                return _fail("Send the form as the page does: a name, a port and one backup file.", 400)
            if files.get("file") is None or capped.size == 0:
                return _fail("Choose a backup file to restore.", 400)
            op = admin.start_restore(form.get("name"), _form_port(form.get("port")),
                                     _form_flag(form.get("keep_devices")), ("upload", path), request.host)
            started = True
            return jsonify(ok=True, op=op), 202
        except Refused as e:
            return _fail(e.message, e.status)
        finally:
            if not started:
                admin.drop_upload(path)

    @app.post("/api/users/restore-from")
    def api_restore_from():
        """A new user from one of this server's backups, or a removed user's
        kept data (R1): {name, port, keep_devices, backup | removed}."""
        data = body()
        backup, removed = data.get("backup"), data.get("removed")
        if (backup is None) == (removed is None):
            return _fail("Name one backup, or one removed user's kept data, to restore.", 400)
        source = ("backup", backup) if backup is not None else ("removed", removed)
        try:
            return jsonify(ok=True, op=admin.start_restore(data.get("name"), data.get("port"),
                                                           data.get("keep_devices"), source, request.host)), 202
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

    @app.post("/api/users/<name>/restart")
    def api_restart_user(name):
        return done(lambda: admin.restart_user(name))

    @app.post("/api/users/<name>/remove")
    def api_remove_user(name):
        return done(lambda: admin.remove_user(name))

    @app.post("/api/users/<name>/setup-link")
    def api_setup_link(name):
        # The link goes back to the page once, in this reply; nothing keeps it.
        try:
            return jsonify(ok=True, **admin.setup_link(name, request.host))
        except Refused as e:
            return _fail(e.message, e.status)
        except OpError as e:
            return _fail(e.message, 502, detail=e.detail)

    @app.post("/api/removed/<name>/delete-data")
    def api_delete_data(name):
        return done(lambda: admin.delete_data(name, body().get("confirm")))

    def download(start):
        """A download, without the password: the archive streamed as an
        attachment, or its refusal - or a 502 if it failed before its first
        byte. A failure part-way cuts the download off."""
        try:
            filename, chunks = start()
        except Refused as e:
            return _fail(e.message, e.status)
        except OpError as e:
            return _fail(e.message, 502, detail=e.detail)
        return Response(chunks, mimetype="application/gzip", headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        })

    @app.get("/api/backups/<name>/download")
    def api_download_backup(name):
        return download(lambda: admin.download_backup(name))

    @app.post("/api/backups/<name>/delete")
    def api_delete_backup(name):
        return done(lambda: admin.delete_backup(name))

    @app.get("/api/removed/<name>/download")
    def api_download_kept(name):
        return download(lambda: admin.download_kept(name))

    @app.post("/api/library/publish")
    def api_publish_again():
        return done(admin.publish_again)

    # Organisation repos (A6). Adding one answers with its public deploy
    # key, to add on GitHub - never the private one, which no route returns.
    @app.post("/api/repos")
    def api_add_repo():
        try:
            return jsonify(ok=True, **admin.add_repo(body().get("url")))
        except Refused as e:
            return _fail(e.message, e.status)
        except OpError as e:
            return _fail(e.message, 502, detail=e.detail)

    @app.post("/api/repos/<slug>/connect")
    def api_connect_repo(slug):
        try:
            return jsonify(ok=True, op=admin.start_repo_connect(slug)), 202
        except Refused as e:
            return _fail(e.message, e.status)

    @app.post("/api/repos/<slug>/update")
    def api_update_repo(slug):
        try:
            return jsonify(ok=True, op=admin.start_repo_update(slug)), 202
        except Refused as e:
            return _fail(e.message, e.status)

    @app.post("/api/repos/<slug>/remove")
    def api_remove_repo(slug):
        return done(lambda: admin.remove_repo(slug))

    return app


def serve():
    repo = os.environ.get("OPENFIT_REPO", "").rstrip("/")
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        sys.exit(f"[admin] OPENFIT_REPO must be the OpenFit repo's path (got {repo!r})")
    data_dir = os.environ.get("ADMIN_DATA", "/data")
    os.makedirs(data_dir, exist_ok=True)
    admin = Admin(repo, data_dir)
    admin.publish_at_startup()  # before the checks and the server start: nothing else runs meanwhile
    auth = Auth(data_dir)
    if auth.setup_code:
        print(f"[admin] No password yet. Open http://<server IP>:{PORT} and use this setup code: {auth.setup_code}", flush=True)
    admin.start_schedule()
    create_app(admin, auth, load_secret_key(data_dir)).run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    serve()
