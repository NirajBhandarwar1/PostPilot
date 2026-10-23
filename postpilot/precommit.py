"""Secret check before anything goes to GitHub. You run it by hand; nothing commits for you.

  python -m postpilot precommit            check staged files (or every project file if no git yet)
  python -m postpilot precommit a.py b.md  check just these files

It refuses when a file is `.env`, contains one of the real secret values from `.env`, or
contains something shaped like an API key. Secret values are never printed.
"""

import re
import subprocess
from pathlib import Path

from .config import ROOT

# Settings in .env that are configuration, not secrets. Their values (model names, provider
# lists) legitimately appear in code and docs, so matching them would block every commit.
NOT_SECRET = re.compile(
    r"^(LLM_PROVIDERS|WRITER_PROVIDERS|\w+_MODEL|REASONING_EFFORT|USER_SETUP|CURRENT_MODELS|CYCLE_DAYS|CHECK_MINUTES|"
    r"RADAR_DAYS|OUTPUT_DIR|VOICE_FILE|OLLAMA_URL|CUSTOM_BASE_URL|CUSTOM_STRICT)$")

# Known key shapes: a prefix plus a long run of key characters (bare prefixes like "sk-" or
# "hf_" would also match ordinary words such as "risk-" or "hf_api").
KEY_SHAPES = re.compile(
    r"(sk-or-v1-[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9]{20,}|nvapi-[A-Za-z0-9_-]{20,}"
    r"|hf_[A-Za-z0-9]{20,}|cfut_[A-Za-z0-9_]{20,})")

SKIP_DIRS = {".venv", ".git", "data", ".gstack", "__pycache__"}


def secret_values(env_path: Path) -> dict[str, str]:
    """Name -> value for every .env entry that is a secret (6+ characters)."""
    out = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.split(" #")[0].strip().strip('"').strip("'")
        if len(value) >= 6 and not NOT_SECRET.match(name):
            out[name] = value
    return out


def staged_files(root: Path) -> list[Path] | None:
    """Files staged in git, or None when the folder is not a git repo yet."""
    try:
        res = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=root,
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    return [root / p for p in res.stdout.splitlines() if p.strip()]


def _ignored_names(root: Path) -> set[str]:
    """Plain names from .gitignore (e.g. `.env`, `data/`), which git would never stage."""
    gi = root / ".gitignore"
    if not gi.exists():
        return set()
    names = set()
    for line in gi.read_text(encoding="utf-8").splitlines():
        line = line.strip().rstrip("/")
        if line and not line.startswith("#") and "*" not in line:
            names.add(line)
    return names


def project_files(root: Path) -> list[Path]:
    """Every file `git add .` would pick up (no git repo yet), so the check is still useful."""
    skip = SKIP_DIRS | _ignored_names(root)
    return [p for p in root.rglob("*")
            if p.is_file() and not (set(p.relative_to(root).parts) & skip)
            and not p.name.endswith((".pyc",))]


def scan(files: list[Path], secrets: dict[str, str]) -> list[str]:
    """Problems found, described without ever showing a secret value."""
    problems = []
    for f in files:
        if f.name == ".env":
            problems.append(f"{f}: the .env file itself must never be committed")
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, value in secrets.items():
            if value in text:
                problems.append(f"{f}: contains the value of {name} from .env")
        m = KEY_SHAPES.search(text)
        if m:
            line_no = text.count("\n", 0, m.start()) + 1
            problems.append(f"{f}:{line_no}: something shaped like an API key ({m.group(0)[:6]}...)")
    return problems


def run(paths: list[str] | None = None, root: Path = ROOT) -> int:
    secrets = secret_values(root / ".env")
    if paths:
        files, where = [Path(p).resolve() for p in paths], "the given files"
    else:
        staged = staged_files(root)
        files, where = (staged, "staged files") if staged is not None else (project_files(root), "all project files (no git repo yet)")
    problems = scan(files, secrets)
    print(f"Checked {len(files)} {where} against {len(secrets)} secret values from .env")
    if problems:
        print("BLOCKED. Do not commit until these are fixed:")
        for p in problems:
            print("  -", p)
        return 1
    print("OK: no secrets found.")
    return 0
