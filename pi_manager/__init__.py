from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("pi-manager")
except PackageNotFoundError:
    __version__ = "dev"
