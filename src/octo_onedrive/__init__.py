from . import _version

__version__ = _version.get_versions()["version"]

from .onedrive import OneDriveComm

__all__ = ["OneDriveComm"]
