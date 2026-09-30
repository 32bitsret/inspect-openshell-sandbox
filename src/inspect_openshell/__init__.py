"""OpenShell sandbox environment for Inspect AI.

Use ``sandbox="openshell"`` in a Task, with an ``openshell.yaml`` next to the
task file to choose the image, working directory and policy.
"""

from ._sandbox import OpenShellSandboxEnvironment

__all__ = ["OpenShellSandboxEnvironment"]
__version__ = "0.1.0"
