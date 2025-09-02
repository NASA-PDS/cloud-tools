# -*- coding: utf-8 -*-
"""My PDS Module."""
import importlib.resources

__version__ = VERSION = importlib.resources.files(__name__).joinpath("VERSION.txt").read_text().strip()
