# DLoader-cli

Unisoc（展锐）UIS8910 等平台的**通用烧写命令行工具**（Python 版）。

工具、PAC、串口全部自动发现，支持 USB 不插拔反复烧写。底层调用 Unisoc ResearchDownload 自带的 `CmdDloader.exe`，本身不携带任何厂商二进制。

**特性**：零硬编码（常量集中在 `dloader/config.py`）、可单元测试（`pytest` + mock）、设备枚举用 `pyserial`（不依赖 pywin32）。占口进程诊断仅在 Windows 生效。

---

## 安装

```powershell
# 依赖只有 pyserial
pip install pyserial

# 可选：装成命令（之后任意目录可直接 dloader）
pip install -e .
```

不装也行，用 `python -m dloader` 一样能跑；或打包生成的 `dloader.exe`。

### 首次使用：告诉它工具和 PAC 在哪

`CmdDloader.exe` 和 PAC 都不在仓库里，第一次用需要指定位置。三种方式等价：

```powershell
# 推荐：存一次，之后任意目录都能用
dloader config --tool-dir '<...>\ResearchDownload\Bin' --pac '<...>.pac'

# 或每次显式传
dloader flash --pac '<...>.pac' --tool-dir '<...>\Bin'

# 或环境变量
$env:DLOADER_TOOL = '<...>\Bin'
```

不配也能跑，但有前提：

- **PAC 自动发现只看当前目录**——必须在 SDK 项目根目录运行，否则找不到
- **工具自动发现会向上扫 6 层**——在深目录下可能要几分钟

所以配一次 `config` 最省事。详见「自动发现规则」一节。

---

## 快速开始

```powershell
# 1. 检查环境（工具、PAC、端口能不能找到）
dloader detect

# 2. 看当前设备
dloader ports

# 3. 烧写（PAC 和工具自动发现，烧完自动复位开机）
dloader flash

# 4. 全自动循环烧 5 轮（USB 一直插着，不按键不断电）
dloader loop --rounds 5
```

两种调用方式等价：

```powershell
dloader ports              # 装了 pip install -e .
python -m dloader ports    # 直接用模块
```

---

## 命令

| 命令 | 作用 |
|---|---|
| `flash` | 烧写一次，默认复位到正常模式 |
| `loop` | 全自动循环烧写（AT 软触发进下载模式，不插拔 USB） |
| `trigger` | 只把设备切入下载模式，不烧写 |
| `ports` | 列出当前设备与串口 |
| `detect` | 探测工具 / PAC / 端口，检查环境 |
| `check` | 检查项目源码是否具备自动烧写闭环所需能力 |
| `pacinfo` | 解析 PAC 分区与 NV 备份信息 |
| `config` | 查看或保存配置 |
| `help` | 显示帮助 |

---

## 参数

### 通用

| 参数 | 说明 |
|---|---|
| `--pac <文件>` | PAC 路径，默认自动发现 |
| `--tool-dir <目录>` | `CmdDloader.exe` 所在目录，默认自动发现。指向不同版本即可切换烧写工具 |
| `--port <N>` | 下载口 COM 号，默认自动发现 |
| `--at-port <N>` | AT 口 COM 号，默认自动发现 |
| `--timeout-ms <ms>` | 设备进下载模式的等待超时，默认取配置 |
| `--scan-root <目录>` | 查找 CmdDloader 的根目录，可重复 |

### 烧写

| 参数 | 说明 |
|---|---|
| `--reset` | 烧完复位到正常模式（`flash` 默认；`loop` 始终复位，参数可写） |
| `--power-off` | 烧完断电（与 `--reset` 互斥；**仅 `flash`**，`loop` 不接受） |
| `--write-sn1 <IMEI>` | 写入 IMEI1 / SN1 |
| `--write-sn2 <IMEI>` | 写入 IMEI2 / SN2 |
| `--skip-file-id <ID[,ID]>` | 跳过指定分区（逗号分隔），**需要 CmdDloader v27+** |
| `--ez-mode` | 精简输出，便于脚本解析 |

`--skip-file-id` 会先探测工具版本：v22.x 虽然接受该参数但会静默忽略，
本工具会直接报错退出，避免"以为只烧了某几个分区，其实整包都烧了"。

