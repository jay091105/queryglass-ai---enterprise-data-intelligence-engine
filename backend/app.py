"""Convenience launcher for the QueryGlass FastAPI backend."""

from pathlib import Path
import os
import sys

import uvicorn


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if __name__ == "__main__":
    sys.path.insert(0, str(PROJECT_ROOT))
    os.chdir(PROJECT_ROOT)
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8002)
