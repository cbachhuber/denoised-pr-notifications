"""Fixtures that run the real run.sh against a local HTTPS webhook sink."""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from actors import Actor, app_installation_token, request
from scenarios import KNOWN_GAPS, SCENARIOS, Cast, Expectation

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_DEADLINE = timedelta(minutes=3)
QUIET_PERIOD_SECONDS = 30
POLL_INTERVAL_SECONDS = 10
STALE_AFTER = timedelta(hours=1)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.is_relative_to(Path(__file__).parent):
            item.add_marker(pytest.mark.e2e)


@dataclass(frozen=True)
class Card:
    path: str
    payload: dict[str, Any]

    @property
    def texts(self) -> list[str]:
        content = self.payload["attachments"][0]["content"]
        return [block["text"] for block in content["body"]]

    @property
    def link(self) -> str:
        match = re.search(r"\]\((https://[^)\s]+)\)", self.texts[0])
        assert match, self.texts[0]
        return match[1]

    @property
    def actor(self) -> str:
        return self.texts[0].removeprefix("[").split(" ", 1)[0]


@dataclass
class Sink:
    port: int
    cards: list[Card] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def url(self, path: str) -> str:
        return f"https://localhost:{self.port}{path}"

    def received(self, path: str) -> list[Card]:
        with self.lock:
            return [card for card in self.cards if card.path == path]


