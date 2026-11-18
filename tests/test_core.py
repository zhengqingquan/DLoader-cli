"""核心逻辑单元测试：不需要真实设备，全部用 mock / 临时目录。"""

from __future__ import annotations

import argparse
import io
import json

import pytest

from dloader.cli import ensure_skip_file_id_supported, validate_inputs
from dloader.config import DEFAULTS, config_path, load_config, parse_vid_pid
from dloader.discovery import (
    DiscoveryError,
    describe_tool,
    find_cmddloader,
    find_pac,
    get_exe_version,
    list_pac_file_ids,
    validate_skip_file_ids,
)
from dloader.flash import (
    _decode,
    invoke_flash_run,
    is_flash_success,
    probe_skip_file_id,
    unsupported_skip_file_id_message,
)
from dloader.port_diag import (
    _device_name_matches,
    _suspect_process_label,
    find_processes_locking_com,
    find_suspect_serial_processes,
    format_open_port_hint,
    is_open_port_failure,
    normalize_com_port,
    show_flash_failure,
)


# ---------------------------------------------------------------------------
# VID/PID 解析
# ---------------------------------------------------------------------------
def test_parse_vid_pid_basic():
    assert parse_vid_pid("VID_0525&PID_A4A7") == (0x0525, 0xA4A7)
    assert parse_vid_pid("VID_1782&PID_3D03") == (0x1782, 0x3D03)


def test_parse_vid_pid_case_insensitive():
    assert parse_vid_pid("vid_0525&pid_a4a7") == (0x0525, 0xA4A7)


def test_parse_vid_pid_invalid():
    with pytest.raises(ValueError):
        parse_vid_pid("garbage")
    with pytest.raises(ValueError):
        parse_vid_pid("")


# ---------------------------------------------------------------------------
# 成功判据
# ---------------------------------------------------------------------------
def test_flash_output_matches_both_wording():
    cfg = dict(DEFAULTS)
    assert is_flash_success("... DownLoad Passed, Elapsed Times = 56s", cfg)
    assert is_flash_success("Download Result = Passed;", cfg)


def test_flash_output_rejects_failure():
    cfg = dict(DEFAULTS)
    assert not is_flash_success("Download Result = Failed;", cfg)
    assert not is_flash_success("", cfg)


def test_pass_patterns_configurable():
    cfg = dict(DEFAULTS)
    cfg["PassPatterns"] = ["ALL_GOOD"]
    assert is_flash_success("status: ALL_GOOD", cfg)
    assert not is_flash_success("DownLoad Passed", cfg)


# ---------------------------------------------------------------------------
# 输出解码
# ---------------------------------------------------------------------------
def test_decode_utf8():
    assert _decode("烧写完成".encode("utf-8")) == "烧写完成"


def test_decode_gbk():
    assert _decode("烧写完成".encode("gbk")) == "烧写完成"


