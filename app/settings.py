"""papertex 本地配置：首次生成、校验、原子保存与 SSH 连接测试。"""

import json
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8817,
    "local_library": "~/PaperLibrary",
    "review_space": "~/ReviewSpace",
    "remote_name": "远端服务器",
    "ssh_alias": "",
    "ssh_host": "",
    "ssh_user": "",
    "ssh_port": 22,
    "ssh_key": "",
    "remote_library": "",
    "remote_workshop": "",
}

LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,253}$")
_USER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,31}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_REMOTE_COMMANDS = ("find", "tar", "sha256sum", "head", "cp", "mkdir", "mv", "rm", "cat")


def _text(value, label, limit=300):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是字符串")
    value = value.strip()
    if _CONTROL.search(value):
        raise ValueError(f"{label}不能包含控制字符")
    if len(value) > limit:
        raise ValueError(f"{label}过长")
    return value


def _port(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是 1-65535 的整数")
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是 1-65535 的整数")
    if not 1 <= port <= 65535:
        raise ValueError(f"{label}必须是 1-65535 的整数")
    return port


def _remote_path(value, label):
    value = _text(value, label)
    if not value:
        return ""
    if not value.startswith("/"):
        raise ValueError(f"{label}必须是绝对路径")
    value = value.rstrip("/") or "/"
    if value == "/":
        raise ValueError(f"{label}不能是根目录")
    parts = value.split("/")[1:]
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"{label}不能包含空段、. 或 ..")
    return value


def _local_path(value, label="本机库路径", fallback=None):
    value = _text(value, label) or (DEFAULTS["local_library"] if fallback is None else fallback)
    expanded = os.path.expanduser(value)
    if not expanded.startswith("/"):
        raise ValueError(f"{label}必须是绝对路径或以 ~ 开头")
    if os.path.abspath(expanded) == "/":
        raise ValueError(f"{label}不能是根目录")
    return value


def _key_path(value):
    value = _text(value, "SSH 密钥路径")
    if not value:
        return ""
    expanded = os.path.expanduser(value)
    if not expanded.startswith("/"):
        raise ValueError("SSH 密钥路径必须是绝对路径或以 ~ 开头")
    if not os.path.isfile(expanded):
        raise ValueError("SSH 密钥路径不存在或不是文件")
    return value


def normalize(raw, strict=True):
    """校验并补全配置；strict 要求远端信息完整。"""
    if not isinstance(raw, dict):
        raise ValueError("配置必须是 JSON 对象")
    out = dict(raw)

    bind_host = _text(raw.get("host"), "监听地址", 64) or DEFAULTS["host"]
    if bind_host not in LOOPBACK_HOSTS:
        raise ValueError("监听地址只能是 localhost、127.0.0.1 或 ::1")
    out["host"] = bind_host
    out["port"] = _port(raw.get("port", DEFAULTS["port"]), "监听端口")
    out["local_library"] = _local_path(raw.get("local_library"))
    out["review_space"] = _local_path(raw.get("review_space"), "审稿目录",
                                      DEFAULTS["review_space"])
    lib_abs = os.path.abspath(os.path.expanduser(out["local_library"]))
    review_abs = os.path.abspath(os.path.expanduser(out["review_space"]))
    home_abs = os.path.abspath(os.path.expanduser("~"))
    if (review_abs in (lib_abs, home_abs)
            or not review_abs.startswith(home_abs + os.sep)
            or lib_abs.startswith(review_abs + os.sep)
            or review_abs.startswith(lib_abs + os.sep)):
        raise ValueError("审稿目录必须是论文库之外的独立子目录")

    alias = _text(raw.get("ssh_alias"), "SSH 别名", 64)
    ssh_host = _text(raw.get("ssh_host"), "SSH 主机", 253)
    ssh_user = _text(raw.get("ssh_user"), "SSH 用户名", 32)
    if alias and not _ALIAS.fullmatch(alias):
        raise ValueError("SSH 别名只能包含字母、数字、点、下划线、短横线")
    if alias:
        ssh_host = ""
        ssh_user = ""
        key = ""
    else:
        key = _key_path(raw.get("ssh_key"))
        if (ssh_host or ssh_user) and not (ssh_host and ssh_user):
            raise ValueError("直连方式需要同时填写主机和用户名")
    if ssh_host and (not _HOST.fullmatch(ssh_host) or ssh_host.startswith("-")):
        raise ValueError("SSH 主机格式不正确")
    if ssh_user and not _USER.fullmatch(ssh_user):
        raise ValueError("SSH 用户名格式不正确")
    out["ssh_alias"] = alias
    out["ssh_host"] = ssh_host
    out["ssh_user"] = ssh_user
    out["ssh_port"] = _port(raw.get("ssh_port", DEFAULTS["ssh_port"]) or DEFAULTS["ssh_port"],
                            "SSH 端口")
    out["ssh_key"] = key

    out["remote_library"] = _remote_path(raw.get("remote_library"), "远端资料库")
    out["remote_workshop"] = _remote_path(raw.get("remote_workshop"), "远端项目根目录")

    name = _text(raw.get("remote_name"), "服务器名称", 40)
    out["remote_name"] = name or alias or "远端服务器"

    has_target = bool(alias or (ssh_host and ssh_user))
    if strict and not has_target:
        raise ValueError("请填写 SSH 别名，或填写主机与用户名")
    if strict and bool(out["remote_library"]) != bool(out["remote_workshop"]):
        raise ValueError("远端资料库和项目根目录需要同时填写")
    if strict and has_target and not out["remote_library"]:
        raise ValueError("请填写远端资料库和项目根目录")
    return out


