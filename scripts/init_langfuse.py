"""Generate untracked local Langfuse credentials without printing their values."""

from pathlib import Path
import os
import secrets


def main():
    """Create a private environment file once; never replace existing credentials."""
    destination = Path(__file__).resolve().parents[1] / ".env.langfuse"
    values = {
        "HELPDESK_TRACING_ENABLED": "true",
        "FASTMCP_TELEMETRY_MODE": "propagation_only",
        "LANGFUSE_BASE_URL": "http://127.0.0.1:3000",
        "LANGFUSE_PUBLIC_KEY": f"pk-lf-{secrets.token_hex(16)}",
        "LANGFUSE_SECRET_KEY": f"sk-lf-{secrets.token_hex(32)}",
        "LANGFUSE_INIT_USER_EMAIL": "developer@helpdesk.test",
        "LANGFUSE_INIT_USER_PASSWORD": secrets.token_hex(24),
        **{key: secrets.token_hex(32) for key in (
            "POSTGRES_PASSWORD", "CLICKHOUSE_PASSWORD", "REDIS_AUTH",
            "MINIO_ROOT_PASSWORD", "LANGFUSE_SALT", "LANGFUSE_ENCRYPTION_KEY", "NEXTAUTH_SECRET",
        )},
    }
    try:
        with open(destination, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as handle:
            handle.write("".join(f"{key}={value}\n" for key, value in values.items()))
    except FileExistsError:
        print("Existing .env.langfuse preserved.")
        return
    print("Local credentials saved to .env.langfuse with owner-only permissions.")


if __name__ == "__main__":
    main()