# ---------------------------------------------------------------------------
# 烧写结果解析（mock subprocess）
# ---------------------------------------------------------------------------
class _FakeProc:
    """给 probe_skip_file_id 等仍走 subprocess.run 的用例用。"""

    def __init__(self, stdout: bytes, stderr: bytes = b"", code: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = code


class _FakePopen:
    """给 invoke_flash_run 流式读取用的假进程。"""

    def __init__(self, stdout: bytes, code: int = 0):
        self.stdout = io.BytesIO(stdout)
        self.stderr = None
        self.returncode = code

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_invoke_flash_run_success(monkeypatch, tmp_path):
    exe = tmp_path / "CmdDloader.exe"
    exe.write_bytes(b"MZ")
    pac = tmp_path / "a.pac"
    pac.write_bytes(b"x")

    monkeypatch.setattr(
        "dloader.flash.subprocess.Popen",
        lambda *a, **k: _FakePopen(
            b"DownLoad Passed, Elapsed Times = 56s\nPORT = COM17\nIMEI = 123456789012345"
        ),
    )

    cfg = dict(DEFAULTS)
    r = invoke_flash_run(exe, pac, 17, cfg, reset=True)

    assert r.success is True
    assert r.elapsed_sec == 56.0
    assert r.port == "COM17"
    assert r.imei == "123456789012345"


def test_invoke_flash_run_failure(monkeypatch, tmp_path):
    exe = tmp_path / "CmdDloader.exe"
    exe.write_bytes(b"MZ")
    pac = tmp_path / "a.pac"
    pac.write_bytes(b"x")

    monkeypatch.setattr(
        "dloader.flash.subprocess.Popen",
        lambda *a, **k: _FakePopen(b"Download Result = Failed; Open port fail"),
    )

    cfg = dict(DEFAULTS)
    r = invoke_flash_run(exe, pac, 17, cfg, reset=True)

    assert r.success is False
    # 输出里没有 "Elapsed Times" 时回落到本地计时，所以耗时非空
    assert isinstance(r.elapsed_sec, float)
    assert r.elapsed_sec >= 0


def test_invoke_flash_run_streams_output(monkeypatch, tmp_path, capsys):
    exe = tmp_path / "CmdDloader.exe"
    exe.write_bytes(b"MZ")
    pac = tmp_path / "a.pac"
    pac.write_bytes(b"x")

    monkeypatch.setattr(
        "dloader.flash.subprocess.Popen",
        lambda *a, **k: _FakePopen(b"FDL Connecting\nDownload Result = Passed;"),
    )

    cfg = dict(DEFAULTS)
    r = invoke_flash_run(exe, pac, 17, cfg, reset=True)
    out = capsys.readouterr().out

    assert r.success is True
    assert "FDL Connecting" in out
    assert "Download Result = Passed;" in out


def test_is_open_port_failure_detects_common_markers():
    assert is_open_port_failure("Download Result = Failed; Error Messag = [PS2264]Open port fail;")
    assert is_open_port_failure("[ERROR] DownLoad Failed: Open port fail, Elapsed Times =")
    assert is_open_port_failure("Access denied")
    assert not is_open_port_failure("Download Result = Passed;")
    assert not is_open_port_failure("")


def test_normalize_com_port():
    assert normalize_com_port(17) == "COM17"
    assert normalize_com_port("17") == "COM17"
    assert normalize_com_port("com17") == "COM17"
    assert normalize_com_port("COM 17") == "COM17"
    assert normalize_com_port(None) is None
    assert normalize_com_port("") is None


def test_suspect_process_label():
    assert _suspect_process_label("Logel.exe", None) == "Logel.exe"
    assert _suspect_process_label("dloader.exe", "") == "dloader.exe"
    assert (
        _suspect_process_label("python.exe", r"python.exe -m dloader flash")
        == "python.exe (-m dloader)"
    )
    assert _suspect_process_label("python.exe", "vibe-trading-mcp") is None


def test_format_open_port_hint_lists_holders_and_extra_suspects():
    text = format_open_port_hint(
        port=17,
        holders=[(1001, "dloader.exe")],
        suspects=[(1001, "dloader.exe"), (3492, "Logel.exe")],
    )
    assert "using COM17" in text
    assert "PID 1001  dloader.exe" in text
    assert "Also running" in text
    assert "PID 3492  Logel.exe" in text
    # 精确结果里已有的 PID 不重复出现在嫌疑段
    assert text.count("PID 1001") == 1


def test_format_open_port_hint_suspects_when_no_holders(monkeypatch):
    monkeypatch.setattr("dloader.port_diag._com_port_busy", lambda com: True)
    text = format_open_port_hint(
        port="COM17",
        holders=[],
        suspects=[(3492, "Logel.exe"), (2002, "python.exe (-m dloader)")],
    )
    assert "COM17 is in use" in text
    assert "exact holder could not be identified" in text
    assert "Suspect processes currently running:" in text
    assert "PID 3492  Logel.exe" in text
    assert "PID 2002  python.exe (-m dloader)" in text


def test_format_open_port_hint_ghost_without_suspects(monkeypatch):
    monkeypatch.setattr("dloader.port_diag._com_port_busy", lambda com: False)
    text = format_open_port_hint(port="COM17", holders=[], suspects=[])
    assert "stale ghost COM" in text
    assert "PID" not in text


def test_show_flash_failure_combined_hint(capsys, monkeypatch):
    monkeypatch.setattr(
        "dloader.port_diag.find_processes_locking_com",
        lambda port, exclude_pids=None, timeout_sec=3.0: [],
    )
    monkeypatch.setattr("dloader.port_diag._com_port_busy", lambda com: True)
    monkeypatch.setattr(
        "dloader.port_diag.find_suspect_serial_processes",
        lambda exclude_pids=None: [(3492, "Logel.exe")],
    )
    show_flash_failure(
        "Download Result = Failed; Error Messag = [PS2264]Open port fail;\n",
        port=17,
    )
    out = capsys.readouterr().out
    assert "Open port fail" in out
    assert "COM17 is in use" in out
    assert "PID 3492  Logel.exe" in out


def test_show_flash_failure_no_hint_for_other_errors(capsys):
    show_flash_failure("Download Result = Failed; Error Messag = checksum error;\n")
    out = capsys.readouterr().out
    assert "Hint:" not in out
    assert "PID" not in out


def test_invoke_flash_run_elapsed_fallback_v27(monkeypatch, tmp_path):
    """v27 不输出 "Elapsed Times"，应回落到本地计时而不是 None。"""
    exe = tmp_path / "CmdDloader.exe"
    exe.write_bytes(b"MZ")
    pac = tmp_path / "a.pac"
    pac.write_bytes(b"x")

    monkeypatch.setattr(
        "dloader.flash.subprocess.Popen",
        lambda *a, **k: _FakePopen(b"Download Result = Passed;"),
    )

    cfg = dict(DEFAULTS)
    r = invoke_flash_run(exe, pac, 17, cfg, reset=True)

    assert r.success is True
    assert isinstance(r.elapsed_sec, float)
    assert r.elapsed_sec >= 0


def test_invoke_flash_run_reset_poweroff_conflict(tmp_path):
    cfg = dict(DEFAULTS)
    with pytest.raises(ValueError):
        invoke_flash_run(
            tmp_path / "x.exe", tmp_path / "x.pac", 17, cfg,
            reset=True, power_off=True,
        )


# ---------------------------------------------------------------------------
# 工具 / PAC 发现
# ---------------------------------------------------------------------------
def test_find_cmddloader_explicit(tmp_path):
    bin_dir = tmp_path / "Bin"
    bin_dir.mkdir()
    exe = bin_dir / "CmdDloader.exe"
    exe.write_bytes(b"MZ")

    cfg = dict(DEFAULTS)
    found = find_cmddloader(cfg, tool_dir=str(bin_dir))
    assert found == exe.resolve()


def test_find_cmddloader_explicit_missing(tmp_path):
    cfg = dict(DEFAULTS)
    with pytest.raises(DiscoveryError):
        find_cmddloader(cfg, tool_dir=str(tmp_path))


def test_find_pac_in_cwd(tmp_path, monkeypatch):
    pac = tmp_path / "test.pac"
    pac.write_bytes(b"x")
    monkeypatch.chdir(tmp_path)

    cfg = dict(DEFAULTS)
    cfg["Pac"] = ""
    assert find_pac(cfg) == pac.resolve()


def test_find_pac_in_build_img(tmp_path, monkeypatch):
    img = tmp_path / "build" / "target_builddir" / "img"
    img.mkdir(parents=True)
    pac = img / "firmware.pac"
    pac.write_bytes(b"x")
    monkeypatch.chdir(tmp_path)

    cfg = dict(DEFAULTS)
    cfg["Pac"] = ""
    assert find_pac(cfg) == pac.resolve()


def test_find_pac_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    cfg = dict(DEFAULTS)
    cfg["Pac"] = ""
    with pytest.raises(DiscoveryError):
        find_pac(cfg)


def test_find_pac_explicit_missing(tmp_path):
    cfg = dict(DEFAULTS)
    with pytest.raises(DiscoveryError):
        find_pac(cfg, pac=str(tmp_path / "nope.pac"))


# ---------------------------------------------------------------------------
# --skip-file-id 版本能力探测
# ---------------------------------------------------------------------------
_V27_USAGE = (
    b"CmdDloader usage:\n"
    b"-SkipFileID <FileID-String>             FileID which want to skip download\n"
)
_V22_USAGE = (
    b"CmdDloader Usage\n"
    b"CmdDloader.exe <-pac file> [-port portnumber] [-ReadNV nvfile]\n"
)


def _exe(tmp_path):
    exe = tmp_path / "CmdDloader.exe"
    exe.write_bytes(b"MZ")
    return exe


def test_probe_skip_file_id_v27_supported(monkeypatch, tmp_path):
    exe = _exe(tmp_path)
    monkeypatch.setattr(
        "dloader.flash.subprocess.run", lambda *a, **k: _FakeProc(_V27_USAGE)
    )
    ok, text = probe_skip_file_id(exe)
    assert ok is True
    assert "SkipFileID" in text


def test_probe_skip_file_id_v22_unsupported(monkeypatch, tmp_path):
    exe = _exe(tmp_path)
    monkeypatch.setattr(
        "dloader.flash.subprocess.run", lambda *a, **k: _FakeProc(_V22_USAGE)
    )
    ok, text = probe_skip_file_id(exe)
    assert ok is False
    assert "SkipFileID" not in text


def test_probe_skip_file_id_handles_missing_exe(tmp_path):
    ok, text = probe_skip_file_id(tmp_path / "nope.exe")
    assert ok is False
    assert "probe failed" in text


def test_probe_skip_file_id_handles_timeout(monkeypatch, tmp_path):
    import subprocess as sp

    exe = _exe(tmp_path)

    def _boom(*a, **k):
        raise sp.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr("dloader.flash.subprocess.run", _boom)
    ok, text = probe_skip_file_id(exe, timeout_sec=1)
    assert ok is False
    assert "probe failed" in text


def test_ensure_skip_rejects_unsupported_version(monkeypatch, tmp_path):
    exe = _exe(tmp_path)
    monkeypatch.setattr(
        "dloader.flash.subprocess.run", lambda *a, **k: _FakeProc(_V22_USAGE)
    )
    with pytest.raises(RuntimeError) as ei:
        ensure_skip_file_id_supported(exe, "CP,TFBOOT")
    msg = str(ei.value)
    assert "does not support -SkipFileID" in msg
    assert "v27" in msg


def test_ensure_skip_allows_supported_version(monkeypatch, tmp_path):
    exe = _exe(tmp_path)
    monkeypatch.setattr(
        "dloader.flash.subprocess.run", lambda *a, **k: _FakeProc(_V27_USAGE)
    )
    ensure_skip_file_id_supported(exe, "CP")  # must not raise


def test_ensure_skip_noop_without_flag(monkeypatch, tmp_path):
    def _unexpected(*a, **k):
        raise AssertionError("probe must not run when --skip-file-id is absent")

    monkeypatch.setattr("dloader.flash.subprocess.run", _unexpected)
    ensure_skip_file_id_supported(tmp_path / "x.exe", None)  # must not raise


def test_unsupported_message_contains_guidance():
    msg = unsupported_skip_file_id_message(r"X:\Bin\CmdDloader.exe", "CmdDloader usage:\n")
    assert "does not support -SkipFileID" in msg
    assert "v27" in msg
    assert r"X:\Bin\CmdDloader.exe" in msg


def test_invoke_flash_run_passes_skip_file_id(monkeypatch, tmp_path):
    exe = _exe(tmp_path)
    pac = tmp_path / "a.pac"
    pac.write_bytes(b"x")
    captured = {}

    def fake_popen(*a, **k):
        captured["args"] = list(a[0])
        return _FakePopen(b"Download Result = Passed;")

    monkeypatch.setattr("dloader.flash.subprocess.Popen", fake_popen)
    cfg = dict(DEFAULTS)
    r = invoke_flash_run(exe, pac, 17, cfg, reset=True, skip_file_ids="CP,TFBOOT")

    assert r.success is True
    idx = captured["args"].index("-SkipFileID")
    assert captured["args"][idx + 1] == "CP,TFBOOT"


def test_invoke_flash_run_omits_skip_flag_by_default(monkeypatch, tmp_path):
    exe = _exe(tmp_path)
    pac = tmp_path / "a.pac"
    pac.write_bytes(b"x")
    captured = {}

    def fake_popen(*a, **k):
        captured["args"] = list(a[0])
        return _FakePopen(b"Download Result = Passed;")

    monkeypatch.setattr("dloader.flash.subprocess.Popen", fake_popen)
    cfg = dict(DEFAULTS)
    invoke_flash_run(exe, pac, 17, cfg, reset=True)

    assert "-SkipFileID" not in captured["args"]


# ---------------------------------------------------------------------------
# 配置层：绝不静默降级
# ---------------------------------------------------------------------------
def test_load_config_warns_on_bad_json(tmp_path, monkeypatch, capsys):
    p = tmp_path / "c.json"
    p.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setenv("DLOADER_CONFIG", str(p))

    cfg = load_config()

    out = capsys.readouterr().out
    assert "[WARN]" in out
    assert "not valid JSON" in out
    assert cfg["TimeoutMs"] == DEFAULTS["TimeoutMs"]


def test_load_config_warns_on_unknown_key(tmp_path, monkeypatch, capsys):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"ToolDir": "x", "TypoKey": 1}), encoding="utf-8")
    monkeypatch.setenv("DLOADER_CONFIG", str(p))

    cfg = load_config()

    out = capsys.readouterr().out
    assert "unknown config key 'TypoKey'" in out
    assert cfg["ToolDir"] == "x"


