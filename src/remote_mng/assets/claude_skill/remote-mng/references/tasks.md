# 个人任务与状态控制台

以下入口中的 `<SKILL_BASE_DIR>` 替换为加载 Skill 时的实际目录。任务 ID 表示一次用户工作，step-id 表示其中一次行动；提前分配稳定 ID，响应丢失后用它们查询。不要把反复调用原步骤当成查询，也不能换 ID 盲目重发。

## 检查和创建

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json doctor
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json target inspect lab --directory /tmp
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json task create deploy-01 --title "部署并验证测试包" --target lab --artifact-file ./build/package.tar.gz
```

本地 doctor 不启动 daemon。目标检查建立连接并进行只读探测，不部署或安装；`not_checked` 不是通过。检查旧 daemon 时不要未经核对活动会话就重启。

**计划使用 `job` 时，首次提交前确认检查结果中的 `helper` 和 `job_dependencies` 均为 `pass`。** 辅助程序缺失且安装属于当前任务需要和授权范围时，先安装、再检查；不要直接尝试 `job start` 来探测安装状态：

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json helper install lab
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json target inspect lab --directory /tmp
```

已检查通过则跳过安装；普通 exec 或前台操作不因缺少 helper 而必须安装。更多能力条件见 [持久作业](jobs.md)。

## 跟踪每一步

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json file upload lab ./build/package.tar.gz /tmp/package.tar.gz --wait --task deploy-01 --step-id upload --label "上传产物"
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json job start lab --script ./deploy.sh --job-id deploy-01 --task deploy-01 --step-id deploy --label "部署"
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json task get deploy-01 --refresh
```

等待前一步实际完成后才启动依赖它的步骤。`job start` 返回 running 表示仍在运行，不能直接执行验证。`task get --refresh` 查询关联远端作业，默认 get/list 只观察本地记录。网络失败时保留的 `last_confirmed_state` 不是当前状态。

步骤出现错误时先检查其 `error.code`。仅 `helper_not_installed` 明确表示本次作业未执行：完成已授权安装并检查通过后，工具允许用原 task/step/job ID 和相同参数继续该步骤。`job_id_conflict` 不授予对旧作业的管理权，不能把旧作业关联为本次成功或取消旧作业；先核对冲突，再为确属新的工作创建新的步骤和作业 ID。

其他 `unknown`、超时或响应丢失应使用 `task get --refresh`、原 `job status` 和日志查询，**不要循环原 `task invoke` 或 `job start`**。重复步骤返回旧记录不代表重新执行。`job_not_found` 也不能证明从未执行；检查目标与状态目录后仍无足够证据，就说明任务暂时阻塞及需要核实的内容，保留原 ID，不靠重发命令推进。

部署已确认成功后，再执行项目约定的验证：

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json exec lab --script ./verify.sh --wait --task deploy-01 --step-id verify --label "验证版本与功能"
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json task seal deploy-01
```

seal 表示本次计划的所有步骤已经提交，不能把失败或未知状态改成成功。任务 `succeeded/outcome=steps_completed` 表示记录步骤完成；`business_verification=not_inferred` 表示工具没有替你判断业务。根据验证脚本退出码、版本和测试输出总结实际结果。

## 前台操作

按交互参考配置 profile 并打开前台，把实际返回值替换到命令：

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session step SESSION_ID "TI RUN SMOKE" --token CONTROL_TOKEN --request-id smoke-01 --expect "RESULT PASS" --timeout 20 --task deploy-01 --step-id smoke
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json session step-get SESSION_ID smoke-01
```

request-id 标识会话中的输入，step-id 关联个人任务。step 先记录再发送，默认区分大小写的文本匹配。超时后重复同一 request-id、输入和模式只继续观察，改变 timeout 不会重发；step-get 只读。后续发生其他输入或终端断开时，旧步骤可能保持未知。不要把缺少匹配解释成业务失败。

重复 task 中的 session.open 只返回会话 ID，不保存控制令牌。根据 `control_required/advice` 重新取得写入权；`claim --force` 会撤销当前控制者，需核对是在恢复自己这次工作。已确认退出后直接关闭或释放，不再次发送退出输入。

## 换对话与个人配方

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json task list
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json task get deploy-01 --refresh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json project inspect --file ./rmg-project.json
```

project inspect 验证本地输入文件、计算摘要并输出步骤 method/params，**不执行清单中的命令**。先核对是否对应用户本次任务；将已核对步骤 params 保存为临时 JSON 后，可显式调用：

```sh
bash "<SKILL_BASE_DIR>/scripts/rmg.sh" --json task invoke deploy-01 --step-id upload --method transfer.start --params-file ./upload-params.json --label "上传产物"
```

也可使用普通 CLI 加 --task/--step-id。清单只引用一个已配置的本地目标，凭据不写入清单，构建仍用业务项目已有工具。

## 查看与取消

用户要查看时调用 `ui` 打开本地网页，`ui --no-open` 仅返回地址。URL 含本地访问密钥，不分享。页面显示任务、设备检查、执行日志、观察时间和日志缺口。

明确需要取消时，用网页确认或 `task cancel TASK_ID`。只取消本任务自己创建的受管作业；取消请求不等于已经退出，之后刷新确认。普通 exec、前台应用和其他业务进程不会因此被终止。
