#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stop_app.py —— 只停 CMIS 自己的进程，不碰机器上别的 python。

    python packaging/stop_app.py            # 停掉占 5000 端口的服务 + CMIS_Module_Manager.exe
    python packaging/stop_app.py --port 5001

为什么要有这个脚本（2026-09-19）：重建 EXE / 重启 app.py 前曾用
`taskkill /F /IM python.exe /T` 清场 —— 那是按镜像名全机杀 python.exe，
同一台机器上别的 Claude 会话的长跑（REA 的 GUI 验证链、dlweb 的收件箱守护、
auto-continue 的探针）和用户自己的脚本被整棵掐掉，时间戳逐秒吻合，
而且死得无声：TerminateProcess 没有 traceback，受害方要靠排除法才能查到这里。

CMIS 自己的进程只有两种：监听 127.0.0.1:5000 的 Flask 服务（python app.py，
或打包后的 CMIS_Module_Manager.exe）。所以清场只需要：
  1. 谁在 LISTENING 5000 → 按 PID 杀（连子进程一起）
  2. 名字叫 CMIS_Module_Manager.exe 的进程 → 按镜像名杀（那是我们自己的 exe，别人不会叫这个名）
两条都不会碰到 python.exe 这个通用镜像名。
"""
from __future__ import annotations

import argparse
import subprocess
import sys


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except Exception as exc:  # noqa: BLE001
        return f"<{type(exc).__name__}: {exc}>"


def pids_listening(port: int) -> list[int]:
    """netstat 里 LISTENING 在该端口的 PID（去重，排除 0）。"""
    out = _run(["netstat", "-ano"])
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(f":{port}"):
                try:
                    pid = int(parts[4])
                except ValueError:
                    continue
                if pid and pid not in pids:
                    pids.append(pid)
    return pids


def kill_pid_tree(pid: int) -> str:
    return _run(["taskkill", "/F", "/T", "/PID", str(pid)]).strip()


def kill_image(name: str) -> str:
    return _run(["taskkill", "/F", "/T", "/IM", name]).strip()


def configured_port() -> int:
    """app.py 实际会用的端口：CMIS_PORT > 仓库根目录的 cmis_settings.json > 5000。

    端口可以在界面里改，改完存进 cmis_settings.json。写死 5000 的话，
    改过端口的开发实例就停不掉了——而这个脚本是唯一允许的清场方式。
    """
    import json
    import os

    def valid(v):
        try:
            v = int(str(v).strip())
        except (TypeError, ValueError):
            return None
        return v if 1024 <= v <= 65535 else None

    env = valid(os.environ.get("CMIS_PORT", ""))
    if env:
        return env
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "cmis_settings.json")
    try:
        with open(path, encoding="utf-8") as fh:
            saved = valid((json.load(fh) or {}).get("port"))
    except (OSError, ValueError, AttributeError):
        saved = None
    return saved or 5000


def main() -> int:
    ap = argparse.ArgumentParser(description="停掉 CMIS 自己的服务进程（按端口 PID + 自家 exe 名），不按 python.exe 镜像名杀")
    ap.add_argument("--port", type=int, default=configured_port())
    ap.add_argument("--exe", default="CMIS_Module_Manager.exe")
    args = ap.parse_args()
    if sys.platform != "win32":
        print("stop_app.py 只针对 Windows（netstat/taskkill）")
        return 2
    n = 0
    for pid in pids_listening(args.port):
        print(f"port {args.port} LISTENING pid={pid} -> {kill_pid_tree(pid)}")
        n += 1
    r = kill_image(args.exe)
    if "SUCCESS" in r.upper():
        print(f"{args.exe} -> {r}")
        n += 1
    print(f"stopped {n} target(s); python.exe by image name was NOT touched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
