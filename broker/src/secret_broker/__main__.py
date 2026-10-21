"""Entry point: python -m secret_broker"""

from __future__ import annotations

import sys

import uvicorn

from . import logs
from .app import create_app
from .auth import Authenticator
from .config import ConfigError, Settings
from .push import Pusher
from .service import Broker
from .vault import Vault


def main() -> None:
    logs.setup()
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"secret-broker: {e}", file=sys.stderr)
        sys.exit(2)
    vault = Vault(settings.db_path)
    broker = Broker(vault, Pusher(settings, vault), settings.public_origin)
    app = create_app(broker, Authenticator(settings), settings.vapid_public_key)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_config=None,
        # No access log: paths alone are harmless, but nothing here needs them.
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
