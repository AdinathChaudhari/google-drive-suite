"""py2app entry-point stub — see setup_app.py / build-app.sh.

Kept as a tiny separate file (rather than pointing py2app straight at the
package) so the frozen .app has one obvious top-level script to launch.
"""
from gdrive_toolkit.hub.menubar import main

main()
