# 端到端测试（P3-C1）

对一套**已经部署好的测试环境**跑：用合成的部门、岗位、员工、Wiki 空间和临时 Dify 知识库，按员工和 Dify 工作流的真实调用方式走一遍，测完全部删掉。不 mock 任何服务。

**不要指向生产环境**：测试要用管理员 Key 新建和删除数据。

## 测什么

| 文件 | 内容 |
| --- | --- |
| `test_policy.py` | 制度按审核流程发布 → Frappe Webhook → X5 写进 Dify（检查 `eb_scope`、`eb_source`）→ X3 检索：岗位 B 能看到两个空间，岗位 A 只能看到全员空间；撤回后检索不到；非员工什么都查不到 |
| `test_x4.py` | X4 工具只查令牌本人：岗位职责、假期余额、我的请假、待我审批；伪造令牌查不到；HR 在岗位上关掉“查岗位职责”“查假期”后，该岗位员工马上被拒绝（提示没有开通），其它功能和别的岗位不受影响，打开后恢复 |
| `test_hr_assistant.py` | HR 助手对话（模拟 dify-on-wechat，`user` 填企业微信 userid）：查岗位职责、查余额、非员工提示；发起请假要本人在同一会话里回复“确认请假”才提交，别人说、换会话说都不提交，重复确认不重复提交；审批人能查到待审批 |
| `test_wecom.py` | 企业微信统一回调（模拟企业微信服务器推加密消息）：A 请假后，上级 B 的卡片点“通过”进 HR 环节，HR C 的卡片点“通过”单据批完；另一张单 B 点“退回”。伪造签名、申请人自己点、点别人的卡片都返回 403 且单据不变，重复点不重复处理。普通消息转给 dify-on-wechat 进 HR 助手，同一员工两条消息在同一个会话里，回答的是本人岗位职责；同一 MsgId 重推不会再回一次 |
| `test_onboarding.py` | 入职待办：HR 保存新员工档案后，按岗位模板自动生成一张已提交的入职单，每项活动一个任务分给负责人（ToDo）；负责人能看自己的任务，看不到入职单、录用通知和新员工档案；重复保存不重复生成；任务都办完后入职单变成已完成 |
| 全部 | 合成员工的手机号、身份证号、银行卡号不出现在 X3、X4、Dify 文档和 HR 助手的任何返回里 |

还没覆盖：真实企业微信收发卡片和消息（要真机联测）。

## 怎么跑

在能访问测试环境的机器上，把下面的环境变量写进 `tests/e2e/.env`（已被 `.gitignore` 忽略，不要提交）：

| 变量 | 说明 |
| --- | --- |
| `EB_E2E_FRAPPE_URL`、`EB_E2E_FRAPPE_HOST` | Frappe 地址；按域名路由时用 `HOST` 指定站点名 |
| `EB_E2E_FRAPPE_ADMIN_KEY`、`EB_E2E_FRAPPE_ADMIN_SECRET` | 测试环境管理员的 API Key，测完可以在后台删掉 |
| `EB_E2E_COMPANY` | 可选，不填用第一家公司 |
| `EB_E2E_DIFY_API_URL`、`EB_E2E_DIFY_HOST` | Dify 服务 API，例如 `https://dify.example.com/v1` |
| `EB_E2E_DIFY_DATASET_KEY` | Dify 知识库 API Key（和 X5 用的同一个工作空间） |
| `EB_E2E_DIFY_INDEXING_TECHNIQUE` | 临时知识库的索引方式，默认 `economy`；和 X5 的 `EB_DIFY_INDEXING_TECHNIQUE` 一致 |
| `EB_E2E_HR_APP_KEY` | 可选，HR 助手应用的 API Key；不填跳过对话测试（对话要模型 Key） |
| `EB_E2E_X3_URL`、`EB_E2E_X3_TOKEN` | X3 地址和 `EB_KB_SERVICE_TOKEN` |
| `EB_E2E_X4_URL`、`EB_E2E_X4_TOKEN` | X4 地址和 `EB_MCP_SERVICE_TOKEN` |
| `EB_E2E_WECOM_CALLBACK_URL`、`EB_E2E_WECOM_CALLBACK_HOST` | 可选，企业微信回调地址 `…/api/method/enterprise_base.wecom.callback.receive`；不填跳过 `test_wecom.py` |
| `EB_E2E_WECOM_CORP_ID`、`EB_E2E_WECOM_AGENT_ID`、`EB_E2E_WECOM_TOKEN`、`EB_E2E_WECOM_AES_KEY` | 测试环境的企业微信应用参数，和站点 `eb_wecom_cards`、dify-on-wechat 配的一致。用合成的一套，不要用真实企业的 |
| `EB_E2E_WECOM_CHAT` | 默认 `true`；dify-on-wechat 没起时设成 `false`，只测审批卡片 |
| `EB_E2E_LEAVE_TYPE` | 发起请假用的假期类型，默认 `年假` |
| `EB_E2E_SYNC_TIMEOUT_SECONDS` | 等同步完成的最长秒数，默认 180 |

```bash
cd tests/e2e
uv sync
uv run pytest -v
```

前提：X3、X4、X5 已按各自 README 部署，Frappe 的制度 Webhook 已启用（`EB_SYNC_WEBHOOK_SECRET`），X3、X4、X5 用的接口账号已启用。测企业微信回调还要：站点 `eb_wecom_cards` 已启用（`make wecom-cards`），请假审批流程已配好，dify-on-wechat 已起并连着 HR 助手应用。测试会新建一个 HR 用户当 HR 审批人。测试环境连不上企业微信接口，卡片记录会是“发送失败”，这是正常的。

没配 `EB_E2E_FRAPPE_URL` 时整套跳过，CI 里只做 lint 和类型检查。

测试中途失败也会尽量删掉合成数据；没删掉的会在结果里警告，带本次的标记（`E2E` 开头），在 Frappe 里按这个标记搜出来删即可。

## 测试情况

2026-10-16 在 Claude 云端的本地整套环境（Frappe 15 + HRMS + Wiki、Dify 1.17.1、X3/X4/X5 本地进程）上 7 项全部通过，约 30 秒，测完无残留。HR 助手用的是把模型节点换成规则节点的测试版应用；接上真实模型后要再跑一次。

2026-10-19 加了企业微信回调后，在同一套环境（main bb82cca，加上 dify-on-wechat 和合成的企业微信应用参数）上 10 项全部通过，约 75 秒，测完无残留。同日加了入职待办，新测试单独通过，测完无残留。

2026-10-20 加了岗位助手开关用例后，在同一套环境（Frappe App main 6217646，X4 bb82cca）上跑：X3、X4（含新用例）、X5 制度同步、企业微信卡片审批、入职待办共 9 项通过，测完无残留。HR 助手对话 2 项没跑通，原因是环境里的测试版 HR 助手应用还是 10-16 建的，接的是旧的本地端口和旧令牌，要按当前 `config/dify/hr-assistant.yml` 重新建一个测试版应用再跑；这两项和本次改动无关。

2026-10-20 按当前 `config/dify/hr-assistant.yml` 重建了测试版 HR 助手应用（模型节点换成规则节点，接部署内网的 X3/X4），在同一套环境（main f409016）上全套 12 项：11 项通过，测完无残留；跳过 1 项，是企业微信聊天转 HR 助手，因为这次没起 dify-on-wechat（10-19 已跑通）。HR 助手对话 2 项通过，接上真实模型后还要再跑一次。
