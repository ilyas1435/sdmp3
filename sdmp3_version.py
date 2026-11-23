"""Validation for the app's public major.minor release number."""

import re


_VERSION_PATTERN = re.compile(r'^[0-9]+\.[0-9]+$')


def validate_version(value):
    """Return a trimmed major.minor version or fail explicitly."""
    if not isinstance(value, str):
        raise ValueError('VERSION must use major.minor, for example 3.0')
    version = value.strip()
    if not _VERSION_PATTERN.fullmatch(version):
        raise ValueError('VERSION must use major.minor, for example 3.0')
    return version