```powershell
# 只烧 PS 和 MMIRes：把其余可选分区的 FileID 全部跳过
dloader flash --skip-file-id "BOOTLOADER,CP,TFBOOT,OperatorData,NV"

# 只烧 NV：跳过所有代码分区
dloader flash --skip-file-id "BOOTLOADER,PS,MMIRes,CP,TFBOOT,OperatorData"
```

分区的可选性来自 PAC 内的 XML：`FDL`/`FDL2` 是强制项（`CheckFlag=1`），
`PS`/`MMIRes`/`CP`/`NV` 可选（`CheckFlag=0`），其余为不检查项（`CheckFlag=2`）。
`IMS_TOGGLE`、`CONFIG_NV`、`FLASH` 是纯擦除操作，每次烧写都会自动执行。

### 循环

| 参数 | 说明 |
|---|---|
| `--rounds <N>` | 循环轮数，默认取配置 |
| `--interval <N>` | 每轮间隔秒数，默认取配置，`0` 表示不等待 |

---

## 输出与提示

所有输出带级别前缀，便于脚本判定和人工排查：

| 前缀 | 含义 | 是否阻断 |
|---|---|---|
| `[ERROR]` | 致命错误，命令终止 | 是 |
| `[FAIL]` | 某个步骤失败（`loop` 会中止） | 是 |
| `[WARN]` | 有问题但继续（配置降级、skip ID 拼错等） | 否 |
| `[INFO]` | 过程信息（串口重试、软触发） | 否 |
| `[WAIT]` | 正在等待端口出现 | 否 |
| `[OK]` | 某一步成功 | 否 |
| `[INTERRUPTED]` | 用户 Ctrl+C | 是 |

`[WARN]` 覆盖的关键场景：

| 场景 | 提示内容 |
|---|---|
| 配置文件不是合法 JSON | `config is not valid JSON, using defaults: <路径> (<原因>)` |
| 配置里有未知键（拼错） | `unknown config key 'X' ignored (typo?)` |
| 环境变量值非法 | `environment variable DLOADER_BAUD='abc' is invalid, ignored` |
| 保存配置时旧文件读不出 | `existing config is unreadable and will be replaced` |
| 配置的 `Pac` 不存在 | `configured Pac does not exist, falling back to auto-discovery` |
| `--skip-file-id` 写了不存在的分区 | `unknown FileID 'X' (not in this PAC, check spelling)` |
| `--skip-file-id` 有重复 | `duplicate FileID 'X' in --skip-file-id` |
| AT 口打不开（触发阶段） | `cannot open COMxx to send the trigger command` |
| 串口未就绪在重试 | `COMxx not ready yet (attempt 1/6), retrying...`（`[INFO]`） |

这些**都是警告，不阻断执行**。只有两种直接报错：`--skip-file-id` 为空，
以及工具版本不支持 `-SkipFileID`。

`flash` / `loop` 遇到 `Open port fail` / `PS2264` / Access denied 时，会额外打印英文 **Hint**（Windows 下尽量列出占口 PID；并补充 Logel 等嫌疑进程）。句柄扫描为尽力而为（默认约 3s 预算，单次系统调用仍可能更久）。`dloader --help` 的 epilog 也有说明。

---

## 配置项

所有项在 `dloader/config.py` 的 `DEFAULTS` 里有默认值，可被 `dloader.config.json` 或环境变量覆盖。

### 路径

| 键 | 默认 | 说明 |
|---|---|---|
| `ToolDir` | `""` | CmdDloader.exe 所在目录，空=自动发现 |
| `Pac` | `""` | PAC 完整路径，空=自动发现 |
| `AtPort` | `0` | 固定 AT 口 COM 号，0=自动发现 |

### 设备识别（换平台只改这三项）

| 键 | 默认 |
|---|---|
| `DownloadVidPid` | `VID_0525&PID_A4A7` |
| `NormalVidPid` | `VID_1782&PID_3D03` |
| `AtNamePattern` | `SPRD AT` |

### 串口与触发

| 键 | 默认 |
|---|---|
| `BaudRate` | `115200` |
| `AutoLoaderCommand` | `AT+SPREF="AUTODLOADER"` |

### 超时与等待

| 键 | 默认 | 说明 |
|---|---|---|
| `TimeoutMs` | `30000` | 传给 `CmdDloader -timeout` |
| `EnterDownloadSec` | `30` | 等待进入下载模式 |
| `BootWaitSec` | `70` | 等待自动开机 |
| `FlashRunTimeoutSec` | `600` | 单次烧写进程总超时 |
| `AtProbeWaitMs` | `800` | 发 AT 后等响应 |
| `PollMs` | `400` | 端口轮询间隔 |

