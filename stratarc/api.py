"""`stratarc api`: the read-only local API.

Usage:
  api serve [--socket PATH | --port N] [--root R]    serve /v1 on a Unix socket (default) or a loopback port
  api schema                                         print the published JSON schema

The server answers GET only; every other method gets 405. It has no authentication beyond the socket file's mode (0600). A port binds 127.0.0.1 and nothing else. Every route reads through the same library functions the commands use and writes nothing. Every string in a response has the home shown as `~` and token-shaped substrings replaced by `[REDACTED:<kind>]`; provider secrets are never read, only their presence is reported.

Routes (all under /v1): health, config/keys, config/explain, projects, runtimes, adapters, providers, log, log/{id}, verify/last, schema. The envelope is `{ok, data, error}` with `error` carrying `code`, `message`, `param` and `hint`. A list answers with `total`, `shown`, `truncated`, `rows`, `next_offset` and `output_truncated`; `limit` and `offset` page it, and a response over 200000 bytes loses rows from its end and says so.

Exit codes of `main`: 0 ok, 2 invalid input, 5 unavailable, 130 interrupted.
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import json
import os
import re
import socket
import socketserver
import stat
import sys
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from stratarc import __version__, changelog, layers, providers, registry, verify
from stratarc import home_layout as layout
from stratarc.config import ConfigError, load_config
from stratarc.layers import LayerError, Layers
from stratarc.paths import source_root
from stratarc.resources import data_dir

API_VERSION = "v1"
RESPONSE_CAP = 200000
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
LOOPBACK = "127.0.0.1"
SOCKET_NAME = "api.sock"
SOCKET_PATH_LIMIT = 100
SCHEMA_NAME = "api-v1.schema.json"
SCOPE = ("project", "agent", "account", "runtime")
LOG_FILTERS = ("id", "actor_kind", "actor", "layer", "key", "status", "cause_id", "source_ref", "file", "command", "project", "since", "until")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?(\.\d+)?Z?)?$")
_CHANGE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_UNAVAILABLE_MARK = "[REDACTED:detector-unavailable]"


class ApiError(Exception):
    """A request the API refuses; carries the HTTP status and the envelope error fields."""

    def __init__(self, status: int, code: str, message: str, *, param: str | None = None, hint: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.param = param
        self.hint = hint


# ---- the published schema -------------------------------------------------------------------


def schema_text() -> str:
    return data_dir(f"schema/{SCHEMA_NAME}").read_text(encoding="utf-8")


def schema_document() -> dict[str, Any]:
    return json.loads(schema_text())


# ---- redaction and shaping ------------------------------------------------------------------


def _strings(value: Any, into: set[str]) -> None:
    if isinstance(value, str):
        into.add(value)
    elif isinstance(value, list):
        for item in value:
            _strings(item, into)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, into)


def _replace(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        return mapping[value]
    if isinstance(value, list):
        return [_replace(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: _replace(v, mapping) for k, v in value.items()}
    return value


def clean(value: Any) -> Any:
    """The value with the home shown as `~` and every token-shaped substring redacted. Fails closed with a 503."""
    found: set[str] = set()
    _strings(value, found)
    texts = sorted(t for t in found if t)
    mapping = {"": ""}
    if texts:
        redacted = changelog.redact_many([layers.tilde(t) for t in texts])
        if any(_UNAVAILABLE_MARK in r for r in redacted):
            raise ApiError(503, "redaction-unavailable", "The secret scanner is not available, so no data can be shown.", hint="Run `stratarc doctor` to check the install.")
        mapping.update(zip(texts, redacted))
    return _replace(value, mapping)


def _dump(envelope: dict[str, Any]) -> bytes:
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _page(rows: list[Any], offset: int, limit: int, **extra: Any) -> dict[str, Any]:
    window = rows[offset : offset + limit]
    end = offset + len(window)
    data: dict[str, Any] = {
        "total": len(rows),
        "shown": len(window),
        "truncated": end < len(rows),
        "rows": window,
        "next_offset": end if end < len(rows) else None,
        "output_truncated": False,
    }
    data.update(extra)
    return data


def _fits(data: dict[str, Any]) -> bool:
    return len(_dump({"ok": True, "data": data, "error": None})) <= RESPONSE_CAP


def fit(data: Any, offset: int = 0) -> Any:
    """Drop rows (or report files) from the end until the envelope is within the response cap."""
    if not isinstance(data, dict):
        return data
    name = "rows" if "rows" in data else "files" if "files" in data else None
    if name is None or _fits(data):
        return data
    items = data[name]
    low, high = 0, len(items)  # the most items that fit lies in [low, high)
    while low < high:
        middle = (low + high + 1) // 2
        if _fits({**data, name: items[:middle]}):
            low = middle
        else:
            high = middle - 1
    out = {**data, name: items[:low], "output_truncated": True}
    if name == "rows":
        out["shown"] = low
        out["truncated"] = True
        out["next_offset"] = offset + low
    return out


# ---- request parameters ---------------------------------------------------------------------


class Query:
    """The query string of one request, checked against the names a route accepts."""

    def __init__(self, raw: str, allowed: tuple[str, ...]) -> None:
        values: dict[str, str] = {}
        for name, items in parse_qs(raw, keep_blank_values=True).items():
            if name not in allowed:
                raise ApiError(400, "unknown-param", f'The parameter "{name}" is not accepted here.', param=name, hint="Accepted: " + (", ".join(allowed) or "none") + ".")
            if len(items) > 1:
                raise ApiError(400, "duplicate-param", f'The parameter "{name}" was given more than once.', param=name)
            if items[0] != "":
                values[name] = items[0]
        self.values = values

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def require(self, name: str) -> str:
        value = self.values.get(name)
        if value is None:
            raise ApiError(400, "missing-param", f'The parameter "{name}" is required.', param=name)
        return value

    def integer(self, name: str, default: int, low: int, high: int) -> int:
        raw = self.values.get(name)
        if raw is None:
            return default
        try:
            number = int(raw)
        except ValueError:
            raise ApiError(400, "invalid-param", f'"{name}" must be a whole number.', param=name) from None
        if not low <= number <= high:
            raise ApiError(400, "invalid-param", f'"{name}" must be between {low} and {high}.', param=name)
        return number

    def paging(self) -> tuple[int, int]:
        return self.integer("limit", DEFAULT_LIMIT, 1, MAX_LIMIT), self.integer("offset", 0, 0, 2**31)


_PAGING = ("limit", "offset")


# ---- routes ---------------------------------------------------------------------------------

_LAYER_NOT_FOUND = {"unknown-key", "unknown-project", "unknown-agent", "unknown-account", "unknown-runtime"}


def _layer_error(exc: LayerError, root: Path) -> ApiError:
    where = ""
    if exc.file is not None:
        where = layers.display_path(exc.file, root) + (f":{exc.line}" if exc.line else "") + ": "
    status = 404 if exc.code in _LAYER_NOT_FOUND else 400
    return ApiError(status, exc.code, where + exc.message, param=exc.key, hint=exc.hint or None)


def _error_dict(exc: LayerError, root: Path) -> dict[str, Any]:
    err = _layer_error(exc, root)
    return {"code": err.code, "message": err.message, "param": err.param, "hint": err.hint}


def _loaded(root: Path, query: Query) -> tuple[Layers, dict[str, str | None]]:
    scope = {name: query.get(name) for name in SCOPE}
    if not root.is_dir():
        raise ApiError(400, "source-root-missing", f"The source root {layers.tilde(str(root))} does not exist.", hint="Start the server with an existing --root.")
    try:
        return layers.load(root, **scope), scope
    except LayerError as exc:
        raise _layer_error(exc, root) from None


def _resolution(res: layers.Resolution, root: Path) -> dict[str, Any]:
    return {
        "key": res.key,
        "value": res.value,
        "decided_by": {"layer": res.decided_by.layer, "op": res.decided_by.op},
        "steps": [
            {
                "layer": s.layer,
                "file": layers.display_path(s.file, root) if s.file else s.label,
                "line": s.line,
                "op": s.op,
                "mode": s.mode,
                "value": s.value,
                "overrode": [{"layer": layer, "value": value} for layer, value in s.overrode],
            }
            for s in res.steps
        ],
    }


def route_health(root: Path, query: Query) -> dict[str, Any]:
    return {"status": "ok", "api": API_VERSION, "version": __version__, "read_only": True, "source_root": str(root)}


def route_config_keys(root: Path, query: Query) -> dict[str, Any]:
    limit, offset = query.paging()
    layered, scope = _loaded(root, query)
    done, failed = layered.resolve_all()
    rows = [{"key": k, "value": r.value, "decided_by": {"layer": r.decided_by.layer, "op": r.decided_by.op}} for k, r in done.items()]
    return _page(rows, offset, limit, scope=scope, errors=[_error_dict(e, root) for e in failed.values()])


def route_config_explain(root: Path, query: Query) -> dict[str, Any]:
    key = query.require("key")
    limit, offset = query.paging()
    layered, scope = _loaded(root, query)
    keys = layered.expand(key)
    if not keys:
        try:
            layered.resolve(key)
        except LayerError as exc:
            raise _layer_error(exc, root) from None
    try:
        rows = [_resolution(layered.resolve(k), root) for k in keys]
    except LayerError as exc:
        raise _layer_error(exc, root) from None
    return _page(rows, offset, limit, scope=scope)


def route_projects(root: Path, query: Query) -> dict[str, Any]:
    limit, offset = query.paging()
    base = root / "projects-root"
    rows = []
    for entry in sorted(base.iterdir()) if base.is_dir() else []:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        agents = sorted({f.stem for f in (entry / "agents").iterdir() if f.is_file()}) if (entry / "agents").is_dir() else []
        rows.append(
            {
                "name": entry.name,
                "has_permissions": (entry / "permissions.json").is_file(),
                "has_settings": (entry / "stratarc.toml").is_file(),
                "agents": agents,
            }
        )
    return _page(rows, offset, limit)


def route_runtimes(root: Path, query: Query) -> dict[str, Any]:
    from stratarc.adapters._common import runtime_registry

    limit, offset = query.paging()
    try:
        configured = load_config(root).runtimes
    except ConfigError as exc:
        raise ApiError(400, "config-invalid", str(exc), hint="Fix stratarc.toml.") from None
    rows = []
    for name, runtime in runtime_registry().items():
        setting = configured.get(name)
        target = setting.target if setting and setting.target else runtime.target()
        rows.append(
            {
                "name": name,
                "adapter_module": runtime.module,
                "default_target": str(runtime.target()),
                "hook_registry": runtime.hook_registry,
                "enabled": bool(setting and setting.enabled),
                "target": str(target),
                "target_exists": Path(target).is_dir(),
            }
        )
    return _page(rows, offset, limit)


def route_adapters(root: Path, query: Query) -> dict[str, Any]:
    limit, offset = query.paging()
    rows = []
    for name, manifest in registry.list_adapters().items():
        row = {k: v for k, v in manifest.items() if k not in ("schema_version", "module", "source")}
        row["deprecation"] = registry.read_deprecation(name)
        rows.append(row)
    return _page(rows, offset, limit)


def route_providers(root: Path, query: Query) -> dict[str, Any]:
    limit, offset = query.paging()
    rows = []
    for doc in providers.list_providers():
        row = {
            "name": str(doc.get("name", "")),
            "endpoint": str(doc.get("endpoint", "")),
            "models": [str(m.get("id", "")) for m in doc.get("models", []) if isinstance(m, dict)],
            "has_secret": bool(doc.get("secret")),
        }
        if isinstance(doc.get("description"), str):
            row["description"] = doc["description"]
        rows.append(row)
    return _page(rows, offset, limit)


def route_log(root: Path, query: Query) -> dict[str, Any]:
    limit, offset = query.paging()
    filters = {name: query.get(name) for name in LOG_FILTERS if query.get(name)}
    for name in ("since", "until"):
        if name in filters and not _DATE.match(filters[name]):
            raise ApiError(400, "invalid-param", f'"{name}" must be a date (YYYY-MM-DD) or a UTC timestamp.', param=name)
    try:
        changes = changelog.query(filters, limit=None)
    except changelog.ChangeLogError as exc:
        raise ApiError(400, "invalid-param", str(exc)) from None
    except changelog.DatabaseUnavailable as exc:
        raise ApiError(503, "log-unavailable", f"The change log cannot be read: {exc}", hint="Try again, or read stratarc.jsonl.") from None
    return _page(changes, offset, limit)


def route_log_entry(root: Path, query: Query, change_id: str) -> dict[str, Any]:
    if not _CHANGE_ID.match(change_id):
        raise ApiError(400, "invalid-param", "The change id is not valid.", param="id")
    try:
        return changelog.explain_data(change_id)
    except changelog.ChangeLogError:
        raise ApiError(404, "unknown-change", f"No change {change_id} in the log.", param="id", hint="List changes with GET /v1/log.") from None
    except changelog.DatabaseUnavailable as exc:
        raise ApiError(503, "log-unavailable", f"The change log cannot be read: {exc}") from None


def route_verify_last(root: Path, query: Query) -> dict[str, Any]:
    report = verify.last()
    if report is None:
        raise ApiError(404, "no-verify-report", "No verify report has been recorded.", hint="Run `stratarc verify run`.")
    report = dict(report)
    files = report.get("files", [])
    report["files"] = files if isinstance(files, list) else []
    report["files_total"] = len(report["files"])
    report["output_truncated"] = False
    return report


def route_schema(root: Path, query: Query) -> dict[str, Any]:
    return schema_document()


# path -> (handler, accepted query names)
ROUTES: dict[str, tuple[Callable[[Path, Query], dict[str, Any]], tuple[str, ...]]] = {
    "/v1/health": (route_health, ()),
    "/v1/config/keys": (route_config_keys, (*SCOPE, *_PAGING)),
    "/v1/config/explain": (route_config_explain, ("key", *SCOPE, *_PAGING)),
    "/v1/projects": (route_projects, _PAGING),
    "/v1/runtimes": (route_runtimes, _PAGING),
    "/v1/adapters": (route_adapters, _PAGING),
    "/v1/providers": (route_providers, _PAGING),
    "/v1/log": (route_log, (*LOG_FILTERS, *_PAGING)),
    "/v1/verify/last": (route_verify_last, ()),
    "/v1/schema": (route_schema, ()),
}


def dispatch(root: Path, target: str) -> tuple[int, dict[str, Any]]:
    """The status and envelope for a GET of `target` (path and query)."""
    try:
        parts = urlsplit(target)
        path = unquote(parts.path)
        if len(path) > 1:
            path = path.rstrip("/")
        offset = 0
        if path.startswith("/v1/log/") and path != "/v1/log/":
            query = Query(parts.query, ())
            data: Any = route_log_entry(root, query, path[len("/v1/log/") :])
        elif path in ROUTES:
            handler, allowed = ROUTES[path]
            query = Query(parts.query, allowed)
            offset = query.integer("offset", 0, 0, 2**31) if "offset" in allowed else 0
            data = handler(root, query)
        else:
            raise ApiError(404, "not-found", f"There is no route {path}.", hint="Start at GET /v1/health; GET /v1/schema lists the routes.")
        data = fit(clean(data), offset)
        body = {"ok": True, "data": data, "error": None}
        if len(_dump(body)) > RESPONSE_CAP:
            raise ApiError(500, "response-too-large", "The response does not fit in the response cap.")
        return 200, body
    except ApiError as err:
        return err.status, {"ok": False, "data": None, "error": _error_body(err)}
    except Exception:  # noqa: BLE001 - a route bug must not leak internals or stop the server
        return 500, {"ok": False, "data": None, "error": {"code": "internal-error", "message": "The request failed.", "param": None, "hint": None}}


def _error_body(err: ApiError) -> dict[str, Any]:
    try:
        return clean({"code": err.code, "message": err.message, "param": err.param, "hint": err.hint})
    except ApiError:
        return {"code": err.code, "message": "", "param": None, "hint": None}


# ---- the server -----------------------------------------------------------------------------

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _host_name(header: str) -> str:
    """The host part of a Host header, without its port."""
    if header.startswith("["):
        return header.split("]")[0] + "]"
    return header.rsplit(":", 1)[0] if ":" in header else header


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "stratarc-api"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def address_string(self) -> str:
        return "local"

    def _send(self, status: int, envelope: dict[str, Any], *, allow: bool = False) -> None:
        body = _dump(envelope)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if allow:
            self.send_header("Allow", "GET")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if getattr(self.server, "loopback_only", False):
            if _host_name(self.headers.get("Host") or "") not in _LOOPBACK_HOSTS:
                err = ApiError(403, "forbidden-host", "The Host header is not a loopback name.")
                self._send(403, {"ok": False, "data": None, "error": _error_body(err)})
                return
        status, envelope = dispatch(self.server.root, self.path)  # type: ignore[attr-defined]
        self._send(status, envelope)

    def _refuse(self) -> None:
        err = ApiError(405, "method-not-allowed", f"{self.command} is not allowed; the API is read-only.", hint="Use GET.")
        self._send(405, {"ok": False, "data": None, "error": _error_body(err)}, allow=True)

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _refuse  # noqa: N815


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Serves on a Unix socket file with mode 0600."""

    daemon_threads = True
    loopback_only = False

    def __init__(self, path: Path, root: Path) -> None:
        self.root = root
        self.socket_path = path
        self._claim(path)
        super().__init__(str(path), Handler)

    @staticmethod
    def _claim(path: Path) -> None:
        if len(str(path).encode()) > SOCKET_PATH_LIMIT:
            raise OSError(f"The socket path is longer than {SOCKET_PATH_LIMIT} bytes: {path}")
        if not path.parent.is_dir():
            path.parent.mkdir(parents=True, mode=0o700)
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(mode):
            raise OSError(f"{path} exists and is not a socket.")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
        except OSError:
            path.unlink()  # a stale socket left by a stopped server
        else:
            raise OSError(f"Another server is already listening on {path}.")
        finally:
            probe.close()

    def server_bind(self) -> None:
        previous = os.umask(0o177)
        try:
            super().server_bind()
        finally:
            os.umask(previous)
        os.chmod(self.socket_path, 0o600)

    def server_close(self) -> None:
        super().server_close()
        with contextlib.suppress(OSError):
            self.socket_path.unlink()

    @property
    def address(self) -> str:
        return str(self.socket_path)


class TcpServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Serves on 127.0.0.1 only."""

    daemon_threads = True
    loopback_only = True

    def __init__(self, port: int, root: Path) -> None:
        self.root = root
        super().__init__((LOOPBACK, port), Handler)

    @property
    def address(self) -> str:
        return f"http://{LOOPBACK}:{self.server_address[1]}"


def default_socket() -> Path:
    return layout.state_dir() / SOCKET_NAME


def make_server(root: Path, *, socket_path: Path | None = None, port: int | None = None) -> UnixServer | TcpServer:
    """A bound, not yet serving server: a loopback port when `port` is given, else a Unix socket."""
    root = Path(root).resolve()
    if port is not None:
        return TcpServer(port, root)
    return UnixServer(Path(socket_path) if socket_path else default_socket(), root)


def serve_in_thread(server: UnixServer | TcpServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return thread


# ---- command line ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stratarc api", description="The read-only local API.")
    sub = parser.add_subparsers(dest="action", required=True)
    serve = sub.add_parser("serve", help="serve /v1 on a Unix socket or a loopback port")
    where = serve.add_mutually_exclusive_group()
    where.add_argument("--socket", metavar="PATH", help="the socket file (default: state/api.sock in the home)")
    where.add_argument("--port", metavar="N", type=int, help="a port on 127.0.0.1 instead of a socket (0 picks a free one)")
    serve.add_argument("--root", metavar="R", help="the source root")
    sub.add_parser("schema", help="print the published JSON schema")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.action == "schema":
        sys.stdout.write(schema_text())
        return 0
    if args.port is not None and not 0 <= args.port <= 65535:
        print("error invalid-input  --port must be between 0 and 65535.", file=sys.stderr)
        return 2
    root = source_root(Path(args.root) if args.root else None)
    if not root.is_dir():
        print(f"error source-root-missing  The source root {layers.tilde(str(root))} does not exist.\n  Pass an existing directory with --root.", file=sys.stderr)
        return 2
    try:
        server = make_server(root, socket_path=Path(args.socket).expanduser() if args.socket else None, port=args.port)
    except OSError as exc:
        print(f"error unavailable  {layers.tilde(str(exc))}", file=sys.stderr)
        return 5
    print(f"listening on {layers.tilde(server.address)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
