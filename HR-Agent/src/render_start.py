"""Render-only launcher: expand PORT without relying on shell interpolation."""
import os
import sys


def command(service: str, port: str) -> list[str]:
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("PORT must be a valid port number")
    if service == "api":
        return [sys.executable, "-m", "uvicorn", "src.api:create_app", "--factory",
                "--host", "0.0.0.0", "--port", port, "--workers", "1"]
    if service == "ui":
        return [sys.executable, "-m", "streamlit", "run", "app.py",
                "--server.address", "0.0.0.0", "--server.port", port,
                "--server.headless", "true", "--browser.gatherUsageStats", "false"]
    raise ValueError("Expected api or ui")


if __name__ == "__main__":
    args = command(sys.argv[1], os.environ.get("PORT", "10000"))
    os.execv(args[0], args)
