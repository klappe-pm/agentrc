"""The read-only local API: routes, envelope, refusals, redaction and the server's own limits."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import stat
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

import jsonschema
import pytest

from stratarc import api, changelog, verify

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "layers" / "source"
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2  # built at run time so no literal token sits in the file


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, stratarc_home):
    for name in [n for n in os.environ if n.startswith("STRATARC_") and n != "STRATARC_HOME"]:
        monkeypatch.delenv(name)
    monkeypatch.delenv("LLM_ROOT_PROJECTS_DIR", raising=False)


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("localhost")
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


class Reply(NamedTuple):
    status: int
    headers: dict[str, str]
    raw: bytes

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.raw)


class Served(NamedTuple):
    server: Any
    root: Path
    home: Path

    def request(self, method: str, target: str, headers: dict[str, str] | None = None) -> Reply:
        connection = UnixConnection(self.server.address)
        try:
            connection.request(method, target, headers=headers or {})
            response = connection.getresponse()
            return Reply(response.status, {k.lower(): v for k, v in response.getheaders()}, response.read())
        finally:
            connection.close()

    def get(self, target: str) -> Reply:
        return self.request("GET", target)


def snapshot(*roots: Path) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for base in roots:
        for path in sorted(base.rglob("*")):
            out[str(path)] = path.read_bytes() if path.is_file() else None
    return out


def seed(root: Path, home: Path) -> None:
    (root / "projects-root" / "notes" / "stratarc.toml").write_text(f'apikey = "{TOKEN}"\nlabel = "plain"\n', encoding="utf-8")
    state = home / ".stratarc"
    (state / "providers").mkdir(parents=True)
    provider = {
        "schema_version": 1,
        "name": "alpha",
        "endpoint": "https://alpha.example.com/v1",
        "models": [{"id": "m1"}, {"id": "m2", "display_name": "Model Two"}],
        "secret": "secret://team/alpha-key",
        "description": f"left a key here {TOKEN}",
    }
    (state / "providers" / "alpha.json").write_text(json.dumps(provider), encoding="utf-8")
    first = changelog.record({"actor_kind": "human", "actor": "tester", "command": "config set a", "layer": "project", "key": "settings.label", "projects": ["notes"]}, enabled=True)
    changelog.record({"actor_kind": "hook", "actor": "ci", "command": "sync", "layer": "base", "key": "permissions.timeout", "cause_id": first}, enabled=True)
    report = {
        "schema": 1,
        "id": "ver-20260101000000-abc",
        "ts": "2026-01-01T00:00:00Z",
        "scope": "all",
        "root": str(root),
        "result": "verified",
        "exit": 0,
        "counts": {"verified": 1, "drift": 0, "skipped": 0},
        "files": [{"area": "runtime", "name": "codex", "file": f"{home}/.codex/a", "status": "verified"}],
        "notes": [f"token {TOKEN}"],
    }
    path = verify.last_report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report), encoding="utf-8")


@pytest.fixture
def served(tmp_path, stratarc_home):
    root = tmp_path / "source"
    shutil.copytree(FIXTURE, root)
    seed(root, stratarc_home)
    sockets = Path(tempfile.mkdtemp(prefix="sa-"))
    server = api.make_server(root, socket_path=sockets / "s")
    thread = api.serve_in_thread(server)
    yield Served(server, root, stratarc_home)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    shutil.rmtree(sockets, ignore_errors=True)


SCHEMA = api.schema_document()


def check(route: str, body: dict[str, Any]) -> None:
    jsonschema.Draft202012Validator(SCHEMA).validate(body)
    assert body["ok"] is True
    sub = {"$schema": SCHEMA["$schema"], "$ref": SCHEMA["x-routes"][route], "$defs": SCHEMA["$defs"]}
    jsonschema.Draft202012Validator(sub).validate(body["data"])


def check_error(body: dict[str, Any], code: str) -> None:
    jsonschema.Draft202012Validator(SCHEMA).validate(body)
    assert body["ok"] is False and body["data"] is None
    assert body["error"]["code"] == code


ALL_GETS = [
    ("/v1/health", "/v1/health"),
    ("/v1/config/keys", "/v1/config/keys"),
    ("/v1/config/keys?project=notes&runtime=codex", "/v1/config/keys"),
    ("/v1/config/explain?key=permissions.network&project=notes", "/v1/config/explain"),
    ("/v1/projects", "/v1/projects"),
    ("/v1/runtimes", "/v1/runtimes"),
    ("/v1/adapters", "/v1/adapters"),
    ("/v1/providers", "/v1/providers"),
    ("/v1/log", "/v1/log"),
    ("/v1/log?actor_kind=hook&since=2020-01-01", "/v1/log"),
    ("/v1/verify/last", "/v1/verify/last"),
    ("/v1/schema", "/v1/schema"),
]


# ---- routes and envelope ---------------------------------------------------------------------


@pytest.mark.parametrize(("target", "route"), ALL_GETS)
def test_every_route_matches_the_schema(served, target, route):
    reply = served.get(target)
    assert reply.status == 200
    assert reply.headers["content-type"].startswith("application/json")
    check(route, reply.body)


def test_every_published_route_is_served(served):
    assert set(SCHEMA["x-routes"]) - {"/v1/log/{id}"} == {route for _, route in ALL_GETS}


def test_log_detail_route(served):
    rows = served.get("/v1/log").body["data"]["rows"]
    assert [r["actor_kind"] for r in rows] == ["hook", "human"]
    cause = rows[1]["id"]
    reply = served.get(f"/v1/log/{cause}")
    assert reply.status == 200
    check("/v1/log/{id}", reply.body)
    assert reply.body["data"]["caused"] == [rows[0]["id"]]


def test_log_filters_and_paging(served):
    data = served.get("/v1/log?actor_kind=human").body["data"]
    assert (data["total"], data["shown"], data["truncated"], data["next_offset"]) == (1, 1, False, None)
    page = served.get("/v1/log?limit=1").body["data"]
    assert (page["total"], page["shown"], page["truncated"], page["next_offset"]) == (2, 1, True, 1)
    rest = served.get("/v1/log?limit=1&offset=1").body["data"]
    assert (rest["shown"], rest["truncated"], rest["next_offset"]) == (1, False, None)
    assert rest["rows"][0]["id"] != page["rows"][0]["id"]


def test_config_keys_resolve_through_the_layers(served):
    data = served.get("/v1/config/keys?project=notes").body["data"]
    values = {r["key"]: r for r in data["rows"]}
    assert values["permissions.timeout"]["value"] == 60
    assert values["permissions.timeout"]["decided_by"] == {"layer": "project", "op": "set"}
    assert data["scope"] == {"project": "notes", "agent": None, "account": None, "runtime": None}


def test_config_explain_shows_the_chain(served):
    data = served.get("/v1/config/explain?key=permissions.timeout&project=notes").body["data"]
    (row,) = data["rows"]
    assert [s["layer"] for s in row["steps"]] == ["base", "project"]
    assert row["steps"][1]["file"] == "projects-root/notes/permissions.json"
    assert row["steps"][1]["overrode"] == [{"layer": "base", "value": 30}]


def test_projects_runtimes_adapters_providers(served):
    projects = {r["name"]: r for r in served.get("/v1/projects").body["data"]["rows"]}
    assert set(projects) == {"cycle", "nomode", "notes"}
    assert projects["notes"]["agents"] == ["lead", "quiet", "reviewer", "worker"] and projects["notes"]["has_settings"] is True
    runtimes = {r["name"]: r for r in served.get("/v1/runtimes").body["data"]["rows"]}
    assert runtimes["codex"]["enabled"] is True and runtimes["codex"]["target"] == "~/.codex"
    assert runtimes["claude"]["enabled"] is False
    adapters = {r["name"]: r for r in served.get("/v1/adapters").body["data"]["rows"]}
    assert "codex" in adapters and adapters["codex"]["deprecation"] is None
    (provider,) = served.get("/v1/providers").body["data"]["rows"]
    assert provider["models"] == ["m1", "m2"] and provider["has_secret"] is True


def test_verify_last_and_schema_route(served):
    report = served.get("/v1/verify/last").body["data"]
    assert report["result"] == "verified" and report["files_total"] == 1 and report["output_truncated"] is False
    assert served.get("/v1/schema").body["data"] == SCHEMA


def test_health_reports_read_only(served):
    data = served.get("/v1/health").body["data"]
    assert data["read_only"] is True and data["api"] == "v1"


def test_trailing_slash_is_tolerated(served):
    assert served.get("/v1/health/").status == 200


# ---- errors ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "status", "code"),
    [
        ("/", 404, "not-found"),
        ("/v2/health", 404, "not-found"),
        ("/v1/nope", 404, "not-found"),
        ("/v1/health?x=1", 400, "unknown-param"),
        ("/v1/projects?limit=0", 400, "invalid-param"),
        ("/v1/projects?limit=abc", 400, "invalid-param"),
        ("/v1/projects?offset=-1", 400, "invalid-param"),
        ("/v1/projects?limit=1&limit=2", 400, "duplicate-param"),
        ("/v1/config/explain", 400, "missing-param"),
        ("/v1/config/explain?key=nope.nothing", 404, "unknown-key"),
        ("/v1/config/keys?project=ghost", 404, "unknown-project"),
        ("/v1/config/keys?runtime=ghost", 404, "unknown-runtime"),
        ("/v1/log?since=yesterday", 400, "invalid-param"),
        ("/v1/log/chg-missing", 404, "unknown-change"),
        ("/v1/log/bad%20id", 400, "invalid-param"),
    ],
)
def test_errors_use_the_envelope(served, target, status, code):
    reply = served.get(target)
    assert reply.status == status
    check_error(reply.body, code)
    assert set(reply.body["error"]) == {"code", "message", "param", "hint"}


def test_verify_last_without_a_report_is_not_found(served):
    verify.last_report_path().unlink()
    reply = served.get("/v1/verify/last")
    assert reply.status == 404
    check_error(reply.body, "no-verify-report")


def test_a_failing_route_answers_500_without_internals(served, monkeypatch):
    def boom(*_args):
        raise RuntimeError("secret internal detail")

    monkeypatch.setitem(api.ROUTES, "/v1/projects", (boom, ()))
    reply = served.get("/v1/projects")
    assert reply.status == 500
    check_error(reply.body, "internal-error")
    assert b"internal detail" not in reply.raw


# ---- read only -------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def test_other_methods_are_refused(served, method):
    reply = served.request(method, "/v1/health")
    assert reply.status == 405
    assert reply.headers["allow"] == "GET"
    if method != "HEAD":
        check_error(reply.body, "method-not-allowed")


def test_a_refused_write_carries_no_effect(served):
    before = snapshot(served.root, served.home)
    for method in ("POST", "PUT", "DELETE"):
        connection = UnixConnection(served.server.address)
        connection.request(method, "/v1/config/keys", body=json.dumps({"settings.label": "changed"}), headers={"Content-Type": "application/json"})
        assert connection.getresponse().status == 405
        connection.close()
    assert snapshot(served.root, served.home) == before


def test_reading_every_route_writes_nothing(served):
    before = snapshot(served.root, served.home)
    for target, _ in ALL_GETS:
        served.get(target)
    first = served.get("/v1/log").body["data"]["rows"][0]["id"]
    served.get(f"/v1/log/{first}")
    assert snapshot(served.root, served.home) == before


# ---- socket and port -------------------------------------------------------------------------


def test_socket_file_mode_is_0600(served):
    mode = stat.S_IMODE(os.stat(served.server.address).st_mode)
    assert mode == 0o600


def test_socket_is_removed_on_close(tmp_path):
    sockets = Path(tempfile.mkdtemp(prefix="sa-"))
    try:
        server = api.make_server(tmp_path, socket_path=sockets / "s")
        assert (sockets / "s").exists()
        server.server_close()
        assert not (sockets / "s").exists()
    finally:
        shutil.rmtree(sockets, ignore_errors=True)


def test_a_stale_socket_is_reclaimed_and_a_live_one_refused(tmp_path):
    sockets = Path(tempfile.mkdtemp(prefix="sa-"))
    try:
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(sockets / "s"))
        stale.close()  # the file stays, nothing listens
        first = api.make_server(tmp_path, socket_path=sockets / "s")
        with pytest.raises(OSError, match="already listening"):
            api.make_server(tmp_path, socket_path=sockets / "s")
        first.server_close()
        (sockets / "s").write_text("not a socket")
        with pytest.raises(OSError, match="not a socket"):
            api.make_server(tmp_path, socket_path=sockets / "s")
    finally:
        shutil.rmtree(sockets, ignore_errors=True)


def test_a_port_binds_loopback_only(tmp_path):
    server = api.make_server(tmp_path, port=0)
    thread = api.serve_in_thread(server)
    try:
        assert server.server_address[0] == "127.0.0.1"
        port = server.server_address[1]
        connection = http.client.HTTPConnection("127.0.0.1", port)
        connection.request("GET", "/v1/health")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["data"]["status"] == "ok"
        connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", port)
        connection.request("GET", "/v1/health", headers={"Host": "rebind.example.org"})
        response = connection.getresponse()
        assert response.status == 403
        assert json.loads(response.read())["error"]["code"] == "forbidden-host"
        connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", port)
        connection.request("POST", "/v1/health")
        assert connection.getresponse().status == 405
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_there_is_no_way_to_choose_another_address():
    import inspect

    assert set(inspect.signature(api.make_server).parameters) == {"root", "socket_path", "port"}
    assert api.LOOPBACK == "127.0.0.1"


# ---- redaction -------------------------------------------------------------------------------


def test_no_secret_value_or_token_shaped_string_in_any_response(served):
    targets = [t for t, _ in ALL_GETS] + ["/v1/config/keys?project=notes", "/v1/config/explain?key=settings.apikey&project=notes"]
    targets += [f"/v1/log/{r['id']}" for r in served.get("/v1/log").body["data"]["rows"]]
    for target in targets:
        text = served.get(target).raw.decode("utf-8")
        assert TOKEN not in text, target
        assert "a1B2c3D4e5F6g7H8i9J0" not in text, target
        assert "secret://" not in text, target
        assert changelog.redact(text) == text, target
    keys = {r["key"]: r["value"] for r in served.get("/v1/config/keys?project=notes").body["data"]["rows"]}
    assert keys["settings.apikey"] == "[REDACTED:github_token]"
    assert keys["settings.label"] == "plain"
    (provider,) = served.get("/v1/providers").body["data"]["rows"]
    assert provider["description"] == "left a key here [REDACTED:github_token]"


def test_the_home_is_shown_as_a_tilde(served):
    for target, _ in ALL_GETS:
        assert str(served.home) not in served.get(target).raw.decode("utf-8"), target
    assert served.get("/v1/verify/last").body["data"]["files"][0]["file"] == "~/.codex/a"


def test_redaction_failing_closed_withholds_the_data(served, monkeypatch):
    monkeypatch.setattr(changelog, "redact_many", lambda values: ["[REDACTED:detector-unavailable]" for _ in values])
    reply = served.get("/v1/projects")
    assert reply.status == 503
    check_error(reply.body, "redaction-unavailable")


# ---- cap and paging --------------------------------------------------------------------------


def test_a_large_list_is_cut_at_the_cap_and_can_be_continued(served, monkeypatch):
    for index in range(40):
        doc = {"schema_version": 1, "name": f"p{index:02d}", "endpoint": "https://p.example.com/v1", "models": [{"id": "m"}], "description": "d" * 300}
        (served.home / ".stratarc" / "providers" / f"p{index:02d}.json").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(api, "RESPONSE_CAP", 4000)
    reply = served.get("/v1/providers?limit=1000")
    assert len(reply.raw) <= 4000
    data = reply.body["data"]
    check("/v1/providers", reply.body)
    assert data["total"] == 41 and data["output_truncated"] is True and data["truncated"] is True
    assert 0 < data["shown"] < 41 and data["next_offset"] == data["shown"]
    names = [r["name"] for r in data["rows"]]
    while data["next_offset"] is not None:
        data = served.get(f"/v1/providers?limit=1000&offset={data['next_offset']}").body["data"]
        names += [r["name"] for r in data["rows"]]
    assert names == sorted(names) and len(names) == 41 == len(set(names))


def test_the_default_page_size_and_cap_are_documented_values():
    assert (api.RESPONSE_CAP, api.DEFAULT_LIMIT) == (200000, 100)


def test_a_large_verify_report_is_cut(served, monkeypatch):
    path = verify.last_report_path()
    report = json.loads(path.read_text())
    report["files"] = [{"area": "runtime", "name": "codex", "file": f"f{i}", "status": "verified"} for i in range(500)]
    path.write_text(json.dumps(report))
    monkeypatch.setattr(api, "RESPONSE_CAP", 6000)
    reply = served.get("/v1/verify/last")
    assert len(reply.raw) <= 6000
    data = reply.body["data"]
    assert data["output_truncated"] is True and data["files_total"] == 500 and 0 < len(data["files"]) < 500


# ---- command line ----------------------------------------------------------------------------


def test_api_schema_prints_the_published_schema(capsys):
    assert api.main(["schema"]) == 0
    assert json.loads(capsys.readouterr().out) == SCHEMA


def test_the_schema_is_a_valid_draft_2020_12_schema():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


def test_serve_rejects_a_missing_root_and_a_bad_port(tmp_path, capsys):
    assert api.main(["serve", "--root", str(tmp_path / "absent")]) == 2
    assert "source-root-missing" in capsys.readouterr().err
    assert api.main(["serve", "--port", "70000", "--root", str(tmp_path)]) == 2


def test_serve_rejects_both_a_socket_and_a_port(tmp_path):
    with pytest.raises(SystemExit) as exc:
        api.main(["serve", "--socket", str(tmp_path / "s"), "--port", "1"])
    assert exc.value.code == 2


def test_serve_reports_an_unusable_socket_path(tmp_path, capsys):
    short = Path(tempfile.mkdtemp(prefix="sa-"))
    try:
        (short / "file").write_text("x")
        assert api.main(["serve", "--root", str(tmp_path), "--socket", str(short / "file")]) == 5
        assert "not a socket" in capsys.readouterr().err
    finally:
        shutil.rmtree(short, ignore_errors=True)
    assert api.main(["serve", "--root", str(tmp_path), "--socket", str(tmp_path / ("d" * 120) / "s")]) == 5
    assert "longer than" in capsys.readouterr().err