def test_load_config_warns_on_bad_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DLOADER_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setenv("DLOADER_BAUD", "abc")

    load_config()

    out = capsys.readouterr().out
    assert "DLOADER_BAUD" in out and "invalid" in out


def test_load_config_silent_when_clean(tmp_path, monkeypatch, capsys):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"ToolDir": "x"}), encoding="utf-8")
    monkeypatch.setenv("DLOADER_CONFIG", str(p))
    monkeypatch.delenv("DLOADER_BAUD", raising=False)

    load_config()

    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# PAC 定位与分区表
# ---------------------------------------------------------------------------
def _fake_pac(tmp_path, ids=("FDL", "FDL2", "PS", "NV", "TFBOOT")):
    pac = tmp_path / "a.pac"
    body = "".join(f"<ID>{i}</ID>" for i in ids)
    pac.write_bytes(b"junk<SchemeList>" + body.encode() + b"</SchemeList>junk")
    return pac


def test_find_pac_warns_when_configured_missing(tmp_path, monkeypatch, capsys):
    cfg = dict(DEFAULTS)
    cfg["Pac"] = str(tmp_path / "gone.pac")
    (tmp_path / "a.pac").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)

    p = find_pac(cfg)

    assert p.name == "a.pac"
    out = capsys.readouterr().out
    assert "configured Pac does not exist" in out


