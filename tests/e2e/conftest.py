"""Fixtures that run the real run.sh against a local HTTPS webhook sink."""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


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
