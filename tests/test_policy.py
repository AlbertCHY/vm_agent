import config
from core.policy import get_vm_policy, load_policy
from subgraphs.vm_bootstrap.steps import get_bootstrap_plan


def test_example_policy_is_loaded():
    policy = get_vm_policy()
    # conftest 只提供 *.example.toml，應退回讀取範本
    assert policy.default_sg == "ap"
    assert policy.default_storage_type == "HDD"
    assert policy.exclude_os_options == ["Other"]


def test_network_segment():
    policy = get_vm_policy()
    assert policy.network_segment_for("proj-demo") == "NET_A01"
    assert policy.network_segment_for("A-anything") == "NET_A02"   # 前綴不分大小寫
    assert policy.network_segment_for("other") == "NET_DEFAULT"
    assert policy.network_segment_for(None) == "NET_DEFAULT"


def test_naming():
    policy = get_vm_policy()
    assert policy.clean_function("Web Server!") == "WebServe"      # 只留英數字、最多 8 字元
    assert policy.clean_function(None) == "VM"
    assert policy.name_prefix("proj-demo", "AP") == "proj-demo-AP"
    assert policy.deployment_name("proj-demo", "AP", 3) == "proj-demo-AP03"


def test_bootstrap_steps_policy_is_valid():
    data = load_policy("bootstrap_steps_linux")
    ids = [s["id"] for s in data["steps"]]
    assert ids[0] == "add_pub_key" and "set_passwords" in ids
    # 機密樣板變數只能出現在 secret_env
    secret_vars = ("{admin_password}", "{user2_password}", "{mdr_token}", "{ngav_token}", "{mdr_cid}", "{ngav_cid}")
    for step in data["steps"] + [data["cleanup"]]:
        for v in (step.get("env") or {}).values():
            assert not any(s in v for s in secret_vars), step["id"]
    plan = get_bootstrap_plan("linux")
    for name in plan.script_files:
        assert (config.BOOTSTRAP_LINUX_SCRIPTS_DIR / name).is_file(), name


def test_windows_bootstrap_steps_policy_is_valid():
    data = load_policy("bootstrap_steps_windows")
    ids = [s["id"] for s in data["steps"]]
    assert ids[0] == "add_pub_key"
    assert "set_dns" in ids and "set_sccm" in ids and "mount_extra_disk" in ids and ids[-1] == "check_system"
    assert "cleanup" not in data
    secret_vars = ("{admin_password}", "{user2_password}", "{mdr_token}", "{ngav_token}", "{mdr_cid}", "{ngav_cid}")
    for step in data["steps"]:
        for v in (step.get("env") or {}).values():
            assert not any(s in v for s in secret_vars), step["id"]
    plan = get_bootstrap_plan("windows")
    assert plan.cleanup is None and plan.reboot_on_success is True
    for name in plan.script_files:
        assert (config.BOOTSTRAP_WINDOWS_SCRIPTS_DIR / name).is_file(), name
