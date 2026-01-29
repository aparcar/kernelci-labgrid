"""KernelCI Labgrid Pull Agent - connect Labgrid labs to KernelCI."""

__version__ = "0.1.0"

from kernelci_labgrid.scheduler.pull_agent import LabgridPullAgent

__all__ = [
    "__version__",
    "LabgridPullAgent",
]
