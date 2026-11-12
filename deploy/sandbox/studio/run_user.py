"""Run the program the studio's model wrote, /in/main.py, as ``__main__``.

Started by entry.py with ``python -I``: no ``PYTHON*`` variable and no user site counts. The
image's typography helper (``studio_type``, in the read-only /opt/studio/lib) comes after the
standard library and the image's packages, so it can replace none of them. The packages
installed for this run, when there are any, are read-only in /deps and come last, so that they
cannot replace a library the image holds, the helper included.
"""

import os
import runpy
import sys

sys.path.append("/opt/studio/lib")
if os.path.isdir("/deps"):
    sys.path.append("/deps")
sys.argv = ["/in/main.py"]
runpy.run_path("/in/main.py", run_name="__main__")