def _system_ca_file() -> Path:
    paths = ssl.get_default_verify_paths()
    candidates = [
        os.environ.get("SSL_CERT_FILE"),
        paths.cafile,
        paths.openssl_cafile,
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/ssl/cert.pem",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise RuntimeError("no system CA bundle found")


@pytest.fixture(scope="session")
def tls(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    """Self-signed localhost certificate, its key, and a CA bundle trusting it alongside the system CAs."""
    folder = tmp_path_factory.mktemp("tls")
    cert, key, bundle = folder / "cert.pem", folder / "key.pem", folder / "bundle.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    # Replacing rather than extending the system CAs would break the calls to github.com.
    bundle.write_text(_system_ca_file().read_text() + cert.read_text())
    return cert, key, bundle


@pytest.fixture
def sink(tls: tuple[Path, Path, Path]) -> Iterator[Sink]:
    with serve_sink(tls) as state:
        yield state


@contextlib.contextmanager
def serve_sink(tls: tuple[Path, Path, Path]) -> Iterator[Sink]:
    cert, key, _ = tls
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    state = Sink(port=server.server_address[1])

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with state.lock:
                state.cards.append(Card(self.path, payload))
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server.RequestHandlerClass = Handler
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()


@dataclass
class Notifier:
    """One cron-like installation of run.sh, with every path it touches redirected into a temp folder."""

    folder: Path
    sink: Sink
    ca_bundle: Path

    @property
    def state_dir(self) -> Path:
        return self.folder / "state"

    @property
    def log(self) -> str:
        log = self.state_dir / "log"
        return log.read_text() if log.exists() else ""

    def run(
        self, token: str, recipients: dict[str, str], **extra_env: str
    ) -> subprocess.CompletedProcess[str]:
        recipients_file = self.folder / "recipients.toml"
        recipients_file.write_text(
            "".join(f"{login} = '{url}'\n" for login, url in recipients.items())
        )
        # A clean environment, so nothing from the developer's shell or ~/.config/pr-notify leaks in.
        env = {
            "HOME": os.environ["HOME"],
            "GH_TOKEN": token,
            "ENV_FILE": str(self.folder / "absent-env"),
            "STATE_DIR": str(self.state_dir),
            "RECIPIENTS_FILE": str(recipients_file),
            "TEAMS_WEBHOOK_URL_FOR_ALERTS": self.sink.url("/alerts"),
            "POST_SPACING": "0",
            "SSL_CERT_FILE": str(self.ca_bundle),
            "CURL_CA_BUNDLE": str(self.ca_bundle),
            **extra_env,
        }
        return subprocess.run(
            [str(REPO_ROOT / "run.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )


@pytest.fixture
def notifier(tmp_path: Path, sink: Sink, tls: tuple[Path, Path, Path]) -> Notifier:
    return Notifier(folder=tmp_path, sink=sink, ca_bundle=tls[2])


@dataclass(frozen=True)
class World:
    expectations: dict[str, Expectation]
    cards: list[Card]
    log: str


def _env(name: str) -> str:
    if not (value := os.environ.get(name)):
        pytest.skip(f"{name} is unset; see CONTRIBUTING.md")
    return value


def _cast(repo: str) -> Cast:
    me = Actor(_env("E2E_ME_LOGIN"), _env("E2E_TOKEN_ME"), repo)

    def app(name: str) -> Actor:
        key = (
            os.environ.get(f"E2E_{name}_PRIVATE_KEY")
            or Path(
                os.path.expandvars(_env(f"E2E_{name}_PRIVATE_KEY_FILE"))
            ).read_text()
        )
        token = app_installation_token(_env(f"E2E_{name}_APP_ID"), key, repo)
        return Actor(name.lower(), token, repo)

    if run_id := os.environ.get("GITHUB_RUN_ID"):
        server, origin = (
            os.environ["GITHUB_SERVER_URL"],
            os.environ["GITHUB_REPOSITORY"],
        )
        run_link = f"{server}/{origin}/actions/runs/{run_id}"
        run_id = f"{run_id}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    else:
        run_id = f"local-{secrets.token_hex(3)}"
        run_link = f"local run {run_id}"
    return Cast(me, app("ALICE"), app("BOB"), run_id, run_link)


def _close_stale_pull_requests(me: Actor) -> None:
    """Leftovers from crashed runs would crowd the 50 pull requests pr-notify looks at."""
    cutoff = datetime.now(UTC) - STALE_AFTER
    stale = request(
        "GET",
        f"/repos/{me.repo}/issues?labels=e2e&state=open&per_page=100",
        me.token,
    )
    for issue in stale:
        if datetime.fromisoformat(issue["created_at"]) < cutoff:
            me.call(
                "PATCH",
                f"/repos/{me.repo}/pulls/{issue['number']}",
                {"state": "closed"},
            )


@pytest.fixture(scope="session")
def world(
    tmp_path_factory: pytest.TempPathFactory, tls: tuple[Path, Path, Path]
) -> Iterator[World]:
    """Arrange every scenario, then run run.sh like cron would until the expected cards arrive."""
    cast = _cast(f"{_env('E2E_ORG')}/{_env('E2E_REPO')}")
    _close_stale_pull_requests(cast.me)
    # Allows for this machine's clock running ahead of GitHub's.
    since = datetime.now(UTC) - timedelta(minutes=1)
    expectations: dict[str, Expectation] = {}
    with serve_sink(tls) as sink:
        try:
            for name, scenario in SCENARIOS.items():
                expectations[name] = scenario(cast)
            notifier = Notifier(tmp_path_factory.mktemp("world"), sink, tls[2])
            recipients = {cast.me.name: sink.url("/me")}
            expected = {
                posted.url
                for name, expectation in expectations.items()
                if name not in KNOWN_GAPS
                for posted in expectation.reported
            }

            def run(**extra_env: str) -> None:
                result = notifier.run(cast.me.token, recipients, **extra_env)
                assert result.returncode == 0, notifier.log

            # Only the first run is told where to start; the rest rely on the stored state, as under cron.
            run(SINCE=since.isoformat())
            deadline = datetime.now(UTC) + CONTROL_DEADLINE
            while not expected <= {card.link for card in sink.received("/me")}:
                if datetime.now(UTC) > deadline:
                    break
                time.sleep(POLL_INTERVAL_SECONDS)
                run()
            time.sleep(QUIET_PERIOD_SECONDS)
            run()
            yield World(expectations, sink.received("/me"), notifier.log)
        finally:
            for expectation in expectations.values():
                cast.me.close(expectation.pull)
