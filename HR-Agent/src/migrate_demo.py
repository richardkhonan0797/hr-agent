"""Apply only the additive demo quota migration; never seed or reset data."""
import psycopg

from .config import get_settings
from .demo_quota import SCHEMA


def main() -> int:
    try:
        with psycopg.connect(get_settings().database_url, connect_timeout=15) as connection:
            connection.execute(SCHEMA)
    except Exception:
        print("Quota migration failed; check database access and permissions.")
        return 1
    print("Demo quota migration complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
