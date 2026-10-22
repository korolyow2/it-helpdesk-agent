"""Initialize a persistent mock once and start its single HTTP worker."""

import os
from pathlib import Path

import uvicorn

from helpdesk.mock.init_db import initialize_database, upgrade_database
from helpdesk.paths import DATA_DIR, DEFAULT_DATABASE


def main():
    """Preserve existing tickets and approvals on every container restart."""
    database = Path(os.environ.get("MOCK_DATABASE", str(DEFAULT_DATABASE)))
    if database.exists():
        upgrade_database(database)
    else:
        initialize_database(database, DATA_DIR / "seed.json")
    uvicorn.run("helpdesk.mock.api:from_env", factory=True, host="0.0.0.0", port=8001)


if __name__ == "__main__":
    main()
