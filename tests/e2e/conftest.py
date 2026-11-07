"""Fixtures that run the real run.sh against a local HTTPS webhook sink."""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
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
from actors import Actor, RunParticipants
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from scenarios import KNOWN_GAPS, SCENARIOS, Expectation

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

    def build_url(self, path: str) -> str:
        return f"https://localhost:{self.port}{path}"

    def collect_cards(self, path: str) -> list[Card]:
        with self.lock:
            return [card for card in self.cards if card.path == path]


def find_system_ca_file() -> Path:
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
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    localhost = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(localhost)
        .issuer_name(localhost)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        # Python's default context verifies strictly, which wants a well-formed CA, not a bare leaf.
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(public_key),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    # Replacing rather than extending the system CAs would break the calls to github.com.
    bundle.write_text(find_system_ca_file().read_text() + cert.read_text())
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
            "TEAMS_WEBHOOK_URL_FOR_ALERTS": self.sink.build_url("/alerts"),
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
class DeliveredNotifications:
    """What each scenario expected, and every card the notifier actually delivered for the whole run."""

    expectations: dict[str, Expectation]
    cards: list[Card]
    log: str


def close_stale_pull_requests(me: Actor) -> None:
    """Leftovers from crashed runs would crowd the 50 pull requests pr-notify looks at."""
    cutoff = datetime.now(UTC) - STALE_AFTER
    for pull in me.call_api("GET", f"/repos/{me.repo}/pulls?state=open&per_page=100"):
        if datetime.fromisoformat(pull["created_at"]) < cutoff:
            me.call_api(
                "PATCH",
                f"/repos/{me.repo}/pulls/{pull['number']}",
                {"state": "closed"},
            )


@pytest.fixture(scope="session")
def delivered_notifications(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    tls: tuple[Path, Path, Path],
) -> Iterator[DeliveredNotifications]:
    """Arrange the selected scenarios, then run run.sh like cron would until the expected cards arrive."""
    # Only the scenarios this session will assert on, so `-k` saves the GitHub round trips too.
    selected = [
        callspec.params["scenario"]
        for item in request.session.items
        if (callspec := getattr(item, "callspec", None))
        and "scenario" in callspec.params
    ]
    run = RunParticipants.from_environment()
    close_stale_pull_requests(run.me)
    # Allows for this machine's clock running ahead of GitHub's.
    since = datetime.now(UTC) - timedelta(minutes=1)
    expectations: dict[str, Expectation] = {}
    with serve_sink(tls) as sink:
        try:
            for name in selected:
                expectations[name] = SCENARIOS[name](run)
            notifier = Notifier(tmp_path_factory.mktemp("notifier"), sink, tls[2])
            recipients = {run.me.name: sink.build_url("/me")}
            expected = {
                activity.url
                for name, expectation in expectations.items()
                if name not in KNOWN_GAPS
                for activity in expectation.reported
            }

            def run_notifier(**extra_env: str) -> None:
                result = notifier.run(run.me.token, recipients, **extra_env)
                assert result.returncode == 0, notifier.log

            # Only the first run is told where to start; the rest rely on the stored state, as under cron.
            run_notifier(SINCE=since.isoformat())
            deadline = datetime.now(UTC) + CONTROL_DEADLINE
            while not expected <= {card.link for card in sink.collect_cards("/me")}:
                if datetime.now(UTC) > deadline:
                    break
                time.sleep(POLL_INTERVAL_SECONDS)
                run_notifier()
            time.sleep(QUIET_PERIOD_SECONDS)
            run_notifier()
            yield DeliveredNotifications(
                expectations, sink.collect_cards("/me"), notifier.log
            )
        finally:
            for expectation in expectations.values():
                run.me.close_pull_request(expectation.pull)
