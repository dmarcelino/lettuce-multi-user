"""The `vault-run` helper agents run (agent-skills/using-vault-secrets), against a
stub of the vault's /redeem; test_app covers the real endpoint. tests/run-broker.sh
mounts agent-skills/ at /agent-skills, which is this path inside the test image."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from .conftest import CARD

HELPER = Path(__file__).resolve().parents[2] / "agent-skills" / "using-vault-secrets" / "vault-run"
PASSWORD = 'p@ss w/rd&"q\\TEST_938742'
NOTES = "Gate code TEST_938742\nleave it at the back door"
ENV = {"VAULT_NUMBER": CARD, "VAULT_PASSWORD": PASSWORD, "VAULT_SECURITY_CODE": "123", "VAULT_NOTES": NOTES}


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.server.seen.append((self.path, body))
        if self.path == "/redeem" and body == {"handle": "good"}:
            data, status = json.dumps({"secret": "Visa", "env": ENV}).encode(), 200
        else:
            data, status = b'{"error": "unknown or expired handle"}', 404
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def vault_url():
    assert HELPER.is_file(), f"{HELPER} missing; run the tests with tests/run-broker.sh"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", server.seen
    server.shutdown()


def run(url, *args, extra_env=None, stdin=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("VAULT_")}
    env.update(VAULT_URL=url, **(extra_env or {}))
    return subprocess.run([sys.executable, str(HELPER), *args], env=env, capture_output=True, timeout=30, input=stdin)


def test_command_gets_the_values_and_its_exit_code(vault_url):
    url, seen = vault_url
    check = "import os, sys; sys.exit(3 if os.environ['VAULT_NUMBER'] == '%s' else 9)" % CARD
    r = run(url, "good", "--", sys.executable, "-c", check)
    assert r.returncode == 3, r.stderr
    assert seen == [("/redeem", {"handle": "good"})]


def test_output_is_scrubbed(vault_url):
    script = r"""
import json, os, re, sys, urllib.parse
n, p = os.environ["VAULT_NUMBER"], os.environ["VAULT_PASSWORD"]
lower = re.sub("%[0-9A-F]{2}", lambda m: m.group().lower(), urllib.parse.quote(p, safe=""))
print("number", n)
print("spaced", " ".join(n[i:i + 4] for i in range(0, 16, 4)))
print("dashed", "-".join(n[i:i + 4] for i in range(0, 16, 4)), file=sys.stderr)
print("password", p, "url", urllib.parse.quote(p, safe=""), "form", urllib.parse.quote_plus(p))
print("json", json.dumps(p), "lower", lower)
print("notes", os.environ["VAULT_NOTES"])
print("code", os.environ["VAULT_SECURITY_CODE"], end="")  # last line, no newline
"""
    r = run(vault_url[0], "good", "--", sys.executable, "-c", script)
    out, err = r.stdout.decode(), r.stderr.decode()
    assert r.returncode == 0, err
    for text in (out, err):
        assert CARD not in text and "4111 1111" not in text and "4111-1111" not in text
        assert "TEST_938742" not in text and "p%40ss" not in text and "back door" not in text
    assert out.splitlines()[:6] == [
        "number [VAULT_NUMBER]",
        "spaced [VAULT_NUMBER]",
        "password [VAULT_PASSWORD] url [VAULT_PASSWORD] form [VAULT_PASSWORD]",
        'json "[VAULT_PASSWORD]" lower [VAULT_PASSWORD]',
        "notes [VAULT_NOTES]",  # a multi-line value is scrubbed line by line
        "[VAULT_NOTES]",
    ]
    assert err.strip() == "dashed [VAULT_NUMBER]"
    # Too short to scrub without mangling output: a security code stays.
    assert out.endswith("code 123")


def test_stdin_reaches_the_command(vault_url):
    r = run(vault_url[0], "good", "--", "sh", "-c", 'read x; [ "$x" = hello ]', stdin=b"hello\n")
    assert r.returncode == 0, r.stderr


def test_unknown_handle(vault_url):
    r = run(vault_url[0], "stale", "--", "true")
    assert r.returncode == 2 and b"unknown or expired" in r.stderr


def test_vault_unreachable():
    r = run("http://127.0.0.1:9", "good", "--", "true")
    assert r.returncode == 2 and b"cannot reach the vault" in r.stderr


def test_proxy_settings_are_ignored(vault_url):
    """The handle goes to the vault only, never to a proxy from the agent's environment."""
    proxy = {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "no_proxy": ""}
    r = run(vault_url[0], "good", "--", "true", extra_env=proxy)
    assert r.returncode == 0, r.stderr


def test_refuses_to_shadow_a_value_already_set(vault_url):
    r = run(vault_url[0], "good", "--", "true", extra_env={"VAULT_NUMBER": "outer"})
    assert r.returncode == 2 and b"VAULT_NUMBER" in r.stderr


def test_command_not_found_keeps_the_handle(vault_url):
    url, seen = vault_url
    for command in ("no-such-command-938742", "./no-such-script.sh"):
        r = run(url, "good", "--", command)
        assert r.returncode == 127 and command.encode() in r.stderr
    assert seen == []  # checked before redeeming, so the approval is not wasted


def test_background_process_does_not_hang(vault_url):
    started = time.monotonic()
    r = run(vault_url[0], "good", "--", "sh", "-c", "sleep 30 & echo started")
    assert r.returncode == 0 and r.stdout == b"started\n"
    assert time.monotonic() - started < 3.5  # one grace period, not one per stream


COUNT_SIGNALS = """
import signal, sys, time
got = []
signal.signal(signal.SIGINT, lambda n, f: got.append(n))
print("ready", flush=True)
time.sleep(1.5)
print("got", len(got), flush=True)
"""


def spawn(url, *command):
    env = {k: v for k, v in os.environ.items() if not k.startswith("VAULT_")}
    env["VAULT_URL"] = url
    child = subprocess.Popen(
        [sys.executable, str(HELPER), "good", "--", *command],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,  # its own process group, like a shell job
    )
    assert child.stdout.readline() == b"ready\n"
    time.sleep(0.3)
    return child


def test_ctrl_c_reaches_the_command_once(vault_url):
    child = spawn(vault_url[0], sys.executable, "-c", COUNT_SIGNALS)
    os.killpg(child.pid, signal.SIGINT)  # what a terminal's Ctrl-C does
    out, err = child.communicate(timeout=10)
    assert out == b"got 1\n", err


def test_sigterm_is_passed_on(vault_url):
    child = spawn(vault_url[0], sys.executable, "-c", COUNT_SIGNALS)
    child.send_signal(signal.SIGTERM)  # to vault-run alone
    child.communicate(timeout=10)
    assert child.returncode == 128 + signal.SIGTERM


@pytest.mark.parametrize("args", [(), ("good",), ("good", "--"), ("good", "true"), ("--", "true")])
def test_usage(vault_url, args):
    url, seen = vault_url
    r = run(url, *args)
    assert r.returncode == 2 and b"usage: vault-run" in r.stderr
    assert seen == []  # a bad command line never uses up the handle
