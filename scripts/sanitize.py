#!/usr/bin/env python3
"""sanitize.py — deliveryman 工作流 Step 4：应用变更。

读取 scan.py 产出的 JSON 计划，把命中"将复制"的文件复制到 output/deliver/<mode>-<ts>/，
对文本类文件应用 redactions。所有写入目标必须在 <project>/output/deliver/ 之下。

完成后追加 .gitignore（若首次）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402
import _office  # noqa: E402


# 受众公开度排序：dev（最私密）< user。
# 合并接受集时 mode 取"更公开者"：dev 接受项在 user 档重审通过后升格为 user，下次同档不再重复询问。
_MODE_RANK = {"dev": 0, "user": 2}


def _more_public_mode(prev_mode, cur_mode: str) -> str:
    if not prev_mode:
        return cur_mode
    return prev_mode if _MODE_RANK.get(prev_mode, 0) >= _MODE_RANK.get(cur_mode, 0) else cur_mode


def _persist_decisions(project_root: Path, plan: dict, mode: str, keep_ai_context: bool) -> str:
    """裁决固化：只在用户 y 之后（即本函数所在的 sanitize 阶段）落笔。
    常规运行对接受集做并集合并；--fresh（全部重审）那次整体替换——这是撤回接受的唯一通道。
    写失败不致命：交付已完成，缓存写失败不能反噬。返回 saved / save-failed 供打印。
    """
    try:
        triples = plan.get("detect_triples") or []
        fresh = bool(plan.get("fresh"))
        old, status = _lib.load_state(project_root)
        merged = {}
        if status == "ok" and not fresh:
            for e in old.get("accepted_detect") or []:
                if isinstance(e, dict):
                    k = (e.get("file"), e.get("rule"), e.get("value"))
                    if None not in k:
                        merged[k] = e
        stamp = _lib.now_stamp()
        for t in triples:
            if not isinstance(t, dict):
                continue
            k = (t.get("file"), t.get("rule"), t.get("value"))
            if None in k:
                continue
            prev = merged.get(k) or {}
            merged[k] = {
                "file": k[0], "rule": k[1], "value": k[2],
                "count": t.get("count"),
                "first_accepted": prev.get("first_accepted") or stamp,
                # 升格：取更公开者（dev<user）——修"dev 接受项在 user 下永不收敛"bug（§1.2）
                "mode": _more_public_mode(prev.get("mode"), mode),
            }
        modes_cfg = {}
        if status == "ok" and not fresh:
            modes_cfg = dict(old.get("modes") or {})
        modes_cfg[mode] = {"keep_ai_context": bool(keep_ai_context)}
        new_state = {
            "updated_at": stamp,
            "last_mode": mode,
            "modes": modes_cfg,
            "accepted_detect": sorted(merged.values(),
                                      key=lambda e: (e["file"], e["rule"], e["value"])),
        }
        # 形态固化：mode==user 且 source != config（config 是权威、不需固化）。
        # provenance：inferred（对推测按 y）→ inferred_ratified；user_chosen 原样；
        # carried（沿用）→ 保留 state 里原有 source。记证据指纹供下次漂移检测。
        variant_val = plan.get("user_variant")
        variant_src = plan.get("variant_source")
        variant_fp = plan.get("variant_fingerprint")
        if mode == "user" and variant_val and variant_src and variant_src != "config":
            if variant_src == "inferred":
                persisted_src = "inferred_ratified"
            elif variant_src == "carried":
                prev = (old.get("variant") or {}).get("source") if status == "ok" else None
                persisted_src = prev or "inferred_ratified"
            else:
                persisted_src = variant_src  # user_chosen
            new_state["variant"] = {
                "value": variant_val,
                "source": persisted_src,
                "fingerprint": variant_fp,
                "ratified_at": stamp,
            }
        return "saved" if _lib.save_state(project_root, new_state) else "save-failed"
    except Exception:
        return "save-failed"


def _write_suggested_config(project_root: Path, plan: dict, config: dict):
    """会话级口头 keep/remove 增量的持久化引导：生成 config 草稿供用户手动采纳。
    永不代写项目根（不变式 1）——草稿落在 output/deliver/ 合法写入区。
    """
    so = plan.get("session_overrides") or {}
    add_keep = so.get("keep") or []
    add_remove = so.get("remove") or []
    if not add_keep and not add_remove:
        return None
    keep = list(config.get("keep") or [])
    keep += [k for k in add_keep if k not in keep]
    remove = list(config.get("remove") or [])
    remove += [r for r in add_remove if r not in remove]
    lines = [
        "# deliveryman 生成的配置草稿（不会自动生效）",
        "# 本文件在 output/deliver/ 下：已被 .gitignore 忽略、永不进交付物",
        "# 采纳方法：把下面的字段手动合并进项目根的 deliver.config.yaml",
        "# 注意：keep/remove 含你本次会话的口头增量，可能带本机路径字面量——采纳前过目",
        "",
    ]
    if keep:
        lines.append("keep:")
        lines.extend(f'  - "{k}"' for k in keep)
    if remove:
        lines.append("remove:")
        lines.extend(f'  - "{r}"' for r in remove)
    p = _lib.deliver_root(project_root) / "deliver.config.suggested.yaml"
    _lib.assert_under(project_root, p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def run(project_root: Path, plan_json: Path, mode: str, keep_ai_context: bool, config: dict, rules: dict) -> dict:
    plan = json.loads(plan_json.read_text(encoding="utf-8"))
    # fail-closed：plan 结构版本高于本版支持 → 中止（state 可丢，plan 不可误读）；
    # 无版本字段的旧 plan 按 v1 兼容（只需 files_to_copy）
    plan_schema = plan.get("schema_version", 1)
    if not isinstance(plan_schema, int) or plan_schema > 3:
        raise SystemExit(f"scan JSON schema_version={plan_schema} 高于本版支持（<=3）——请用配套 scan.py 重扫")
    files_to_copy = plan["files_to_copy"]
    plan_output_root = plan.get("output_root")  # 仅作为信息记录

    # 形态跨脚本闭环：以 plan.user_variant 为准，但重算比对——
    # 若 config 在扫描后被改（形态漂移），fail-fast 逼用户重扫，绝不用旧 plan 配新 config。
    # v2 及更早的 plan 无 user_variant 字段（office 恒关）——回退当前重算，不 fail。
    user_variant = None
    if mode == "user":
        plan_variant = plan.get("user_variant")
        config_variant = _lib.resolve_user_variant(config)
        ucfg = config.get("user") or {}
        config_declares = bool(ucfg.get("entry") or ucfg.get("artifacts"))
        # config 漂移检测：仅当 config【实际声明】形态时——config 声明 ≠ plan 记录 = config 扫后被改。
        # 推测/沿用形态（config 无声明）：信任 plan.user_variant（scan 已用推测+state 决策），
        # 不能拿 resolve_user_variant(config={})=document 去比对推测的 artifact（那会永远误报）。
        if config_declares and plan_variant is not None and plan_variant != config_variant:
            raise SystemExit(
                f"user_variant 不一致：plan={plan_variant} 但当前 config 重算={config_variant}——"
                "config 在扫描后被修改，请用 scan.py 重扫后再 sanitize"
            )
        user_variant = plan_variant if plan_variant is not None else config_variant

    ts = _lib.now_stamp()
    target_dir = _lib.deliver_root(project_root) / f"{mode}-{ts}"
    _lib.assert_under(project_root, target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    redactions = _lib.compile_redactions(rules, config.get("custom_redactions"))
    redact_exclude = config.get("redact_exclude") or []

    # office_clean = scan 承诺清洗集（fail-closed 只限此集；缺省=不清洗）。
    # v2 旧 plan 无此字段 → 空集 → office 改写恒关（未承诺未验证的文件绝不动）。
    office_clean_set = set(plan.get("office_clean") or [])
    office_rules = rules.get("office") or {}
    office_cleaned = 0
    office_failed = []      # 承诺集内五道验证失败 → fail-closed（该文件不落盘）

    total_redact_hits = 0
    redact_files = 0
    bytes_written = 0
    # 运行记录只放 rel 路径 + 异常类名（不含绝对路径）；
    # 完整错误文本（str(e) 内嵌绝对路径）落交付目录【外】的 _reports/errors-<ts>.log。
    errors = []          # -> manifest：[{"file": rel, "error": "<异常类名>"}]
    error_details = []   # -> errors-<ts>.log：完整文本

    def _record_error(kind: str, rel: str, e: Exception):
        errors.append({"file": rel, "error": type(e).__name__})
        error_details.append(f"[{kind}] {rel}: {e!r}")

    for entry in files_to_copy:
        # 兼容旧 plan：长度 2 是 (rel, size)；长度 3 是 (src_rel, dst_rel, size)
        if len(entry) == 2:
            src_rel, dst_rel, _size = entry[0], entry[0], entry[1]
        else:
            src_rel, dst_rel, _size = entry
        src = project_root / src_rel
        dst = target_dir / dst_rel
        _lib.assert_under(project_root, dst)
        rel = src_rel  # 用于 redaction applies_to 匹配（基于源路径模式）
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            # 承诺清洗集 → OOXML docProps L1 清洗；五道验证失败 fail-closed（该文件不落盘）
            if src_rel in office_clean_set:
                ok, reasons = _office.rewrite_ooxml(src, dst, office_rules)
                if ok:
                    office_cleaned += 1
                    try:
                        bytes_written += dst.stat().st_size
                    except OSError:
                        pass
                else:
                    # fail-closed 仅限承诺集：不落盘、进 errors、完成报告列出并给活路（redact_exclude）
                    office_failed.append(rel)
                    errors.append({"file": rel, "error": "OfficeCleanFailed"})
                    error_details.append(f"[office清洗失败·fail-closed] {rel}: {'; '.join(reasons)}")
                continue
            # redact_exclude 命中：原样复制，跳过全部内容级替换（与 scan.py 的豁免语义一致）
            if _lib.is_likely_binary(src) or (redact_exclude and _lib.match_any(rel, redact_exclude)):
                shutil.copy2(src, dst)
                try:
                    bytes_written += dst.stat().st_size
                except OSError:
                    pass
            else:
                try:
                    text = src.read_text(encoding="utf-8", errors="replace")
                except Exception as e:
                    _record_error("读取失败", rel, e)
                    shutil.copy2(src, dst)
                    continue
                new_text, hits = _lib.apply_redactions(text, rel, redactions)
                if hits:
                    redact_files += 1
                    for _, n in hits:
                        total_redact_hits += n
                dst.write_text(new_text, encoding="utf-8", newline="")
                bytes_written += len(new_text.encode("utf-8"))
        except Exception as e:
            _record_error("写入失败", rel, e)

    # 完整错误文本落交付目录【外】的 _reports/errors-<ts>.log（含绝对路径，不进对外交付物）
    error_log = None
    if error_details:
        rep_dir = _lib.deliver_root(project_root) / "_reports"
        rep_dir.mkdir(parents=True, exist_ok=True)
        error_log = rep_dir / f"errors-{ts}.log"
        _lib.assert_under(project_root, error_log)
        error_log.write_text("\n".join(error_details) + "\n", encoding="utf-8")

    # 本次运行的记录。写在副本目录【之外】的 _reports/ 下：副本里只放用户要交付的文件，
    # 不夹带工具自己的记录（来源项目文件夹名等属于本机信息）。package.py 从 _reports/ 读它。
    manifest = {
        "deliveryman_version": "0.7.2",
        "mode": mode,
        "user_variant": user_variant,   # package.py 据此分派（program=启动器；document/artifact=dir-only）
        "timestamp": ts,
        "source_project_name": project_root.name,   # 只存目录名，不存绝对路径
        "output_root": plan_output_root,
        "file_count": len(files_to_copy),
        "bytes_written": bytes_written,
        "redaction_files": redact_files,
        "redaction_hits": total_redact_hits,
        "office_cleaned": office_cleaned,   # docProps L1 清洗成功的 OOXML 数
        "keep_ai_context": keep_ai_context,
        "errors": errors,
    }
    manifest_path = _lib.manifest_path_for(target_dir)
    _lib.assert_under(project_root, manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # 裁决固化 + 配置草稿——都发生在用户 y 之后、都写在 output/deliver/ 合法区内
    state_result = _persist_decisions(project_root, plan, mode, keep_ai_context)
    suggested = _write_suggested_config(project_root, plan, config)

    return {
        "target_dir": target_dir,
        "manifest": manifest,
        "state": state_result,
        "suggested_config": suggested,
        "error_log": error_log,
        "office_failed": office_failed,   # fail-closed 未落盘的 OOXML（完成报告列出 + 给活路）
    }


def _require_confirmation(project_root: Path, plan_json: Path, mode: str, confirmed: str) -> str:
    """生成副本前的闸门：必须带上用户确认时的原话，且计划必须是该档最近一次扫描的结果。

    脚本无法知道用户是否真的看过报告，但可以做到两件事：没有确认原话就拒绝执行（漏掉
    "等用户确认"这一步时会在这里被拦下）；计划过期就拒绝执行（用户看到的是新报告，
    执行的却是旧计划）。确认原话会记入 _reports/confirmations.log 备查。
    """
    words = (confirmed or "").strip()
    if not words:
        raise SystemExit(
            "还没有用户确认，未生成任何文件。\n"
            "先把扫描输出的一览表和待裁决条目给用户看；得到明确同意后，"
            "加上 --confirmed \"<用户确认时的原话>\" 重新调用。"
        )
    try:
        plan = json.loads(plan_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"读不了扫描计划 {plan_json}: {e}")
    plan_mode = plan.get("mode")
    if plan_mode and plan_mode != mode:
        raise SystemExit(f"扫描计划是 {plan_mode} 档的，但本次要生成 {mode} 档——请用对应档的计划，或重新扫描。")
    plan_root = plan.get("project_root")
    if plan_root and Path(plan_root).resolve() != project_root:
        raise SystemExit("扫描计划属于另一个项目，未生成任何文件——请在本项目重新扫描。")
    mine = plan_json.stat().st_mtime
    for other in plan_json.parent.glob("scan-*.json"):
        if other == plan_json or other.stat().st_mtime <= mine:
            continue
        try:
            other_mode = json.loads(other.read_text(encoding="utf-8")).get("mode")
        except (OSError, ValueError):
            continue
        if other_mode == mode:
            raise SystemExit(
                f"这份计划不是 {mode} 档最近一次扫描的结果（更新的是 {other.name}），未生成任何文件。\n"
                "用户确认的应当是最新那份报告——请改用最新的计划，或重新扫描后再请用户确认。"
            )
    return words


def _log_confirmation(project_root: Path, plan_json: Path, mode: str, words: str, target_dir: Path) -> None:
    log = _lib.deliver_root(project_root) / "_reports" / "confirmations.log"
    _lib.assert_under(project_root, log)
    log.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"time": _lib.now_stamp(), "mode": mode, "plan": plan_json.name,
                       "target": target_dir.name, "user_said": words}, ensure_ascii=False)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def main(argv=None):
    p = argparse.ArgumentParser(description="deliveryman sanitize+copy")
    p.add_argument("--confirmed", default=None,
                   help="用户确认时的原话（必填）；没有它不生成任何文件")
    p.add_argument("--project", default=".")
    p.add_argument("--mode", required=True, choices=["dev", "user"])
    p.add_argument("--scan-json", required=True, help="scan.py 输出的 JSON 计划路径")
    p.add_argument("--config", default=None)
    p.add_argument("--keep-ai-context", action="store_true")
    args = p.parse_args(argv)


    project_root = Path(args.project).resolve()
    config_path = Path(args.config).resolve() if args.config else None
    plan_json = Path(args.scan_json).resolve()

    words = _require_confirmation(project_root, plan_json, args.mode, args.confirmed)

    rules = _lib.load_rules()
    config = _lib.load_project_config(project_root, config_path)

    result = run(project_root, plan_json, args.mode, args.keep_ai_context, config, rules)
    _log_confirmation(project_root, plan_json, args.mode, words, result["target_dir"])

    # .gitignore 追加（唯一允许的源项目写入）
    appended = _lib.ensure_gitignore_has_deliver(project_root)

    print(f"TARGET_DIR={result['target_dir']}")
    print(f"FILE_COUNT={result['manifest']['file_count']}")
    print(f"USER_VARIANT={result['manifest'].get('user_variant')}")
    print(f"BYTES_WRITTEN={result['manifest']['bytes_written']}")
    print(f"REDACT_HITS={result['manifest']['redaction_hits']}")
    print(f"OFFICE_CLEANED={result['manifest'].get('office_cleaned', 0)}")
    if result.get("office_failed"):
        print(f"OFFICE_FAIL_CLOSED={len(result['office_failed'])}"
              "（docProps 清洗五道验证未过，该文件未落盘——加入 deliver.config.yaml.redact_exclude "
              "可跳过清洗、原样交付）", file=sys.stderr)
        for rel in result["office_failed"][:20]:
            print(f"  - {rel}", file=sys.stderr)
    print(f"GITIGNORE_APPENDED={'yes' if appended else 'no'}")
    print(f"STATE={result['state']}")
    if result.get("suggested_config"):
        print(f"SUGGESTED_CONFIG={result['suggested_config']}")
    if result["manifest"]["errors"]:
        print(f"ERRORS={len(result['manifest']['errors'])}", file=sys.stderr)
        for e in result["manifest"]["errors"][:20]:
            print(f"  - {e['file']} ({e['error']})", file=sys.stderr)
        if result.get("error_log"):
            print(f"ERROR_LOG={result['error_log']}（完整错误文本，含绝对路径，未进交付物）", file=sys.stderr)


if __name__ == "__main__":
    main()
