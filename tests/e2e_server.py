"""Only Playwright uses this application. It cannot provision Colab machines."""
import os
import tempfile
from pathlib import Path

from backend.app import create_app
from tests.fake_colab import PROFILES, FakeColab

directory = Path(os.environ['WORKBENCH_E2E_DATA']) if os.environ.get('WORKBENCH_E2E_DATA') else Path(tempfile.mkdtemp(prefix='workbench-e2e-'))
app = create_app(directory, driver=FakeColab(delay=1), account_profiles=PROFILES)
