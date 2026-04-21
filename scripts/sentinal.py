#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SEVERITY_ORDER = ["Critical", "High", "Medium", "Low", "Info"]
SEVERITY_RANK = {name: idx for idx, name in enumerate(SEVERITY_ORDER)}
NO_ISSUES_PHRASES = (
    "no findings",
    "no issues",
    "all clear",
    "passed",
    "clean",
    "nothing found",
    "0 findings",
)

ACTIVE_SKILLS_ROOT = Path(
    os.environ.get("OPENCLAW_SKILLS_DIR", "~/.openclaw/skills")
).expanduser()
QUARANTINE_ROOT = Path(
    os.environ.get("OPENCLAW_QUARANTINE_DIR", "~/.openclaw/skills-quarantine")
).expanduser()
WORKSPACE_ROOT = Path(
    os.environ.get("OPENCLAW_WORKSPACE_DIR", "~/.openclaw")
).expanduser()
STAGE_ROOT = Path(
    os.environ.get("OPENCLAW_STAGE_DIR", str(WORKSPACE_ROOT / ".skill_stage"))
).expanduser()


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def sanitize(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = re.sub(r"-+", "-", cleaned).strip(".-_")
    return cleaned or "skill"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def extract_skill_name(skill_dir: Path) -> str:
    skill_md = skill_dir / "SKILL.md"
    if skill_md.exists():
        try:
            for line in skill_md.read_text(encoding="utf-8", errors="ignore").splitlines()[:120]:
                match = re.match(r"^name:\s*(.+?)\s*$", line)
                if match:
                    return sanitize(match.group(1))
        except OSError:
            pass
    return sanitize(skill_dir.name)


def scanner_base() -> list[str]:
    custom = os.environ.get("SENTINAL_SCANNER_CMD")
    if custom:
        return shlex.split(custom)
    if shutil.which("uv"):
        return ["uv", "run", "skill-scanner"]
    if shutil.which("skill-scanner"):
        return ["skill-scanner"]
    raise SystemExit(
        "skill-scanner not found. Install it or set SENTINAL_SCANNER_CMD."
    )


def clawhub_base() -> list[str]:
    custom = os.environ.get("SENTINAL_CLAWHUB_CMD")
    if custom:
        return shlex.split(custom)
    if shutil.which("npx"):
        return ["npx", "-y", "clawhub"]
    raise SystemExit("clawhub not found. Install node+npx or set SENTINAL_CLAWHUB_CMD.")


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    print("+", shlex.join(cmd))
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None)


def default_report_path(label: str, kind: str) -> Path:
    label = sanitize(label)
    return Path(tempfile.gettempdir()) / f"sentinal-{kind}-{label}-{stamp()}.md"


def parse_report(text: str) -> tuple[dict[str, int], str]:
    counts = {name: 0 for name in SEVERITY_ORDER}
    severity_seen = False

    heading_re = re.compile(r"^\s*#{1,6}\s*(Critical|High|Medium|Low|Info)\b", re.I)
    bullet_re = re.compile(r"^\s*(?:[-*]|\d+\.)\s*(Critical|High|Medium|Low|Info)\b", re.I)
    summary_re = re.compile(r"^\s*(Critical|High|Medium|Low|Info)\s*[:\-]\s*(\d+)\b", re.I)

    for line in text.splitlines():
        matched = False
        for rx in (summary_re, heading_re, bullet_re):
            match = rx.match(line)
            if not match:
                continue
            severity = match.group(1).title()
            if rx is summary_re:
                counts[severity] += int(match.group(2))
            else:
                counts[severity] += 1
            severity_seen = True
            matched = True
            break
        if matched:
            continue

    lowered = text.lower()
    if counts["Critical"] or counts["High"]:
        return counts, "blocked"
    if counts["Medium"] or counts["Low"] or counts["Info"]:
        return counts, "warning"
    if any(phrase in lowered for phrase in NO_ISSUES_PHRASES):
        return counts, "clean"
    if severity_seen:
        return counts, "clean"
    return counts, "unknown"


def summarize(counts: dict[str, int]) -> str:
    parts = [f"{name}={counts[name]}" for name in SEVERITY_ORDER if counts[name]]
    return ", ".join(parts) if parts else "no severity markers"


def read_report(report_path: Path | None) -> tuple[dict[str, int], str, str | None]:
    if report_path is None:
        return {name: 0 for name in SEVERITY_ORDER}, "unknown", None
    if not report_path.exists():
        return {name: 0 for name in SEVERITY_ORDER}, "unknown", None
    text = report_path.read_text(encoding="utf-8", errors="ignore")
    counts, verdict = parse_report(text)
    return counts, verdict, text


