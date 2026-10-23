"""Settings, loaded from the .env file next to the project (no extra packages needed)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # .../postpilot


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.split(" #")[0].strip().strip('"').strip("'")
        if value:
            os.environ[key] = value  # .env wins, so a stale system-wide key can't shadow it


_load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    cycle_days: float = float(env("CYCLE_DAYS", "3"))
    check_minutes: float = float(env("CHECK_MINUTES", "10"))

    gmail_address: str = env("GMAIL_ADDRESS")
    gmail_app_password: str = env("GMAIL_APP_PASSWORD").replace(" ", "")
    to_email: str = env("TO_EMAIL") or env("GMAIL_ADDRESS")

    github_token: str = env("GITHUB_TOKEN")

    output_dir: Path = Path(env("OUTPUT_DIR", str(ROOT.parent / "linkedin")))
    voice_file: Path = Path(env("VOICE_FILE", str(ROOT.parent / "linkedin" / "voice.md")))
    data_dir: Path = field(default=ROOT / "data")

    @property
    def has_email(self) -> bool:
        return bool(self.gmail_address and self.gmail_app_password and self.to_email)


settings = Settings()
