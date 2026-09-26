"""Paths shared by local and container deployments."""
import os
from pathlib import Path


def data_path(filename: str) -> Path:
    return Path(os.getenv("DATA_DIR", "data")) / filename