def scan_with_scanner(target: Path, *, bulk: bool = False, report_path: Path | None = None) -> tuple[int, Path, dict[str, int], str]:
    ensure_dir(report_path.parent if report_path else Path(tempfile.gettempdir()))
    report_path = report_path or default_report_path(target.name, "bulk" if bulk else "single")
    cmd = scanner_base() + [
        "scan-all" if bulk else "scan",
        str(target),
        "--format",
        "markdown",
        "--detailed",
        "--output",
        str(report_path),
    ]
    result = run(cmd)
    if result.returncode != 0:
        return result.returncode, report_path, {name: 0 for name in SEVERITY_ORDER}, "scanner-error"
    counts, verdict, _ = read_report(report_path)
    return 0, report_path, counts, verdict


def find_installed_skill_dir(stage_skills_dir: Path, expected_slug: str | None = None) -> Path:
    if not stage_skills_dir.exists():
        raise SystemExit(f"Expected staged skill directory not found: {stage_skills_dir}")

    dirs = [p for p in stage_skills_dir.iterdir() if p.is_dir()]
    if not dirs:
        raise SystemExit(f"No staged skill directory found under {stage_skills_dir}")

    if expected_slug:
        normalized = sanitize(expected_slug)
        for candidate in dirs:
            if sanitize(candidate.name) == normalized:
                return candidate

    skill_md_dirs = [p for p in dirs if (p / "SKILL.md").exists()]
    if len(skill_md_dirs) == 1:
        return skill_md_dirs[0]
    if len(dirs) == 1:
        return dirs[0]

    names = ", ".join(sorted(p.name for p in dirs))
    raise SystemExit(f"Could not determine staged skill directory inside {stage_skills_dir}: {names}")


def copy_skill_tree(source: Path, destination_root: Path, *, force: bool = False) -> Path:
    skill_name = extract_skill_name(source)
    destination = destination_root / skill_name
    if destination.exists():
        if not force:
            raise SystemExit(f"Destination already exists: {destination}. Use --force to replace it.")
        shutil.rmtree(destination)
    ensure_dir(destination_root)
    shutil.copytree(source, destination)
    return destination


def quarantine_skill(source: Path, *, force: bool = False) -> Path:
    resolved_source = source.resolve()
    active_root = ACTIVE_SKILLS_ROOT.resolve()
    try:
        resolved_source.relative_to(active_root)
    except ValueError as exc:
        raise SystemExit(f"Refusing to quarantine a path outside the active skill tree: {source}") from exc

    quarantine_root = ensure_dir(QUARANTINE_ROOT)
    destination = quarantine_root / f"{extract_skill_name(source)}-{stamp()}"
    if destination.exists():
        if not force:
            raise SystemExit(f"Quarantine destination already exists: {destination}")
        shutil.rmtree(destination)
    shutil.move(str(source), str(destination))
    return destination


def print_result(target: Path, report_path: Path, counts: dict[str, int], verdict: str) -> None:
    print(f"Report: {report_path}")
    print(f"Target: {target}")
    print(f"Severity: {summarize(counts)}")
    print(f"Verdict: {verdict}")


def cmd_scan(args: argparse.Namespace) -> int:
    source = Path(args.path).expanduser().resolve()
    if not source.exists():
        raise SystemExit(f"Path does not exist: {source}")
    report_path = Path(args.report).expanduser() if args.report else None
    rc, report_path, counts, verdict = scan_with_scanner(source, bulk=False, report_path=report_path)
    print_result(source, report_path, counts, verdict)
    if rc != 0:
        return rc
    return 2 if verdict == "blocked" else 0


def cmd_scan_all(args: argparse.Namespace) -> int:
    source = Path(args.path).expanduser().resolve()
    if not source.exists():
        raise SystemExit(f"Path does not exist: {source}")
    report_path = Path(args.report).expanduser() if args.report else None
    rc, report_path, counts, verdict = scan_with_scanner(source, bulk=True, report_path=report_path)
    print_result(source, report_path, counts, verdict)
    if rc != 0:
        return rc
    return 2 if verdict == "blocked" else 0


def cmd_scan_install_local(args: argparse.Namespace) -> int:
    source = Path(args.path).expanduser().resolve()
    if not source.exists():
        raise SystemExit(f"Path does not exist: {source}")
    rc, report_path, counts, verdict = scan_with_scanner(source, bulk=False, report_path=None)
    print_result(source, report_path, counts, verdict)
    if rc != 0:
        return rc
    if verdict == "blocked":
        return 2
    destination = copy_skill_tree(source, Path(args.dest_root).expanduser(), force=args.force)
    print(f"Installed: {destination}")
    return 0


