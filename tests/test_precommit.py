"""Secret check (D8 manual, D16 real .env values + key shapes)."""

from postpilot import precommit

FAKE_GROQ = "gsk_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"


def make_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY=supersecretvalue123\nGROQ_MODEL=openai/gpt-oss-120b\n"
                   "GMAIL_APP_PASSWORD=abcd efgh ijkl mnop\nCYCLE_DAYS=3\nEMPTY=\n", encoding="utf-8")
    return env


def test_secret_values_skip_config(tmp_path):
    vals = precommit.secret_values(make_env(tmp_path))
    assert set(vals) == {"GROQ_API_KEY", "GMAIL_APP_PASSWORD"}


def test_blocks_real_value_without_printing_it(tmp_path):
    secrets = precommit.secret_values(make_env(tmp_path))
    f = tmp_path / "notes.md"
    f.write_text("my key is supersecretvalue123 oops", encoding="utf-8")
    problems = precommit.scan([f], secrets)
    assert problems and "GROQ_API_KEY" in problems[0]
    assert "supersecretvalue123" not in " ".join(problems)


def test_blocks_gmail_password_with_no_prefix(tmp_path):
    secrets = precommit.secret_values(make_env(tmp_path))
    f = tmp_path / "a.py"
    f.write_text('PASS = "abcd efgh ijkl mnop"', encoding="utf-8")
    assert precommit.scan([f], secrets)


def test_blocks_key_shape_and_env_file(tmp_path):
    f = tmp_path / "leak.py"
    f.write_text(f'KEY = "{FAKE_GROQ}"', encoding="utf-8")
    assert any("shaped like an API key" in p for p in precommit.scan([f], {}))
    env = make_env(tmp_path)
    assert any(".env file itself" in p for p in precommit.scan([env], {}))


def test_ordinary_text_passes(tmp_path):
    f = tmp_path / "doc.md"
    f.write_text("A risk-free plan; set hf_api later; sk- prefixes are documented; model openai/gpt-oss-120b.",
                 encoding="utf-8")
    assert precommit.scan([f], precommit.secret_values(make_env(tmp_path))) == []


def test_no_git_scans_project_but_skips_gitignored(tmp_path):
    (tmp_path / ".gitignore").write_text(".env\ndata/\n", encoding="utf-8")
    make_env(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x.txt").write_text("supersecretvalue123", encoding="utf-8")
    (tmp_path / "ok.py").write_text("print(1)", encoding="utf-8")
    files = {p.name for p in precommit.project_files(tmp_path)}
    assert files == {".gitignore", "ok.py"}
