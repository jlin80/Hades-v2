"""Process entrypoint.

Run with:
    python -m hades.main
or, in the container:
    uvicorn hades.main:app --host $API_HOST --port $API_PORT
"""

import uvicorn

from hades.api.app import create_app
from hades.config.settings import get_settings

app = create_app()


def run() -> None:
    """Start the HTTP server using the configured host and port."""
    settings = get_settings()
    uvicorn.run(
        "hades.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # logging is configured by hades.observability.logging
        access_log=True,
    )


if __name__ == "__main__":
    run()
