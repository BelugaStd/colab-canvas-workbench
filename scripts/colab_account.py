"""Project-local account entry point for google-colab-cli 0.8.0 on Windows."""

import getpass
import json
import os
import sys
import webbrowser
from importlib import resources
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS = Path(os.environ.get("WORKBENCH_DATA", ROOT / ".data")) / "colab-accounts"


def login(account, credentials_path):
    from colab_cli.auth import PUBLIC_SCOPES, REMOTE_REDIRECT_URI
    from google_auth_oauthlib.flow import InstalledAppFlow

    print(f"\nGoogle Colab login: account slot {account}")
    if credentials_path.exists():
        answer = input("This slot is already logged in. Replace its login? [y/N] ")
        if answer.strip().lower() != "y":
            return
    config = json.loads(resources.files("colab_cli").joinpath("oauth_config.json").read_text())
    flow = InstalledAppFlow.from_client_config(config, PUBLIC_SCOPES)
    flow.redirect_uri = REMOTE_REDIRECT_URI
    # NOTE: Use the registered Google SDK redirect and remote flow shipped by CLI 0.8.0.
    url, _ = flow.authorization_url(prompt="consent", token_usage="remote")
    print("Sign in and approve access in your browser. If it does not open, visit:\n")
    print(url)
    print("\nPaste the code from Google's page into THIS window, not into chat.")
    webbrowser.open(url)
    code = getpass.getpass("Authorization code (hidden): ").strip()
    if not code:
        raise ValueError("No authorization code entered")
    flow.fetch_token(code=code)
    credentials = flow.credentials
    if not credentials.refresh_token:
        raise ValueError("Google did not return a refresh token; authorize again")
    payload = {**json.loads(credentials.to_json()), "type": "authorized_user"}
    temporary = credentials_path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream)
    temporary.replace(credentials_path)
    print(f"\nLogin saved locally for account slot {account}. You may close this window.")


def main(account):
    if account not in (1, 2, 3):
        raise ValueError("Unknown account slot")
    directory = ACCOUNTS / str(account)
    credentials_path = ACCOUNTS / "credentials" / f"{account}.json"
    arguments = sys.argv[1:]
    if arguments == ["login"]:
        try:
            login(account, credentials_path)
        except (Exception, KeyboardInterrupt) as exc:
            print(f"\nLogin did not complete ({type(exc).__name__}). Run login again.", file=sys.stderr)
            raise SystemExit(1) from None
        return

    informational = not arguments or arguments[0] in {"help", "version"} or any(
        option in arguments for option in ("--help", "-h")
    )
    if not informational and not credentials_path.is_file():
        print(f"Account {account} is not logged in. Run its local login shortcut first.", file=sys.stderr)
        raise SystemExit(1)

    # WARN: Both normal CLI calls and the SSH bridge must use this slot's ADC file.
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(credentials_path.resolve())
    from colab_cli.cli import main as cli_main
    from colab_cli.common import state
    from colab_cli.history import HistoryLogger
    from colab_cli.state import SettingsStore

    state._settings_store = SettingsStore(str(directory / "settings.json"))
    state._history = HistoryLogger(str(directory / "history"))
    sys.argv = [sys.argv[0], "--auth", "adc", "--config", str(directory / "sessions.json"), *arguments]
    cli_main()
