"""Lazy proxy for heavy optional libraries so process start (and health probes) are not delayed by their import."""
import importlib


class LazyModule:
    def __init__(self, name: str):
        self._name, self._mod = name, None

    def __getattr__(self, attr):
        if self._mod is None:
            self._mod = importlib.import_module(self._name)
        return getattr(self._mod, attr)


cv2 = LazyModule("cv2")
