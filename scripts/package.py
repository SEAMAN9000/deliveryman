#!/usr/bin/env python3
"""package.py — deliveryman 工作流 Step 5-6：可选构建钩子 + 封版。

输入：已经被 sanitize 的交付目录 `<project>/output/deliver/<mode>-<ts>/`。
两档四形态：只有 user·程序形态（`user.entry`）才有构建/启动器动作——
读本次运行记录（`_reports/manifest-<副本目录名>.json`）的 `user_variant` 分派：
  - program                  → 执行 build_cmd → 复制 entry → 生成 RUN.bat / run.sh 启动器
  - document / artifact / dev → 无动作，交付目录本身即"可直接 git 提交、拷走即用"的提交版
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402


def _render_launcher(template_path: Path, entry_rel: str) -> str:
    tpl = template_path.read_text(encoding="utf-8")
    return tpl.replace("{{ENTRY}}", entry_rel)


def build_log_path(target_dir: Path) -> Path:
    """构建日志归档路径：交付目录【同级】的 _logs/<子目录名>.log。
    刻意落在交付目录之外——构建日志可能含本机路径，绝不能进对外的提交版。
    """
    return target_dir.parent / "_logs" / f"{target_dir.name}.log"


def run_build_cmd(project_root: Path, build_cmd: str) -> tuple[int, str]:
    """在源项目根执行 build_cmd，返回 (returncode, log_excerpt)。"""
    try:
        proc = subprocess.run(
            build_cmd,
            shell=True,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=600,
        )
        log = (proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or "")
        return proc.returncode, log[-4000:]
    except subprocess.TimeoutExpired:
        return 124, "build_cmd 超时（>600s）"
    except Exception as e:
        return 1, f"build_cmd 执行异常: {e}"


def copy_entry_and_extras(project_root: Path, target_dir: Path, user_cfg: dict,
                          rules: dict, redactions: list, redact_exclude: list,
                          mode: str = "user") -> list[str]:
    """封版阶段处理 user.entry。两条路径：
      - 预构建 entry（scan 时已存在）：scan 已把它纳入 files_to_copy、sanitize 已复制并 redact。
        此处交付目录里已有该文件——**跳过，绝不用未清洗的原始文件覆盖已清洗版本**。
      - build_cmd 生成的 entry（scan 时不存在）：sanitize 没复制到，此处过滤复制 + fail-closed——
        命中不可绕过必删组（secrets/vcs/...）则不落盘、抛错；文本类过 redaction。
    返回成功处理的相对路径列表。
    """
    copied = []
    entry = user_cfg.get("entry")
    if not entry:
        return copied
    # 词法闸门：entry 必须是项目内相对路径（拒绝绝对/盘符/UNC/.. 上跳）——
    # 在任何 (project_root / entry) 拼接与文件系统探测【之前】断言，防越界写/外连
    if not _lib.is_safe_relative_spec(entry):
        raise IOError(f"user.entry 越界（绝对路径/盘符/UNC/.. 上跳不允许）: {entry}")
    dst = target_dir / entry
    _lib.assert_under(project_root, dst)
    if dst.exists():
        # 预构建路径：sanitize 已复制 redacted 版本，不覆盖。
        copied.append(entry)
        return copied
    # build_cmd 生成路径：scan 时不存在，未经 sanitize 管线——此处过滤复制。
    src = (project_root / entry).resolve()
    if not src.exists():
        raise IOError(f"user.entry 构建后仍不存在: {src}")
    if src.is_dir():
        raise IOError(
            f"user.entry 构建后为目录: {entry} —— program 形态 entry 必须是单个可启动文件。"
            "要带整个产物目录，用 user.extra_include 声明（它们会经 scan 管线过 secret 过滤与清洗）。"
        )
    # fail-closed：build_cmd 产物命中不可绕过必删组（secrets/vcs/tool_caches/...）→ 不落盘、抛错。
    hit = _lib.blacklist_hit(entry, rules, mode, False)
    if hit is not None and hit[0] not in _lib.ENTRY_BYPASSABLE_GROUPS:
        raise IOError(
            f"user.entry 构建产物命中必删组 `{hit[0]}`: {entry} —— fail-closed，未落盘。"
            "请检查 build_cmd 输出，或用 deliver.config.yaml.remove 排除后重打。"
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    if _lib.is_likely_binary(src) or (redact_exclude and _lib.match_any(entry, redact_exclude)):
        shutil.copy2(src, dst)
    else:
        text = src.read_text(encoding="utf-8", errors="replace")
        new_text, _hits = _lib.apply_redactions(text, entry, redactions)
        dst.write_text(new_text, encoding="utf-8", newline="")
    copied.append(entry)
    return copied


def generate_launchers(target_dir: Path, entry_rel: str, skill_dir: Path) -> None:
    """生成 RUN.bat 与 run.sh 放在 target_dir 根。"""
    tdir = skill_dir / "templates"
    bat_tpl = tdir / "RUN.bat.tpl"
    sh_tpl = tdir / "run.sh.tpl"
    bat_path = target_dir / "RUN.bat"
    sh_path = target_dir / "run.sh"
    bat_path.write_text(_render_launcher(bat_tpl, entry_rel.replace("/", "\\")), encoding="utf-8", newline="\r\n")
    sh_path.write_text(_render_launcher(sh_tpl, entry_rel), encoding="utf-8", newline="\n")
    try:
        os.chmod(sh_path, 0o755)
    except OSError:
        # Windows 不支持 chmod，忽略
        pass


def _read_manifest_variant(target_dir: Path):
    """读本次运行记录里的 user_variant；缺失/坏 → None。

    记录在副本目录之外的 _reports/ 下；旧版本生成的副本把它放在副本目录里，一并兼容。
    """
    mp = _lib.manifest_path_for(target_dir)
    if not mp.exists():
        mp = target_dir / "_deliver_manifest.json"
    if not mp.exists():
        return None
    try:
        m = json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        return None
    return m.get("user_variant")


def main(argv=None):
    p = argparse.ArgumentParser(description="deliveryman package + launcher")
    p.add_argument("--project", default=".")
    p.add_argument("--target", required=True, help="已 sanitize 的交付目录 output/deliver/<mode>-<ts>/")
    p.add_argument("--mode", required=True, choices=["dev", "user"])
    p.add_argument("--config", default=None)
    p.add_argument("--skip-build", action="store_true", help="跳过 user.build_cmd 执行（已构建过）")
    args = p.parse_args(argv)


    project_root = Path(args.project).resolve()
    target_dir = Path(args.target).resolve()
    _lib.assert_under(project_root, target_dir)
    if not target_dir.exists():
        raise SystemExit(f"目标目录不存在: {target_dir}")

    # dev：交付目录即提交版，无构建/启动器/打包动作
    if args.mode != "user":
        print(f"TARGET_DIR={target_dir}")
        print("PACKAGED=dir-only（dev 档无构建与启动器；交付目录即提交版）")
        return

    config_path = Path(args.config).resolve() if args.config else None
    config = _lib.load_project_config(project_root, config_path)
    skill_root = _lib.skill_dir()

    # user 档形态分派：以 sanitize 写入交付目录的 user_variant 为准；缺失回退重算 + warning。
    # 只有 program 形态有构建/启动器；document/artifact 形态交付目录即提交版。
    user_variant = _read_manifest_variant(target_dir)
    if user_variant is None:
        user_variant = _lib.resolve_user_variant(config)
        print(f"WARNING: 运行记录缺 user_variant，回退按当前 config 重算 = {user_variant}",
              file=sys.stderr)
    if user_variant != "program":
        print(f"TARGET_DIR={target_dir}")
        print(f"PACKAGED=dir-only（user·{user_variant} 形态无构建与启动器；交付目录即提交版）")
        return

    user_cfg = config.get("user") or {}
    # 1) build_cmd（在源项目根执行）
    if not args.skip_build:
        bc = user_cfg.get("build_cmd")
        if bc:
            print(f"BUILD_CMD_RUNNING: {bc}")
            rc, log = run_build_cmd(project_root, bc)
            # 日志归档到交付目录【外】的 _logs/——可能含本机路径，不进提交版
            log_path = build_log_path(target_dir)
            _lib.assert_under(project_root, log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(log, encoding="utf-8")
            if rc != 0:
                raise SystemExit(f"build_cmd 退出码 {rc}；日志: {log_path}")
    # 2) 复制 entry（预构建已由 sanitize 处理并跳过；build_cmd 产物在此过滤复制 + fail-closed）
    rules = _lib.load_rules()
    redactions = _lib.compile_redactions(rules, config.get("custom_redactions"))
    redact_exclude = config.get("redact_exclude") or []
    copied = copy_entry_and_extras(project_root, target_dir, user_cfg,
                                   rules, redactions, redact_exclude, mode="user")
    print(f"ENTRY_COPIED={','.join(copied)}")
    # 3) 生成启动器
    entry = user_cfg.get("entry")
    if entry:
        generate_launchers(target_dir, entry, skill_root)
        print("LAUNCHERS=RUN.bat,run.sh")

    print(f"TARGET_DIR={target_dir}")
    print("PACKAGED=dir-only（已生成启动器；交付目录即提交版）")


if __name__ == "__main__":
    main()
