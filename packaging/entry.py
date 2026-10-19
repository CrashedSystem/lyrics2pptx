"""PyInstaller entry script (ADR-033) — ``lyrics2pptx.exe`` runs this.

Deliberately *outside* the package: it is a script, not a module of
``lyrics2pptx``, so nothing imports it (P6: no module may import an entrypoint).
It does one thing — hand over to ``webapp.launch``, which reuses a running
server or starts one and opens the tab.
"""

from lyrics2pptx.webapp import launch

if __name__ == "__main__":
    raise SystemExit(launch())
