"""測試共用設定

在匯入任何專案模組前，把 policies / data / logs 導向暫存目錄：
- policies 只放 *.example.toml 與 bootstrap_steps.toml，測試結果不受本機實際 policy 影響
- data / logs 不會寫入專案目錄
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="aria_test_"))
_POLICIES = _TMP / "policies"
_POLICIES.mkdir()
for f in (ROOT / "policies").glob("*.example.toml"):
    shutil.copy(f, _POLICIES / f.name)
shutil.copy(ROOT / "policies" / "bootstrap_steps.toml", _POLICIES / "bootstrap_steps.toml")

os.environ["POLICIES_DIR"] = str(_POLICIES)
os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["LOG_DIR"] = str(_TMP / "logs")
os.environ["BOOTSTRAP_LOG_DIR"] = str(_TMP / "logs")
os.environ["SHOW_AGENT_THOUGHTS"] = "false"
os.environ.setdefault("GOOGLE_API_KEY", "test-key")

FIXTURES = Path(__file__).parent / "fixtures"