def test_list_pac_file_ids(tmp_path):
    pac = _fake_pac(tmp_path, ids=("FDL", "PS", "NV"))
    assert list_pac_file_ids(pac) == ["FDL", "PS", "NV"]


def test_list_pac_file_ids_missing_xml(tmp_path):
    pac = tmp_path / "a.pac"
    pac.write_bytes(b"binary garbage")
    assert list_pac_file_ids(pac) is None


def test_list_pac_file_ids_unreadable(tmp_path):
    assert list_pac_file_ids(tmp_path / "nope.pac") is None


def test_validate_skip_ids_ok(tmp_path):
    assert validate_skip_file_ids("PS,NV", _fake_pac(tmp_path)) == []


def test_validate_skip_ids_unknown(tmp_path):
    problems = validate_skip_file_ids("PS,NOPE", _fake_pac(tmp_path))
    assert any("NOPE" in p for p in problems)


def test_validate_skip_ids_duplicate(tmp_path):
    problems = validate_skip_file_ids("PS,ps", _fake_pac(tmp_path))
    assert any("duplicate" in p for p in problems)


def test_validate_skip_ids_empty(tmp_path):
    assert validate_skip_file_ids("  ,  ", _fake_pac(tmp_path)) == [
        "--skip-file-id is empty"
    ]


