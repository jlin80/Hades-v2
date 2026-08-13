"""Entry point: ``python -m hades``."""

from __future__ import annotations

import uvicorn

from hades.api.app import create_app
from hades.config import load_settings
from hades.logging import configure_logging


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # our JSON formatter owns the root logger
    )


if __name__ == "__main__":
    main()
