"""Shared runtime state location; resolved when used."""
import os
from pathlib import Path


def state_dir(root):
    return Path(os.environ.get('INDEX_HUB_STATE_DIR', Path(root) / '.cloud-state'))
