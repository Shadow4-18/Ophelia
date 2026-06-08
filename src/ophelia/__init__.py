try:
    import importlib.metadata

    __version__ = importlib.metadata.version("ophelia")
except Exception:
    __version__ = "0.2.0"