### 循环与判据

| 键 | 默认 |
|---|---|
| `Rounds` | `3` |
| `IntervalSec` | `5` |
| `PassPatterns` | `["DownLoad Passed", "Download Result\\s*=\\s*Passed"]` |
| `ElapsedPattern` | `Elapsed Times\\s*=\\s*([\\d\\.]+)\\s*s` |
| `PortPattern` | `PORT\\s*=\\s*COM(\\d+)` |
| `ImeiPattern` | `IMEI\\s*=\\s*(\\S+)` |

### 扫描

| 键 | 默认 | 说明 |
|---|---|---|
| `ScanRoots` | `[]` | 查找 CmdDloader 的根目录，空=用兜底逻辑 |

---

## 自动发现规则

### CmdDloader.exe

1. `--tool-dir` 参数
2. 配置里的 `ToolDir`
3. 环境变量 `DLOADER_TOOL`
4. 配置里的 `ScanRoots`
5. 兜底：当前目录及最多 6 层祖先 + `Program Files` 下名字含 `Unisoc`/`SPRD`/`Spreadtrum`/`ResearchDownload` 的目录

**不写死任何路径**，找不到会提示用 `config --tool-dir` 固定。

**性能提醒**：向上扫描会一直走到盘符根（如 `E:\`），在深目录下等于遍历整块盘。
实测在较深的 SDK 工程目录下不配 `ToolDir` 跑 `detect`，
耗了约 3 分钟、扫出 56 个候选。用 `config --tool-dir` 固定后是秒级。

### PAC

1. `--pac` 参数
2. 配置里的 `Pac`
3. 当前目录 `*.pac`
4. 当前目录 `build/*/img/*.pac`（Unisoc SDK 标准产物结构）

**只在当前目录找，不向父目录扫。** 这是故意的：PAC 决定烧哪一版固件，
扫得太宽有可能选中别的项目的包，那个后果是烧错砖，而不是重跑一次。

所以要么在 SDK 项目根目录运行，要么用 `--pac` / 配置 `Pac` 固定。
配置的 `Pac` 文件不存在时会打 `[WARN]` 并降级回自动发现。

### 串口

`pyserial` 的 `list_ports.comports()` 直接给出 `device` / `vid` / `pid` / `description`，按配置里的 VID/PID 分类：

| 类型 | 判定 |
|---|---|
| 下载模式 | `(vid,pid) == DownloadVidPid`（默认 `0x0525/0xA4A7`） |
| 正常模式 AT | `(vid,pid) == NormalVidPid`（默认 `0x1782/0x3D03`）且名字含 `AtNamePattern` |

不依赖 `Get-PnpDevice`，也不需要 pywin32。

---

## 环境变量

| 变量 | 作用 |
|---|---|
| `DLOADER_CONFIG` | 配置文件路径（默认包根目录 `dloader.config.json`） |
| `DLOADER_TOOL` | CmdDloader 目录 |
| `DLOADER_BAUD` | 波特率 |
| `DLOADER_TIMEOUT_MS` | 超时 |
| `DLOADER_SCAN_ROOTS` | 扫描根，分号分隔 |

优先级：**环境变量 > 配置文件 > 代码默认值**。

---

## 配置读写

```powershell
# 固定工具目录 / PAC / AT 口（带设置参数即保存）
dloader config --tool-dir '<ResearchDownload_Bin目录>'
dloader config --pac '<PAC文件>'
dloader config --at-port 19

# 查看当前生效配置（含所有代码默认值）
dloader config
```

只写显式设置过的键，默认值仍由代码管理——升级默认值不会被旧配置挡住。

高级项（设备标识、波特率、判据正则）直接编辑 `dloader.config.json`。

`dloader.config.json` 里存的是**本机绝对路径**，已在 `.gitignore` 中排除。
每台机器 clone 后各自跑一次 `dloader config` 生成，不要提交到仓库。

---

## 单元测试

```powershell
pip install pytest
python -m pytest tests/ -v
```

用例全部用 mock / 临时目录，**不需要真机**（数量以 `python -m pytest tests/ --collect-only -q` 为准）：

- VID/PID 解析（含大小写、非法输入）
- 成功判据匹配（兼容两种措辞，可配置）
- 输出解码（UTF-8 / GBK）
- 烧写结果解析（mock subprocess，含 v22/v27 两种耗时字段）
- Open port fail Hint（占口 / 嫌疑进程 / 幽灵口文案）
- 工具/PAC 发现（临时目录）
- `--skip-file-id`：版本探测、参数透传、非法 ID 校验
- `check` / `pacinfo` 基础解析
- 配置层告警：坏 JSON、未知键、非法环境变量，以及"干净配置不误报"

这是换 Python 的主要收益：核心逻辑终于可以脱离真机验证了。

---

## CmdDloader 版本兼容

| 版本 | 文件大小 | 实测结果 |
|---|---|---|
| 22.19.13.2 | 6.2 MB | 可烧，`-SkipFileID` **无效**（接受参数但静默忽略） |
| 22.19.13.2 | 2.0 MB | 可烧，同上 |
| 27.26.23.04 | 58 KB | 可烧，`-SkipFileID` **有效** |

三个包都实测跑通过 `flash` 和 `loop`（含烧完自动开机）。

差异点：

- **耗时字段**：v22 输出 `Elapsed Times = 56s`，v27 不再输出，本工具会自动回落到本地计时
- **`-SkipFileID`**：只有 v27+ 真正生效。本工具会探测工具版本，v22 传该参数直接报 `[ERROR]` 退出，不会静默降级
- **成功判据**：v22 是 `DownLoad Passed`，v27 是 `Download Result = Passed;`，`PassPatterns` 两者都兼容

### 烧写时指定工具和 PAC

两个都是命令行参数，互不影响：

```powershell
# 指定 v27 的烧写工具
dloader flash --tool-dir '<...>\Download_R27.26.2304\Bin'

# 指定 v22 的烧写工具
dloader flash --tool-dir '<...>\tools\DEBUG_TOOL\ResearchDownload\Bin'

# 指定某个具体 PAC
dloader flash --pac '<另一个.pac>'

# 两个都指定
dloader flash --tool-dir '<v27 Bin>' --pac '<某.pac>'
```

**怎么确认实际用的是哪个版本**：

- `dloader detect` 的 `[TOOL]` 行带版本号，例如 `CmdDloader.exe  (v27.26.23.4)`
- `dloader flash` 在命令行之前也会打印 `Tool  : v27.26.23.4  (CmdDloader.exe)`

版本号直接读 `CmdDloader.exe` 的 PE 版本资源，读不到时只显示路径、不报错。

---

## 固件 / 项目需具备的能力

主机侧见文末「环境要求」。设备侧按用法分层：

| 能力 | 用途 | 不满足的后果 |
|---|---|---|
| 能进下载模式（硬触发即可） | 单次 `flash` | 烧不了 |
| AT 软触发（`AutoDloader` 等） | `trigger` / `loop` 自动进下载 | 只能手硬触发，闭环断 |
| 烧完能正常复位出下载（FDL / `CHIP_SOFT_ResetNormal`） | 烧完回正常模式 | 可能停在下载态 |
| `auto_power_on = 1`（NV 读**之后**再强制） | 烧完进桌面、AT 回来 | 停充电界面，`loop` 等不到 AT |
| USB 枚举正确（`usbservice_isr.c` + **`usbservice_main.c` 开机起 LOG**） | 烧完 PC 能认出 AT | `VID_0000` / 无口，`loop` 断 |

**只硬触发烧一次**：固件几乎无额外要求。  
**要用 `loop` / 软触发闭环**：上表后四项都要具备。

新项目首次使用前，用源码检查扫一遍：

```powershell
dloader check --project "<项目根>"
```

`check` 会查：PAC、CmdDloader、`auto_power_on`、USB isr 插拔枚举、**USB 开机枚举（`usbservice_main.c`）**、AT 软触发、FDL 复位。  
开机路径要求：源码含 `RESTART_BY_SOFTWARE`，且 `USB_StartService(USB_SERVICE_LOG)` **附近**有 `g_USBService_table[USB_SERVICE_LOG]`；无条件 `UDC_HWInit` + `NULL`/`UCOM` 会判 FAIL（主机易见 `VID_0000`）。启发式取树中第一个 `usbservice_main.c`，注释干扰时可能误判，以实机为准。

建议验证顺序：`check` → `detect` → `ports` → `trigger` → `flash` → `loop --rounds 2`。

> 烧完停在充电界面 ≠ 烧写失败，先查 `auto_power_on`（固件侧）。

---

## 反复烧写原理

下载模式不是烧进 NOR 的，而是 FDL 握手：

1. 工具先把 `nor_fdl1.img` 下载到 RAM
2. 再由 FDL2（`nor_fdl.bin`）擦写 NOR 各分区

再次进入下载模式有两条路：

1. **软触发（推荐）**：向 AT 口发 `AutoLoaderCommand`，设备自己复位进下载模式
2. **硬触发**：断电重启，上电瞬间 ROM 检查 USB 下载请求

`loop` 走第 1 条，全程 USB 不用拔、不用按键、不用断电；烧完**必须复位开机**回 AT，因此 **不接受 `--power-off`**（断电请用 `dloader flash --power-off`）。

**判"可重复烧写"的三条**：烧完下载口消失 + AT 口回来 + `AT` 返回 `OK`。任一条不过，`loop` 立刻中止。

---

## 烧完进主界面还是充电界面？

这是固件侧的事。烧写必然插着 USB，固件 `SaveHWRestartCondition()` 会判定为「充电启动」，除非：

- NV 里 `auto_power_on = 1`，让 `init.c` 把 `RESTART_BY_CHARGE` 改判为 `RESTART_BY_SOFTWARE`

（能力总表与 `check` 用法见上文「固件 / 项目需具备的能力」。）

---

## 常见问题

### 找不到 CmdDloader.exe

```powershell
dloader config --tool-dir '<你的 ResearchDownload\Bin 目录>'
```

### 烧写报 `Open port fail` / `PS2264`

常见两类：

1. **串口被占**：别的程序正打开着该下载口——先关掉再烧  
2. **幽灵口**：枚举里还有下载口，但实际打不开——硬触发重新进下载模式；或用 `dloader ports` 确认后显式 `--port <N>`

工具在这类失败时会额外打印英文 Hint：
1. 先按 COM 查**实际占口进程**（能查到则列 PID/名称）  
2. 再列出本机正在跑的常见嫌疑进程（Logel / ResearchDownload / 其它 dloader 等）作补充  
权限不足时可能查不到精确占有者，但仍会给出嫌疑进程列表。

### 烧写卡在 `Waiting`

设备没稳定停在下载模式，典型是看门狗复位或 USB 供电不足。检查线材/供电后重试。

### 失败但进程退出码是 0

`CmdDloader.exe` 的退出码恒为 0，**必须解析输出**。默认兼容 `DownLoad Passed` 和 `Download Result = Passed;`，换版本后改 `PassPatterns`。

### 配置输出为什么是英文

所有面向用户的输出（`ports` / `detect` / `config` / `flash` / `loop` / `trigger` 的日志，以及 `--help` 文本）**一律使用英文**，避免 Windows 控制台代码页不一致导致的编码错误。中文只保留在源码注释和本文档里。

### 配置文件带 BOM 读不出来

已用 `utf-8-sig` 读取，兼容 Windows PowerShell 写出的带 BOM 文件。

---

## 目录结构

```
DLoader-cli/
├── dloader/
│   ├── __init__.py        版本号
│   ├── __main__.py        python -m dloader 入口
│   ├── config.py          配置（默认值 + JSON + 环境变量 + VID/PID 解析）
│   ├── discovery.py       工具 / PAC / 设备发现
│   ├── device.py          AT 交互、软触发、端口等待
│   ├── flash.py           CmdDloader 调用与结果解析
│   ├── port_diag.py       Open port / 占口进程诊断（Windows）
│   ├── check.py           项目源码兼容性检查
│   ├── pacinfo.py         PAC 分区 / NV 解析
│   └── cli.py             argparse 子命令
├── tests/
│   └── test_core.py       单元测试（无需真机）
├── dloader.spec           PyInstaller 打包
├── dloader.config.json    用户配置（.gitignore 排除，本机生成）
├── CHANGELOG.md
├── .gitignore
├── pyproject.toml
└── README.md
```

---

## 环境要求

- Windows + PowerShell 5.1 或更高（推荐运行环境）
- Python 3.9+
- pyserial 3.5+
- Unisoc ResearchDownload 工具包（提供 `CmdDloader.exe`）
- 已装好的 SPRD USB 驱动
