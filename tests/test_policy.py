import pytest

from core.policy import VMPolicy, get_vm_policy, load_policy


@pytest.fixture
def policy() -> VMPolicy:
    return get_vm_policy()


def test_example_policy_is_loaded(policy):
    # conftest 只提供 *.example.toml，應退回讀取範本
    assert policy.defaults["cluster"] == "cluster-01"
    assert policy.datastore_allowed == ["datastore-hdd", "datastore-ssd"]


def test_network_segment(policy):
    assert policy.network_segment_for("proj-demo") == "NET_A01"
    assert policy.network_segment_for("A-anything") == "NET_A02"   # 前綴不分大小寫
    assert policy.network_segment_for("other") == "NET_DEFAULT"
    assert policy.network_segment_for(None) == "NET_DEFAULT"


def test_apply_defaults(policy):
    spec = policy.apply_defaults({"project_name": "proj-demo", "sg": "db"})
    assert spec["sg"] == "db"                     # 已有值不覆蓋
    assert spec["cluster"] == "cluster-01"
    assert spec["network_segment"] == "NET_A01"
    assert "ap_unit" not in spec                  # 未填 ap_owner 時不補 ap_unit
    assert policy.apply_defaults({"ap_owner": "王小明"})["ap_unit"] == "Default_AP_Unit"


def test_match_os_image(policy):
    available = {"win2022-cht": {}, "ubuntu2404": {}, "custom-image": {"description": "Special Linux"}}
    assert policy.match_os_image("ubuntu2404", available) == "ubuntu2404"
    assert policy.match_os_image("Ubuntu 22.04", available) == "ubuntu2204"
    assert policy.match_os_image("Ubuntu", available) == "ubuntu2404"
    assert policy.match_os_image("Windows Server 2019 English", available) == "win2019-eng"
    assert policy.match_os_image("Windows Server", available) == "win2022-cht"
    assert policy.match_os_image("special linux", available) == "custom-image"
    assert policy.match_os_image("", available) is None


def test_blueprint_rules(policy):
    assert policy.blueprint_query_for("win2022-cht", None) == "Windows"
    assert policy.blueprint_query_for("ubuntu2404", None) == "Ubuntu"
    assert policy.blueprint_query_for("rocky9", "Rocky") == "Rocky"
    assert policy.blueprint_query_for(None, None) == "Windows"
    bps = [{"id": "1", "name": "Ubuntu v1.0"}, {"id": "2", "name": "Ubuntu v2.0"}]
    assert policy.pick_blueprint(bps)["id"] == "2"
    assert policy.pick_blueprint([]) is None


def test_project_description_fields(policy):
    assert policy.fields_from_project_description("REQ1/王小明/HR Dashboard") == {
        "case_no": "REQ1", "ap_owner": "王小明", "description": "HR Dashboard",
    }
    assert policy.fields_from_project_description("") == {}


def test_datastore_by_storage_type(policy):
    assert policy.datastore_for_storage_type("ssd") == "datastore-ssd"
    assert policy.datastore_for_storage_type(None) is None


def test_bootstrap_steps_policy_is_valid():
    data = load_policy("bootstrap_steps")
    ids = [s["id"] for s in data["steps"]]
    assert ids[0] == "add_pub_key" and "set_passwords" in ids
    # 機密樣板變數只能出現在 secret_env
    secret_vars = ("{admin_password}", "{user2_password}", "{mdr_token}", "{ngav_token}", "{mdr_cid}", "{ngav_cid}")
    for step in data["steps"] + [data["cleanup"]]:
        for v in (step.get("env") or {}).values():
            assert not any(s in v for s in secret_vars), step["id"]
