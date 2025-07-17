# -*- coding: utf-8 -*-
"""PDS Namespace."""

try:
    import setuptools

    version = setuptools.__version__.split(".")
    if int(version[0]) <= 67 and int(version[1]) < 3:
        try:
            __import__("pkg_resources").declare_namespace(__name__)
        except Exception:
            import pkgutil

            __path__ = pkgutil.extend_path(__path__, __name__)
except ImportError:
    pass
