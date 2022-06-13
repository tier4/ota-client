def _load_path():
    import sys
    from pathlib import Path

    project_dir = Path(__file__).absolute().parent.parent
    sys.path.insert(0, str(project_dir))


_load_path()

del _load_path
