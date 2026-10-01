"""V12 presentation services over the existing project execution owner.

The supplied package validators, release reader, installer transaction engine
and memory-only session ledger are preserved here. The mounted service uses
existing-project mode: it never claims an unmanaged DLL is a managed install.
"""

from .packages import SetupError
__all__ = ["ProjectSetupService", "SetupError"]


def __getattr__(name):
    # Importing package validation/version-slot helpers does not create a
    # dependency on the complete GUI/controller service.
    if name == "ProjectSetupService":
        from .service import ProjectSetupService
        return ProjectSetupService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
