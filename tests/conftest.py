import os
import tempfile

# isolate the library/state from the user's real one before tunetrack is imported
_HOME = tempfile.mkdtemp(prefix="tunetrack_test_home_")
os.environ["TUNETRACK_HOME"] = _HOME
os.environ["TUNETRACK_PROJECTS"] = os.path.join(_HOME, "projects")
