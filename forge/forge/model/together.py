"""
forge/model/together.py — DEPRECATED
=====================================
This file has been renamed to codegen.py.
This shim re-exports for backwards compatibility.
"""
from forge.model.codegen import CodegenBackend as TogetherBackend  # noqa: F401

__all__ = ["TogetherBackend"]
