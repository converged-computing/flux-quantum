"""Flux CLI plugin discovery shim.

flux scans its CLI plugin search path for *.py files and instantiates any
CLIPlugin subclass found in each module. This file is the ONLY thing that
belongs on FLUX_CLI_PLUGINPATH -- it re-exports QuantumCLIPlugin from the
installed flux_quantum package (absolute import), so the plugin's real code
lives in the package and flux never tries to import package internals as
standalone plugin files.
"""

from flux_quantum.cli import QuantumCLIPlugin  # noqa: F401
