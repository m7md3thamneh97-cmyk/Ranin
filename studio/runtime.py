"""Compose the existing guarded app with the guided teaching workflow."""
from .app import create_app as create_foundation
from .teaching import install

def create_app(*args, **kwargs):
    app = create_foundation(*args, **kwargs)
    install(app)
    return app