def cmd_scan_install_clawhub(args: argparse.Namespace) -> int:
    stage_root = ensure_dir(Path(args.stage_root).expanduser())
    stage_dir = Path(tempfile.mkdtemp(prefix=f"{sanitize(args.slug)}-", dir=str(stage_root)))
    clawhub = clawhub_base() + ["--workdir", str(stage_dir), "--dir", "skills", "install", args.slug]
    if args.version:
        clawhub.extend(["--version", args.version])
    result = run(clawhub)
    if result.returncode != 0:
        return result.returncode

    staged_skills_dir = stage_dir / "skills"
    candidate = find_installed_skill_dir(staged_skills_dir, args.slug)
    rc, report_path, counts, verdict = scan_with_scanner(candidate, bulk=False, report_path=None)
    print_result(candidate, report_path, counts, verdict)
    if rc != 0:
        return rc
    if verdict == "blocked":
        print(f"Blocked staged ClawHub install: {candidate}")
        return 2

    if args.apply:
        destination = copy_skill_tree(candidate, Path(args.dest_root).expanduser(), force=args.force)
        print(f"Installed: {destination}")
    else:
        print(f"Staged install retained at: {candidate}")
    return 0


def cmd_auto_scan(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser().resolve()
    if not root.exists():
        raise SystemExit(f"Path does not exist: {root}")
    rc, report_path, counts, verdict = scan_with_scanner(root, bulk=True, report_path=None)
    print_result(root, report_path, counts, verdict)
    if rc != 0:
        return rc
    return 2 if verdict == "blocked" else 0


def cmd_quarantine(args: argparse.Namespace) -> int:
    source = Path(args.path).expanduser().resolve()
    if not source.exists():
        raise SystemExit(f"Path does not exist: {source}")
    destination = quarantine_skill(source, force=args.force)
    print(f"Quarantined: {destination}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Active Defense Sentinal helper")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Scan a single skill path")
    p_scan.add_argument("path", help="Skill directory to scan")
    p_scan.add_argument("--report", help="Optional report file path")
    p_scan.set_defaults(func=cmd_scan)

    p_scan_all = sub.add_parser("scan-all", help="Scan a directory of skills")
    p_scan_all.add_argument("path", help="Directory containing skills to scan")
    p_scan_all.add_argument("--report", help="Optional report file path")
    p_scan_all.set_defaults(func=cmd_scan_all)

    p_install_local = sub.add_parser(
        "scan-install-local",
        help="Scan a local skill folder and install it into the active skill tree when safe",
    )
    p_install_local.add_argument("path", help="Local skill directory")
    p_install_local.add_argument(
        "--dest-root",
        default=str(ACTIVE_SKILLS_ROOT),
        help="Active skills root (default: ~/.openclaw/skills)",
    )
    p_install_local.add_argument("--force", action="store_true", help="Replace an existing destination")
    p_install_local.set_defaults(func=cmd_scan_install_local)

    p_install_clawhub = sub.add_parser(
        "scan-install-clawhub",
        help="Stage-install a ClawHub skill, scan it, then optionally copy it into the active tree",
    )
    p_install_clawhub.add_argument("slug", help="ClawHub skill slug")
    p_install_clawhub.add_argument("--version", help="Optional version override")
    p_install_clawhub.add_argument(
        "--stage-root",
        default=str(STAGE_ROOT),
        help="Stage root (default: ~/.openclaw/.skill_stage)",
    )
    p_install_clawhub.add_argument(
        "--dest-root",
        default=str(ACTIVE_SKILLS_ROOT),
        help="Active skills root (default: ~/.openclaw/skills)",
    )
    p_install_clawhub.add_argument("--apply", action="store_true", help="Copy the safe staged skill into the active tree")
    p_install_clawhub.add_argument("--force", action="store_true", help="Replace an existing destination")
    p_install_clawhub.set_defaults(func=cmd_scan_install_clawhub)

    p_auto = sub.add_parser("auto-scan", help="Scan the active user skills tree")
    p_auto.add_argument(
        "path",
        nargs="?",
        default=str(ACTIVE_SKILLS_ROOT),
        help="Skill tree to scan (default: ~/.openclaw/skills)",
    )
    p_auto.set_defaults(func=cmd_auto_scan)

    p_quarantine = sub.add_parser("quarantine", help="Move an installed skill into quarantine")
    p_quarantine.add_argument("path", help="Installed skill directory to quarantine")
    p_quarantine.add_argument("--force", action="store_true", help="Replace an existing quarantine destination")
    p_quarantine.set_defaults(func=cmd_quarantine)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
