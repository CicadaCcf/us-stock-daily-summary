"""Configuration for the IB Gateway daily-snapshot bridge.

Reads from `../.env.local` at import time (same file the Vite frontend
uses) so you only maintain one secrets file per project. Everything here
is plain module-level constants — import and use directly:

    from config import IB_HOST, IB_PORT, IB_CLIENT_ID

Only IB connection details and the outbound HTTP proxy are exposed; the
bridge itself does not talk to any HTTP service (IB uses its own TCP
socket), but keeping HTTPS_PROXY here means any future ad-hoc fetchers
(e.g. Yahoo fallbacks) can reuse it without re-parsing the env file.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load `<repo-root>/.env.local`. `__file__` is .../server/config.py, so
# one `.parent` gives us `server/` and another gives the repo root. Using
# `override=False` (the default) means env vars already set in the real
# process environment (e.g. by the cron wrapper) win — which is what you
# want for production schedulers that inject secrets.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _REPO_ROOT / '.env.local'
load_dotenv(_ENV_PATH)

# --- IB Gateway / TWS socket -------------------------------------------
# Defaults match TWS paper trading. IB Gateway paper = 4002, Gateway
# live = 4001, TWS live = 7496, TWS paper = 7497. Override via .env.local.
IB_HOST = os.environ.get('IB_HOST', '127.0.0.1')
IB_PORT = int(os.environ.get('IB_PORT', '7497'))
# Arbitrary integer; must be unique across concurrent IB clients on the
# same Gateway. Pick any int 1-999 that isn't used by another script.
IB_CLIENT_ID = int(os.environ.get('IB_CLIENT_ID', '11'))

# --- Outbound HTTP proxy (unused by IB itself, kept for future fallbacks)
HTTPS_PROXY = os.environ.get('HTTPS_PROXY')
HTTP_PROXY = os.environ.get('HTTP_PROXY')
