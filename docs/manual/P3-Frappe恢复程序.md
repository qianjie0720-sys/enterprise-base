# Frappe 原生恢复程序（P3-G2）

`deploy/restore_frappe.py` 是完整恢复入口的容器内执行部分。目前 `make backup`、加密解包和新项目校验已实现；整套 `make restore` 的 Compose 创建、PostgreSQL、卷及 Qdrant 写入仍在接入，本程序不代表整套恢复已验收。

调用方必须先停下目标环境的 HTTP、队列和调度服务，只保留目标 MariaDB/Redis，并使用锁定版本的 Frappe 镜像。不要在正在运行的正式环境执行。工作目录是容器内 `/home/frappe/frappe-bench`；参数为新站点名和已校验备份目录。目录由 `restore_guard.extract_sites` 产生，含 `database.sql.gz`、`files.tar`、`private-files.tar`、`site_config.json`；复制进容器后归属 frappe，目录 700、文件 600，不要为方便挂载而开放其他用户读取。路径只能含字母、数字、斜线、点、下划线和连字符，不能经过符号链接。

数据库 root 口令和新管理员口令由调用方从目标密钥文件传入 `EB_RESTORE_DB_ROOT_PASSWORD`、`EB_RESTORE_ADMIN_PASSWORD`，不可贴到聊天、命令历史或 PR。程序只在容器内传递原生 bench 所需参数，屏蔽其标准输出和错误输出；失败只报告组件、步骤及异常类型。

程序加 bench 内独占锁，拒绝已有站点，创建随机数据库名和新数据库密码。随后仅复制 `encryption_key`、`backup_encryption_key`，运行原生 `bench restore` 和 `bench migrate`，核对目标数据库身份未变。`--force` 只用于本次刚创建的空库；旧站点数据库名、密码、地址、回调配置不导入。备份是否需要原生解密由 bench 检测，不对明文附件误传解密参数。

恢复站点默认暂停调度、邮件及企业微信审批卡片。失败时立即停止后续步骤，保留隔离目标供排查；调用方仍须停下目标服务、清除解密暂存区，不能自动接管流量或启动对外通知。失败目标不能直接重试，需由操作者确认后删除隔离资源，重新选用全新的项目。

原生备份收集结束后重新复制最新站点配置，避免 Frappe 首次加密备份生成密钥晚于配置快照，导致包里缺少解密密钥。

现有 `frappe-contract` 原生演练已改为调用这个程序，分别恢复未加密和原生加密备份，不再由测试代码补拷解密密钥。它核对员工/岗位/请假单、记录数、附件 SHA256、加密凭据、数据库隔离及关闭通知，再验证真实 HTTP 权限契约。CI 只用合成数据；本机的替身测试只证明失败停止、隔离和输出脱敏。最新原生 CI 结果在本 PR 更新，不将替身测试写成真实恢复结果。全套 Dify/Qdrant 恢复与耗时仍由 Claude 在云端补测，服务器截图待补。