def is_configured(cfg):
    return bool(
        cfg.get("remote_library")
        and cfg.get("remote_workshop")
        and (cfg.get("ssh_alias") or (cfg.get("ssh_host") and cfg.get("ssh_user")))
    )


def ssh_target(cfg):
    if cfg.get("ssh_alias"):
        return [cfg["ssh_alias"]]
    if cfg.get("ssh_host") and cfg.get("ssh_user"):
        args = ["-p", str(cfg.get("ssh_port") or 22)]
        if cfg.get("ssh_key"):
            args += ["-i", os.path.expanduser(cfg["ssh_key"])]
        args.append(f"{cfg['ssh_user']}@{cfg['ssh_host']}")
        return args
    raise ValueError("尚未配置远端服务器")


def ssh_options():
    return ["-o", "BatchMode=yes", "-o", "ConnectTimeout=4"]


def host_target(cfg):
    if cfg.get("ssh_alias"):
        return cfg["ssh_alias"]
    return f"{cfg.get('ssh_user', '')}@{cfg.get('ssh_host', '')}"


def scp_target(cfg):
    """scp 只接收选项；主机由 host_target 单独拼到目标路径上。"""
    if cfg.get("ssh_alias"):
        return []
    args = ["-P", str(cfg.get("ssh_port") or 22)]
    if cfg.get("ssh_key"):
        args += ["-i", os.path.expanduser(cfg["ssh_key"])]
    return args


def target_label(cfg):
    if cfg.get("ssh_alias"):
        return cfg["ssh_alias"]
    if cfg.get("ssh_host"):
        return f"{cfg.get('ssh_user', '')}@{cfg['ssh_host']}"
    return ""


def public(cfg):
    return {
        "host": cfg["host"],
        "port": cfg["port"],
        "local_library": cfg["local_library"],
        "review_space": cfg.get("review_space", DEFAULTS["review_space"]),
        "remote_name": cfg["remote_name"],
        "connection": "alias" if cfg.get("ssh_alias") else (
            "direct" if cfg.get("ssh_host") else ""),
        "ssh_alias": cfg.get("ssh_alias", ""),
        "ssh_host": cfg.get("ssh_host", ""),
        "ssh_user": cfg.get("ssh_user", ""),
        "ssh_port": cfg.get("ssh_port", 22),
        "ssh_key": cfg.get("ssh_key", ""),
        "remote_library": cfg.get("remote_library", ""),
        "remote_workshop": cfg.get("remote_workshop", ""),
        "configured": is_configured(cfg),
    }


def load(path: Path):
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cfg = normalize({}, strict=False)
        write(path, cfg)
        return cfg
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取 {path}：{exc}")
    try:
        return normalize(raw, strict=False)
    except ValueError as exc:
        raise ValueError(f"配置文件 {path} 不合法：{exc}")


