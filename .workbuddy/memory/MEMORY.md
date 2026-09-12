# MEMORY.md — astrbot_plugin_llm_group_guard 项目长期记忆

## 项目概况
- AstrBot 插件「LLM 群守卫」：全体禁言（即时/定时）+ 群消息违规审核（LLM/关键词，撤回/禁言）+ 高召回模式 + 入群审批 + WebUI 管理面板。
- 结构：`main.py`（插件类 / WebAPI / 调度循环 / 命令与 LLM 工具）、`core/`（llm_reviewer、message_guard、whole_ban_scheduler、violation_tracker、high_recall 等纯逻辑）、`pages/guard/`（WebUI 三件套）、`tests/`（离线自测，stub astrbot 依赖）。
- 配置：无 `_conf_schema.json`；默认值在 `main.py` 的 DEFAULT_GROUP_CONFIG；全部由 WebUI 管理，落盘 data_dir/config.json（两级 global + groups）。

## 约定（新增「按群功能」的固定套路）
1. 新配置键必须加进 `DEFAULT_GROUP_CONFIG`（自动进入 `_GROUP_CONFIG_KEYS` 白名单），否则 WebUI 保存会被过滤。
2. 定时能力挂 `_schedule_loop` 的 `_check_*()`（CHECK_INTERVAL=20s）；运行态标志持久化到群配置，重启自动恢复。
3. 新 WebAPI：`_register_web_apis` 里注册 + handler；前端 `app.js` 用 `api("xxx", "POST", {...})`；页签在 index.html 加 tab + section，并挂到 tab 点击与 selectGroup 回调。
4. 测试运行：`python tests/test_<feature>_selftest.py`（每文件独立进程）；改完跑齐 4 个自测 + `python -m py_compile` + `node --check pages/guard/app.js`。
5. 版本三处同步：metadata.yaml / @register / README 徽章；README 补功能说明。
6. 通用开发工作流（含离线自测 stub 技巧）见用户级 skill `astrbot-plugin-dev`。

## 主要功能与状态
- 高召回模式（V1.1.0）：每日定时切换到另一套审核要求与独立模型 + 开关提示 + 手动临时切换；处置方式沿用常规 guard_action（默认禁言）。
- 现有自测文件：入群审批、关键词预审、违规通知、高召回（共 4 个，全绿；约 31 项断言）。

## 已知事项
- 仓库出现过来自外部的自动提交/文件覆盖：编辑后建议 grep 复查关键文件是否真的落盘。
