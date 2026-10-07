"""Render native start command. Never prints secret values or logs in on boot."""

import os

from plms_mcp.cli import main


def start():
    # Render provides this service's HTTPS origin. A custom domain may override it.
    origin = os.environ.get("PLMS_ISSUER_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
    if origin:
        os.environ["PLMS_ISSUER_URL"] = origin
    if os.environ.get("PLMS_ENV_LOGIN") != "1" or not os.environ.get("DATABASE_URL"):
        print("Cloud start requires explicit environment-login opt-in and persistent PostgreSQL.")
        return 2
    port = os.environ.get("PORT", "10000")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        print("Cloud port configuration is invalid.")
        return 2
    return main(["serve", "--transport", "http", "--host", "0.0.0.0", "--port", port])


if __name__ == "__main__":
    raise SystemExit(start())
