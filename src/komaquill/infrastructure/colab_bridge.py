"""Adapt pinned Colab APIs without invoking any allocating command."""

import argparse
import hashlib
import importlib
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.errors import KomaquillError
from komaquill.domain.models import RuntimeObservation
from komaquill.messages import message

USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def account(client: Any, timeout: float) -> str:
    """Hash the stable Google subject without retaining identity responses."""
    response = client.session.get(USERINFO_URL, timeout=timeout)
    response.raise_for_status()
    subject = response.json()["sub"]
    if not isinstance(subject, str) or not subject:
        raise KomaquillError(code="authentication_failed")
    return hashlib.sha256(subject.encode()).hexdigest()


def provider() -> Any:
    """Reuse official authentication without opening an interactive login here."""
    if not (Path.home() / ".config/colab-cli/token.json").is_file():
        raise KomaquillError(code="authentication_failed")
    credentials = importlib.import_module("google.oauth2.credentials").Credentials
    transport = importlib.import_module("google.auth.transport.requests")
    token = Path.home() / ".config/colab-cli/token.json"
    session = transport.AuthorizedSession(credentials.from_authorized_user_file(str(token)))
    api = importlib.import_module("colab_cli.client")
    return api.Client(api.Prod(), session)


def observe(client: Any, receipt: dict[str, Any], timeout: float) -> tuple[RuntimeObservation, Any]:
    """Confirm endpoint presence only under the account recorded at allocation."""
    subject = account(client, timeout)
    endpoint = receipt.get("endpoint_id")
    if not endpoint or subject != receipt.get("account_sha256"):
        return RuntimeObservation(account_sha256=subject, endpoint_id=endpoint), None
    assignments = client.list_assignments()
    selected = next((item for item in assignments if item.endpoint == endpoint), None)
    return RuntimeObservation(
        state="running" if selected is not None else "stopped",
        endpoint_id=endpoint,
        account_sha256=subject,
    ), selected


def session_state(receipt: dict[str, Any], assignment: Any) -> Any:
    """Build a temporary official transport record from fresh provider evidence."""
    model = importlib.import_module("colab_cli.state").SessionState
    info = assignment.runtime_proxy_info
    return model(
        name=receipt["name"],
        endpoint=assignment.endpoint,
        token=info.token,
        url=info.url,
        token_expires_at=info.expires_at(),
    )


def execute(action: str, receipt: dict[str, Any], *arguments: str) -> RuntimeObservation:
    """Observe, terminate or connect to an existing endpoint only."""
    client = provider()
    settings = RuntimeSettings.model_validate(receipt["settings"])
    timeout = settings.request_timeout
    if action == "identity":
        return RuntimeObservation(account_sha256=account(client, timeout))
    observation, assignment = observe(client, receipt, timeout)
    if action == "observe":
        return observation
    if observation.state == "unknown":
        raise KomaquillError(code="identity_mismatch")
    if action == "terminate":
        if assignment is not None:
            client.unassign(assignment.endpoint)
            deadline = time.monotonic() + settings.control_timeout
            while True:
                observation, assignment = observe(client, receipt, timeout)
                if observation.stopped:
                    break
                if observation.state == "unknown" or time.monotonic() >= deadline:
                    raise KomaquillError(code="colab_failed")
                time.sleep(settings.poll_interval)
        return observation
    if assignment is None:
        raise KomaquillError(code="runtime_ended")
    state = session_state(receipt, assignment)
    if action == "proxy":
        # Pinned private bridge: unlike the SSH command, this path cannot allocate.
        bridge = importlib.import_module("colab_cli.commands.ssh")._run_proxy_bridge
        if bridge(state, identity=arguments[0], rm=False):
            raise KomaquillError(code="connection_failed")
    elif action == "download":
        contents = importlib.import_module("colab_cli.contents").ContentsClient(state)
        contents.download(arguments[0], arguments[1])
    else:
        raise KomaquillError(code="invalid_request")
    return observation


def main() -> None:
    """Keep tokens and provider responses inside this bounded child process."""
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("action", choices=["identity", "observe", "terminate", "proxy", "download"])
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("arguments", nargs="*")
    args = parser.parse_intermixed_args()
    try:
        receipt = json.loads(args.receipt.read_bytes())
        result = execute(args.action, receipt, *args.arguments)
        if args.action != "proxy":
            print(result.model_dump_json())
    except (Exception, KeyboardInterrupt):
        print(message("error.colab_failed"), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
