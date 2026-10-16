# SPDX-License-Identifier: Apache-2.0
"""Load Mail authentication at runtime, outside the image and public source."""

import os
import logging
from pathlib import Path


def main():
    logging.disable(logging.CRITICAL)
    os.environ["HTTP_BEARER_TOKEN"] = Path("/run/secrets/mail.token").read_text().strip()
    from mcp_agent_mail.http import create_app
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8765, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
