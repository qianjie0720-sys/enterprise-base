#!/usr/bin/env bash
set -euo pipefail
cd /home/frappe/frappe-bench
./env/bin/python -c 'import sys; assert sys.version_info >= (3, 12)'
cp -r /workspace/frappe_apps/enterprise_base apps/enterprise_base
./env/bin/pip install -e apps/enterprise_base
ls -1 apps > sites/apps.txt
bench set-config -g db_host eb-db
bench set-config -g redis_cache redis://eb-redis:6379
bench set-config -g redis_queue redis://eb-redis:6379
bench set-config -g redis_socketio redis://eb-redis:6379
for site in contract.local import.local; do
  bench new-site "$site" --db-root-password "$EB_TEST_PASSWORD" \
    --admin-password "$EB_TEST_PASSWORD" --mariadb-user-host-login-scope '%' --install-app erpnext
  for app in hrms wiki; do bench --site "$site" install-app "$app"; done
  bench --site "$site" set-config allow_tests true
  if test "$site" = contract.local; then
  ./env/bin/python - "$site" <<'PY'
import sys
import os
import frappe
from enterprise_base.tests.tool_switch_probe import seed_before_install
os.chdir("sites")
frappe.init(site=sys.argv[1])
frappe.connect()
try:
    seed_before_install()
finally:
    frappe.destroy()
PY
  fi
  bench --site "$site" install-app enterprise_base
  if test "$site" = contract.local; then
    bench --site "$site" execute enterprise_base.tests.tool_switch_probe.verify_after_install
  fi
  bench --site "$site" execute frappe.db.set_single_value --args '["System Settings",{"language":"en","time_zone":"Asia/Shanghai","enable_telemetry":0}]'
  if test "$site" = contract.local; then
    bench --site "$site" execute enterprise_base.tests.tool_switch_probe.seed_before_migrate
    for pass in 1 2; do
      bench --site "$site" migrate
      bench --site "$site" execute enterprise_base.tests.tool_switch_probe.verify_after_install
    done
    echo 'contract.local：两次原生迁移后空值补齐、关闭和开启值保留通过。'
  fi
  # 官方 runner 仅在 CI 环境变量存在时才用退出码报告测试失败。
  CI=true bench --site "$site" run-tests --app enterprise_base --skip-test-records
  if test "$site" = contract.local; then
    bench --site "$site" execute enterprise_base.integration.export_configuration
    bench --site "$site" execute enterprise_base.tests.probe.verify_user_migration
    echo '已确认 fixture 导入不重置技术账号凭据。'
  fi
  bench --site "$site" execute enterprise_base.tests.probe.verify_http_contract
  echo "$site：真实 HTTP 认证、允许字段与越权拒绝通过。"
done
umask 077
recovery_root=/home/frappe/frappe-bench/sites/.eb-recovery
recovery_started=$SECONDS
bench --site contract.local execute enterprise_base.tests.recovery_probe.seed
for site in recovery.local recovery-enc.local; do
  if test "$site" = recovery-enc.local; then
    bench --site contract.local execute frappe.db.set_single_value --args '["System Settings","encrypt_backup",1]'
    bench --site contract.local execute frappe.installer.update_site_config --args '["backup_encryption_key","None"]'
  fi
  bench --site contract.local backup --with-files --ignore-backup-conf --backup-path "$recovery_root" \
    --backup-path-db "$recovery_root/database.sql.gz" --backup-path-files "$recovery_root/files.tar" \
    --backup-path-private-files "$recovery_root/private-files.tar" --backup-path-conf "$recovery_root/site_config.json"
  # 与收集程序一致，取回首次加密备份完成后才生成的密钥。
  cp sites/contract.local/site_config.json "$recovery_root/site_config.json"
  # 原生 CI 直接调用实际恢复程序，不另外复制解密密钥以免掩盖程序缺陷。
  EB_RESTORE_DB_ROOT_PASSWORD="$EB_TEST_PASSWORD" EB_RESTORE_ADMIN_PASSWORD="$EB_TEST_PASSWORD" \
    ./env/bin/python /workspace/deploy/restore_frappe.py "$site" "$recovery_root"
  bench --site "$site" set-config allow_tests true
  bench --site "$site" execute enterprise_base.tests.recovery_probe.verify
  bench --site "$site" execute enterprise_base.tests.probe.verify_http_contract
done
rm -rf "$recovery_root"
echo "Frappe 恢复演练耗时：$((SECONDS - recovery_started)) 秒（含合成数据准备、备份、建空站、恢复、迁移和核对）。"
echo '真实 Frappe 测试、配置导出及第二空站点导入完成。'
