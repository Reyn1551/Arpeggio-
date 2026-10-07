"""Arpeggio: a personal AI workbench that orchestrates coding agents and models."""

import logging

__version__ = "0.0.1"

# Silent until core.logs.configure_logging() attaches the JSON-lines handler.
logging.getLogger(__name__).addHandler(logging.NullHandler())