def test_validate_skip_ids_unvalidated_when_pac_unreadable(tmp_path, capsys):
    pac = tmp_path / "a.pac"
    pac.write_bytes(b"binary garbage")
    problems = validate_skip_file_ids("NOPE", pac)
    assert problems == []
    assert "not validated" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------
def test_validate_inputs_rejects_negative_port(tmp_path):
    args = argparse.Namespace(port=-1, skip_file_id=None)
    with pytest.raises(ValueError):
        validate_inputs(args, _fake_pac(tmp_path))


def test_validate_inputs_warns_unknown_skip_id(tmp_path, capsys):
    args = argparse.Namespace(port=0, skip_file_id="NOPE")
    validate_inputs(args, _fake_pac(tmp_path))
    out = capsys.readouterr().out
    assert "[WARN]" in out and "NOPE" in out


def test_validate_inputs_quiet_when_clean(tmp_path, capsys):
    args = argparse.Namespace(port=0, skip_file_id=None)
    validate_inputs(args, _fake_pac(tmp_path))
    assert capsys.readouterr().out == ""

# ---------------------------------------------------------------------------
# 工具版本读取
# ---------------------------------------------------------------------------
def test_get_exe_version_missing_file(tmp_path):
    assert get_exe_version(tmp_path / "nope.exe") is None


