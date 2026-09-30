"""start-server.sh / stop-server.sh lock handling, with a stand-in llama-server (Linux, no GPU)."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("flock"), reason="needs Linux bash and flock")
def test_lock_line_shape_claim_and_release():
    r = subprocess.run(["bash", str(HERE / "serve_lock.sh")], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL" not in r.stdout
