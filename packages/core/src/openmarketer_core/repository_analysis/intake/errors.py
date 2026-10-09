"""Errors raised by the intake stage."""

from __future__ import annotations


class IntakeError(Exception):
    """The repository could not be cloned or scanned. Intake fails closed."""


class ExcludedFileError(PermissionError):
    """A file that must never be read was requested."""
