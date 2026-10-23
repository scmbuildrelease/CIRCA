"""Paths to CIRCA's packaged, version-controlled data files."""

from importlib.resources import files
from pathlib import Path


def data_path(filename: str) -> Path:
    """Return the installed package path for one bundled CIRCA data file."""

    resource = files("circa").joinpath("data", filename)
    if not resource.is_file():
        raise FileNotFoundError(f"CIRCA packaged data file is missing: {filename}")
    return Path(str(resource))
