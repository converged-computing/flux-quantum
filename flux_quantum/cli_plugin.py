"""Discovery shim for flux's CLI plugin loader.

flux scans its CLI plugin search path for *.py files and instantiates any
CLIPlugin subclass it finds (CLIPluginRegistry._add_plugins_from_module). Drop
or symlink THIS file into that path; it re-exports QuantumCLIPlugin from the
installed flux_quantum package so the real logic lives in the package.
"""
from flux_quantum.cli import QuantumCLIPlugin  # noqa: F401
