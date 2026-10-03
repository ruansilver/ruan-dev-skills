#!/usr/bin/env python3
"""ruan-dev-skills 2.0 的机械辅助脚本（标准库 + git）。"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def git(root: Path, *args: str, inp: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.quotepath=off", "-C", str(root), *args],
        capture_output=True, text=True, input=inp,
    )


def fail(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def top_level(path: Path) -> Path | None:
    result = git(path, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


def under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def walk_files(base: Path) -> dict[str, Path]:
    """List regular files relative to base; exclude only base/.git."""
    base = base.resolve()
    if base.is_file():
        return {base.name: base}
    result: dict[str, Path] = {}
    for directory, dirs, files in os.walk(base, followlinks=False):
        directory_path = Path(directory)
        if directory_path == base:
            dirs[:] = [name for name in dirs if name != ".git"]
        for name in files:
            path = directory_path / name
            result[path.relative_to(base).as_posix()] = path
    return result


def empty_dirs(base: Path) -> list[str]:
    if not base.is_dir():
        return []
    found = []
    for directory, dirs, files in os.walk(base, followlinks=False):
        directory_path = Path(directory)
        if directory_path == base:
            dirs[:] = [name for name in dirs if name != ".git"]
        if not dirs and not files and directory_path != base:
            found.append(directory_path.relative_to(base).as_posix())
    return found


def symlinks_inside(source: Path) -> list[str]:
    if source.is_file():
        return []
    found: list[str] = []
    for directory, dirs, files in os.walk(source, followlinks=False):
        directory_path = Path(directory)
        if directory_path == source:
            dirs[:] = [name for name in dirs if name != ".git"]
        for name in list(dirs) + list(files):
            path = directory_path / name
            if path.is_symlink():
                found.append(path.relative_to(source).as_posix())
    return found


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest_entries(base: Path) -> list[dict[str, object]]:
    files = walk_files(base)
    return [
        {"path": rel, "size": files[rel].stat().st_size, "sha256": sha256(files[rel])}
        for rel in sorted(files)
    ]


def compare_files(left: Path, right: Path) -> list[str]:
    left_files, right_files = walk_files(left), walk_files(right)
    problems: list[str] = []
    for rel in sorted(set(left_files) | set(right_files)):
        if rel not in right_files:
            problems.append(f"缺失: {rel}")
        elif rel not in left_files:
            problems.append(f"多出: {rel}")
        elif left_files[rel].stat().st_size != right_files[rel].stat().st_size or sha256(left_files[rel]) != sha256(right_files[rel]):
            problems.append(f"内容不同: {rel}")
    return problems


def check_manifest(plan_dir: Path) -> tuple[dict, list[str]]:
    path = plan_dir / "归档清单.json"
    if not path.is_file():
        fail(f"归档清单不存在: {path}", 4)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        source_name = data["source_name"]
        entries = data["entries"]
        if not isinstance(source_name, str) or not isinstance(entries, list):
            raise ValueError("字段类型错误")
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        fail(f"归档清单无法读取: {path}: {exc}", 4)
    inner = plan_dir / source_name
    if not inner.exists():
        fail(f"归档内容不存在: {inner}", 4)
    actual = manifest_entries(inner)
    expected = sorted(entries, key=lambda item: item.get("path", ""))
    return data, ([] if actual == expected else ["归档清单与归档副本不一致"])


def ensure_root(root: Path, initialize: bool) -> str:
    root = root.resolve()
    if root.name != "AI dev docs":
        fail(f"资料仓目录名必须是 'AI dev docs'，当前为 '{root.name}'。")
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    current = top_level(probe)
    if current is None:
        if not initialize:
            return "no-repo"
        root.mkdir(parents=True, exist_ok=True)
        result = git(root, "init")
        if result.returncode != 0:
            fail(f"资料仓 git init 失败: {result.stderr.strip()}")
        return "initialized"
    if current != root:
        fail(f"资料仓位于另一个 Git 仓内: {current}。请先处理仓库边界。")
    return "ok"


def check_code(code: Path) -> dict[str, object]:
    code = code.resolve()
    if top_level(code) != code:
        fail(f"代码仓必须是 Git 根: {code}")
    head = git(code, "rev-parse", "HEAD")
    status = git(code, "status", "--porcelain", "--untracked-files=all")
    return {"code_head": head.stdout.strip() if head.returncode == 0 else None, "code_dirty": status.stdout.splitlines()}


def cmd_check_root(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    state = ensure_root(root, args.init)
    result: dict[str, object] = {"root": str(root), "state": state}
    if args.code:
        code = Path(args.code).resolve()
        result.update(check_code(code))
        if args.version_dir:
            result["code_rel"] = Path(os.path.relpath(code, Path(args.version_dir).resolve())).as_posix()
    emit(result)


def source_for_archive(source_arg: str, root: Path) -> Path:
    raw = Path(source_arg)
    if raw.is_symlink():
        fail("需求输入本身是符号链接，请提供真实文件或真实目录。")
    source = raw.resolve()
    if not source.exists():
        fail(f"需求材料不存在: {source}")
    if under(source, root):
        fail("需求源位于 AI dev docs 内部，禁止再次摄取。")
    if source.is_dir():
        links = symlinks_inside(source)
        if links:
            fail("需求材料内部存在符号链接，禁止直接摄取:\n" + "\n".join(links))
        empties = empty_dirs(source)
        if empties:
            fail("需求材料包含 Git 无法表达的空目录:\n" + "\n".join(empties))
    if not source.is_file() and not source.is_dir():
        fail(f"需求材料必须是文件或目录: {source}")
    return source


def next_pid(plans: Path) -> str:
    ids = [int(match.group(1)) for item in plans.iterdir() if (match := re.match(r"P(\d+)-", item.name))]
    return f"P{max(ids, default=0) + 1:03d}"


def pending_plan_dirs(root: Path, plans: Path) -> list[str]:
    result = git(root, "status", "--porcelain", "--untracked-files=all", "--", plans.relative_to(root).as_posix())
    pending = set()
    for line in result.stdout.splitlines():
        match = re.search(r"(?:^|/)plans/(P\d+-[^/\\s]+)", line)
        if match:
            pending.add(match.group(1))
    return sorted(pending)


def set_scoped_attributes(root: Path) -> None:
    path = root / ".gitattributes"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if not any(line.strip() == "plans/** -text" for line in text.splitlines()):
        separator = "" if not text or text.endswith("\n") else "\n"
        path.write_text(text + separator + "plans/** -text\n", encoding="utf-8")


def cmd_archive(args: argparse.Namespace) -> None:
    root, version = Path(args.root).resolve(), Path(args.version_dir).resolve()
    if ensure_root(root, False) not in {"ok", "initialized"}:
        fail("资料仓不是独立 Git 根，请先运行 check-root。")
    try:
        version.relative_to(root)
    except ValueError:
        fail("版本目录不在 AI dev docs 内。")
    if not version.is_dir():
        fail(f"版本目录不存在，请先创建: {version}")
    source = source_for_archive(args.source, root)
    plans = version / "plans"
    plans.mkdir(exist_ok=True)
    pending = pending_plan_dirs(root, plans)
    if pending:
        fail("plans/ 下存在未提交的 Pxxx，请先处理:\n" + "\n".join(pending), 3)
    pid = next_pid(plans)
    label = re.sub(r"[^\w\-]+", "-", args.label or "input", flags=re.UNICODE).strip("-")[:30] or "input"
    plan_dir = plans / f"{pid}-{dt.date.today().isoformat()}-{label}"
    plan_dir.mkdir()
    inner = plan_dir / source.name
    try:
        if source.is_dir():
            def ignore_root_git(directory: str, names: list[str]) -> set[str]:
                return {".git"} if Path(directory).resolve() == source else set()
            shutil.copytree(source, inner, symlinks=False, ignore=ignore_root_git)
        else:
            shutil.copy2(source, inner)
        differences = compare_files(source, inner)
        if differences:
            raise RuntimeError("复制后与来源不一致:\n" + "\n".join(differences))
        set_scoped_attributes(root)
        manifest = {
            "format": 1,
            "source_name": source.name,
            "source_type": "directory" if source.is_dir() else "file",
            "entries": manifest_entries(inner),
        }
        (plan_dir / "归档清单.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        relative_plan = plan_dir.relative_to(root).as_posix()
        tracked_paths = [f"{relative_plan}/归档清单.json"] + [f"{relative_plan}/{source.name}/{entry['path']}" for entry in manifest["entries"]]
        if source.is_file():
            tracked_paths = [f"{relative_plan}/{source.name}", f"{relative_plan}/归档清单.json"]
        ignored = git(root, "check-ignore", "-z", "--stdin", inp="\0".join(dict.fromkeys(tracked_paths)) + "\0")
        ignored_paths = [path for path in ignored.stdout.split("\0") if path]
        if ignored_paths:
            raise RuntimeError("以下归档文件会被 .gitignore 忽略:\n" + "\n".join(ignored_paths))
    except Exception as exc:
        shutil.rmtree(plan_dir, ignore_errors=True)
        fail(str(exc), 4)
    emit({"pid": pid, "dir": plan_dir.relative_to(root).as_posix(), "files": len(manifest["entries"])})


def locate_plan(root: Path, plan_dir: Path) -> tuple[Path, str]:
    root, plan_dir = root.resolve(), plan_dir.resolve()
    if top_level(root) != root:
        fail("资料仓不是独立 Git 根。")
    try:
        relative = plan_dir.relative_to(root).as_posix()
    except ValueError:
        fail("Pxxx 目录不在资料仓内。")
    parts = relative.split("/")
    if len(parts) < 3 or parts[-2] != "plans" or not re.fullmatch(r"P\d+-[^/]+", parts[-1]) or not plan_dir.is_dir():
        fail(f"非法 Pxxx 目录: {plan_dir}")
    return plan_dir, relative


def cmd_verify(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    plan_dir, relative = locate_plan(root, Path(args.plan_dir))
    manifest, problems = check_manifest(plan_dir)
    expected = {f"{relative}/归档清单.json"}
    if manifest["source_type"] == "file":
        expected.add(f"{relative}/{manifest['source_name']}")
    else:
        expected |= {f"{relative}/{manifest['source_name']}/{entry['path']}" for entry in manifest["entries"]}
    tracked = set(filter(None, git(root, "ls-files", "-z", "--", relative).stdout.split("\0")))
    missing = sorted(expected - tracked)
    dirty = git(root, "status", "--porcelain", "--untracked-files=all", "--", relative).stdout.splitlines()
    if problems or missing or dirty:
        details = problems + (["Git 未跟踪:"] + missing if missing else []) + (["工作树未提交:"] + dirty if dirty else [])
        fail("归档未完整入库:\n" + "\n".join(details), 4)
    emit({"verified": len(expected), "manifest_entries": len(manifest["entries"])})


def cmd_same(args: argparse.Namespace) -> None:
    plan_dir = Path(args.plan_dir).resolve()
    root = plan_dir.parents[2]
    plan_dir, _ = locate_plan(root, plan_dir)
    source = source_for_archive(args.source, root)
    manifest, problems = check_manifest(plan_dir)
    if problems:
        fail("归档清单与归档副本不一致。", 4)
    if source.name != manifest["source_name"]:
        emit({"same": False, "problems": [f"输入名称 {source.name} 与归档名称 {manifest['source_name']} 不同"]})
        raise SystemExit(1)
    differences = compare_files(source, plan_dir / manifest["source_name"])
    emit({"same": not differences, "problems": differences})
    raise SystemExit(0 if not differences else 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    check = subs.add_parser("check-root")
    check.add_argument("--root", required=True); check.add_argument("--init", action="store_true")
    check.add_argument("--code"); check.add_argument("--version-dir"); check.set_defaults(function=cmd_check_root)
    archive = subs.add_parser("archive")
    archive.add_argument("--root", required=True); archive.add_argument("--version-dir", required=True)
    archive.add_argument("--source", required=True); archive.add_argument("--label", default="input")
    archive.set_defaults(function=cmd_archive)
    verify = subs.add_parser("verify")
    verify.add_argument("--root", required=True); verify.add_argument("--plan-dir", required=True); verify.set_defaults(function=cmd_verify)
    same = subs.add_parser("same")
    same.add_argument("--source", required=True); same.add_argument("--plan-dir", required=True); same.set_defaults(function=cmd_same)
    args = parser.parse_args()
    try:
        args.function(args)
    except SystemExit:
        raise
    except Exception as exc:
        fail(f"执行失败: {exc}", 4)


if __name__ == "__main__":
    main()
