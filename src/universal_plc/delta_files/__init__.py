"""Portable, offline Delta file adapters; see docs/adapters/delta-files.md."""
from .adapter import DeltaFileAdapter
from .limits import FileTransformError

__all__ = ['DeltaFileAdapter', 'FileTransformError']