def write(path: Path, cfg):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("配置文件不能是符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name)
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(cfg, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


def _run_ssh(cfg, command, timeout=12):
    argv = ["ssh", *ssh_options(), *ssh_target(cfg), command]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    except OSError as exc:
        return exc


def _ssh_error(result):
    if result is None:
        return "SSH 连接超时"
    if isinstance(result, OSError):
        return f"无法启动 ssh：{result}"
    stderr = (result.stderr or "").strip()
    low = stderr.lower()
    if "host key verification failed" in low:
        return "主机指纹尚未确认：请先在终端执行一次 ssh 命令并确认指纹"
    if "remote host identification has changed" in low:
        return "远端主机指纹已变化：请核对服务器后更新 known_hosts"
    if "permission denied" in low:
        return "认证失败：请确认密钥已加载到 ssh-agent，或公钥已写入远端 authorized_keys"
    if "could not resolve hostname" in low:
        return "无法解析主机名"
    if "connection refused" in low:
        return "连接被拒绝：请检查 SSH 端口和服务状态"
    if "timed out" in low or "timeout" in low:
        return "连接超时：请检查网络、端口或 VPN"
    return stderr[:300] or "SSH 连接失败"


def _path_prefixes(path):
    parts = PurePosixPath(path).parts
    return [str(PurePosixPath(*parts[:i])) for i in range(2, len(parts) + 1)]


def _path_check(cfg, label, path):
    guard = "for p in " + " ".join(shlex.quote(p) for p in _path_prefixes(path)) + "; do " \
            "if [ -L \"$p\" ]; then exit 73; fi; done; "
    script = (guard + f"if [ ! -d {shlex.quote(path)} ]; then exit 10; fi; "
              f"if [ ! -w {shlex.quote(path)} ]; then exit 11; fi; printf ok")
    result = _run_ssh(cfg, script)
    if result is None or isinstance(result, OSError):
        return {"name": label, "ok": False, "detail": _ssh_error(result)}
    if result.returncode == 0:
        return {"name": label, "ok": True, "detail": path}
    if result.returncode == 10:
        return {"name": label, "ok": False, "detail": f"目录不存在：{path}"}
    if result.returncode == 11:
        return {"name": label, "ok": False, "detail": f"目录不可写：{path}"}
    if result.returncode == 73:
        return {"name": label, "ok": False, "detail": f"路径包含符号链接：{path}"}
    return {"name": label, "ok": False, "detail": _ssh_error(result)}


def _local_check(cfg):
    path = Path(os.path.expanduser(cfg["local_library"]))
    if path.is_symlink():
        return {"name": "本机库", "ok": False, "detail": "本机库路径不能是符号链接"}
    if path.exists():
        if not path.is_dir():
            return {"name": "本机库", "ok": False, "detail": "路径存在但不是目录"}
        if not os.access(path, os.W_OK):
            return {"name": "本机库", "ok": False, "detail": "目录不可写"}
        return {"name": "本机库", "ok": True, "detail": str(path)}
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK):
        return {"name": "本机库", "ok": False, "detail": f"父目录不可写：{parent}"}
    return {"name": "本机库", "ok": True, "detail": f"尚不存在，首次上传时创建：{path}"}


def test_connection(raw):
    """用未保存的表单值做只读检查，不修改任何配置或远端文件。"""
    cfg = normalize(raw, strict=True)
    checks = [_local_check(cfg)]

    connect = _run_ssh(cfg, "printf ok")
    if connect is None or isinstance(connect, OSError) or connect.returncode != 0:
        checks.append({"name": "SSH 连接", "ok": False, "detail": _ssh_error(connect)})
        return {"ok": False, "ssh": False, "target": target_label(cfg), "checks": checks}
    checks.append({"name": "SSH 连接", "ok": True, "detail": target_label(cfg)})

    commands = " ".join(_REMOTE_COMMANDS)
    deps = _run_ssh(cfg, "for c in " + commands + "; do command -v \"$c\" >/dev/null || "
                         "{ printf '%s' \"$c\"; exit 20; }; done; printf ok")
    if deps is None or isinstance(deps, OSError) or deps.returncode != 0:
        missing = "" if deps is None or isinstance(deps, OSError) else (deps.stdout or "").strip()
        detail = f"缺少命令：{missing}" if missing else _ssh_error(deps)
        checks.append({"name": "远端命令", "ok": False, "detail": detail})
    else:
        checks.append({"name": "远端命令", "ok": True, "detail": "find / tar / sha256sum 等可用"})

    checks.append(_path_check(cfg, "远端资料库", cfg["remote_library"]))
    checks.append(_path_check(cfg, "远端项目根", cfg["remote_workshop"]))
    return {
        "ok": all(check["ok"] for check in checks),
        "ssh": True,
        "target": target_label(cfg),
        "checks": checks,
    }
