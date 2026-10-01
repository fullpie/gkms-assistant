"""Neutral import and CLI surface for the shared Plan3 exam executor.

The implementation remains in ``plan3_audition_executor`` so existing callers
keep their original module path.  New code should import this module.
"""

from .plan3_audition_executor import *  # noqa: F403
from .plan3_audition_executor import __all__ as __all__
from .plan3_audition_executor import main


if __name__ == "__main__":
    raise SystemExit(main())
