"""Compose the guarded app with teaching and voice-enrollment workflows."""
from .app import create_app as create_foundation
from .teaching import install as install_teaching
from .enrollment import install as install_enrollment

def create_app(*args, **kwargs):
    app = create_foundation(*args, **kwargs)
    install_teaching(app)
    install_enrollment(app)
    from .becoming import install as install_becoming
    install_becoming(app, public_origin=kwargs.get('public_origin'))
    return app
