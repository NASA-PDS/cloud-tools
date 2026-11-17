"""Exception types for the prefix migration tools."""


class MigrationPlanError(Exception):
    """Raised when a migration plan cannot be built or is internally inconsistent."""


class MigrationStoppedError(MigrationPlanError):
    """Raised to stop a run cleanly (network or credential trouble) after its progress has been saved."""


class MigrationStateError(MigrationPlanError):
    """Raised when saved migration state does not match the plan or the destination it describes."""


class PlanCollisionError(MigrationPlanError):
    """Raised when two different source objects would be written to the same destination key."""
