"""Run the program the studio's model wrote, /in/main.py, as ``__main__``.

Started by entry.py with ``python -I``: no ``PYTHON*`` variable and no user site counts. The
packages installed for this run, when there are any, are read-only in /deps and come after the
image's own, so that they cannot replace a library the image holds.
"""

import os
import runpy
import sys

if os.path.isdir("/deps"):
    sys.path.append("/deps")
sys.argv = ["/in/main.py"]
runpy.run_path("/in/main.py", run_name="__main__")