def test_get_exe_version_non_pe_falls_back(tmp_path):
    exe = tmp_path / "CmdDloader.exe"
    exe.write_bytes(b"MZ not a real pe")
    # 非有效 PE 时返回 None，不应抛异常
    assert get_exe_version(exe) is None


def test_describe_tool_falls_back_to_path(tmp_path):
    exe = tmp_path / "CmdDloader.exe"
    exe.write_bytes(b"MZ")
    assert describe_tool(exe) == str(exe)


def test_describe_tool_with_version(tmp_path, monkeypatch):
    exe = tmp_path / "CmdDloader.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr("dloader.discovery.get_exe_version", lambda p: "27.26.23.4")
    assert describe_tool(exe).endswith("(v27.26.23.4)")

# ---------------------------------------------------------------------------
# PyInstaller / 配置路径
# ---------------------------------------------------------------------------
def test_config_path_frozen_uses_exe_dir(tmp_path, monkeypatch):
    import sys as _sys

    fake_exe = tmp_path / "dloader.exe"
    fake_exe.write_bytes(b"MZ")
    monkeypatch.delenv("DLOADER_CONFIG", raising=False)
    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    monkeypatch.setattr(_sys, "executable", str(fake_exe))
    assert config_path() == (tmp_path / "dloader.config.json").resolve()


def test_config_path_source_uses_package_root(monkeypatch):
    monkeypatch.delenv("DLOADER_CONFIG", raising=False)
    monkeypatch.delattr(__import__("sys"), "frozen", raising=False)
    p = config_path()
    assert p.name == "dloader.config.json"
    assert "dloader" in p.parent.name.lower() or p.parent.name == "DLoader-cli"


# ---------------------------------------------------------------------------
# 项目自检
# ---------------------------------------------------------------------------
def test_check_auto_power_on_pass(tmp_path):
    from dloader.check import check_auto_power_on
    d = tmp_path / "MS_Ref"
    d.mkdir()
    (d / "ref_param.c").write_text("s_ref_param.auto_power_on = 1;", encoding="utf-8")
    assert check_auto_power_on(tmp_path).status == "PASS"


def test_check_auto_power_on_fail(tmp_path):
    from dloader.check import check_auto_power_on
    d = tmp_path / "MS_Ref"
    d.mkdir()
    (d / "ref_param.c").write_text("s_ref_param.auto_power_on = 0;", encoding="utf-8")
    assert check_auto_power_on(tmp_path).status == "FAIL"


def test_check_usb_enum_pass(tmp_path):
    from dloader.check import check_usb_enum
    d = tmp_path / "MS_Ref"
    d.mkdir()
    (d / "usbservice_isr.c").write_text("#if 0\nUSB_StartService(USB_SERVICE_LOG);\n#endif", encoding="utf-8")
    assert check_usb_enum(tmp_path).status == "PASS"


