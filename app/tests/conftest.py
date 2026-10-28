import sys
from pathlib import Path

# make the app package importable when tests run from anywhere
APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
