"""Compose the guarded app with teaching, knowledge and voice-enrollment workflows."""
from .app import create_app as create_foundation
from .teaching import install as install_teaching
from .enrollment import install as install_enrollment
from .knowledge import install as install_knowledge

def create_app(*args, **kwargs):
    app = create_foundation(*args, **kwargs)
    install_teaching(app)
    install_enrollment(app)
    install_knowledge(app)
    from .becoming import install as install_becoming
    install_becoming(app, public_origin=kwargs.get('public_origin'))
    return app
