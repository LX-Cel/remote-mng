"""PyInstaller entry point; the same CLI handles foreground and daemon modes."""
import multiprocessing

from remote_mng.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
