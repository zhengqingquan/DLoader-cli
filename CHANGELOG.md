# DLoader-cli 更新记录

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [1.0.1] - 2026-11-18

### 变更

- `flash` / `loop`：烧写时实时转发 CmdDloader 输出（分区进度等），不再等结束后才看到日志

## [1.0.0] - 2026-11-18

### 新增

- 通用烧写 CLI：`flash` / `loop` / `trigger` / `ports` / `detect` / `config`
- 自动发现 `CmdDloader.exe`、PAC、下载口 / AT 口（`pyserial`，无硬编码本机路径）
- `loop`：AT 软触发进下载，USB 不插拔循环烧写；烧完始终复位开机，不接受 `--power-off`（断电请用 `flash --power-off`），仍接受 `--reset`
- `--skip-file-id`：跳过指定分区（探测 CmdDloader 能力；v22 静默无效时直接报错；仅 v27+ 生效）
- `check`：检查项目是否支持自动烧写闭环（PAC、工具、`auto_power_on`、USB 插拔枚举、开机 USB 枚举 `usbservice_main.c`、AT 触发、FDL 复位等）；仅有 SKIP 时提示仍需人工/实机验证
- `check` 开机枚举：要求 Start LOG 附近有 `g_USBService_table[USB_SERVICE_LOG]`；拦截无条件 `UDC_HWInit` + NULL/UCOM
- `pacinfo`：解析 PAC 分区与 NV 备份（含 IMEI 是否保留）
- `Open port fail` / `PS2264` / Access denied 英文 Hint：优先实际占口进程，再补充 Logel / ResearchDownload / 其它 dloader 等；`--help` epilog 同步说明
- 占口诊断模块 `dloader/port_diag.py`（`flash` 再导出常用符号）：路径分量 / COM 号边界匹配，扫描默认约 3s 尽力预算（仅 Windows）
- 配置层：`dloader.config.json` + 环境变量覆盖；本机路径配置已 `.gitignore`
- 打包：PyInstaller（`dloader.spec`）自 `dloader/__main__.py` 生成单文件 `dloader.exe`
- 面向用户的运行时输出统一为英文；软触发打不开 AT 口时立即 `RuntimeError`
- CmdDloader v22 / v27 普通烧写；成功判据兼容 `DownLoad Passed` 与 `Download Result = Passed;`
- 单元测试（`pytest` + mock）；README 含固件能力、`check` / `loop` / 占口排查说明
