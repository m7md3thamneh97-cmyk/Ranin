"""Compose the guarded app with teaching and voice-enrollment workflows."""
from .app import create_app as create_foundation
from .teaching import install as install_teaching
from .enrollment import install as install_enrollment
from .enrollment_learning import install as install_enrollment_learning

def create_app(*args, **kwargs):
    app = create_foundation(*args, **kwargs)
    install_teaching(app)
    install_enrollment(app)
    install_enrollment_learning(app)
    return app
