"""Development ASGI entry: configure the product before importing app settings."""
from .bootstrap import configure_environment

configure_environment()
from single_port_app import app  # noqa: F401
