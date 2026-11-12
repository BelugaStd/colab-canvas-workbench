"""Isolated real-driver acceptance service; never opens the primary database."""
import os

from backend.app import create_app
from backend.config import ROOT, profiles

os.environ.setdefault("WORKBENCH_SSH_IDENTITY", str(ROOT / ".data/colab-accounts/ssh/id_ed25519"))
app = create_app(ROOT.parent / "creative-validation", account_profiles=profiles())
