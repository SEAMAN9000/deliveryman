#!/usr/bin/env python3
"""scan.py — deliveryman 工作流 Step 2：dry-run 扫描。

只读地遍历源项目，按 mode 决定哪些保留 / 哪些删 / 哪些做内容级 redaction，
把结果写成 markdown 报告到 <project>/output/deliver/_reports/scan-<ts>.md。

不修改源项目除 .gitignore 之外的任何文件（.gitignore 的追加由 sanitize.py Step 5 做，不在 scan 阶段做）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 让本目录可 import 同级模块
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402
import _office  # noqa: E402

# PDF/图片：本工具不清洗，只在报告标注边界
_PDF_EXTS = {".pdf"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".webp", ".heic", ".bmp"}


def build_report(project_root: Path, mode: str, keep_ai_context: bool, config: dict, rules: dict,
                 state: dict | None = None, session_keep=None, session_remove=None,
                 keep_ai_context_source: str = "cli", fresh: bool = False,
                 explicit_variant: str | None = None) -> dict:
    """遍历项目，产出结构化报告 dict（供 markdown 渲染与 sanitize 复用）。

    接收裁决固化 state（只影响呈现分桶，绝不改变扫描范围）与会话级
    keep/remove 增量（口头"加 X / 去 Y"的落地通道；跨次持久靠用户写 deliver.config.yaml）。
    """
    redactions = _lib.compile_redactions(rules, config.get("custom_redactions"))
    # 第一梯队 detect_only：真实项目根路径字面匹配（零误报），排在宽松正则之前
    redactions = _lib.project_path_detect_rules(project_root) + redactions

    # 会话增量与 config 合并；同路径 remove 胜 keep（沿用下方判序：remove 先判）
    user_keep = list(config.get("keep") or []) + list(session_keep or [])
    user_remove = list(config.get("remove") or []) + list(session_remove or [])
    output_root = config.get("output_root")
    extras_from_root = config.get("extras_from_root")
    if extras_from_root is None:
        extras_from_root = _lib.DEFAULT_EXTRAS_FROM_ROOT

    # output_root 预检：路径必须存在
    if output_root:
        or_path = (project_root / output_root).resolve()
        if not or_path.exists():
            raise SystemExit(
                f"output_root 路径不存在: {output_root}（解析为 {or_path}）"
            )
        if not or_path.is_dir():
            raise SystemExit(f"output_root 必须是目录: {output_root}")

    files_to_copy = []     # [(src_rel, dst_rel, size)]
    files_to_remove = {}   # group_name -> [rel_path, ...]
    optional_keep_hits = []  # [rel_path, ...]
    ai_kept_hits = []      # keep_ai_context=true 时"保留中"的 ai_scaffolding 命中（report-only，防盲飞）
    redaction_hits = []    # [(rel_path, rule_name, count, sample, risk)] —— 仅 action=redact
    detect_hits = []       # 同结构 —— action=detect_only，只报告不替换
    detect_values = []     # [{file, rule, value(规范化), count}] —— 供裁决固化分桶
    redact_exclude = config.get("redact_exclude") or []
    redact_exempt = []     # 命中 redact_exclude 的文件——原样复制，跳过全部内容级规则
    warnings = []          # [str]

    # ---- Office 隐私处理收集态 ----
    # office_metadata: clean（默认，L1 改写）| report_only（项目级只报不改——L1 关、L2 仍报）
    office_report_only_mode = (config.get("office_metadata") == "report_only")
    office_clean = []          # 承诺清洗集（rel）：魔数=PK 的 OOXML 且非 report_only 才进
    office_findings = []       # [{file, part, kind, label, count, sample, degraded}] —— L2 只报不改
    office_uncleanable = []    # [{file, reason}] —— OLE（加密/旧格式）或 unknown 魔数：原样复制
    office_report_only_files = []   # report_only 下 L1 关但仍 L2 的 OOXML
    office_degraded_parts = 0  # 降级 part 数
    office_parse_errors = []   # [rel] 坏容器：原样复制 + 警告
    pdf_count = 0              # 交付含 PDF 数（报告里提示未清洗）
    image_count = 0            # 交付含图片数（EXIF 边界提示）

    # mode 必需配置检查
    mode_def = rules["modes"]["modes"].get(mode)
    if mode_def is None:
        raise SystemExit(f"未知 mode: {mode}")
    for req in mode_def.get("requires_config") or []:
        # 支持点路径如 "user.entry"
        cur = config
        ok = True
        for part in req.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if not ok:
            warnings.append(f"⚠️ mode={mode} 必需 `deliver.config.yaml.{req}` 但未声明")

    # user 档形态：先由 config 推导 program/document/artifact 形态。
    # entry/artifacts 是 include 通道（不是 keep 通道）——blacklist 命中永远优先，
    # artifacts 不能复活 secrets（blacklist_hit 在 include 判定之前就 continue）。
    user_variant = None
    skill_info = None
    extra_include = []
    artifacts = []
    inference = None
    variant_source = None
    if mode == "user":
        # 形态决策：config 声明 > state（证据指纹匹配才沿用）> 推测。
        inference = _lib.infer_user_variant(project_root, config)
        ucfg = config.get("user") or {}
        extra_include = ucfg.get("extra_include") or []
        st_variant = (state or {}).get("variant") or {}
        st_fp = st_variant.get("fingerprint")
        if explicit_variant == "skill" and not inference.get("skill"):
            found = inference.get("skill_dirs") or []
            if len(found) > 1:
                why = f"项目里有 {len(found)} 个 skill（{'、'.join(found)}），不知道要发哪一个"
            elif inference["source"] == "config":
                why = "配置里已声明了 user.entry 或 user.artifacts"
            else:
                why = "没有找到带 name 字段的 SKILL.md，或 skill 之外还有成品文件"
            raise SystemExit(
                f"无法按 skill 形态打包：{why}。请在 deliver.config.yaml 的 user.artifacts 里点名要带的文件。"
            )
        if explicit_variant:
            # 用户当场拍板"改成 X"（CLI 最高优先级）→ user_chosen
            user_variant = explicit_variant
            variant_source = "user_chosen"
            artifacts = (inference["artifact_candidates"]
                         if explicit_variant == "artifact" and inference["source"] == "inferred"
                         else (ucfg.get("artifacts") or []))
        elif inference["source"] == "config":
            user_variant = inference["variant"]
            variant_source = "config"
            artifacts = ucfg.get("artifacts") or []
        elif st_fp and st_fp == inference["fingerprint"]:
            # 指纹匹配 → 沿用上次拍板（不重问）
            user_variant = st_variant.get("value") or inference["variant"]
            variant_source = "carried"
            artifacts = inference["artifact_candidates"] if user_variant == "artifact" else (ucfg.get("artifacts") or [])
        else:
            # 推测（含指纹漂移：state 有 variant 但指纹不符 → 重问）
            user_variant = inference["variant"]
            variant_source = "inferred"
            if st_variant and st_fp != inference["fingerprint"]:
                warnings.append(
                    f"⚠️ 形态可能已漂移：上次拍板为 `{st_variant.get('value')}`，但项目结构已变"
                    "（证据指纹不符）——本次已**重新推测并请你重新拍板**，不沿用旧形态。"
                )
            artifacts = inference["artifact_candidates"] if user_variant == "artifact" else (ucfg.get("artifacts") or [])
        if user_variant == "skill" and not inference.get("skill"):
            # 沿用的形态是 skill，但现在认不出唯一的 skill 了：退回推测结果并提醒
            warnings.append("⚠️ 上次确认的形态是 skill，但这次没有认出唯一的 skill 文件夹——已改按推测形态处理，请核对。")
            user_variant = inference["variant"]
            variant_source = "inferred"
        if user_variant == "skill":
            skill_info = inference["skill"]
            # skill 不在项目根时，把 skill 文件夹提升为副本的根（配置已声明 output_root 时以配置为准）
            if skill_info["dir"] and not output_root:
                output_root = skill_info["dir"]
                if config.get("extras_from_root") is None:
                    extras_from_root = _lib.SKILL_FACADE_GLOBS
        elif len(inference.get("skill_dirs") or []) > 1 and inference["source"] != "config":
            warnings.append(
                f"⚠️ 项目里有 {len(inference['skill_dirs'])} 个 skill（{'、'.join(inference['skill_dirs'])}），"
                "没有自动按 skill 形态处理。要发其中一个，请在 deliver.config.yaml 的 user.artifacts 里点名它的文件。"
            )
        # entry 与 artifacts 的每个值过词法闸门（拒绝绝对路径/盘符/UNC/..）——越界 fail-fast
        entry_val = ucfg.get("entry")
        if entry_val and not _lib.is_safe_relative_spec(entry_val):
            raise SystemExit(
                f"user.entry 越界（绝对路径/盘符/UNC/.. 上跳不允许）: {entry_val} —— 必须是源项目内相对路径"
            )
        for a in artifacts:
            if not _lib.is_safe_relative_spec(a):
                raise SystemExit(
                    f"user.artifacts 条目越界: {a} —— 必须是源项目内相对路径/glob，不能绝对或 .. 上跳"
                )
        # program 形态 entry 存在性 + 目录护栏（词法闸门已过，可安全 stat）。
        # 早失败前移：原 package.py Step 6 才报的"entry 不存在"移到 scan 阶段，避免留半成品交付目录。
        if entry_val:
            entry_abs = project_root / entry_val
            has_build_cmd = bool(ucfg.get("build_cmd"))
            if entry_abs.is_dir():
                raise SystemExit(
                    f"user.entry 指向目录: {entry_val} —— program 形态 entry 必须是单个可启动文件"
                    "（如 dist/app.exe、index.html）。要连同整个目录一起交付，把 entry 指向其中的启动"
                    "文件，并用 user.extra_include 声明其余产物——它们会一并过 secret 过滤与内容清洗。"
                )
            if not entry_abs.exists() and not has_build_cmd:
                raise SystemExit(
                    f"user.entry 不存在且无 build_cmd 生成它: {entry_val} —— 请修正路径，"
                    "或声明 user.build_cmd（构建后生成），或改用 document/artifact 形态。"
                )

    # user·程序形态"回带集"：entry 指向的文件（或目录内文件）+ program 形态的 extra_include。
    # 回带集里的文件绕过 build_artifacts 必删组（entry 机制的设计职责），但命中 secrets/vcs 等
    # 其余必删组一律强制剔除；文本类照常过 redaction。见 _lib.ENTRY_BYPASSABLE_GROUPS。
    is_program = (mode == "user" and user_variant == "program")
    program_entry = (config.get("user") or {}).get("entry") if is_program else None
    program_extra = extra_include if is_program else []
    entry_recovered = []      # [rel] 命中 build_artifacts 但经 entry 回带（报告单列，已过清洗）
    entry_forced_out = []     # [(rel, group)] entry 携带但命中不可绕过必删组，强制剔除（报告红字）


    skipped_dirs = []          # 整个跳过、没有逐个扫描的文件夹（依赖、缓存、版本库、以前生成的副本）
    for ap, rel in _lib.walk_project(project_root, skipped_dirs):
        # program 形态回带集判定（entry 覆盖 或 program 形态 extra_include 命中）
        recovered = is_program and (
            _lib.entry_covers(rel, program_entry) or _lib.match_any(rel, program_extra)
        )
        # 用户显式 remove 覆盖
        if _lib.match_any(rel, user_remove):
            files_to_remove.setdefault("user_remove", []).append(rel)
            continue
        # 用户显式 keep 覆盖（绕过 blacklist）
        user_keeps = _lib.match_any(rel, user_keep)
        if user_keeps:
            # keep 覆盖必删组必须显式标黄（config 模板早有此承诺）——防"常驻 keep 例外"静默复活 secret
            raw_hit = _lib.blacklist_hit(rel, rules, mode, False)
            if raw_hit is not None and not raw_hit[1]:
                warnings.append(f"⚠️ keep 覆盖了必删组 `{raw_hit[0]}`: {rel} —— 请确认它不含真实敏感内容")
        else:
            hit = _lib.blacklist_hit(rel, rules, mode, False)
            if hit is not None:
                gname, optional = hit
                if gname == "ai_scaffolding" and keep_ai_context:
                    # 保留中但必须可见（report-only 旁路，不改复制决策）——修"开过 keep 后永久盲飞"
                    ai_kept_hits.append(rel)
                elif recovered and gname in _lib.ENTRY_BYPASSABLE_GROUPS:
                    # entry 回带：绕过 build_artifacts，落到下方 include（included 强制 True）+ 过 redaction
                    entry_recovered.append(rel)
                elif recovered:
                    # entry 携带但命中不可绕过必删组（secrets/vcs/tool_caches/...）→ 强制剔除、永不回带
                    files_to_remove.setdefault(gname, []).append(rel)
                    entry_forced_out.append((rel, gname))
                    continue
                else:
                    files_to_remove.setdefault(gname, []).append(rel)
                    if optional:
                        optional_keep_hits.append(rel)
                    continue
        # 决定是否 include。回带集文件一律 include（已显式声明为交付主体）；
        # artifact 形态：整套绕过 mode include，include = user.artifacts；
        # program/document 形态：mode include（收紧的门面清单）+ artifacts 叠加 + extra_include。
        if recovered:
            included = True
        elif mode == "user" and user_variant == "skill":
            included = (
                user_keeps
                or _lib.in_skill_delivery(rel, skill_info["dir"], project_root)
                or _lib.match_any(rel, artifacts)
                or _lib.match_any(rel, extra_include)
            )
        elif mode == "user" and user_variant == "artifact":
            included = (
                user_keeps
                or _lib.match_any(rel, artifacts)
                or _lib.match_any(rel, extra_include)
            )
        else:
            included = (
                user_keeps
                or _lib.mode_include_hit(rel, rules, mode)
                or _lib.match_any(rel, artifacts)
                or _lib.match_any(rel, extra_include)
            )
        if not included:
            files_to_remove.setdefault("not_in_mode_include", []).append(rel)
            continue
        # output_root rebase
        if output_root:
            dst_rel = _lib.rebase_path(rel, output_root)
            if dst_rel is None:
                if _lib.is_extra_from_root(rel, extras_from_root):
                    dst_rel = rel  # 顶层门面文件原位保留
                else:
                    files_to_remove.setdefault("outside_output_root", []).append(rel)
                    continue
        else:
            dst_rel = rel
        # include：评估 redaction
        try:
            size = ap.stat().st_size
        except OSError:
            size = -1
        files_to_copy.append((rel, dst_rel, size))

        ext = ap.suffix.lower()
        if ext in _PDF_EXTS:
            pdf_count += 1
        elif ext in _IMAGE_EXTS:
            image_count += 1

        if _lib.is_likely_binary(ap):
            # ---- Office 文件按魔数预分类 + L2 内部痕迹检测（挂 binary 分支内，continue 之前）----
            if ext in _office.OOXML_EXTS or ext in _office.OLE_OFFICE_EXTS:
                # redact_exclude 命中 → L1/L2 一并豁免（沿既有语义，报告单列）
                if redact_exclude and _lib.match_any(rel, redact_exclude):
                    redact_exempt.append(rel)
                    continue
                kind = _office.office_container_kind(ap)
                if ext in _office.OOXML_EXTS and kind == "ooxml":
                    # 可清洗 OOXML：clean 模式进承诺集；report_only 只挂 L2 不承诺 L1
                    if office_report_only_mode:
                        office_report_only_files.append(rel)
                    else:
                        office_clean.append(rel)
                    ins = _office.inspect_ooxml(ap)
                    for f in ins["findings"]:
                        office_findings.append(dict(f, file=rel))
                    office_degraded_parts += len(ins["degraded"])
                    if ins["parse_errors"]:
                        office_parse_errors.append(rel)
                else:
                    # OLE（加密 OOXML 或旧 .doc/.xls/.ppt）或 unknown 魔数 → 不可清洗，原样复制
                    if kind == "ole":
                        reason = "encrypted" if ext in _office.OOXML_EXTS else "legacy"
                    else:
                        reason = "unknown"
                    office_uncleanable.append({"file": rel, "reason": reason})
            continue
        if redact_exclude and _lib.match_any(rel, redact_exclude):
            redact_exempt.append(rel)
            continue
        try:
            text = ap.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # 跳过过大的文本（>2MB）避免 dry-run 太慢
        if len(text) > 2_000_000:
            warnings.append(f"⚠️ 跳过 redaction 扫描（>2MB）: {rel}")
            continue
        # 替换类规则按生成副本时的同一顺序，逐条作用在"已替换过的文本"上计数：
        # 一段内容被前一条规则替换后不会再被后一条重复计数，报告的处数与实际替换处数一致。
        # 只检测不替换的规则仍看原文。
        work = text
        for r in redactions:
            if not _lib.redaction_applies(r, rel):
                continue
            is_detect = r.get("action", "redact") == "detect_only"
            src_text = text if is_detect else work
            matches = list(r["regex"].finditer(src_text))
            if not matches:
                continue
            sample = matches[0]
            ctx_start = max(0, sample.start() - 20)
            ctx_end = min(len(src_text), sample.end() + 20)
            sample_str = src_text[ctx_start:ctx_end].replace("\n", "\\n")
            if not is_detect:
                work = r["regex"].sub(r["replacement"], work)
            row = (rel, r["name"], len(matches), sample_str, r["risk"])
            if r.get("action", "redact") == "detect_only":
                detect_hits.append(row)
                # 采集去重后的规范化匹配值——裁决固化按 文件×规则×值 三元组折叠
                by_val = {}
                for m in matches:
                    v = _lib.normalize_detect_value(m.group(0))
                    by_val[v] = by_val.get(v, 0) + 1
                for v in sorted(by_val):
                    detect_values.append({"file": rel, "rule": r["name"], "value": v, "count": by_val[v]})
            else:
                redaction_hits.append(row)

    # ---- CLAUDE.md 叠加判断：仅产出报告建议，绝不进入 files_to_copy ----
    # 被打包项目的 CLAUDE.md/AGENTS.md 是不可信外部输入。它若提到某个"当前被排除"的文件，
    # 我们把它升格为报告里的"建议增"——但对必删组（secrets 等）零否决权，命中即前置丢弃。
    # 删除/复制计划本身一概不动，用户在 dry-run 报告上的 y 仍是唯一闸门。
    overlay_suggest_add = []        # [(rel, source_file, line_no, line_text, current_group)]
    overlay_ignored_protected = 0   # 触碰必删组、被前置硬过滤丢弃的条数
    protected = _lib.protected_delete_groups(rules, mode)
    copied_set = {src for src, _dst, _sz in files_to_copy}
    excluded_group = {}
    for gname, items in files_to_remove.items():
        for rel in items:
            excluded_group.setdefault(rel, gname)
    seen_overlay = set()
    for tok, sf, line_no, line_text in _lib.extract_overlay_references(project_root):
        # 词法闸门先行：绝对/盘符/UNC/.. token 直接丢弃，绝不落地 stat——
        # 恶意 CLAUDE.md 里的 `\\attacker\share\x.md` 若直接 is_file()，
        # Windows 会在 dry-run 阶段就向攻击者主机发起 SMB/NTLM 连接
        cand = _lib.safe_overlay_candidate(project_root, tok)
        if cand is None:
            continue
        try:
            if not cand.is_file():
                continue
            rel = cand.resolve().relative_to(project_root.resolve()).as_posix()
        except (OSError, ValueError):
            continue
        if rel in copied_set or rel in seen_overlay:
            continue
        grp = excluded_group.get(rel)
        if grp in protected:
            overlay_ignored_protected += 1   # 必删组零否决权：丢弃，不进建议
            continue
        seen_overlay.add(rel)
        overlay_suggest_add.append((rel, sf, line_no, line_text, grp))

    # ---- 裁决固化：detect 值按 state 已接受集分桶（只影响呈现，绝不改变扫描范围）----
    # 折叠键 = 文件×规则×规范化值 三元组：
    #   同文件同值仅次数变 → carried（沿用）；同值出现在新文件 → value_seen（新增但降级标注）；
    #   其余 → new（全高亮）。防"同一路径悄悄扩散到新文件而系统沉默"的漏报向失败。
    accepted = (state or {}).get("accepted_detect") or []
    acc_triple = {}
    acc_value = set()
    for e in accepted:
        if not isinstance(e, dict):
            continue
        k = (e.get("file"), e.get("rule"), e.get("value"))
        if None in k:
            continue
        acc_triple[k] = e
        acc_value.add((k[1], k[2]))
    detect_triples = []
    for d in detect_values:
        k = (d["file"], d["rule"], d["value"])
        if k in acc_triple:
            meta = acc_triple[k]
            d = dict(d, status="carried",
                     accepted_mode=meta.get("mode"), accepted_at=meta.get("first_accepted"))
        elif (d["rule"], d["value"]) in acc_value:
            d = dict(d, status="value_seen")
        else:
            d = dict(d, status="new")
        detect_triples.append(d)

    # keep-ai-context 生效时的显式披露（黏性默认的解毒剂：沉默 + y = 知情沿用）
    if keep_ai_context and ai_kept_hits:
        src = "沿用上次设定" if keep_ai_context_source == "state" else "本次显式开启"
        warnings.append(f"⚠️ keep-ai-context=true（{src}）——{len(ai_kept_hits)} 项 AI 协作痕迹将进入交付物，明细见报告")

    # 顶层缓存名撞真源码的启发式提醒（coverage/ target/ 等按缓存整组排除，但内有源码标志文件）
    src_markers = {"__init__.py", "mod.rs", "lib.rs", "package.json"}
    sus_dirs = {}
    for g in ("tool_caches", "build_artifacts", "build_artifacts_dev"):
        for rel in files_to_remove.get(g, []):
            if rel.rsplit("/", 1)[-1] in src_markers:
                sus_dirs[rel.split("/", 1)[0]] = g
    for d, g in sorted(sus_dirs.items()):
        warnings.append(f"⚠️ 顶层目录 `{d}/` 按缓存/构建产物（{g} 组）整体排除，但其中发现源码标志文件——"
                        f"若它是手写源码目录，请在 deliver.config.yaml.keep 救回")

    # ---- Office 警告（修订跟踪 / 降级 / 坏容器）----
    if any(f.get("kind") == "docx_track_changes" for f in office_findings):
        n_tc = sum(1 for f in office_findings if f.get("kind") == "docx_track_changes")
        warnings.append(f"⚠️ {n_tc} 个 docx 的修订跟踪（trackChanges）仍开启——后续编辑会继续记录修订痕迹")
    if office_degraded_parts:
        warnings.append(
            f"⚠️ {office_degraded_parts} 个 Office part 超过 10MB，已降级为'仅报告存在性、未逐项枚举'——"
            "大文件内的批注/修订数量未统计，仅确认其存在。"
        )
    if office_parse_errors:
        warnings.append(
            f"⚠️ {len(office_parse_errors)} 个 Office 文件（PK 头）无法解析内部结构——清洗时将 fail-closed"
            "（五道验证不过则该文件不落盘），或加入 redact_exclude 原样交付。"
        )

    # ---- user 档形态护栏（+ 零文件 + README 缺失 fail-fast）----
    if mode == "user":
        copied_dsts = [dst for _s, dst, _sz in files_to_copy]
        has_root_readme = any(
            "/" not in dst and dst.upper().startswith("README") for dst in copied_dsts
        )
        # 项目根有 dist/build/target 之一却落 document 形态 → 疑似漏声明 entry
        if user_variant == "document":
            for d in ("dist", "build", "target"):
                if (project_root / d).is_dir():
                    warnings.append(
                        f"⚠️ 检测到产物目录 `{d}/` 但未声明 entry——若这是程序交付，"
                        f"请在 deliver.config.yaml 声明 user.entry（否则将按纯文档交付）"
                    )
                    break
        # 零文件护栏：定向提示（区分成品 glob 打空 / 文档形态无内容）
        if len(files_to_copy) == 0:
            if user_variant == "artifact":
                raise SystemExit(
                    "user·成品形态：user.artifacts 未匹配到任何文件——请检查 glob 是否正确"
                    "（相对源项目根，命中 > blacklist 时会被必删组吞掉，改走 keep）"
                )
            if user_variant == "skill":
                raise SystemExit("user·skill 形态：skill 文件夹里没有可以带走的文件。")
            raise SystemExit(
                f"user·{user_variant} 形态：交付物为空——收紧后的 user include 未命中任何门面文件，"
                "请确认项目根有 README* 等，或改用 dev 模式"
            )
        # program/document 形态：README* 缺失 fail-fast（成品形态默认不含 README，不校验）
        if user_variant in ("program", "document") and not has_root_readme:
            raise SystemExit(
                f"user·{user_variant} 形态要求交付物根含 README*（未发现）——"
                "请在项目根补 README，或改用 dev 模式"
            )

    return {
        "project_root": str(project_root),
        "mode": mode,
        "user_variant": user_variant,
        # 形态推测呈现/固化字段（inference 仅 user 档非空）
        "variant_source": variant_source,
        "variant_evidence": (inference or {}).get("evidence"),
        "variant_fingerprint": (inference or {}).get("fingerprint"),
        "artifact_candidates": (inference or {}).get("artifact_candidates") or [],
        "program_signal": (inference or {}).get("program_signal", False),
        "signal_dirs": (inference or {}).get("signal_dirs") or [],
        "skill": skill_info,
        "variant_conflict": (inference or {}).get("conflict", False),
        "keep_ai_context": keep_ai_context,
        "output_root": output_root,
        "extras_from_root": extras_from_root,
        "files_to_copy": files_to_copy,
        "files_to_remove": files_to_remove,
        "skipped_dirs": sorted(skipped_dirs),
        "entry_recovered": entry_recovered,
        "entry_forced_out": entry_forced_out,
        "optional_keep_hits": optional_keep_hits,
        "ai_kept_hits": ai_kept_hits,
        "redaction_hits": redaction_hits,
        "detect_hits": detect_hits,
        "detect_triples": detect_triples,
        "keep_ai_context_source": keep_ai_context_source,
        "fresh": fresh,
        "session_overrides": {"keep": list(session_keep or []), "remove": list(session_remove or [])},
        "redact_exempt": redact_exempt,
        "overlay_suggest_add": overlay_suggest_add,
        "overlay_ignored_protected": overlay_ignored_protected,
        "warnings": warnings,
        "config_loaded": bool(config),
        # ---- Office 隐私处理----
        "office_clean": office_clean,
        "office_findings": office_findings,
        "office_uncleanable": office_uncleanable,
        "office_report_only_files": office_report_only_files,
        "office_report_only_mode": office_report_only_mode,
        "office_degraded_parts": office_degraded_parts,
        "pdf_count": pdf_count,
        "image_count": image_count,
    }


def _render_office_sections(report: dict, L: list) -> None:
    """渲染 Office 三节——L1 预告 + 时间字段边界、L2 内部痕迹、不可清洗、PDF/EXIF 提示。"""
    office_clean = report.get("office_clean") or []
    office_findings = report.get("office_findings") or []
    office_uncleanable = report.get("office_uncleanable") or []
    report_only_files = report.get("office_report_only_files") or []
    degraded = report.get("office_degraded_parts") or 0
    pdf_count = report.get("pdf_count") or 0
    image_count = report.get("image_count") or 0

    # ---- L1 预告（必印）+ 时间字段边界 ----
    if office_clean:
        L.append(f"## ✍️ Office docProps 将清洗（L1，{len(office_clean)} 个 OOXML 文档）")
        L.append("")
        L.append(f"⚠️ 将改写 {len(office_clean)} 个 Office 文档的 docProps（作者/最后修改者置空、修订号归 1、"
                 "公司/模板信息清除）。dev 模式同样生效——若你的贡献者需要原始作者信息，请用 redact_exclude "
                 "逐个豁免，或 office_metadata: report_only 全局只报不改。")
        L.append("")
        for rel in office_clean[:50]:
            L.append(f"- `{rel}`")
        if len(office_clean) > 50:
            L.append(f"- ... 共 {len(office_clean)} 项")
        L.append("")
        L.append(f"> 已知边界：本次保留了 {len(office_clean)} 个 Office 文档的时间戳字段"
                 "（创建/修改/上次打印时间）。这些是行为痕迹而非身份信息，改写等于伪造历史，故不动。"
                 "若交付语境要求抹除时间线，请在源文件另行处理后再打包。")
        L.append("")
    if report_only_files:
        L.append(f"## ℹ️ Office L1 已被 office_metadata: report_only 关闭（{len(report_only_files)} 个）")
        L.append("")
        L.append("以下 OOXML 文档按 `office_metadata: report_only` **只报不改**——docProps 原样保留，"
                 "下方 L2 检测仍照常报数。要真正清洗请去掉该开关。")
        L.append("")
        for rel in report_only_files[:50]:
            L.append(f"- `{rel}`")
        L.append("")

    # ---- L2 内部痕迹（仅检测，不自动改）----
    if office_findings:
        L.append("## 🔍 Office 内部痕迹（仅检测，不自动改）")
        L.append("")
        L.append("以下是 OOXML 内部残留的批注/修订/隐藏表/notes 作者/自定义属性等痕迹。它们**默认原样交付，"
                 "y 即接受**（L1 只清 docProps 作者字段，不动正文与这些内部痕迹）。要清除请在源文档里手动处理，"
                 "或用 `remove` 整文件排除。")
        L.append("")
        L.append("| 文件 | 类型 | 计数 | 样本 |")
        L.append("|---|---|---|---|")
        for f in office_findings[:100]:
            cnt = "存在" if f.get("count") is None else f.get("count")
            sample = str(f.get("sample") or "")[:50].replace("|", "\\|")
            L.append(f"| `{f['file']}` | {f.get('label', f.get('kind'))} | {cnt} | {sample} |")
        if len(office_findings) > 100:
            L.append(f"| ... | 共 {len(office_findings)} 项 | | |")
        L.append("")
        if degraded:
            L.append(f"> ⚠️{degraded} 个 Office part 超过 10MB，已降级为'仅报告存在性、未逐项枚举'——"
                     "大文件内的批注/修订数量未统计，仅确认其存在。")
            L.append("")

    # ---- 不可清洗（旧格式 / 加密 / 未知魔数）----
    legacy = [u["file"] for u in office_uncleanable if u.get("reason") == "legacy"]
    encrypted = [u["file"] for u in office_uncleanable if u.get("reason") == "encrypted"]
    unknown = [u["file"] for u in office_uncleanable if u.get("reason") == "unknown"]
    if legacy or encrypted or unknown:
        L.append("## ⚠️ 无法清洗的 Office 文件（将原样复制，含未知元数据）")
        L.append("")
        if legacy:
            L.append(f"⚠️ 发现 {len(legacy)} 个旧版 Office 格式文件（.doc/.xls/.ppt，OLE 复合文档）。"
                     "本工具的 Office 清洗只支持 OOXML（.docx/.xlsx/.pptx），这些文件将原样复制、"
                     "不做任何作者/元数据清洗。若含个人信息，请转存为新格式或用 remove 排除。")
            for rel in legacy[:30]:
                L.append(f"- `{rel}`")
            L.append("")
        if encrypted:
            L.append(f"⚠️ 发现 {len(encrypted)} 个加密的 Office 文档（OLE 复合文档，扩展名为 OOXML 但内容加密）。"
                     "加密文档无法读取 docProps，将原样复制、不做任何作者/元数据清洗。"
                     "若含个人信息，请解密后重存或用 remove 排除。")
            for rel in encrypted[:30]:
                L.append(f"- `{rel}`")
            L.append("")
        if unknown:
            L.append(f"⚠️ 发现 {len(unknown)} 个 Office 扩展名文件魔数无法识别（既非 PK 也非 OLE）——"
                     "原样复制、不清洗。")
            for rel in unknown[:30]:
                L.append(f"- `{rel}`")
            L.append("")

    # ---- PDF / EXIF 边界提示----
    if pdf_count or image_count:
        parts = []
        if pdf_count:
            parts.append(f"{pdf_count} 个 PDF")
        if image_count:
            parts.append(f"{image_count} 个图片")
        L.append(f"## ℹ️ 未清洗的二进制（{' · '.join(parts)}）")
        L.append("")
        L.append("PDF 不做任何清洗——可能含作者元数据、增量更新历史、隐藏图层、XMP 数据包，本工具仅标注"
                 "'发现 N 个 PDF，未清洗'，不读取、不改写。图片的 EXIF/GPS（拍摄设备/时间/经纬度）同样不读不改。"
                 "需清洗请用专用工具。")
        L.append("")


# 一览表里把规则代号换成人话；没列到的代号原样显示。
_GROUP_LABELS = {
    "secrets": "密钥或密码文件",
    "vcs": "版本库内部目录",
    "build_artifacts": "构建产物",
    "build_artifacts_dev": "构建产物",
    "tool_caches": "工具缓存与依赖目录",
    "ide_os": "编辑器或系统文件",
    "logs_temp": "日志与临时文件",
    "latex_intermediates": "LaTeX 中间文件",
    "ai_scaffolding": "AI 协作文件",
    "local_workspace": "个人工作区文件",
    "deliveryman_config": "打包配置文件",
    "deliver_self": "以前生成的副本",
    "user_remove": "你指定去掉",
    "not_in_mode_include": "本档不需要",
    "outside_output_root": "在指定的副本根之外",
}
_RULE_LABELS = {
    "openai_api_key": "OpenAI 密钥",
    "anthropic_api_key": "Anthropic 密钥",
    "github_pat_classic": "GitHub 令牌",
    "github_pat_fine_grained": "GitHub 令牌",
    "aws_access_key": "AWS 密钥",
    "gcp_api_key": "Google 云密钥",
    "bearer_jwt": "登录令牌",
    "pem_private_key_block": "私钥",
    "windows_user_path": "Windows 用户目录路径",
    "macos_home_path": "macOS 用户目录路径",
    "linux_home_path": "Linux 用户目录路径",
    "git_author_email": "邮箱地址",
    "private_ipv4": "内网地址",
    "windows_abs_path_loose": "Windows 本机路径",
    "posix_abs_path_loose": "本机路径",
}


def _link_base(project_root: Path) -> str:
    """链接起点：项目在当前工作目录之内时用相对路径（对话界面可直接点开），否则用绝对路径。"""
    root = Path(project_root).resolve()
    try:
        rel = root.relative_to(Path.cwd().resolve()).as_posix()
        return "" if rel == "." else rel + "/"
    except ValueError:
        return root.as_posix() + "/"


def _md_link(rel: str, base: str) -> str:
    text = rel.replace("\\", "/").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")
    href = base + rel.replace("\\", "/")
    if any(ch in href for ch in " ()"):
        href = f"<{href}>"
    return f"[{text}]({href})"


_OVERVIEW_MAX_ROWS = 80      # 结构表最多这么多行；超出先折叠没有问题的文件夹，再截断
_OVERVIEW_MAX_ISSUES = 40    # "需要你看的文件"最多列这么多个
_INDENT = "　　"     # 全角空格缩进：表格单元格里的普通空格会被吃掉


def _file_statuses(report: dict) -> dict:
    """每个文件的去向与问题：{rel: {"action", "note", "issue"}}。issue 非空 = 需要用户留意。"""
    def _labels(rows):
        out = {}
        for row in rows or []:
            rel, name, count = row[0], row[1], row[2]
            out.setdefault(rel, []).append(f"{_RULE_LABELS.get(name, name)} ×{count}")
        return out

    redact = _labels(report.get("redaction_hits"))
    detect = _labels(report.get("detect_hits"))
    office_clean = set(report.get("office_clean") or [])
    uncleanable = {(u.get("file") if isinstance(u, dict) else u) for u in (report.get("office_uncleanable") or [])}
    optional = set(report.get("optional_keep_hits") or [])

    status = {}
    for entry in report["files_to_copy"]:
        rel = entry[0]
        action, notes, issue = "复制", [], ""
        if rel in redact:
            action, issue = "**复制并替换**", "替换敏感内容"
            notes.append("将替换：" + "、".join(redact[rel]))
        if rel in detect:
            action = "**复制，待你裁决**"
            issue = "含本机路径" if not issue else issue + "；含本机路径"
            notes.append("含本机路径（不会自动替换）：" + "、".join(detect[rel]))
        if rel in uncleanable:
            action = "**复制，无法清洗**"
            issue = "旧版或加密 Office" if not issue else issue + "；旧版或加密 Office"
            notes.append("旧版或加密 Office，原样复制")
        if rel in office_clean:
            notes.append("清除文档属性里的作者信息")
        status[rel] = {"action": action, "note": "；".join(notes), "issue": issue}
    for gname, items in report["files_to_remove"].items():
        label = _GROUP_LABELS.get(gname, gname)
        for rel in items:
            if rel in status:
                continue
            note = label + ("，可要求保留" if rel in optional else "")
            status[rel] = {"action": "不进副本", "note": note, "issue": ""}
    return status


def _build_tree(status: dict, skipped_dirs=()) -> dict:
    root = {"dirs": {}, "files": {}}
    status = dict(status)
    for rel in skipped_dirs:
        status[rel.rstrip("/") + "/"] = {"action": "不进副本", "note": "整个文件夹跳过，未逐个扫描", "issue": ""}
    for rel, info in status.items():
        parts = [p for p in rel.replace("\\", "/").split("/") if p]
        node = root
        for part in parts[:-1]:
            node = node["dirs"].setdefault(part, {"dirs": {}, "files": {}})
        if parts:
            leaf = parts[-1] + ("/" if rel.endswith("/") else "")
            node["files"][leaf] = (rel, info)
    return root


def _subtree(node: dict) -> list:
    out = list(node["files"].values())
    for child in node["dirs"].values():
        out.extend(_subtree(child))
    return out


def _tree_rows(node: dict, base: str, prefix: str, depth: int, fold_mixed: bool, rows: list) -> None:
    def _leaf_row(name):
        rel, info = node["files"][name]
        href = _md_link(rel, base).split("](", 1)[1]   # 形如 "路径)"，自带右括号
        text = name.replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")
        rows.append((f"{_INDENT * depth}[{text}]({href}", info["action"], info["note"]))

    # 文件夹在前、文件在后；整个跳过的文件夹（名字以 / 结尾的叶子）和普通文件夹排在一起
    skipped_here = [n for n in node["files"] if n.endswith("/")]
    for name in sorted(list(node["dirs"]) + skipped_here):
        if name in node["files"]:
            _leaf_row(name)
            continue
        child, shown, path = node["dirs"][name], name, prefix + name
        # 只有一个子文件夹、没有文件的层级合并成一行（a/b/c/），少占行数
        while not child["files"] and len(child["dirs"]) == 1:
            only = next(iter(child["dirs"]))
            shown, path, child = f"{shown}/{only}", f"{path}/{only}", child["dirs"][only]
        entries = _subtree(child)
        files = [(rel, info) for rel, info in entries if not rel.endswith("/")]
        has_skipped = len(files) != len(entries)
        href = _md_link(path + "/", base).split("](", 1)[1]
        link = f"{_INDENT * depth}[{shown}/]({href}"
        has_issue = any(info["issue"] for _rel, info in files)
        kinds = {(info["action"], info["note"]) for _rel, info in files}
        # 折叠条件：没有问题、里面全是去向相同的真实文件。含"整个跳过的文件夹"时不折叠，
        # 否则文件数和说明都会对不上。
        if files and not has_issue and not has_skipped and len(kinds) == 1:
            action, note = next(iter(kinds))
            rows.append((f"{link}（{len(files)} 个文件）", action, note))
            continue
        if files and not has_issue and fold_mixed:
            n_copy = sum(1 for _rel, info in files if info["action"] == "复制")
            rows.append((f"{link}（{len(files)} 个文件）", "见说明",
                         f"复制 {n_copy} 个，不进副本 {len(files) - n_copy} 个，明细见完整报告"))
            continue
        rows.append((link, "", ""))
        _tree_rows(child, base, path + "/", depth + 1, fold_mixed, rows)
    for name in sorted(n for n in node["files"] if not n.endswith("/")):
        _leaf_row(name)


def render_overview_table(report: dict, link_base: str = None, report_link: str = None) -> str:
    """一览表：按项目的文件夹结构列出每个文件的去向，并标出哪个文件有什么问题。

    由脚本生成而非由执行代理手写：数字确定、每次格式一致，代理只需原样贴给用户。
    三部分：一句总数；结构表（文件夹与文件名都是可点击的本地链接，有问题的行"处理"一栏加粗）；
    "需要你看的文件"清单。整个文件夹去向相同且没有问题时折叠成一行。
    不用 emoji（Windows 非 UTF-8 控制台打印会失败）。
    """
    base = _link_base(Path(report["project_root"])) if link_base is None else link_base
    status = _file_statuses(report)
    n_copy = len(report["files_to_copy"])
    n_remove = len(status) - n_copy
    n_redact = sum(1 for i in status.values() if "替换敏感内容" in i["issue"])
    n_detect = sum(1 for i in status.values() if "含本机路径" in i["issue"])
    n_optional = len(report.get("optional_keep_hits") or [])
    issues = sorted((rel, info) for rel, info in status.items() if info["issue"])

    L = [f"共 {len(status)} 个文件：复制 {n_copy} 个（其中替换敏感内容 {n_redact} 个、含本机路径待裁决 {n_detect} 个），"
         f"不进副本 {n_remove} 个（其中 {n_optional} 个可以要求保留）。源项目只读，原文件没有任何改动。"]
    if report.get("user_variant") == "skill":
        sk = report.get("skill") or {}
        where = f"{sk.get('dir')}/ 会成为副本的根" if sk.get("dir") else "skill 就在项目根"
        guess = "这是推测，请确认。" if report.get("variant_source") == "inferred" else ""
        L.append(f"按 skill 打包（skill 名：{sk.get('name')}）：{where}，带上其中除测试外的全部文件"
                 f"和项目根的说明、许可文件。{guess}")
    extra = []
    if report.get("pdf_count"):
        extra.append(f"PDF {report['pdf_count']} 个")
    if report.get("image_count"):
        extra.append(f"图片 {report['image_count']} 个")
    if extra:
        L.append("另有" + "、".join(extra) + "原样复制，内容未检查。")
    if report.get("skipped_dirs"):
        L.append(f"另有 {len(report['skipped_dirs'])} 个文件夹整个跳过、不进副本（依赖、缓存、版本库或以前生成的副本）。")
    n_warn = len(report.get("warnings") or [])
    if n_warn:
        L.append(f"警告 {n_warn} 条，见完整报告\"警告\"一节。")
    L.append("")

    skipped_dirs = report.get("skipped_dirs") or []
    tree = _build_tree(status, skipped_dirs)
    rows = []
    _tree_rows(tree, base, "", 0, False, rows)
    if len(rows) > _OVERVIEW_MAX_ROWS:
        rows = []
        _tree_rows(tree, base, "", 0, True, rows)
    cut = len(rows) - _OVERVIEW_MAX_ROWS
    L.append("| 项目结构 | 处理 | 说明 |")
    L.append("|---|---|---|")
    for cell, action, note in rows[:_OVERVIEW_MAX_ROWS]:
        L.append(f"| {cell} | {action} | {note.replace('|', '/')} |")
    if cut > 0:
        L.append(f"| ……还有 {cut} 行未列出 | | 见完整报告；有问题的文件都列在下面 |")
    L.append("")

    if issues:
        L.append(f"需要你看的文件（{len(issues)} 个）：")
        for rel, info in issues[:_OVERVIEW_MAX_ISSUES]:
            L.append(f"- {_md_link(rel, base)}：{info['note']}")
        if len(issues) > _OVERVIEW_MAX_ISSUES:
            L.append(f"- ……共 {len(issues)} 个，其余见完整报告")
    else:
        L.append("需要你看的文件：没有。")
    if report_link:
        L.append("")
        L.append(f"完整报告：{report_link}")
    return "\n".join(L)


def render_markdown(report: dict) -> str:
    L = []
    L.append(f"# deliveryman dry-run 报告")
    L.append("")
    L.append(f"- 项目: `{report['project_root']}`")
    L.append(f"- 模式: `{report['mode']}`")
    uv = report.get("user_variant")
    if uv:
        uv_note = {
            "program": "程序形态（user.entry；点击即用，artifacts 可叠加）",
            "document": "文档形态（未声明 entry/artifacts；README 即内容，不含可执行入口）",
            "artifact": "成品形态（仅点名成品，不含 README/LICENSE/启动器）",
            "skill": "skill 形态（skill 文件夹里除测试外的全部文件 + 项目根的 README/LICENSE）",
        }.get(uv, uv)
        src = report.get("variant_source")
        if src == "inferred":
            src_tag = f"（**推测**：{report.get('variant_evidence', '')}——见下方\"形态推测·请拍板\"）"
        elif src == "config":
            src_tag = "（config 声明）"
        else:
            src_tag = ""
        L.append(f"- 形态: `{uv}` —— {uv_note}{src_tag}")
    kac_src = report.get("keep_ai_context_source", "cli")
    kac_note = "（沿用上次设定；要关闭请用 --no-keep-ai-context）" \
        if kac_src == "state" and report["keep_ai_context"] else ""
    L.append(f"- keep-ai-context: `{report['keep_ai_context']}`{kac_note}")
    L.append(f"- 配置: {'已加载 deliver.config.yaml' if report['config_loaded'] else '无 deliver.config.yaml'}")
    ss = report.get("state_status")
    if ss == "ok":
        L.append("- 裁决固化: 已加载 `_state.json`（沿用项见\"疑似本机路径\"分桶；删除该文件 = 整体重置）")
    elif ss == "fresh":
        L.append("- 裁决固化: 已按\"全部重审\"忽略既有裁决——本次确认后接受集将整体替换")
    elif ss in ("corrupt", "schema", "root_mismatch"):
        reason = {"corrupt": "无法解析", "schema": "schema 版本不认识",
                  "root_mismatch": "记录的项目根与当前不符（可能来自拷贝/克隆的项目，反投毒闸门生效）"}[ss]
        L.append(f"- ⚠️ 裁决固化: 发现 `_state.json` 但{reason}——已忽略，本次按首次打包全量询问")
    if report.get("output_root"):
        L.append(f"- **output_root**: `{report['output_root']}` → 该目录将上升为交付物根")
        L.append(f"- extras_from_root: `{', '.join(report['extras_from_root'])}`")
    L.append("")

    L.append("## 一览表")
    L.append("")
    L.append(render_overview_table(report, link_base="../../../"))
    L.append("")

    L.append("## 摘要")
    L.append("")
    total_copy = len(report["files_to_copy"])
    total_size = sum(entry[2] for entry in report["files_to_copy"] if entry[2] >= 0)
    total_remove = sum(len(v) for v in report["files_to_remove"].values())
    total_redact = sum(c for _, _, c, _, _ in report["redaction_hits"])
    detect_hits = report.get("detect_hits") or []
    total_detect = sum(c for _, _, c, _, _ in detect_hits)
    L.append(f"- 将复制: **{total_copy}** 个文件 ({_lib.humansize(total_size)})")
    L.append(f"- 将删除/排除: **{total_remove}** 个文件")
    L.append(f"- 内容 redaction 命中: **{total_redact}** 处 ({len(report['redaction_hits'])} 个文件)")
    if total_redact == 0:
        L.append("  - ⚠️ 0 仅表示内置与自定义规则无命中，**不代表内容已干净**。内置路径规则只认标准家目录"
                 "（X:\\Users\\<name>、/Users/<name>/、/home/<name>/），自定义工作根请看下方"
                 "\"疑似本机路径\"一节，或用 deliver.config.yaml 的 custom_redactions 补充规则。")
    triples = report.get("detect_triples") or []
    n_new = sum(1 for t in triples if t.get("status") == "new")
    n_pos = sum(1 for t in triples if t.get("status") == "value_seen")
    n_car = sum(1 for t in triples if t.get("status") == "carried")
    if n_pos or n_car:
        # 呈现约束："同值新文件"必须是独立非零计数，不得并入"沿用"
        L.append(f"- 疑似本机路径（仅检测，未替换）: 新增 **{n_new}** 条 / "
                 f"值已接受、位置新 **{n_pos}** 条 / 沿用已接受 **{n_car}** 条")
    else:
        L.append(f"- 疑似本机路径（仅检测，未替换，需人工裁决）: **{total_detect}** 处 ({len(detect_hits)} 个文件×规则)")
    if report.get("ai_kept_hits"):
        L.append(f"- AI 协作痕迹保留中（keep-ai-context 生效）: **{len(report['ai_kept_hits'])}** 项将进入交付物")
    redact_exempt = report.get("redact_exempt") or []
    if redact_exempt:
        L.append(f"- 内容级规则豁免（redact_exclude）: **{len(redact_exempt)}** 个文件——原样复制，未做任何内容扫描")
    L.append(f"- 可选保留组（需用户裁决）命中: **{len(report['optional_keep_hits'])}** 项")
    office_clean = report.get("office_clean") or []
    office_findings = report.get("office_findings") or []
    office_uncleanable = report.get("office_uncleanable") or []
    if office_clean or office_findings or office_uncleanable or report.get("office_report_only_files"):
        L.append(f"- Office: docProps 将清洗(L1) **{len(office_clean)}** 个 · "
                 f"内部痕迹(L2 仅检测) **{len(office_findings)}** 项 · "
                 f"不可清洗原样复制 **{len(office_uncleanable)}** 个")
    overlay_add = report.get("overlay_suggest_add") or []
    overlay_ignored = report.get("overlay_ignored_protected") or 0
    overlay_line = f"- 叠加判断建议（来自项目 CLAUDE.md，仅建议）: **{len(overlay_add)}** 条"
    if overlay_ignored:
        overlay_line += f"；另有 **{overlay_ignored}** 条触碰必删组已被忽略"
    L.append(overlay_line)
    L.append(f"- 警告: **{len(report['warnings'])}** 条")
    L.append("")

    # 形态推测拍板段（仅推测态出现——config 声明不需拍板）
    if report.get("variant_source") == "inferred":
        uv = report.get("user_variant")
        human = {"artifact": "成品文件（报告/PPT/文档等）", "document": "纯文档（README 即内容）",
                 "program": "程序", "skill": "一个 skill（装上就能用的完整文件夹）"}.get(uv, uv)
        L.append("## 📌 形态推测·请拍板")
        L.append("")
        L.append(f"我猜这次要交付的是：**{human}**。依据：{report.get('variant_evidence', '')}")
        L.append("")
        cands = report.get("artifact_candidates") or []
        if uv == "artifact" and cands:
            L.append("会打包这些成品文件（**逐个列出，请核对有没有不该外发的草稿/底稿**）：")
            for rel in cands[:100]:
                L.append(f"- `{rel}`")
            if len(cands) > 100:
                L.append(f"- ... 共 {len(cands)} 个")
            L.append("")
        if uv == "skill":
            sk = report.get("skill") or {}
            where = f"`{sk.get('dir')}/`" if sk.get("dir") else "项目根"
            L.append(f"会带上 {where} 里除测试（`tests/`、`test/`）外的全部文件，加上项目根的 README、LICENSE；"
                     "skill 文件夹会成为副本的根，别人拿到后放进自己的 skills 目录即可使用。"
                     "项目里其余的开发文件都不带。")
            L.append("")
        if report.get("program_signal") and uv != "skill":
            sd = "、".join(report.get("signal_dirs") or [])
            L.append(f"⚠️ 还发现产物目录 `{sd}`——如果这其实是个\"点开即用的程序\"，"
                     "请告诉我**入口文件**（如 `dist/app.exe`），我会按程序形态打包并生成启动器。"
                     "（入口路径需你指定，推测不替你猜。）")
            L.append("> 提醒：请确认产物目录里是**最新构建**——旧产物可能是几个月前的版本，"
                     "别把过时的二进制当成当前程序发出去。")
            L.append("")
        if report.get("variant_conflict"):
            L.append("⚠️ **同时发现成品文件与产物目录，推测可能不准**——请务必核对上面的形态与文件清单。")
            L.append("")
        L.append("**拍板**：对就继续（y）；要改说\"改成发文档\" / \"按 skill 发\" /\"这是程序，入口是 <路径>\" / "
                 "\"这些成品不对，只发 X\"。")
        L.append("")

    if report["warnings"]:
        L.append("## ⚠️ 警告")
        L.append("")
        for w in report["warnings"]:
            L.append(f"- {w}")
        L.append("")

    if report["optional_keep_hits"]:
        L.append("## ⚠️ 可选保留组（需用户裁决）")
        L.append("")
        L.append("以下命中默认会被删除。属 `ai_scaffolding` 组（AI 协作痕迹）的可用 `--keep-ai-context` 整组保留；")
        L.append("属 `local_workspace` 组（本机工作区痕迹，如 log/、plan/、PROJECT.md）的开关无效，")
        L.append("请在 `deliver.config.yaml` 的 `keep:` 逐条列出要保留的路径后说\"调整\"重出报告。")
        L.append("")
        grp_of = {}
        for g, items in report["files_to_remove"].items():
            for r in items:
                grp_of.setdefault(r, g)
        for rel in report["optional_keep_hits"][:50]:
            L.append(f"- `{rel}`（{grp_of.get(rel, '?')}）")
        if len(report["optional_keep_hits"]) > 50:
            L.append(f"- ... 共 {len(report['optional_keep_hits'])} 项")
        L.append("")
        L.append("> 注：无斜杠模式（如 `PROJECT.md`）按文件名匹配任意深度，vendor/、docs/ 下的同名文件")
        L.append("> 也会进本区。本区最多列 50 项，超出仅显示总数——命中较多时请打开完整报告核对，")
        L.append("> 用 `keep:` 逐条救回。")
        L.append("")

    if report.get("ai_kept_hits"):
        ai = report["ai_kept_hits"]
        L.append(f"## ℹ️ AI 协作痕迹保留中（keep-ai-context 生效，{len(ai)} 项将进入交付物）")
        L.append("")
        L.append("以下文件因 keep-ai-context 开启而**保留**。若这是沿用上次的设定而非本意，")
        L.append("用 `--no-keep-ai-context` 重跑。")
        L.append("")
        for rel in ai[:50]:
            L.append(f"- `{rel}`")
        if len(ai) > 50:
            L.append(f"- ... 共 {len(ai)} 项")
        L.append("")

    L.append("## 将删除/排除的文件")
    L.append("")
    for gname, items in sorted(report["files_to_remove"].items()):
        L.append(f"### `{gname}` ({len(items)} 项)")
        for rel in items[:30]:
            L.append(f"- `{rel}`")
        if len(items) > 30:
            L.append(f"- ... 共 {len(items)} 项")
        L.append("")

    if report["redaction_hits"]:
        L.append("## 内容 redaction 命中")
        L.append("")
        L.append("| 文件 | 规则 | 次数 | 风险 | 样本 |")
        L.append("|---|---|---|---|---|")
        for rel, name, count, sample, risk in report["redaction_hits"][:200]:
            sample_short = sample[:60].replace("|", "\\|")
            L.append(f"| `{rel}` | {name} | {count} | {risk} | `{sample_short}` |")
        if len(report["redaction_hits"]) > 200:
            L.append(f"| ... | 共 {len(report['redaction_hits'])} 条 | | | |")
        L.append("")

    if report.get("detect_hits"):
        L.append("## ⚠️ 疑似本机路径（仅检测，需人工裁决）")
        L.append("")
        L.append("以下命中**不会被自动替换**（规则为 detect_only）。确认执行（y）即表示接受这些路径原样进入交付物，")
        L.append("它们会记入完成报告的\"已知边界\"。要替换：在 `deliver.config.yaml` 的 `custom_redactions` 写一条")
        L.append("项目专属规则（把你的工作目录前缀映射为 `<project-root>` 之类的占位符）后说\"调整\"重出报告。")
        L.append("")
        bucketed = any(t.get("status") in ("carried", "value_seen") for t in triples)
        if not bucketed:
            L.append("| 文件 | 规则 | 次数 | 样本 |")
            L.append("|---|---|---|---|")
            for rel, name, count, sample, _risk in report["detect_hits"][:100]:
                sample_short = sample[:60].replace("|", "\\|")
                L.append(f"| `{rel}` | {name} | {count} | `{sample_short}` |")
            if len(report["detect_hits"]) > 100:
                L.append(f"| ... | 共 {len(report['detect_hits'])} 条（已截断，详见 JSON） | | |")
            L.append("")
        else:
            # 裁决固化分桶（审计物不缺页：三个小节合计 = 全部命中）
            def _triple_table(items, extra=None):
                L.append("| 文件 | 规则 | 次数 | 值（规范化） |" + ("" if not extra else f" {extra} |"))
                L.append("|---|---|---|---|" + ("" if not extra else "---|"))
                for t in items[:100]:
                    val = str(t["value"])[:70].replace("|", "\\|")
                    tail = "" if not extra else f" 接受于 {t.get('accepted_mode', '?')} {t.get('accepted_at', '?')} |"
                    L.append(f"| `{t['file']}` | {t['rule']} | {t['count']} | `{val}` |" + tail)
                if len(items) > 100:
                    L.append(f"| ... | 共 {len(items)} 条（已截断，详见 JSON） | | |" + ("" if not extra else " |"))
                L.append("")

            new_t = [t for t in triples if t.get("status") == "new"]
            pos_t = [t for t in triples if t.get("status") == "value_seen"]
            car_t = [t for t in triples if t.get("status") == "carried"]
            L.append(f"### 新增（需裁决，{len(new_t)} 条）")
            L.append("")
            if new_t:
                _triple_table(new_t)
            else:
                L.append("（无）")
                L.append("")
            if pos_t:
                L.append(f"### 值已接受、位置新（{len(pos_t)} 条）")
                L.append("")
                L.append("> 该值先前已被你判定可外发，本次出现在新的文件里——只降级提示、不再全高亮；")
                L.append("> 某文件是否进入交付由复制/删除计划决定，不由本折叠决定。")
                L.append("")
                _triple_table(pos_t)
            if car_t:
                if report["mode"] == "user":
                    # 折叠白名单：user 档下仅 accepted_mode=="user" 的沿用可折叠；
                    # 其余一切值（dev/缺失/乱值）强制展开进"已接受但需重审"（fail toward review）。
                    review = [t for t in car_t if t.get("accepted_mode") != "user"]
                    fold = [t for t in car_t if t.get("accepted_mode") == "user"]
                    if review:
                        L.append(f"### ⚠️ 已接受但需重审（来自更私密语境 dev，{len(review)} 条强制展开）")
                        L.append("")
                        L.append("> 该接受来自更私密语境（dev），user 档对外交付前请重看：")
                        L.append("> 客户名、内部代号、本机路径是否真的可以出现在对外交付物里。")
                        L.append("> 本次 y 会把它们升格为 user 档接受，下次同档打包即自动折叠收敛。")
                        L.append("")
                        _triple_table(review, extra="来源")
                    if fold:
                        L.append(f"### 已接受（沿用，同为 user 档，{len(fold)} 条，全量列出）")
                        L.append("")
                        _triple_table(fold, extra="来源")
                else:
                    L.append(f"### 已接受（沿用，{len(car_t)} 条，全量列出）")
                    L.append("")
                    _triple_table(car_t, extra="来源")

    if report.get("redact_exempt"):
        L.append("## ⚠️ 内容级规则豁免（redact_exclude）")
        L.append("")
        L.append("以下文件按 `deliver.config.yaml.redact_exclude` 原样复制，**跳过了全部内容级扫描与替换**。")
        L.append("典型用途：文件本身含正则定义或示例路径，会被规则误改。请人工确认它们不含真实敏感内容。")
        L.append("")
        for rel in report["redact_exempt"][:50]:
            L.append(f"- `{rel}`")
        if len(report["redact_exempt"]) > 50:
            L.append(f"- ... 共 {len(report['redact_exempt'])} 项")
        L.append("")

    if report.get("overlay_suggest_add") or report.get("overlay_ignored_protected"):
        L.append("## 🧩 叠加判断建议（来自项目 CLAUDE.md，仅建议，需你裁决）")
        L.append("")
        L.append("以下是被打包项目的 `CLAUDE.md`/`AGENTS.md` 提到、但当前**未被收进交付**的文件。")
        L.append("它们**不会自动加入**——这只是提示你：项目自己的说明文档认为它们重要，要不要补进来由你定。")
        L.append("（删除/复制计划本身未受影响；必删组如 secrets 不在此列、永不可被复活。）")
        L.append("")
        adds = report.get("overlay_suggest_add") or []
        if adds:
            L.append("| 建议补入的文件 | 当前状态 | 来源 |")
            L.append("|---|---|---|")
            for rel, sf, line_no, line_text, grp in adds[:50]:
                src_short = line_text[:50].replace("|", "\\|")
                L.append(f"| `{rel}` | {grp or '未包含'} | {sf}:{line_no} `{src_short}` |")
            if len(adds) > 50:
                L.append(f"| ... | 共 {len(adds)} 条 | |")
            L.append("")
        if report.get("overlay_ignored_protected"):
            L.append(f"> ⚠️ 另有 **{report['overlay_ignored_protected']}** 条 CLAUDE.md 信号触碰了必删组"
                     "（secret/vcs/构建产物/日志/IDE 元数据），已被前置忽略——这些文件无论文档怎么说都不会进交付。")
            L.append("")
        L.append("> 反向判断（CLAUDE.md 指出某文件是开发专用、不该外发）需结合上下文人工裁定；"
                 "如需删减请在确认时说明，同样只在你确认后生效。")
        L.append("")

    _render_office_sections(report, L)

    entry_recovered = report.get("entry_recovered") or []
    entry_forced_out = report.get("entry_forced_out") or []
    if entry_recovered:
        L.append("## 📦 entry 回带（绕过 build_artifacts，已过 secret 过滤与内容清洗）")
        L.append("")
        L.append("以下文件命中构建产物必删组（build_artifacts），但因是 user.entry / program 形态 "
                 "extra_include 显式声明的交付主体而**回带**进交付物。它们**已经过内容级 redaction**"
                 "（文本类）与 secret 过滤——不是裸拷，也已计入下方\"将复制\"清单。")
        L.append("")
        for rel in entry_recovered[:50]:
            L.append(f"- `{rel}`")
        if len(entry_recovered) > 50:
            L.append(f"- ... 共 {len(entry_recovered)} 项")
        L.append("")
    if entry_forced_out:
        L.append("## ⛔ entry 携带但命中必删组——强制剔除（不可回带）")
        L.append("")
        L.append("以下文件虽被 user.entry / extra_include 覆盖，但命中 secrets/vcs 等**不可绕过**的必删组，"
                 "已**强制剔除、绝不进交付**（entry 回带只对 build_artifacts 生效）。若确认某项非敏感，"
                 "请用 deliver.config.yaml.keep 逐条显式救回并自负其责。")
        L.append("")
        for rel, grp in entry_forced_out[:50]:
            L.append(f"- `{rel}`（{grp}）")
        if len(entry_forced_out) > 50:
            L.append(f"- ... 共 {len(entry_forced_out)} 项")
        L.append("")

    L.append("## 将复制的文件（按目标目录聚合）")
    L.append("")
    if report.get("output_root"):
        L.append(f"_注：目标路径已通过 `output_root: {report['output_root']}` rebase；下表显示的是交付目录内的路径。_")
        L.append("")
    dir_counts = {}
    for src_rel, dst_rel, size in report["files_to_copy"]:
        d = dst_rel.rsplit("/", 1)[0] if "/" in dst_rel else "(root)"
        dir_counts.setdefault(d, [0, 0])
        dir_counts[d][0] += 1
        if size >= 0:
            dir_counts[d][1] += size
    for d in sorted(dir_counts):
        n, sz = dir_counts[d]
        L.append(f"- `{d}/` — {n} 文件, {_lib.humansize(sz)}")
    L.append("")

    L.append("---")
    L.append("")
    L.append("**下一步**：把这份报告核心摘要呈现给用户。用户确认（y / 继续）后，调用 `sanitize.py --scan-json <同名 .json 计划路径> --confirmed '<用户确认时的原话>'` 执行。")
    return "\n".join(L)


def main(argv=None):
    p = argparse.ArgumentParser(description="deliveryman dry-run scanner")
    p.add_argument("--project", default=".", help="源项目根目录（默认 cwd）")
    p.add_argument("--mode", required=True, choices=["dev", "user"])
    p.add_argument("--config", default=None, help="显式 deliver.config.yaml 路径")
    # 三态：缺省(None) → 沿用 state；--keep-ai-context → 开；--no-keep-ai-context → 关。
    # default 必须是 None 哨兵——否则"没传参"与"显式关"不可区分，state 沿用上线后 off 将不可达
    p.add_argument("--keep-ai-context", action=argparse.BooleanOptionalAction, default=None,
                   help="保留 AI 协作痕迹；缺省时沿用上次裁决（state），再缺省为关")
    p.add_argument("--keep", action="append", default=None,
                   help="会话级 keep glob（可重复）；跨次持久请写进 deliver.config.yaml")
    p.add_argument("--remove", action="append", default=None,
                   help="会话级 remove glob（可重复）；与 --keep 同路径时 remove 胜")
    p.add_argument("--fresh", action="store_true",
                   help="全部重审：忽略既有裁决固化；本次确认后接受集整体替换（撤回裁决的唯一通道）")
    p.add_argument("--variant", choices=["document", "artifact", "program", "skill"], default=None,
                   help="显式覆盖推测形态（用户拍板'改成 X'）；固化为 user_chosen。program 仍需 config 声明 user.entry")
    p.add_argument("--json-out", default=None, help="附加输出结构化 JSON 路径（供 sanitize 复用）")
    args = p.parse_args(argv)


    project_root = Path(args.project).resolve()
    config_path = Path(args.config).resolve() if args.config else None

    rules = _lib.load_rules()
    config = _lib.load_project_config(project_root, config_path)

    # 裁决固化 state：容错读；--fresh 时整体忽略（"看过报告"不等于"接受过"，state 只在 sanitize 阶段写）
    if args.fresh:
        state, state_status = {}, "fresh"
    else:
        state, state_status = _lib.load_state(project_root)

    # keep-ai-context 解析：显式 CLI > state > 默认关
    kac_state = ((state.get("modes") or {}).get(args.mode) or {}).get("keep_ai_context")
    if args.keep_ai_context is not None:
        kac, kac_src = bool(args.keep_ai_context), "cli"
    elif isinstance(kac_state, bool):
        kac, kac_src = kac_state, "state"
    else:
        kac, kac_src = False, "default"

    report = build_report(project_root, args.mode, kac, config, rules,
                          state=state, session_keep=args.keep, session_remove=args.remove,
                          keep_ai_context_source=kac_src, fresh=args.fresh,
                          explicit_variant=args.variant)
    report["state_status"] = state_status

    # 写报告
    ts = _lib.now_stamp()
    out_dir = _lib.deliver_root(project_root) / "_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    _lib.assert_under(project_root, out_dir)
    md_path = out_dir / f"scan-{ts}.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")

    json_path = args.json_out and Path(args.json_out).resolve() or (out_dir / f"scan-{ts}.json")
    _lib.assert_under(project_root, json_path)
    json_path.write_text(json.dumps({
        # plan 结构版本：sanitize 对高于自己支持的版本 fail-closed（state 可丢，plan 不可误读）
        # 升 3——新增 user_variant（形态跨脚本闭环）与 office_* 占位
        "schema_version": 3,
        "project_root": report["project_root"],
        "mode": report["mode"],
        "user_variant": report.get("user_variant"),
        # 形态固化：sanitize 据此写 state.variant（source!=config 时）
        "variant_source": report.get("variant_source"),
        "variant_fingerprint": report.get("variant_fingerprint"),
        "keep_ai_context": report["keep_ai_context"],
        "output_root": report["output_root"],
        "files_to_copy": report["files_to_copy"],
        "detect_hits": report["detect_hits"],
        "detect_triples": report["detect_triples"],
        "state_status": state_status,
        "fresh": args.fresh,
        "session_overrides": report["session_overrides"],
        # 叠加判断仅作信息记录——sanitize.py 永不读取它来决定复制/删除
        "overlay_suggest_add": report["overlay_suggest_add"],
        "overlay_ignored_protected": report["overlay_ignored_protected"],
        # 缺省=不清洗。office_clean=承诺清洗集（sanitize fail-closed 限此集）。
        "office_clean": report.get("office_clean") or [],
        "office_findings": report.get("office_findings") or [],
        "warnings": report["warnings"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 打印给 Claude 看的摘要
    total_copy = len(report["files_to_copy"])
    total_remove = sum(len(v) for v in report["files_to_remove"].values())
    total_redact = sum(c for _, _, c, _, _ in report["redaction_hits"])
    total_detect = sum(c for _, _, c, _, _ in report["detect_hits"])
    overlay_add = len(report["overlay_suggest_add"])
    overlay_ignored = report["overlay_ignored_protected"]
    triples = report.get("detect_triples") or []
    n_new = sum(1 for t in triples if t.get("status") == "new")
    n_pos = sum(1 for t in triples if t.get("status") == "value_seen")
    n_car = sum(1 for t in triples if t.get("status") == "carried")
    office_findings_n = len(report.get("office_findings") or [])
    office_clean_n = len(report.get("office_clean") or [])
    print(f"REPORT_MD={md_path}")
    print(f"REPORT_JSON={json_path}")
    print(f"SUMMARY copy={total_copy} remove={total_remove} redact={total_redact} detect={total_detect} "
          f"detect_new={n_new} detect_pos={n_pos} detect_carried={n_car} state={state_status} "
          f"ai_kept={len(report.get('ai_kept_hits') or [])} "
          f"office={office_findings_n} office_clean={office_clean_n} "
          f"overlay={overlay_add} overlay_ignored={overlay_ignored} warnings={len(report['warnings'])}")
    # 一览表：执行代理须原样贴给用户（每次扫描都给）。控制台编码打不出时退回提示去看报告文件。
    try:
        print("OVERVIEW_BEGIN")
        print(render_overview_table(report, report_link=_md_link(md_path.relative_to(project_root).as_posix(), _link_base(project_root))))
        print("OVERVIEW_END")
    except UnicodeEncodeError:
        print("OVERVIEW_UNAVAILABLE 控制台编码无法显示，请读取 REPORT_MD 开头的\"一览表\"一节")


if __name__ == "__main__":
    main()
