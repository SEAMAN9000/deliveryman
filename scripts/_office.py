"""_office.py — OOXML（docx/pptx/xlsx 家族）隐私处理。

三件事：
1. 魔数探测 office_container_kind —— PK 头=ooxml（可清洗）/ OLE 头=ole（加密或旧格式，不可清洗）。
2. L1 清洗 rewrite_ooxml—— 只改 docProps/core.xml 与 docProps/app.xml，
   在【原始 XML 字节】上做正则靶向替换（禁用 xml.etree 整树重序列化，它会丢命名空间声明、
   把 xsi:type 值里的 QName 前缀弄成未声明）；写 dst 后跑五道往返验证，任一失败 → 该文件不落盘。
3. L2 检测 inspect_ooxml—— xml.etree localname 计数；单 part 超限先熔断降级为"存在性"，
   防解压炸弹在 ET.fromstring 阶段 OOM。

只用标准库：zipfile / xml.etree.ElementTree / re / pathlib。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

# ---- 覆盖扩展名（计划 §1.5 union）----
OOXML_EXTS = frozenset({
    ".docx", ".docm", ".dotx",
    ".pptx", ".pptm", ".potx", ".ppsx",
    ".xlsx", ".xlsm", ".xltx",
})
# 旧版 OLE 复合文档格式（不可清洗，原样复制并在报告里警告）
OLE_OFFICE_EXTS = frozenset({".doc", ".xls", ".ppt"})

# 只改这两个 part，其余逐 entry 字节拷贝
DOCPROPS_TARGETS = ("docProps/core.xml", "docProps/app.xml")

# 单 part 解析上限——超过先熔断，不 read/parse
DEFAULT_PART_SIZE_LIMIT = 10 * 1024 * 1024

# 容器读操作可能抛的异常集合（fail-closed：任一即视为不可清洗/坏容器）
_ZIP_ERRORS = (zipfile.BadZipFile, OSError, RuntimeError, EOFError, NotImplementedError)


# ==================== 魔数探测 ====================

def office_container_kind(path) -> str:
    """读前 8 字节判容器类型：
      - PK\\x03\\x04            → "ooxml"（zip 容器，可清洗）
      - \\xD0\\xCF\\x11\\xE0    → "ole"（OLE 复合文档：加密 docx 或旧 .doc/.xls/.ppt，不可清洗）
      - 其它                    → "unknown"
    """
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return "unknown"
    if head[:4] == b"PK\x03\x04":
        return "ooxml"
    if head[:4] == b"\xD0\xCF\x11\xE0":
        return "ole"
    return "unknown"


# ==================== L1 字节级靶向替换 ====================

def _elem_regex(localname: bytes):
    """构造匹配单个扁平元素（含可选前缀、可选属性、配对或自闭合）的字节级正则。
    (?![\\w.\\-]) 防止 creator 误命中 creatorX；DOTALL 让 .*? 跨行匹配元素内容。
    捕获组 1 = 前缀（含冒号，如 b"dc:"；无前缀时为 b""）——替换时按源保留。
    """
    L = re.escape(localname)
    return re.compile(
        rb"<((?:[\w.\-]+:)?)" + L + rb"(?![\w.\-])"
        rb"(?:\s[^>]*?)?"
        rb"(?:/>|>.*?</(?:[\w.\-]+:)?" + L + rb"\s*>)",
        re.DOTALL,
    )


def _sub_empty(data: bytes, localname: str):
    """把元素改写为自闭合空元素 <pfx:local/>（保留源前缀），至多一次。返回 (new_bytes, n)。"""
    L = localname.encode("utf-8")
    rx = _elem_regex(L)

    def repl(m):
        return b"<" + m.group(1) + L + b"/>"

    return rx.subn(repl, data, count=1)


def _sub_set(data: bytes, localname: str, value: bytes):
    """把元素内容替换为 value（保留源前缀），至多一次。返回 (new_bytes, n)。"""
    L = localname.encode("utf-8")
    rx = _elem_regex(L)

    def repl(m):
        p = m.group(1)
        return b"<" + p + L + b">" + value + b"</" + p + L + b">"

    return rx.subn(repl, data, count=1)


def _rewrite_docprops_bytes(data: bytes, part_name: str, office_rules: dict):
    """按处置表对单个 docProps XML 字节做靶向替换。返回 (new_bytes, change_count)。"""
    if part_name.endswith("core.xml"):
        rules = (office_rules or {}).get("core_properties") or []
    elif part_name.endswith("app.xml"):
        rules = (office_rules or {}).get("app_properties") or []
    else:
        return data, 0
    total = 0
    for r in rules:
        if not isinstance(r, dict):
            continue
        action = r.get("action")
        local = r.get("localname")
        if not local:
            continue
        if action == "empty":
            data, n = _sub_empty(data, local)
            total += n
        elif action == "set":
            val = str(r.get("value", "")).encode("utf-8")
            data, n = _sub_set(data, local, val)
            total += n
        # keep_report / keep：L1 不动
    return data, total


# ==================== 五道往返验证 ====================

_DECLARED_NS_RE = re.compile(r"xmlns:([A-Za-z_][\w.\-]*)\s*=")
_ATTR_VAL_RE = re.compile(r'=\s*"([^"]*)"|=\s*\'([^\']*)\'')
_QNAME_RE = re.compile(r"^([A-Za-z_][\w.\-]*):[A-Za-z_][\w.\-]*$")


def _qname_prefixes_declared(xml: bytes) -> bool:
    """第五道验证：输出中所有【内容内 QName】（典型如 xsi:type="dcterms:W3CDTF" 的值）
    的前缀在文档里仍有 xmlns 声明。ET.fromstring 只校验元素/属性名的前缀，不校验属性【值】里的
    QName——这正是 ET 整树重序列化会悄悄弄坏、四道验证却全绿放行的缺口。
    """
    text = xml.decode("utf-8", errors="replace")
    declared = set(_DECLARED_NS_RE.findall(text))
    declared.add("xml")  # 内建前缀
    for m in _ATTR_VAL_RE.finditer(text):
        val = m.group(1) if m.group(1) is not None else m.group(2)
        if val is None:
            continue
        q = _QNAME_RE.match(val)
        if q and q.group(1) not in declared:
            return False
    return True


def _verify_roundtrip(dst: Path, src_names, src_crc: dict, rewritten: dict):
    """写 dst 后五道往返验证。返回 (ok, reasons)。"""
    try:
        with zipfile.ZipFile(dst, "r") as z:
            # ① 完整性
            bad = z.testzip()
            if bad is not None:
                return False, [f"testzip 失败: {bad}"]
            # ② namelist 与源集合一致
            if set(z.namelist()) != set(src_names):
                return False, ["namelist 与源集合不一致"]
            # ③ 改写后的 docProps XML 可 fromstring
            for name in rewritten:
                try:
                    ET.fromstring(z.read(name))
                except ET.ParseError as e:
                    return False, [f"{name} 改写后无法解析: {e}"]
            # ④ 非 docProps entry 的 CRC 与源逐条相等
            for zi in z.infolist():
                if zi.filename in rewritten:
                    continue
                if zi.CRC != src_crc.get(zi.filename):
                    return False, [f"{zi.filename} CRC 与源不一致（非 docProps entry 被改动）"]
            # ⑤ 内容内 QName 前缀声明完整
            for name in rewritten:
                if not _qname_prefixes_declared(z.read(name)):
                    return False, [f"{name} 存在未声明前缀的内容内 QName"]
    except _ZIP_ERRORS as e:
        return False, [f"验证阶段打开 dst 失败: {type(e).__name__}"]
    return True, []


def _safe_unlink(p: Path) -> None:
    try:
        if p.exists():
            p.unlink()
    except OSError:
        pass


def rewrite_ooxml(src, dst, office_rules: dict):
    """L1 清洗：把 src（OOXML 容器）清洗后写到 dst。
    只改 docProps/core.xml 与 docProps/app.xml（字节级靶向替换），其余 entry 逐条字节拷贝；
    entry 名保序、compress_type 沿用源、date_time 统一 (1980,1,1,0,0,0)、external_attr 0o644<<16。
    写 dst 后跑五道往返验证；任一失败 → 删 dst（不留残留产物）、返回 (False, reasons)。
    返回 (True, []) 表示清洗成功且验证全绿。
    """
    src = Path(src)
    dst = Path(dst)
    # 1) 读源：全部 entry 数据 + CRC + compress_type + 名序
    try:
        with zipfile.ZipFile(src, "r") as zin:
            infos = zin.infolist()
            src_names = [zi.filename for zi in infos]
            src_data = {zi.filename: zin.read(zi.filename) for zi in infos}
            src_crc = {zi.filename: zi.CRC for zi in infos}
            src_compress = {zi.filename: zi.compress_type for zi in infos}
    except _ZIP_ERRORS as e:
        _safe_unlink(dst)
        return False, [f"打开源容器失败（坏 zip / 加密 / 不支持）: {type(e).__name__}"]

    # 2) 字节级改写目标 part（源里存在才改）
    rewritten = {}
    for name in DOCPROPS_TARGETS:
        if name in src_data:
            new_bytes, _n = _rewrite_docprops_bytes(src_data[name], name, office_rules)
            rewritten[name] = new_bytes

    # 3) 写 dst：逐 entry read→writestr，保序、保 compress_type、统一时戳与权限位
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dst, "w") as zout:
            for name in src_names:
                data = rewritten.get(name, src_data[name])
                nzi = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
                nzi.compress_type = src_compress.get(name, zipfile.ZIP_DEFLATED)
                nzi.external_attr = 0o644 << 16
                zout.writestr(nzi, data)
    except _ZIP_ERRORS as e:
        _safe_unlink(dst)
        return False, [f"写 dst 失败: {type(e).__name__}"]

    # 4) 五道往返验证——任一失败 fail-closed
    ok, reasons = _verify_roundtrip(dst, src_names, src_crc, rewritten)
    if not ok:
        _safe_unlink(dst)
        return False, reasons
    return True, []


# ==================== L2 内部痕迹检测 ====================

def _local(tag):
    """ET tag "{ns}local" → "local"；已是裸名则原样返回。"""
    if isinstance(tag, str) and "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _attr_by_local(elem, attr_local):
    """按本地名取属性值（忽略命名空间前缀）。"""
    for k, v in elem.attrib.items():
        if _local(k) == attr_local:
            return v
    return None


def _count_local(root, localname) -> int:
    return sum(1 for e in root.iter() if _local(e.tag) == localname)


def _collect_attr(root, elem_local, attr_local, limit=6):
    out = []
    seen = set()
    for e in root.iter():
        if _local(e.tag) != elem_local:
            continue
        v = _attr_by_local(e, attr_local)
        if v and v not in seen:
            seen.add(v)
            out.append(v)
        if len(out) >= limit:
            break
    return out


def _existence_finding(part, kind, label):
    """大文件降级/仅确认存在时的占位 finding（count=None 表示未逐项枚举）。"""
    return {"part": part, "kind": kind, "label": label,
            "count": None, "sample": "(大文件已降级，仅确认存在)", "degraded": True}


_NOTES_RE = re.compile(r"^ppt/notesSlides/notesSlide\d+\.xml$")
_XL_COMMENTS_RE = re.compile(r"^xl/comments\d+\.xml$")
_XL_EXTLINK_RE = re.compile(r"^xl/externalLinks/externalLink\d+\.xml$")


def inspect_ooxml(path, size_limit=DEFAULT_PART_SIZE_LIMIT) -> dict:
    """L2：检测 OOXML 内部残留痕迹（批注/修订/隐藏表/notes 作者/自定义属性/缩略图 等），
    只报数不改写。返回 {"findings": [...], "degraded": [part...], "parse_errors": [part...]}。

    每个 finding: {part, kind, label, count(int|None), sample(str), degraded(bool)}。
    单 part 先查 ZipInfo.file_size，超 size_limit → 熔断，跳过 read/parse、降级为"存在性"。
    """
    path = Path(path)
    findings = []
    degraded = []
    parse_errors = []
    ext = path.suffix.lower()

    try:
        z = zipfile.ZipFile(path, "r")
    except _ZIP_ERRORS as e:
        return {"findings": findings, "degraded": degraded,
                "parse_errors": [f"container:{type(e).__name__}"]}

    try:
        infos = {zi.filename: zi for zi in z.infolist()}
        names = list(infos.keys())

        def parse(name):
            """返回 (root|None, status)；status ∈ ok/absent/degraded/error。
            file_size 熔断必须发生在 read 之前。
            """
            zi = infos.get(name)
            if zi is None:
                return None, "absent"
            if zi.file_size > size_limit:
                return None, "degraded"
            try:
                return ET.fromstring(z.read(name)), "ok"
            except (ET.ParseError, *_ZIP_ERRORS):
                return None, "error"

        # ----- docx -----
        if ext in (".docx", ".docm", ".dotx"):
            if "word/comments.xml" in infos:
                root, st = parse("word/comments.xml")
                if st == "degraded":
                    degraded.append("word/comments.xml")
                    findings.append(_existence_finding("word/comments.xml", "docx_comments", "批注"))
                elif st == "error":
                    parse_errors.append("word/comments.xml")
                elif root is not None:
                    n = _count_local(root, "comment")
                    if n:
                        authors = _collect_attr(root, "comment", "author")
                        findings.append({"part": "word/comments.xml", "kind": "docx_comments",
                                         "label": "批注", "count": n,
                                         "sample": ", ".join(authors), "degraded": False})
            if "word/document.xml" in infos:
                root, st = parse("word/document.xml")
                if st == "degraded":
                    degraded.append("word/document.xml")
                    findings.append(_existence_finding("word/document.xml", "docx_revisions", "修订(ins/del)"))
                elif st == "error":
                    parse_errors.append("word/document.xml")
                elif root is not None:
                    # 只计 w:ins + w:del；格式类 w:rPrChange/w:pPrChange 不计
                    n = _count_local(root, "ins") + _count_local(root, "del")
                    if n:
                        findings.append({"part": "word/document.xml", "kind": "docx_revisions",
                                         "label": "修订(ins/del)", "count": n, "sample": "", "degraded": False})
            if "word/settings.xml" in infos:
                root, st = parse("word/settings.xml")
                if st == "degraded":
                    degraded.append("word/settings.xml")
                elif st == "error":
                    parse_errors.append("word/settings.xml")
                elif root is not None and _count_local(root, "trackChanges") > 0:
                    findings.append({"part": "word/settings.xml", "kind": "docx_track_changes",
                                     "label": "修订跟踪仍开启", "count": 1, "sample": "", "degraded": False})
            if "word/people.xml" in infos:
                root, st = parse("word/people.xml")
                if st == "degraded":
                    degraded.append("word/people.xml")
                    findings.append(_existence_finding("word/people.xml", "docx_people", "审阅者(people)"))
                elif st == "error":
                    parse_errors.append("word/people.xml")
                elif root is not None:
                    n = _count_local(root, "person")
                    if n:
                        names_s = _collect_attr(root, "person", "author")
                        findings.append({"part": "word/people.xml", "kind": "docx_people",
                                         "label": "审阅者(people)", "count": n,
                                         "sample": ", ".join(names_s), "degraded": False})

        # ----- pptx -----
        elif ext in (".pptx", ".pptm", ".potx", ".ppsx"):
            note_parts = sorted(n for n in names if _NOTES_RE.match(n))
            nonempty = 0
            any_deg = False
            for np_ in note_parts:
                root, st = parse(np_)
                if st == "degraded":
                    degraded.append(np_)
                    any_deg = True
                    continue
                if st == "error":
                    parse_errors.append(np_)
                    continue
                if root is not None:
                    # 空 notes 是常态——只计含非空 a:t 文本的 notesSlide（否则系统性误报）
                    if any(_local(e.tag) == "t" and (e.text or "").strip() for e in root.iter()):
                        nonempty += 1
            if nonempty:
                findings.append({"part": "ppt/notesSlides/", "kind": "pptx_notes",
                                 "label": "备注(非空)", "count": nonempty, "sample": "", "degraded": False})
            elif any_deg:
                findings.append(_existence_finding("ppt/notesSlides/", "pptx_notes", "备注(非空)"))
            if "ppt/commentAuthors.xml" in infos:
                root, st = parse("ppt/commentAuthors.xml")
                if st == "degraded":
                    degraded.append("ppt/commentAuthors.xml")
                    findings.append(_existence_finding("ppt/commentAuthors.xml", "pptx_comment_authors", "批注作者"))
                elif st == "error":
                    parse_errors.append("ppt/commentAuthors.xml")
                elif root is not None:
                    authors = _collect_attr(root, "cmAuthor", "name", limit=10)
                    if authors:
                        findings.append({"part": "ppt/commentAuthors.xml", "kind": "pptx_comment_authors",
                                         "label": "批注作者", "count": len(authors),
                                         "sample": ", ".join(authors), "degraded": False})

        # ----- xlsx -----
        elif ext in (".xlsx", ".xlsm", ".xltx"):
            if "xl/workbook.xml" in infos:
                root, st = parse("xl/workbook.xml")
                if st == "degraded":
                    degraded.append("xl/workbook.xml")
                    findings.append(_existence_finding("xl/workbook.xml", "xlsx_hidden_sheets", "隐藏表"))
                elif st == "error":
                    parse_errors.append("xl/workbook.xml")
                elif root is not None:
                    hidden = 0
                    hidden_names = []
                    for e in root.iter():
                        if _local(e.tag) == "sheet":
                            state = _attr_by_local(e, "state")
                            if state in ("hidden", "veryHidden"):
                                hidden += 1
                                nm = _attr_by_local(e, "name")
                                if nm:
                                    hidden_names.append(nm)
                    if hidden:
                        findings.append({"part": "xl/workbook.xml", "kind": "xlsx_hidden_sheets",
                                         "label": "隐藏表", "count": hidden,
                                         "sample": ", ".join(hidden_names[:5]), "degraded": False})
            comment_parts = [n for n in names if _XL_COMMENTS_RE.match(n)]
            total_comments = 0
            cdeg = False
            for cp_ in comment_parts:
                root, st = parse(cp_)
                if st == "degraded":
                    degraded.append(cp_)
                    cdeg = True
                    continue
                if st == "error":
                    parse_errors.append(cp_)
                    continue
                if root is not None:
                    total_comments += _count_local(root, "comment")
            if total_comments:
                findings.append({"part": "xl/comments*.xml", "kind": "xlsx_comments",
                                 "label": "批注", "count": total_comments, "sample": "", "degraded": False})
            elif cdeg:
                findings.append(_existence_finding("xl/comments*.xml", "xlsx_comments", "批注"))
            ext_links = [n for n in names if _XL_EXTLINK_RE.match(n)]
            if ext_links:
                findings.append({"part": "xl/externalLinks/", "kind": "xlsx_external_links",
                                 "label": "外部链接", "count": len(ext_links), "sample": "", "degraded": False})

        # ----- 通用：custom.xml 属性名 + 截断样本；thumbnail 存在性 -----
        if "docProps/custom.xml" in infos:
            root, st = parse("docProps/custom.xml")
            if st == "degraded":
                degraded.append("docProps/custom.xml")
                findings.append(_existence_finding("docProps/custom.xml", "custom_props", "自定义属性"))
            elif st == "error":
                parse_errors.append("docProps/custom.xml")
            elif root is not None:
                pairs = []
                for e in root.iter():
                    if _local(e.tag) == "property":
                        nm = _attr_by_local(e, "name") or "?"
                        val = ""
                        for c in list(e):
                            if (c.text or "").strip():
                                val = c.text.strip()
                                break
                        pairs.append((nm, val[:20]))
                if pairs:
                    sample = "; ".join(f"{k}={v}" for k, v in pairs[:5])
                    findings.append({"part": "docProps/custom.xml", "kind": "custom_props",
                                     "label": "自定义属性", "count": len(pairs),
                                     "sample": sample, "degraded": False})
        thumb = [n for n in names if n.lower().startswith("docprops/thumbnail.")]
        if thumb:
            findings.append({"part": thumb[0], "kind": "thumbnail", "label": "缩略图",
                             "count": 1, "sample": thumb[0].rsplit("/", 1)[-1], "degraded": False})
    finally:
        z.close()

    return {"findings": findings, "degraded": degraded, "parse_errors": parse_errors}
