"""Text runtime console; passwords and capabilities remain in process memory."""

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit
from urllib.request import Request
from uuid import UUID, uuid4


class ConsoleClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000") -> None:
        url = urlsplit(base_url)
        if url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
            raise ValueError("Supply an API origin without credentials, path, query or fragment")
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Remote API origins require HTTPS")
        self.base_url = base_url.rstrip("/")
        self.staff_token = ""
        self.token = ""
        self.session_id = ""
        self.revision = 0
        self.language = "en"
        self.http_commands = 0
        self.provider_requests = 0

    def request(self, path: str, body: dict | None = None, token: str = "") -> dict:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(
            self.base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method="POST" if body is not None else "GET",
        )
        # Redirects must never forward a runtime or staff bearer to another origin.
        from urllib.request import HTTPRedirectHandler, build_opener

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                return None

        with build_opener(NoRedirect).open(request, timeout=65) as response:
            return json.load(response)

    def login(self, email: str, password: str) -> None:
        self.staff_token = self.request("/auth/login", {"email": email, "password": password})[
            "access_token"
        ]

    def _update(self, payload: dict) -> None:
        self.revision = payload.get("revision", self.revision)
        self.language = payload.get("state", {}).get("lang", self.language)
        self.token = payload.get("token", self.token)

    def create(self, agent_id: str) -> None:
        payload = self.request("/agent-sessions", {"agent_id": agent_id}, self.staff_token)
        self.session_id = payload["session_id"]
        self._update(payload)
        self.staff_token = ""

    def renew(self) -> None:
        self._update(self.request(f"/runtime/sessions/{self.session_id}/renew", {}, self.token))

    def inspect(self) -> dict:
        payload = self.request(f"/runtime/sessions/{self.session_id}", token=self.token)
        self._update(payload)
        return payload

    def turn(self, text: str, *, action: dict | None = None, live: bool = False) -> dict:
        body = {"command_id": uuid4().hex, "expected_revision": self.revision, "text": text}
        if live:
            body["live"] = True
            suffix = "turn"
        else:
            if action is None:
                raise ValueError("Scripted mode requires an explicit action")
            body["action"] = action
            suffix = "commands"
        payload = self.request(f"/runtime/sessions/{self.session_id}/{suffix}", body, self.token)
        self.http_commands += 1
        self.provider_requests += payload.get("provider_requests", 0)
        self._update(payload)
        return payload


def run_script(
    client: ConsoleClient, turns: list[dict], *, live: bool = False, output: TextIO = sys.stdout
) -> None:
    for turn in turns:
        payload = client.turn(turn.get("text", ""), action=turn.get("action"), live=live)
        result = payload.get("result", {})
        say = result.get("say", {})
        if isinstance(say, dict):
            print(say.get(client.language, ""), file=output)
        else:
            raise ValueError("Runtime response lacks bilingual code-generated say")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--email", required=True)
    parser.add_argument("--agent-id", type=UUID, required=True)
    parser.add_argument("--script", type=Path)
    parser.add_argument(
        "--live", action="store_true", help="Explicitly permit hosted planner requests"
    )
    args = parser.parse_args()
    try:
        client = ConsoleClient(args.url)
        password = getpass.getpass("Staff password: ")
        client.login(args.email, password)
        del password
        client.create(str(args.agent_id))
        if args.script:
            turns = json.loads(args.script.read_text(encoding="utf-8"))
            if not isinstance(turns, list) or len(turns) > 500:
                raise ValueError("Script must be a bounded list of synthetic turns")
            run_script(client, turns, live=args.live)
        else:
            if not args.live:
                raise ValueError(
                    "Interactive text requires --live; scripted actions require --script"
                )
            while True:
                text = input("> ")
                if text == "/quit":
                    break
                if text == "/renew":
                    client.renew()
                    continue
                run_script(client, [{"text": text}], live=True)
        print(
            f"HTTP commands: {client.http_commands}; reported provider requests: {client.provider_requests}"
        )
    except (EOFError, KeyboardInterrupt):
        return
    except Exception:
        raise SystemExit(
            "Console stopped; check API availability, authorization and command revision."
        )


if __name__ == "__main__":
    main()
