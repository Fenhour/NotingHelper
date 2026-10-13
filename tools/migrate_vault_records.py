from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path
import re
import shutil


@dataclass
class Record:
    title: str
    source: str
    supplement: str = ""


def clean_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in value.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def split_records(text: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"(?m)^##\s+(?!#)(.+?)\s*$", text))
    result: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result.append((match.group(1).strip(), text[match.end() : end]))
    return result


def parse_lesson(path: Path) -> list[Record]:
    records: list[Record] = []
    for title, body in split_records(path.read_text(encoding="utf-8-sig")):
        if re.match(r"^\d+\.", title):
            title = re.sub(r"^\d+\.\s*", "", title)
        body = clean_text(body)
        marker = re.search(r"(?im)^[-=]{3,}\s*原文\s*[:：]?\s*$", body)
        if marker:
            supplement = clean_text(body[: marker.start()])
            source = clean_text(body[marker.end() :])
        else:
            supplement, source = "", body
        records.append(Record(title, source, supplement))
    return records


def parse_detail(path: Path) -> list[Record]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8-sig")
    pattern = re.compile(
        r"(?ms)^###\s*问题/知识点来源\s*\n.*?\n\n(.*?)(?=^###\s*内容补充和对原文的进一步说明|\Z)"
    )
    records: list[Record] = []
    for index, match in enumerate(pattern.finditer(text), 1):
        source = clean_text(match.group(1))
        next_start = match.end()
        next_match = re.match(
            r"(?ms)^###\s*内容补充和对原文的进一步说明\s*\n.*?\n\n(.*?)(?=^###\s*问题/知识点来源|\Z)",
            text[next_start:],
        )
        supplement = clean_text(next_match.group(1)) if next_match else ""
        title = f"迁移记录 {index}"
        records.append(Record(title, source, supplement))
    return records


def safe_stem(title: str, fallback: str) -> str:
    value = clean_text(title).split("\n", 1)[0]
    value = re.sub(r"[\[\]`*_#<>|{}\\/:\"?]", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")[:52].rstrip(" .")
    return value or fallback


def rewrite_images_and_links(text: str, source_dir: Path, body_dir: Path, materials_dir: Path) -> str:
    def replace_wikilink(match: re.Match[str]) -> str:
        name = match.group(1)
        candidates = [source_dir / name, source_dir.parent / name, source_dir.parent.parent / name]
        source = next((candidate for candidate in candidates if candidate.is_file()), source_dir / name)
        if not source.is_file() and source_dir.is_dir():
            source = next(iter(source_dir.rglob(name)), source)
        if source.is_file():
            target = body_dir / source.name
            shutil.copy2(source, target)
            return f"![[{target.name}]]"
        return match.group(0)

    text = re.sub(r"!\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", replace_wikilink, text)
    def replace_markdown_image(match: re.Match[str]) -> str:
        image_path = Path(match.group(2).replace("/", "\\"))
        source = image_path if image_path.is_absolute() else (source_dir / image_path)
        if source.is_file():
            target = body_dir / source.name
            shutil.copy2(source, target)
            return f"![[{target.name}]]"
        return match.group(0)

    return re.sub(r"!\[([^\]]*)\]\((<?[^)>]+>?)\)", replace_markdown_image, text)


def rewrite_source_links(text: str, body_dir: Path, materials_dir: Path) -> str:
    relative = Path(__import__("os").path.relpath(materials_dir, body_dir)).as_posix()
    text = text.replace("大模型与多模态纵览/NotingHelper-Sources/", f"{relative}/")
    return text.replace("NotingHelper-Sources/", f"{relative}/")


def migrate(vault: Path) -> None:
    vault = vault.resolve()
    body_root = vault / "正文库"
    materials_root = vault / "文档资料库"
    body_root.mkdir(parents=True, exist_ok=True)
    materials_root.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")

    old_sources = list(vault.rglob("NotingHelper-Sources"))
    for source_dir in old_sources:
        if source_dir.resolve() == materials_root.resolve() or not source_dir.is_dir():
            continue
        for item in source_dir.rglob("*"):
            if item.is_file():
                target = materials_root / item.relative_to(source_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copy2(item, target)

    targets = [
        (vault / "大模型与多模态纵览" / "Lesson1question.md", "大模型与多模态纵览"),
        (vault / "大模型与多模态纵览" / "recordQuestion.md", "大模型与多模态纵览"),
        (vault / "leetcodeHot100.md", "leetcodeHot100"),
    ]
    for index_path, category in targets:
        if not index_path.is_file():
            continue
        original = index_path.read_text(encoding="utf-8-sig")
        backup = index_path.with_name(f"{index_path.stem}.legacy-{today}.md")
        if not backup.exists():
            shutil.copy2(index_path, backup)

        if index_path.name.casefold() == "lesson1question.md":
            records = parse_lesson(index_path)
            detail = next(index_path.parent.glob("*-details-*.md"), None)
            detail_records = parse_detail(detail) if detail else []
            if detail_records:
                if len(records) >= 4:
                    detail_records[0].title = records[3].title
                    records[3:] = detail_records
                else:
                    records.extend(detail_records)
        elif index_path.name.casefold() == "recordquestion.md":
            records = parse_detail(next(index_path.parent.glob("*-details-*.md"), Path()))
            if records:
                records[0].title = "词嵌入层与语言模型头代码解析（未解决）"
        else:
            records = [Record("用哈希表实现两数之和", original.split("\n", 2)[-1])]

        links: list[str] = []
        for number, record in enumerate(records, 1):
            stem = safe_stem(record.title, f"未命名记录-{number}")
            digest = hashlib.sha256(f"{index_path}:{number}:{record.source}".encode("utf-8")).hexdigest()[:4]
            record_stem = f"{stem}-{today}-{digest}"
            record_dir = body_root / category / record_stem
            record_dir.mkdir(parents=True, exist_ok=True)
            body_path = record_dir / f"{record_stem}.md"
            source = rewrite_source_links(
                rewrite_images_and_links(record.source, index_path.parent, record_dir, materials_root),
                record_dir,
                materials_root,
            )
            supplement = rewrite_source_links(
                rewrite_images_and_links(record.supplement, index_path.parent, record_dir, materials_root),
                record_dir,
                materials_root,
            )
            source_anchor = f"nh-body-source-{digest}{number:02d}"
            supplement_anchor = f"nh-body-supplement-{digest}{number:02d}"
            body_parts = [f"# {record.title}", "", "## 原文", "", f"^{source_anchor}", "", source or "（未提取到原文）"]
            if supplement:
                body_parts.extend(["", "## 补充说明", "", f"^{supplement_anchor}", "", supplement])
            body_path.write_text("\n".join(body_parts).rstrip() + "\n", encoding="utf-8")
            relative = body_path.relative_to(index_path.parent).as_posix() if body_path.is_relative_to(index_path.parent) else body_path.relative_to(vault).as_posix()
            links.append(f"- [[{relative}#^{source_anchor}|原文]]")
            if supplement:
                links.append(f"- [[{relative}#^{supplement_anchor}|补充说明]]")

        heading = f"# {index_path.stem}"
        index_path.write_text(heading + "\n\n" + "\n".join(links) + "\n", encoding="utf-8")

    print(f"迁移完成：{vault}")
    print(f"正文库：{body_root}")
    print(f"文档资料库：{materials_root}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="将 NotingHelper 旧问题文档迁移为索引/正文/资料库结构")
    parser.add_argument("vault", type=Path)
    migrate(parser.parse_args().vault)
