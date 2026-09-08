# region MODULE_CONTRACT [DOMAIN(7): PackageMetadata; CONCEPT(7): VersionIdentity; TECH(7): PythonPackage]
## @file __init__.py
## @brief Package identity for the local browser-bridge job agent.
## @modulecontract
## @purpose Expose the installed version without initializing network or secret stores.
## @scope Package metadata only.
## @invariants Importing job_finder has no side effects.
## @changes LAST_CHANGE: [v0.2.0 — Browser session bridge release.]
## @modulemap
## DATA 8[Installed package version] => __version__
def _module_contract() -> None:
    pass
# endregion MODULE_CONTRACT
# GREP_SUMMARY: package, version, job finder, browser bridge
# STRUCTURE: import package -> metadata only -> version 0.2.0

__version__ = "0.2.0"