def test_check_usb_boot_pass(tmp_path):
    from dloader.check import check_usb_boot

    d = tmp_path / "MS_Ref"
    d.mkdir()
    (d / "usbservice_main.c").write_text(
        "\n".join(
            [
                "if (RESTART_BY_CHARGE || RESTART_BY_SOFTWARE || CHGMNG_IsChargeConnect()) {",
                "  while (!g_USBService_table[USB_SERVICE_LOG]);",
                "  UCOM_init();",
                "  USB_StartService(USB_SERVICE_LOG);",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    assert check_usb_boot(tmp_path).status == "PASS"


def test_check_usb_boot_fail_missing_software(tmp_path):
    from dloader.check import check_usb_boot

    d = tmp_path / "MS_Ref"
    d.mkdir()
    (d / "usbservice_main.c").write_text(
        "UDC_HWInit();\nUSB_StartService(USB_SERVICE_NULL);\n",
        encoding="utf-8",
    )
    r = check_usb_boot(tmp_path)
    assert r.status == "FAIL"
    assert "VID_0000" in r.hint


def test_check_usb_boot_fail_ucom_init_alone(tmp_path):
    from dloader.check import check_usb_boot

    d = tmp_path / "MS_Ref"
    d.mkdir()
    (d / "usbservice_main.c").write_text(
        "\n".join(
            [
                "UCOM_init();  /* elsewhere */",
                "if (RESTART_BY_SOFTWARE) {",
                "  USB_StartService(USB_SERVICE_LOG);",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    r = check_usb_boot(tmp_path)
    assert r.status == "FAIL"
    assert "g_USBService_table" in r.detail


def test_check_usb_boot_fail_blind_ucom(tmp_path):
    from dloader.check import check_usb_boot

    d = tmp_path / "MS_Ref"
    d.mkdir()
    (d / "usbservice_main.c").write_text(
        "UDC_HWInit();\nUSB_StartService(USB_SERVICE_UCOM);\n",
        encoding="utf-8",
    )
    r = check_usb_boot(tmp_path)
    assert r.status == "FAIL"
    assert "NULL/UCOM" in r.detail


def test_check_pac_missing(tmp_path):
    from dloader.check import check_pac
    assert check_pac(tmp_path).status == "FAIL"


def test_print_report_skip_only_prompts_manual_verify(capsys):
    from dloader.check import CheckResult, print_report

    code = print_report(
        [CheckResult("at", "AT soft trigger", "SKIP", "not found", "verify at runtime")]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "SKIPPED" in out
    assert "manually" in out


def test_invoke_autoloader_raises_when_port_unavailable(monkeypatch):
    from dloader.device import invoke_autoloader

    class _Boom:
        def __init__(self, *a, **k):
            raise OSError("Access is denied")

    monkeypatch.setattr("dloader.device.serial.Serial", _Boom)
    with pytest.raises(RuntimeError, match="cannot open COM9"):
        invoke_autoloader("COM9", dict(DEFAULTS))


def test_find_processes_locking_com_early_exits(monkeypatch):
    assert find_processes_locking_com("") == []
    assert find_processes_locking_com("not-a-port") == []
    monkeypatch.setattr("dloader.port_diag.sys.platform", "linux")
    assert find_processes_locking_com("COM17") == []


def test_find_processes_locking_com_no_device_path(monkeypatch):
    monkeypatch.setattr("dloader.port_diag.sys.platform", "win32")
    monkeypatch.setattr("dloader.port_diag._com_device_path", lambda com: None)
    assert find_processes_locking_com("COM17") == []


def test_device_name_matches_relaxed_forms():
    device = r"\Device\USBSER000"
    assert _device_name_matches(device, device, "COM17")
    assert _device_name_matches(r"\??\COM17", device, "COM17")
    assert _device_name_matches(r"\Device\Serial1\COM17", device, "COM17")
    assert _device_name_matches(device.lower(), device, "COM17")
    assert _device_name_matches(
        r"\??\GLOBALROOT\Device\USBSER000", device, "COM17"
    )
    assert not _device_name_matches(r"\Device\USBSER001", device, "COM17")
    assert not _device_name_matches("", device, "COM17")
    # 短分量不得 endswith 误伤长设备名
    assert not _device_name_matches(r"\Device\USBSER00", device, "COM17")
    assert not _device_name_matches(r"\Device\Serial1", r"\Device\Serial10", "COM10")
    # COM1 不得误匹配含 COM17 的路径
    assert not _device_name_matches(r"\??\COM17", r"\Device\Serial0", "COM1")
    assert not _device_name_matches(r"\\.\COM17", device, "COM1")
    assert _device_name_matches(r"\??\COM1", r"\Device\Serial0", "COM1")


def test_help_mentions_open_port_hint():
    from dloader.cli import build_parser

    text = build_parser().format_help()
    assert "Open port fail" in text
    assert "Hint" in text


def test_find_suspect_serial_processes_parses_json(monkeypatch):
    monkeypatch.setattr("dloader.port_diag.sys.platform", "win32")
    payload = json.dumps(
        [
            {"ProcessId": 111, "Name": "Logel.exe", "CommandLine": None},
            {"ProcessId": 222, "Name": "notepad.exe", "CommandLine": None},
            {
                "ProcessId": 333,
                "Name": "python.exe",
                "CommandLine": "python -m dloader flash",
            },
        ]
    )

    class _Proc:
        returncode = 0
        stdout = payload

    monkeypatch.setattr("dloader.port_diag.subprocess.run", lambda *a, **k: _Proc())
    monkeypatch.setattr("dloader.port_diag.os.getpid", lambda: 1)
    found = find_suspect_serial_processes(exclude_pids={999})
    labels = {label for _, label in found}
    assert "Logel.exe" in labels
    assert any("-m dloader" in label for label in labels)
    assert "notepad.exe" not in labels


def test_cmd_loop_rejects_power_off():
    import dloader.cli as cli

    args = argparse.Namespace(
        tool_dir=None,
        pac=None,
        port=None,
        at_port=None,
        timeout_ms=0,
        scan_roots=None,
        power_off=True,
        write_sn1=None,
        write_sn2=None,
        skip_file_id=None,
        ez_mode=False,
        rounds=1,
        interval_sec=0,
    )
    with pytest.raises(RuntimeError, match="cannot use --power-off"):
        cli.cmd_loop(args, dict(DEFAULTS))


def test_cmd_loop_always_resets(monkeypatch):
    import dloader.cli as cli

    calls: list[dict] = []

    class _Result:
        success = True
        elapsed_sec = 1.0
        output = "Download Result = Passed;"

    monkeypatch.setattr(cli, "find_cmddloader", lambda *a, **k: r"C:\CmdDloader.exe")
    monkeypatch.setattr(cli, "find_pac", lambda *a, **k: r"C:\a.pac")
    monkeypatch.setattr(cli, "ensure_skip_file_id_supported", lambda *a, **k: None)
    monkeypatch.setattr(cli, "validate_inputs", lambda *a, **k: None)
    monkeypatch.setattr(cli, "resolve_at_port", lambda *a, **k: "COM10")
    monkeypatch.setattr(cli, "send_at", lambda *a, **k: "OK\r\n")
    monkeypatch.setattr(cli, "invoke_autoloader", lambda *a, **k: None)
    waits = iter(["COM17", "COM10"])
    monkeypatch.setattr(cli, "wait_sprd_port", lambda *a, **k: next(waits))
    monkeypatch.setattr(cli, "get_download_port", lambda *a, **k: None)

    def _flash(*a, **k):
        calls.append(k)
        return _Result()

    monkeypatch.setattr(cli, "invoke_flash_run", _flash)

    args = argparse.Namespace(
        tool_dir=None,
        pac=None,
        port=None,
        at_port=None,
        timeout_ms=0,
        scan_roots=None,
        write_sn1=None,
        write_sn2=None,
        skip_file_id=None,
        ez_mode=False,
        rounds=1,
        interval_sec=0,
    )
    assert cli.cmd_loop(args, dict(DEFAULTS)) == 0
    assert calls and calls[0]["reset"] is True
    assert calls[0]["power_off"] is False


def test_loop_parser_has_no_power_off():
    from dloader.cli import build_parser

    ns = build_parser().parse_args(["loop", "--rounds", "1"])
    assert not hasattr(ns, "power_off")
    with pytest.raises(SystemExit):
        build_parser().parse_args(["loop", "--power-off"])
    # --reset 仍接受（loop 始终复位，兼容旧脚本）
    ns_reset = build_parser().parse_args(["loop", "--reset", "--rounds", "1"])
    assert ns_reset.reset is True


# ---------------------------------------------------------------------------
# PAC 包分析
# ---------------------------------------------------------------------------
def test_parse_pac(tmp_path):
    from dloader.pacinfo import parse_pac
    head = "BP_R1.0.0".encode("utf-16-le") + b"\x00\x00"
    xml = (
        '<BMAConfig><Product name="P1"><SchemeName>P1</SchemeName>'
        '<NVItem name="IMEI" backup="1"><ID>0xFFFFFFFF</ID></NVItem>'
        '<NVItem name="WIFI" backup="0"><ID>0x199</ID></NVItem>'
        '</BMAConfig>'
    ).encode("ascii")
    p = tmp_path / "t.pac"
    p.write_bytes(head + b"\x00" * 16 + xml)
    info = parse_pac(p)
    assert info.product == "P1"
    assert info.keeps_imei
    assert len(info.nv_items) == 2


def test_parse_pac_no_xml(tmp_path):
    from dloader.pacinfo import parse_pac
    p = tmp_path / "t.pac"
    p.write_bytes("BP_R1.0.0".encode("utf-16-le") + b"\x00\x00" + b"\x00" * 32)
    info = parse_pac(p)
    assert info.product == ""
    assert info.nv_items == []
    assert not info.keeps_imei
