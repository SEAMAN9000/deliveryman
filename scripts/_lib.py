"""deliveryman 共享辅助库（stdlib-only）。"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import hashlib
import json
import os
import re
from pathlib import Path

import _yaml_min


# -------- 路径与时间 --------

def now_stamp() -> str:
    """生成 output/deliver/<mode>-<ts>/ 用的时间戳。"""
    return _dt.datetime.now().strftime("%Y-%m-%d-%H%M")


def skill_dir() -> Path:
    """deliveryman skill 的根目录（scripts/ 的父目录）。"""
    return Path(__file__).resolve().parent.parent


def rules_dir() -> Path:
    return skill_dir() / "rules"


def templates_dir() -> Path:
    return skill_dir() / "templates"


def deliver_root(project_root: Path) -> Path:
    """交付产物的唯一输出根：<project>/output/deliver/。
    所有写入目标（交付子目录、_reports、_logs）都在它之下；assert_under 也以它为基准。
    单一事实源——改输出位置只改这一处。
    """
    return project_root / "output" / "deliver"


def assert_under(project_root: Path, target: Path) -> None:
    """守护：target 必须在 project_root/output/deliver/ 之下。否则抛 IOError。"""
    try:
        rel = target.resolve().relative_to(deliver_root(project_root).resolve())
        _ = rel  # 用 _ 避免 unused warning
    except (ValueError, OSError) as e:
        raise IOError(
            f"deliveryman 不变式违反：写入路径 {target} 不在 {project_root}/output/deliver/ 之下。原因: {e}"
        )


# -------- 规则加载 --------

def load_rules() -> dict:
    """加载 blacklist / redactions / modes / office 四份规则。"""
    return {
        "blacklist": _yaml_min.load(rules_dir() / "blacklist.yaml"),
        "redactions": _yaml_min.load(rules_dir() / "redactions.yaml"),
        "modes": _yaml_min.load(rules_dir() / "modes.yaml"),
        "office": _yaml_min.load(rules_dir() / "office.yaml"),
    }


def load_project_config(project_root: Path, explicit: Path | None = None) -> dict:
    """加载项目级 deliver.config.yaml（可选）。"""
    if explicit is not None:
        if not explicit.exists():
            raise IOError(f"指定的配置文件不存在: {explicit}")
        return _yaml_min.load(explicit) or {}
    candidate = project_root / "deliver.config.yaml"
    if candidate.exists():
        return _yaml_min.load(candidate) or {}
    return {}


# -------- user 档形态判定--------

def resolve_user_variant(config: dict) -> str:
    """由 config 推导 user 档的三形态之一（纯函数，不触碰文件系统）：
      - 有 `user.entry`                 → "program"（点击即用的可执行入口；artifacts 可叠加为 include 增量）
      - 无 entry 但有 `user.artifacts`  → "artifact"（仅点名成品，绕过 mode include 整套）
      - 皆无                            → "document"（纯文档交付，README 即内容）
    entry 与 artifacts 共存合法 = program 形态 + artifacts 叠加。
    """
    user = (config or {}).get("user") or {}
    if user.get("entry"):
        return "program"
    if user.get("artifacts"):
        return "artifact"
    return "document"


# 成品文档扩展名（"要交给别人看的成品"，非源码非中间产物）——推测 artifact 形态的信号
_DELIVERABLE_EXTS = frozenset({
    ".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xls",
    ".odt", ".ods", ".odp", ".key", ".pages", ".numbers",
})
# 顶层产物目录名——出现即"可能是程序"信号（但【不】自动定 program：入口需用户声明）
_PROGRAM_SIGNAL_DIRS = ("dist", "build", "target", "out")


# -------- skill 项目识别 --------

# skill 说明书的固定文件名；它所在的文件夹就是一个 skill
SKILL_MANIFEST = "SKILL.md"
# skill 形态下随 skill 一起带上的项目根门面文件
SKILL_FACADE_GLOBS = ["README*", "LICENSE*", "NOTICE*"]
# skill 文件夹内不随包的子文件夹（给维护者的测试）
_SKILL_SKIP_DIRS = frozenset({"tests", "test"})
# skill 就是项目根时，根上不随包的文件
_SKILL_ROOT_SKIP_GLOBS = ["CHANGELOG*", "deliver.config.yaml"]
# 嵌套布局下去哪几处找 skill 文件夹（相对项目根；"" 表示根下一层的任意文件夹）
_SKILL_PARENT_DIRS = (".claude/skills", ".agents/skills", ".opencode/skills", "skills", "work", "src", "")
# 这几处是 AI 工具放"本项目自用 skill"的位置：skill 出现在这里，不一定说明项目本身是在做 skill
_SKILL_TOOL_DIRS = (".claude/skills/", ".agents/skills/", ".opencode/skills/")


def _skill_manifest_name(path: Path):
    """读 SKILL.md 开头的 name 字段；不是合法的 skill 说明书返回 None。"""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return None
    head = head.lstrip(chr(0xFEFF))
    if not head.startswith("---"):
        return None
    body = head[3:]
    end = body.find(chr(10) + "---")
    if end != -1:
        body = body[:end]
    for line in body.splitlines():
        m = re.match(r"\s*name\s*:\s*(.+?)\s*$", line)
        if m:
            return m.group(1).strip("'\"") or None
    return None


def find_skill_dirs(project_root: Path) -> list:
    """只读地找出项目里的 skill 文件夹，返回 [{"dir": 相对路径（根为 ""）, "name": skill 名}]。
    项目根自己有 SKILL.md 时只返回根；否则到几个惯用位置的下一层去找。"""
    root = project_root.resolve()
    name = _skill_manifest_name(root / SKILL_MANIFEST) if (root / SKILL_MANIFEST).is_file() else None
    if name:
        return [{"dir": "", "name": name}]
    found, seen = [], set()
    for parent in _SKILL_PARENT_DIRS:
        base = root / parent if parent else root
        if not base.is_dir():
            continue
        try:
            children = sorted(c for c in base.iterdir() if c.is_dir())
        except OSError:
            continue
        for c in children:
            if c.name in _HARD_SKIP_DIRS or (not parent and c.name.startswith(".")):
                continue
            mf = c / SKILL_MANIFEST
            if not mf.is_file():
                continue
            nm = _skill_manifest_name(mf)
            rel = c.relative_to(root).as_posix()
            if nm and rel not in seen and rel not in _DELIVER_PRUNE_RELS:
                seen.add(rel)
                found.append({"dir": rel, "name": nm})
    return found


def in_skill_delivery(rel_path: str, skill_dir: str, project_root: Path) -> bool:
    """skill 形态下这个文件是否随包：skill 文件夹里除测试外的全部文件，
    加上项目根的 README / LICENSE / NOTICE（skill 文件夹里已有同名文件时以 skill 里的为准）。"""
    rel = _norm(rel_path)
    sd = _norm(skill_dir).strip("/")
    if sd:
        if rel.startswith(sd + "/"):
            inner = rel[len(sd) + 1:]
        elif "/" not in rel and match_any(rel, SKILL_FACADE_GLOBS):
            return not (project_root / sd / rel).exists()
        else:
            return False
    else:
        inner = rel
        if "/" not in rel and match_any(rel, _SKILL_ROOT_SKIP_GLOBS):
            return False
    parts = inner.split("/")
    return not any(seg in _SKILL_SKIP_DIRS for seg in parts[:-1])


def infer_user_variant(project_root: Path, config: dict) -> dict:
    """形态推测（只读遍历，不改任何状态）。
    config 显式声明（user.entry / user.artifacts）**永远优先**——直接返回 source="config"、不推测。
    无声明时推测 **document/artifact 轴**（【不】推测 program——程序入口需内置工具链表，违反
    "不内置语言识别"，故仍由用户声明 user.entry），采集证据指纹（供固化失效检测）与呈现依据。

    返回 dict：
      variant             推测/声明的形态（program|document|artifact）
      source              "config"（声明）| "inferred"（推测）
      artifact_candidates 推测 artifact 时的候选文件（逐个列出，不聚合——防混入不该发的文件）
      program_signal      是否发现顶层产物目录（提示用户"可能是程序，入口是哪个？"）
      signal_dirs         命中的产物目录名
      conflict            成品文件与产物目录同时存在（信号冲突，推测标不可靠）
      fingerprint         证据指纹（顶层目录集 + 成品文件集的 hash）；config 声明时为 None
      evidence            一句话呈现依据
    """
    user = (config or {}).get("user") or {}
    if user.get("entry") or user.get("artifacts"):
        return {
            "variant": resolve_user_variant(config),
            "source": "config",
            "artifact_candidates": [],
            "program_signal": False,
            "signal_dirs": [],
            "conflict": False,
            "fingerprint": None,
            "evidence": "config 显式声明",
            "skill": None,
            "skill_dirs": [],
        }
    root = project_root.resolve()
    deliverables = sorted(
        rel for _ap, rel in walk_project(project_root)
        if Path(rel).suffix.lower() in _DELIVERABLE_EXTS
    )
    signal_dirs = [d for d in _PROGRAM_SIGNAL_DIRS if (root / d).is_dir()]
    program_signal = bool(signal_dirs)
    # 指纹只覆盖【形态特征】：产物目录 + 成品文件——不含全部顶层目录，
    # 否则第一次打包创建 output/deliver/ 后指纹必变、永远无法沿用（carried）。
    fp_material = "|".join(signal_dirs) + "##" + "|".join(deliverables)
    # 认出 skill 时才把它计入指纹——没有 skill 的项目指纹不变，以前确认过的形态照常沿用
    skills = find_skill_dirs(project_root)
    skill = skills[0] if len(skills) == 1 else None
    if skill:
        inside = (skill["dir"] + "/") if skill["dir"] else ""
        deliverables_outside = [d for d in deliverables if inside and not d.startswith(inside)]
        if deliverables_outside and skill["dir"].startswith(_SKILL_TOOL_DIRS):
            # 项目里有成品文件，skill 又只是放在 AI 工具的自用位置：更像"带着一个辅助 skill 的成品项目"
            skill = None
    if skill:
        fp_material += "##skill:" + skill["dir"]
    fingerprint = hashlib.sha256(fp_material.encode("utf-8")).hexdigest()[:16]
    if skill:
        where = f"`{skill['dir']}/`" if skill["dir"] else "项目根"
        variant = "skill"
        evidence = f"{where} 有 SKILL.md（skill 名：{skill['name']}）、无 config 声明"
        if deliverables_outside:
            evidence += (f"；skill 之外还有 {len(deliverables_outside)} 个成品文件（pdf、docx 等）没有带，"
                         "要发的其实是它们就说\"改成发成品\"")
    elif deliverables:
        variant, evidence = "artifact", f"项目里 {len(deliverables)} 个成品文件、无 config 声明"
    else:
        variant, evidence = "document", "未见成品文件，README 即内容"
    return {
        "variant": variant,
        "source": "inferred",
        "artifact_candidates": deliverables,
        "program_signal": program_signal,
        "signal_dirs": signal_dirs,
        "conflict": bool(deliverables and program_signal and not skill),
        "fingerprint": fingerprint,
        "evidence": evidence,
        "skill": skill,
        "skill_dirs": [k["dir"] or "." for k in skills],
    }


# -------- glob 匹配 --------

def _norm(p: str) -> str:
    return p.replace("\\", "/")


def match_pattern(rel_path: str, pattern: str) -> bool:
    """匹配一条 glob 模式。
    - 前导 "/" 表示根锚定（相对项目根）——"/*.md" 命中根级 a.md 但不命中 docs/a.md
    - 末尾 "/" 表示目录（与该目录及其下所有内容匹配）
    - "**/" 匹配零层或多层目录——"**/*.py" 必须命中根级 a.py 与任意深度的 b/c.py
    - 否则 fnmatch
    """
    rel_path = _norm(rel_path).lstrip("/")
    pattern = _norm(pattern)
    # 前导 "/" 根锚定：只匹配相对项目根的该路径。防假锚定——fnmatch 的 "*" 会跨斜杠，
    # 故单层锚定模式必须先断言 rel_path 无斜杠，多层锚定按段等长逐段匹配（"*" 不跨段）。
    if pattern.startswith("/"):
        anchored = pattern.lstrip("/")
        if not anchored:
            return False
        if anchored.endswith("/"):
            prefix = anchored.rstrip("/")
            return rel_path == prefix or rel_path.startswith(prefix + "/")
        if "/" not in anchored:
            # 单层锚定：rel_path 必须也是单层（无斜杠），否则 "*" 会假锚定到深层同名文件
            if "/" in rel_path:
                return False
            return fnmatch.fnmatch(rel_path, anchored)
        # 多层锚定（无 ** —— 终表未用）：段数须相等，每段 fnmatch，"*" 不跨斜杠
        pat_segs = anchored.split("/")
        path_segs = rel_path.split("/")
        if len(pat_segs) != len(path_segs):
            return False
        return all(fnmatch.fnmatch(ps, pp) for ps, pp in zip(path_segs, pat_segs))
    if pattern.endswith("/"):
        # 目录：以该目录为前缀，或恰好等于该目录
        prefix = pattern.rstrip("/")
        return rel_path == prefix or rel_path.startswith(prefix + "/")
    if "**" in pattern:
        # 占位符法：**/ → (?:.*/)?（零层或多层），裸 ** → .*；其余 glob 字符走 fnmatch.translate
        s_dirs = "\x00DSTARSLASH\x00"
        s_any = "\x00DSTAR\x00"
        proxy = pattern.replace("**/", s_dirs).replace("**", s_any)
        regex = fnmatch.translate(proxy)
        regex = regex.replace(re.escape(s_dirs), "(?:.*/)?")
        regex = regex.replace(re.escape(s_any), ".*")
        return re.fullmatch(regex, rel_path) is not None
    # 顶层文件名：允许只匹配 basename
    if "/" not in pattern:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        base = rel_path.rsplit("/", 1)[-1]
        return fnmatch.fnmatch(base, pattern)
    return fnmatch.fnmatch(rel_path, pattern)


def match_any(rel_path: str, patterns) -> bool:
    if not patterns:
        return False
    for pat in patterns:
        if match_pattern(rel_path, pat):
            return True
    return False


# -------- blacklist 决策 --------

def blacklist_hit(rel_path: str, rules: dict, mode: str, keep_ai_context: bool):
    """如果该 rel_path 命中 blacklist，返回 (group_name, optional_keep_bool)；否则 None。"""
    bl = rules["blacklist"]["groups"]
    for gname, g in bl.items():
        if mode not in g.get("modes", []):
            continue
        if gname == "ai_scaffolding" and keep_ai_context:
            continue
        if match_any(rel_path, g.get("patterns") or []):
            return gname, bool(g.get("optional_keep"))
    return None


# -------- mode include 决策 --------

def mode_include_hit(rel_path: str, rules: dict, mode: str) -> bool:
    m = rules["modes"]["modes"].get(mode)
    if not m:
        return False
    if match_any(rel_path, m.get("exclude_extra") or []):
        return False
    return match_any(rel_path, m.get("include_globs") or [])


# -------- 项目遍历 --------

# 遍历时直接跳过的硬目录（性能优化；这些也会被 blacklist 命中）
# 注意："deliver" 有意【不】按目录名硬跳——名字剪枝会让任意层级的 src/deliver/ 源码
# 目录隐形消失。交付产物剪枝改为路径锚定（见 walk_project）：只剪 <root>/output/deliver
# 与 <root>/deliver，src/deliver/ 必须可见。
_HARD_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode",
                   # 名字不会与源码目录相撞的纯缓存：任意层级都跳过（黑名单里的同名规则只认项目顶层）
                   ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox"}

# 交付产物的路径锚定剪枝目标（相对项目根的 posix 路径）——防"套娃"，且不误伤同名源码目录
_DELIVER_PRUNE_RELS = {"deliver", "output/deliver"}


def walk_project(project_root: Path, skipped: list = None):
    """yield (abs_path, rel_path) 对源项目做只读遍历。

    skipped：传入列表时，把整个跳过、没有逐个扫描的文件夹（相对路径）记进去，供报告展示。
    """
    project_root = project_root.resolve()
    for root, dirs, files in os.walk(project_root):
        try:
            rel_root = Path(root).relative_to(project_root).as_posix()
        except ValueError:
            rel_root = ""
        # 原地剪枝：硬跳过目录（按名字）+ 交付产物（按路径锚定）
        kept = []
        for d in dirs:
            child_rel = d if rel_root in ("", ".") else f"{rel_root}/{d}"
            if d in _HARD_SKIP_DIRS or child_rel in _DELIVER_PRUNE_RELS:
                if skipped is not None:
                    skipped.append(child_rel)
                continue
            kept.append(d)
        dirs[:] = kept
        for fname in files:
            ap = Path(root) / fname
            try:
                rp = ap.relative_to(project_root).as_posix()
            except ValueError:
                continue
            yield ap, rp


# -------- CLAUDE.md 叠加判断（仅提取信号，绝不做取舍决定）--------

# 叠加判断读取被打包项目的这些文件作为"文件取舍提示"。它们是【不可信外部输入】，
# 只能升格为 dry-run 报告里的建议，永不直接驱动复制/删除——见 scan.build_report。
OVERLAY_SOURCE_FILES = ("CLAUDE.md", "AGENTS.md")


def _looks_like_path(tok: str) -> bool:
    """粗判一个 token 是否像文件路径：含目录分隔符，或有文件扩展名；排除带空格的命令行。"""
    if not tok or len(tok) > 200 or " " in tok:
        return False
    if "/" in tok:
        return True
    return re.search(r"\.[A-Za-z0-9]{1,8}$", tok) is not None


def extract_overlay_references(project_root: Path, source_files=OVERLAY_SOURCE_FILES) -> list:
    """从项目的 CLAUDE.md/AGENTS.md 提取被反引号包裹、形似文件路径的引用。
    返回 [(token, source_file, line_no, line_text), ...]。只提取，不做任何取舍决定。
    """
    out = []
    for sf in source_files:
        p = project_root / sf
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for m in re.finditer(r"`([^`\n]+)`", line):
                tok = m.group(1).strip()
                if _looks_like_path(tok):
                    out.append((tok, sf, i, line.strip()))
    return out


def protected_delete_groups(rules: dict, mode: str) -> set:
    """optional_keep=false 的必删组（secrets/vcs/build_artifacts/logs_temp/ide_os/deliver_self）。
    叠加层对这些组【零否决权】——CLAUDE.md 说什么都不能让这些文件复活。
    """
    bl = rules["blacklist"]["groups"]
    return {g for g, gd in bl.items()
            if mode in (gd.get("modes") or []) and not gd.get("optional_keep")}


# user·程序形态下 user.entry / program 形态 extra_include 携带的文件，命中【本白名单内】
# 的必删组时可被"回带"进交付（经 sanitize 管线：过 redaction、进报告枚举），命中其余任何
# 必删组（secrets/vcs/tool_caches/...）一律强制剔除。白名单模型（curator 裁定）：将来新增
# 黑名单组默认落在"剔除"这一安全侧，绕过必须显式列名。build_artifacts=user 档产物组，
# build_artifacts_dev=dev 档同名组（entry 机制仅 user 档用，列上后者仅为完备）。
ENTRY_BYPASSABLE_GROUPS = frozenset({"build_artifacts", "build_artifacts_dev"})


def entry_covers(rel_path: str, entry: str) -> bool:
    """判断 rel_path 是否被 user.entry 覆盖：entry 指向该文件本身，或 entry 是目录且 rel 在其下。
    纯词法，不触碰文件系统；entry 为空返回 False。"""
    if not entry:
        return False
    e = _norm(entry).strip("/")
    if not e:
        return False
    r = _norm(rel_path).lstrip("/")
    return r == e or r.startswith(e + "/")


# -------- redactions --------

# 文件扩展名分类
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
    ".mp3", ".mp4", ".webm", ".avi", ".mov", ".wav", ".ogg",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".class", ".jar",
    ".pyc", ".pyo", ".o", ".a", ".lib",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
}


def is_likely_binary(path: Path) -> bool:
    if path.suffix.lower() in _BINARY_EXTS:
        return True
    try:
        # 只读前 4096 字节——read_bytes()[:4096] 会把 GB 级文件全量读入内存，
        # artifact 形态大文件在 dry-run 阶段就 OOM/卡死 = 阻断强制前置。
        with open(path, "rb") as f:
            chunk = f.read(4096)
    except Exception:
        return True
    if b"\x00" in chunk:
        return True
    return False


def compile_redactions(rules: dict, custom: list | None) -> list:
    """编译 redactions.yaml 的 patterns + 用户自定义。
    每条返回 dict: {name, regex, replacement, applies_to, risk, notes, action}
    action: "redact"（默认，sanitize 时替换）| "detect_only"（只进报告，永不替换）
    """
    out = []
    base = rules["redactions"].get("patterns") or []
    for item in base:
        try:
            rx = re.compile(item["pattern"])
        except re.error as e:
            raise ValueError(f"redactions 正则编译失败: {item.get('name')}: {e}")
        out.append({
            "name": item.get("name", "anonymous"),
            "regex": rx,
            "replacement": item.get("replacement", "<REDACTED>"),
            "applies_to": item.get("applies_to") or ["*"],
            "risk": item.get("risk", "balanced"),
            "notes": item.get("notes", ""),
            "action": item.get("action", "redact"),
        })
    for item in custom or []:
        try:
            rx = re.compile(item["pattern"])
        except re.error as e:
            raise ValueError(f"custom_redactions 正则编译失败: {item.get('name', '?')}: {e}")
        out.append({
            "name": item.get("name", "custom"),
            "regex": rx,
            "replacement": item.get("replacement", "<REDACTED_CUSTOM>"),
            "applies_to": item.get("applies_to") or ["*"],
            "risk": item.get("risk", "balanced"),
            "notes": item.get("notes", "(user-defined)"),
            "action": item.get("action", "redact"),
        })
    return out


def project_path_detect_rules(project_root: Path) -> list:
    """第一梯队：用扫描时已知的真实项目根路径（及其上一级目录）做字面检测。
    零误报——这些字符串出现在待发布文本里就是本机路径泄露。detect_only，永不替换。
    """
    sep = r"[\\/]+"  # 兼容 \、/、转义后的 \\
    rules = []
    anchors = []
    root = project_root.resolve()
    anchors.append(("project_root_path", root))
    parent = root.parent
    # 上一级目录捕捉"引用兄弟项目"的泄露；盘符根 / 文件系统根太宽，不进锚点
    if len(parent.parts) > 2:
        anchors.append(("project_parent_path", parent))
    for name, p in anchors:
        parts = p.parts
        drive = parts[0].rstrip("\\/")  # Windows 是 "D:"，POSIX 根是 ""
        body = sep.join(re.escape(seg) for seg in parts[1:])
        if drive:
            pattern = re.escape(drive) + sep + body
            rx = re.compile(pattern, re.IGNORECASE)
        else:
            rx = re.compile("/" + body)
        rules.append({
            "name": name,
            "regex": rx,
            "replacement": "<LOCAL_PATH>",
            "applies_to": ["*"],
            "risk": "exact",
            "notes": f"字面匹配本机真实路径 {p}（含正/反斜杠与转义变体）",
            "action": "detect_only",
        })
    return rules


def redaction_applies(rule: dict, rel_path: str) -> bool:
    return match_any(rel_path, rule["applies_to"])


def apply_redactions(text: str, rel_path: str, redactions: list):
    """对一段文本应用 redactions，返回 (new_text, hits)。
    hits: list of (rule_name, count)
    detect_only 规则在此被跳过——它们只进 dry-run 报告，永不改写内容。
    """
    hits = []
    for r in redactions:
        if r.get("action", "redact") != "redact":
            continue
        if not redaction_applies(r, rel_path):
            continue
        new_text, n = r["regex"].subn(r["replacement"], text)
        if n:
            hits.append((r["name"], n))
            text = new_text
    return text, hits


# -------- .gitignore 追加 --------

def manifest_path_for(target_dir: Path) -> Path:
    """某次副本的运行记录路径：与副本目录同级的 _reports/manifest-<副本目录名>.json。

    记录不放进副本目录——副本里只有用户要交付的文件。
    """
    target_dir = Path(target_dir)
    return target_dir.parent / "_reports" / f"manifest-{target_dir.name}.json"


def ensure_gitignore_has_deliver(project_root: Path) -> bool:
    """保证 <project>/.gitignore 末尾有 `output/deliver/` 一行。
    返回 True 表示新追加，False 表示已存在。
    """
    gi = project_root / ".gitignore"
    if gi.exists():
        content = gi.read_text(encoding="utf-8", errors="replace")
        for line in content.splitlines():
            if line.strip() in ("output/deliver/", "/output/deliver/", "output/deliver", "/output/deliver"):
                return False
        sep = "" if content.endswith("\n") else "\n"
        gi.write_text(content + sep + "# added by deliveryman skill\noutput/deliver/\n", encoding="utf-8")
        return True
    else:
        gi.write_text("# added by deliveryman skill\noutput/deliver/\n", encoding="utf-8")
        return True


# -------- output_root rebase --------

DEFAULT_EXTRAS_FROM_ROOT = [
    "README*",
    "LICENSE*",
    "CHANGELOG*",
    "CONTRIBUTING*",
    "CODE_OF_CONDUCT*",
    ".gitignore",
    ".gitattributes",
]


def rebase_path(rel_path: str, output_root: str | None) -> str | None:
    """根据 output_root 计算交付目标的相对路径。
    - 若 output_root 为空：返回原 rel_path（无变换）。
    - 若 rel_path 落在 output_root 之下：去掉前缀，作为目标路径。
    - 否则返回 None，表示该文件不在交付物根范围内（caller 需另判 extras_from_root）。
    """
    if not output_root:
        return rel_path
    output_root = _norm(output_root).strip("/")
    rel = _norm(rel_path)
    if rel == output_root:
        return ""
    if rel.startswith(output_root + "/"):
        return rel[len(output_root) + 1:]
    return None


def is_extra_from_root(rel_path: str, extras: list) -> bool:
    """判断该文件是否是"项目根的对外门面"（README/LICENSE 等）——只看顶层。"""
    rel = _norm(rel_path)
    if "/" in rel:
        return False
    return match_any(rel, extras or [])


# -------- 裁决固化（state）--------
# state 是"对已见实例的确认"缓存，不是配置：丢失/损坏的唯一后果是多问一遍（安全方向）。
# 开放式规则（keep/remove/custom_redactions）永不进 state——它们改变交付计划，必须活在
# 用户手维护、可版本控制的 deliver.config.yaml 里。

STATE_SCHEMA_VERSION = 1


def state_path(project_root: Path) -> Path:
    """裁决固化文件：<project>/output/deliver/_state.json。
    该位置三重保证永不外泄：.gitignore 覆盖、walk 遍历剪枝、deliver_self 黑名单兜底。
    """
    return deliver_root(project_root) / "_state.json"


def load_state(project_root: Path):
    """容错读取 state。返回 (state_dict, status)，status ∈ ok/absent/corrupt/schema/root_mismatch；
    非 ok 时 state_dict 为空。任何异常都降级为"无 state"，绝不 fail-fast——state 是可丢缓存，
    缓存坏了若能中止打包，缓存就成了承重墙。
    root_mismatch 是反投毒闸门：克隆/拷贝来的项目里混入的 _state.json（记录的项目根与
    当前不符）不得生效，其"已接受"清单一律作废、回到全量询问。
    """
    p = state_path(project_root)
    if not p.is_file():
        return {}, "absent"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}, "corrupt"
    if not isinstance(data, dict):
        return {}, "corrupt"
    if data.get("schema_version") != STATE_SCHEMA_VERSION:
        return {}, "schema"
    stored_root = str(data.get("project_root") or "")
    real_root = str(project_root.resolve())
    if os.name == "nt":
        same = stored_root.casefold() == real_root.casefold()
    else:
        same = stored_root == real_root
    if not same:
        return {}, "root_mismatch"
    return data, "ok"


def save_state(project_root: Path, state: dict) -> bool:
    """原子写 state。tmp 文件必须建在目标同目录——同卷才能 os.replace；
    跨盘（如 TEMP 在 C: 而项目在 D:）会抛 ERROR_NOT_SAME_DEVICE。
    写失败不致命（交付已完成，缓存写失败不能反噬）：返回 False 由调用方告警。
    """
    p = state_path(project_root)
    assert_under(project_root, p)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(state)
        payload["schema_version"] = STATE_SCHEMA_VERSION
        payload["project_root"] = str(project_root.resolve())
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            os.replace(tmp, p)
        except PermissionError:
            # Windows 下目标被占用：退化为直接覆写，不中止
            p.write_text(text, encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass
        return True
    except Exception:
        return False


def normalize_detect_value(value: str) -> str:
    """detect 命中值的折叠规范化：连续 \\ 与 / 段统一为单个 /；
    仅盘符开头（^[A-Za-z]:，即 Windows 路径）casefold，POSIX 路径保留大小写。
    带盘符的路径：盘符大小写、斜杠方向不同视为同一指纹；不带盘符的路径大小写不同则视为不同指纹。
    """
    v = re.sub(r"[\\/]+", "/", (value or "").strip())
    if re.match(r"^[A-Za-z]:", v):
        v = v.casefold()
    return v


def safe_overlay_candidate(project_root: Path, token: str):
    """叠加判断 token → 项目内候选路径；绝对路径/盘符/UNC/上跳（..）token 一律返回 None。
    必须在 is_file() 等任何文件系统探测【之前】调用——恶意 CLAUDE.md 里的
    `\\\\attacker\\share\\x.md` 若直接拼 Path 再 stat，Windows 会向外发起 SMB/NTLM 连接。
    本函数纯词法判断，不触碰文件系统。
    """
    t = (token or "").strip()
    if t.startswith("./"):  # 只剥 "./" 前缀，不可用 lstrip——会吞掉 dotfile 的点
        t = t[2:]
    t = t.rstrip("/")
    n = _norm(t)  # 反斜杠统一为 /；UNC \\\\host 变 //host，被下面的绝对路径判定拒绝
    if not n or n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return None
    parts = [seg for seg in n.split("/") if seg not in ("", ".")]
    if not parts or any(seg == ".." for seg in parts):
        return None
    return project_root.joinpath(*parts)


def is_safe_relative_spec(token: str) -> bool:
    """词法闸门：user.entry / user.artifacts 的【每个值】必须是项目内相对路径或 glob。
    与 safe_overlay_candidate 同构——拒绝绝对路径/盘符/UNC/`..` 上跳，但接受 `*`/`**` 通配
    （artifacts 是 include 通道，值可含 glob），且【不触碰文件系统】。越界返回 False，调用方 fail-fast。
    """
    t = (token or "").strip()
    if t.startswith("./"):
        t = t[2:]
    t = t.rstrip("/")
    n = _norm(t)  # 反斜杠统一为 /；UNC \\host 变 //host，被下面的绝对路径判定拒绝
    if not n or n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return False
    parts = [seg for seg in n.split("/") if seg not in ("", ".")]
    if not parts or any(seg == ".." for seg in parts):
        return False
    return True


# -------- 文件大小友好显示 --------

def humansize(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}{unit}"
        n /= 1024
    return f"{n:.1f}TB"
