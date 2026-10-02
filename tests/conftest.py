import os
import tempfile

# app.main builds a module-level app at import; keep its data dir out of the repo.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="dashboard-test-"))
