import atexit
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.app.repo import InMemoryRepo  # noqa: E402
from backend.app.seed import load_seed  # noqa: E402

_turing_client = None


def _turing_repo():
    """TuringRepo reseeded per test. Embedded engine by default (no server needed);
    set TURINGDB_TEST_HOST=http://localhost:6666 to test against a running server instead."""
    global _turing_client
    from turingdb import TuringDB
    from backend.app.turing_repo import TuringRepo
    if _turing_client is None:
        host = os.environ.get("TURINGDB_TEST_HOST")
        if host:
            _turing_client = TuringDB(host=host)
        else:
            data_dir = tempfile.mkdtemp(prefix="edth-tdb-")
            atexit.register(shutil.rmtree, data_dir, ignore_errors=True)  # graph dumps add up fast
            _turing_client = TuringDB(type="embedded", data_dir=data_dir)
    repo = TuringRepo(_turing_client, graph_prefix="test_")
    repo.load_seed(load_seed())
    return repo


@pytest.fixture(params=["memory", "turing"])
def repo(request):
    if request.param == "memory":
        return InMemoryRepo()
    if os.environ.get("EDTH_SKIP_TURING"):
        pytest.skip("EDTH_SKIP_TURING set")
    return _turing_repo()
