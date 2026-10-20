"""仅临时 CI：原生备份/恢复前后的数据、附件和加密凭据核对。"""

import hashlib
import json
from pathlib import Path

import frappe
from frappe.utils import add_days, today
from frappe.utils.backups import get_or_generate_backup_encryption_key
from frappe.utils.file_manager import save_file

TYPES = ("Employee", "Designation", "Leave Application", "File")
WORK = Path(".eb-recovery")


def guard(site):
    sites = site if isinstance(site, tuple) else (site,)
    if frappe.local.site not in sites or not frappe.conf.allow_tests:
        frappe.throw("仅允许指定的临时 CI 测试站点执行恢复演练。")


def digest(content):
    return hashlib.sha256(content).hexdigest()


def seed():
    guard("contract.local")
    # 先生成密钥，使两轮备份的配置都包含它；首轮附件仍为明文。
    get_or_generate_backup_encryption_key()
    WORK.mkdir(mode=0o700)  # 已存在则失败，不复用上次演练的数据。
    data = json.loads(Path(__file__).with_name("p2_data.json").read_text())
    for row in data["records"]:
        if row["doctype"] != "Version" and not frappe.db.exists(row["doctype"], row["name"]):
            frappe.get_doc(row).insert()
    user = frappe.get_doc(
        {**data["user"], "email": "recovery@test.invalid", "roles": [dict(role="Employee")]}
    ).insert()
    user.api_key, user.api_secret = "synthetic-recovery-key", "synthetic-recovery-secret"
    user.save()
    position = frappe.get_doc(
        dict(doctype="Designation", designation_name="EB Recovery Position", eb_tool_leave_apply=0)
    ).insert()
    holidays = frappe.get_doc(
        dict(
            doctype="Holiday List",
            holiday_list_name="EB Recovery Holidays",
            from_date=add_days(today(), -365),
            to_date=add_days(today(), 365),
        )
    ).insert()
    employee = frappe.get_doc(
        {
            **data["employee"],
            "first_name": "Recovery Synthetic",
            "user_id": user.name,
            "create_user_permission": 0,
            "designation": position.name,
            "holiday_list": holidays.name,
            "leave_approver": "Administrator",
            "eb_hr_approver": "Administrator",
            "wecom_userid": "recovery-synthetic",
        }
    ).insert()
    leave = frappe.get_doc(
        {
            "doctype": "Leave Application",
            **data["leave"],
            "employee": employee.name,
            "from_date": add_days(today(), 1),
            "to_date": add_days(today(), 1),
            "posting_date": today(),
            "leave_approver": "Administrator",
        }
    ).insert()
    attachments = []
    for private in (0, 1):
        content = f"synthetic recovery attachment {private}".encode()
        file = save_file(
            f"recovery-{private}.txt", content, "Employee", employee.name, is_private=private
        )
        attachments.append(dict(name=file.name, sha256=digest(content), private=private))
    frappe.db.commit()
    state = dict(
        counts={dt: frappe.db.count(dt) for dt in TYPES},
        anchors={
            "Employee": employee.name,
            "Designation": position.name,
            "Leave Application": leave.name,
        },
        attachments=attachments,
    )
    path = WORK / "state.json"
    path.write_text(json.dumps(state))
    path.chmod(0o600)


def verify():
    guard(("recovery.local", "recovery-enc.local"))
    original = json.loads((WORK / "site_config.json").read_text())
    assert frappe.conf.db_name != original["db_name"]
    assert frappe.conf.db_password != original["db_password"]
    assert frappe.conf.pause_scheduler and frappe.conf.mute_emails
    assert frappe.conf.eb_wecom_cards == {"enabled": False}
    assert frappe.conf.backup_encryption_key == original["backup_encryption_key"]
    state = json.loads((WORK / "state.json").read_text())
    assert {dt: frappe.db.count(dt) for dt in TYPES} == state["counts"]
    for dt, name in state["anchors"].items():
        assert frappe.db.exists(dt, name)
    assert (
        frappe.db.get_value("Designation", state["anchors"]["Designation"], "eb_tool_leave_apply")
        == 0
    )
    for expected in state["attachments"]:
        file = frappe.get_doc("File", expected["name"])
        assert file.is_private == expected["private"]
        assert digest(Path(file.get_full_path()).read_bytes()) == expected["sha256"]
    assert (
        frappe.get_doc("User", "recovery@test.invalid").get_password("api_secret")
        == "synthetic-recovery-secret"
    )
    print("Frappe 恢复演练：记录数、员工/岗位/请假单、公开/私有附件 SHA256、关闭值及加密凭据通过。")
