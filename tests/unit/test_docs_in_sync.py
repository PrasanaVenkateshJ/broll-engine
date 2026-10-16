"""README.md, .env.example and CLAUDE.md must list every setting and every error code the code has."""
import re
from pathlib import Path

from app.config import Settings
from app.errors import ErrorCode

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_every_setting_is_in_env_example_and_the_readme_table():
    env = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", (ROOT / ".env.example").read_text(encoding="utf-8"), re.M))
    in_readme = set(re.findall(r"^\| `([A-Z][A-Z0-9_]*)` \|", README, re.M))
    assert set(Settings.model_fields) == env, "config.py and .env.example differ"
    assert set(Settings.model_fields) <= in_readme, sorted(set(Settings.model_fields) - in_readme)


def test_every_error_code_is_in_the_readme_table_with_a_retryable_column():
    rows = {m.group(1): m.group(0) for m in re.finditer(r"^\| `([A-Z_]+)` \|.*\|$", README, re.M)}
    for code in ErrorCode:
        assert code.value in rows, f"{code.value} missing from the README error table"
        assert rows[code.value].count("|") == 5, f"{code.value}: the row needs 4 columns"


def test_every_error_code_is_listed_in_claude_md():
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    section = claude.split("## Error codes", 1)[1].split("##", 1)[0]
    for code in ErrorCode:
        assert re.search(rf"\b{code.value}\b", section), f"{code.value} missing from CLAUDE.md"
