"""Test .env config loading and priority over os.environ."""
import importlib.util, os, sys
from pathlib import Path

plugin_dir = Path("/mnt/data/workplace/QwenPaw/plugins/qwenpaw-ops-observer")
env_file = plugin_dir / ".env"
backup = env_file.read_text() if env_file.exists() else None

# Test 1: .env with MySQL URL
env_file.write_text(
    "OPS_OBSERVER_DB_URL=mysql://root:secret@127.0.0.1:3306/ops_observer\n"
    "QWENPAW_WORKING_DIR=/tmp/test_ops\n"
)
for m in [k for k in sys.modules if k.startswith("plugin_qwenpaw")]:
    del sys.modules[m]
spec = importlib.util.spec_from_file_location("plugin_qwenpaw_ops_observer",
    str(plugin_dir / "__init__.py"), submodule_search_locations=[str(plugin_dir)])
pkg = importlib.util.module_from_spec(spec)
sys.modules["plugin_qwenpaw_ops_observer"] = pkg
spec.loader.exec_module(pkg)

cfg_spec = importlib.util.spec_from_file_location("plugin_qwenpaw_ops_observer.config",
    str(plugin_dir / "config.py"), submodule_search_locations=[str(plugin_dir)])
cfg = importlib.util.module_from_spec(cfg_spec)
sys.modules["plugin_qwenpaw_ops_observer.config"] = cfg
cfg_spec.loader.exec_module(cfg)

assert cfg.get_db_url() == "mysql://root:secret@127.0.0.1:3306/ops_observer", cfg.get_db_url()
assert str(cfg.get_working_dir()) == "/tmp/test_ops", cfg.get_working_dir()
sd = cfg.get_secret_dir(cfg.get_working_dir())
assert str(sd) == "/tmp/test_ops.secret", sd
print("Test 1 (.env MySQL URL): PASSED")
print("  db_url:", cfg.get_db_url())
print("  working_dir:", cfg.get_working_dir())
print("  secret_dir:", sd)

# Test 2: .env absent, os.environ fallback
if backup is not None:
    env_file.write_text(backup)
elif env_file.exists():
    env_file.unlink()
cfg._load_env.cache_clear()
os.environ["QWENPAW_WORKING_DIR"] = "/tmp/env_fallback"
os.environ.pop("OPS_OBSERVER_DB_URL", None)
assert cfg.get_db_url() == ""
assert str(cfg.get_working_dir()) == "/tmp/env_fallback"
print("Test 2 (os.environ fallback): PASSED")
print("  db_url:", repr(cfg.get_db_url()))
print("  working_dir:", cfg.get_working_dir())

# Test 3: .env overrides os.environ
env_file.write_text("QWENPAW_WORKING_DIR=/tmp/from_env_file\n")
cfg._load_env.cache_clear()
os.environ["QWENPAW_WORKING_DIR"] = "/tmp/from_os_env"
assert str(cfg.get_working_dir()) == "/tmp/from_env_file"
print("Test 3 (.env priority over os.environ): PASSED")
print("  working_dir (from .env, not os):", cfg.get_working_dir())

# Cleanup
if backup is not None:
    env_file.write_text(backup)
elif env_file.exists():
    env_file.unlink()
os.environ.pop("QWENPAW_WORKING_DIR", None)
print("\nALL CONFIG TESTS PASSED")
