from schemas import (
    CRITICAL_FIELDS,
    REQUIRED_FIELDS,
    VMDeploymentParams,
    field_label,
    validate_vm_spec,
)


def _valid_spec(**overrides):
    spec = {
        "deployment_name": "proj-demo-AP01",
        "blueprint_id": "bp-1",
        "project_id": "p-1",
        "cpu_count": 4,
        "memory_size": 8192,
        "os_image": "ubuntu2404",
        "sg": "ap",
        "network_segment": "NET_A01",
        "ip_mode": "DHCP",
        "cluster": "cluster-01",
        "datastore": "datastore-ssd",
        "case_no": "REQ1",
        "ap_owner": "王小明",
        "os_owner": "王小明",
        "ap_unit": "資訊服務部",
        "description": "測試",
    }
    spec.update(overrides)
    return spec


def test_field_lists_derived_from_model():
    assert len(REQUIRED_FIELDS) == 16
    assert "blueprint_name" not in REQUIRED_FIELDS
    assert set(CRITICAL_FIELDS) == {"project_id", "blueprint_id", "cpu_count", "memory_size", "case_no", "ap_owner"}


def test_field_label_uses_policy_choices():
    assert "datastore-hdd / datastore-ssd" in field_label("datastore")
    assert "ap / db / web" in field_label("sg")
    assert field_label("cpu_count", with_hint=False) == "CPU 核心數 (vCore)"


def test_valid_spec_passes_and_builds_payload():
    assert validate_vm_spec(_valid_spec()) == []
    payload = VMDeploymentParams(**_valid_spec()).to_aria_request_payload()
    assert payload["inputs"]["Datastore"] == "datastore-ssd"
    assert payload["inputs"]["cpuCount"] == 4


def test_policy_validation_errors():
    errors = validate_vm_spec(_valid_spec(datastore="unknown-ds", sg="xx", memory_size=999999, cpu_count=0))
    joined = "\n".join(errors)
    assert "datastore" in joined and "sg" in joined and "memory_size" in joined and "cpu_count" in joined


def test_missing_field_error():
    spec = _valid_spec()
    spec.pop("case_no")
    assert any("case_no" in e for e in validate_vm_spec(spec))
