"""工程卫生回归测试。

这些检查不是功能测试，而是防止"看不见的问题"再次溜进仓库：

* 源码里混入零宽字符 / BOM —— 零宽空格能让 Flask 的路由规则直接抛
  ``malformed url rule``，而编辑器里完全看不出来。
* ``except:`` 裸捕获 —— 会把 ``KeyboardInterrupt`` / ``SystemExit`` 一起吞掉，
  线上表现为"进程莫名卡住"。
* 密钥明文写进代码 —— 只能从环境变量 / .env 读。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    "node_modules", "venv", ".venv", "dist", "build", "__pycache__",
    ".git", ".ruff_cache", ".pytest_cache", "cookies", "cookiesFile",
    "videoFile", "uploadFile", "logs", "backups",
}
CODE_SUFFIXES = {".py", ".js", ".vue", ".ts", ".json", ".toml", ".yml", ".yaml"}

# 零宽空格/连接符/BOM/方向控制符等"看不见"的字符。
#
# 注意：U+200D (ZWJ) 和 U+200C (ZWNJ) 不在列表里——它们是合法字符，
# emoji 组合序列（❤️‍🩹）和印度系文字都要用，误报会让这条规则失去意义。
# 真正危险的是 U+200B（零宽空格）：编辑器里完全看不出来，却能让
# Flask 的路由规则抛 ``malformed url rule``。
INVISIBLE_CHARS = {
    0x00A0: "NO-BREAK SPACE",
    0x180E: "MONGOLIAN VOWEL SEPARATOR",
    0x200B: "ZERO WIDTH SPACE",
    0x200E: "LEFT-TO-RIGHT MARK",
    0x200F: "RIGHT-TO-LEFT MARK",
    0x202A: "LEFT-TO-RIGHT EMBEDDING",
    0x202E: "RIGHT-TO-LEFT OVERRIDE",
    0x2060: "WORD JOINER",
    0x2066: "LEFT-TO-RIGHT ISOLATE",
    0xFEFF: "ZERO WIDTH NO-BREAK SPACE (BOM)",
}


def _iter_source_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in CODE_SUFFIXES:
            continue
        yield path


def test_no_invisible_characters_in_source():
    """源码里不允许出现零宽字符 / BOM。"""
    offenders: list[str] = []
    for path in _iter_source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for char in line:
                if ord(char) in INVISIBLE_CHARS:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{lineno}: "
                        f"U+{ord(char):04X} ({INVISIBLE_CHARS[ord(char)]})"
                    )
                    break
    assert not offenders, "源码里出现了不可见字符：\n" + "\n".join(offenders)


def test_no_bom_at_start_of_python_files():
    """Python 源文件不应带 UTF-8 BOM（会让首行解析出现隐藏差异）。"""
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            if path.read_bytes().startswith(b"\xef\xbb\xbf"):
                offenders.append(str(path.relative_to(ROOT)))
        except OSError:
            continue
    assert not offenders, "以下 Python 文件带 BOM：\n" + "\n".join(offenders)


def test_no_bare_except_in_source():
    """不允许 ``except:`` 裸捕获。"""
    bare_except = re.compile(r"^\s*except\s*:", re.MULTILINE)
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for match in bare_except.finditer(text):
            lineno = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, "以下位置使用了裸 except：\n" + "\n".join(offenders)


def test_no_hardcoded_api_keys_in_source():
    """代码里不允许出现看起来像真实密钥的字面量。"""
    secret_pattern = re.compile(
        r"""(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|app[_-]?secret)"""
        r"""\s*[:=]\s*["'][A-Za-z0-9_\-]{16,}["']"""
    )
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for match in secret_pattern.finditer(text):
            lineno = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, "以下位置疑似硬编码密钥：\n" + "\n".join(offenders)


@pytest.mark.parametrize("module_name", [
    "publish_jobs",
    "verify_codes",
    "dm_ops",
])
def test_module_imports_cleanly(module_name):
    """核心模块必须能干净导入（防止循环 import 之类的结构性破坏）。"""
    import importlib

    module = importlib.import_module(module_name)
    assert module is not None


# ---- P2-4 密钥治理 ----------------------------------------------------

# 已知的占位符/示例值：这些不是真密钥，不该报警。
SECRET_PLACEHOLDERS = {
    "your_api_key_here", "changeme", "change_me", "placeholder",
    "example", "dummy", "test", "none", "null", "xxx", "todo",
}


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().strip("\"'").lower()
    if not lowered:
        return True
    if lowered in SECRET_PLACEHOLDERS:
        return True
    # 形如 CHANGE_ME_openssl_rand_hex_24 / your_key_goes_here
    if lowered.startswith(("change_me", "changeme", "your_", "replace_", "example_")):
        return True
    # 全是同一字符的假值
    return len(set(lowered)) <= 2


def _is_git_ignored(path: Path) -> bool:
    """Skip machine-local credentials that Git cannot commit by construction."""
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", str(path.relative_to(ROOT))],
            cwd=ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return result.returncode == 0


def test_no_real_secrets_in_env_files():
    """``.env`` / ``.env.*`` 文件里不允许出现看起来像真密钥的值。

    ``.env.example`` 这类模板文件本来就要写变量名，值必须是占位符。
    真密钥一旦提交进仓库，git 历史会永久保留，删掉文件也救不回来。
    """
    secret_name = re.compile(
        # 允许前缀：YUNCE_ACCESS_TOKEN / OPS_WEBHOOK_SECRET / WB_API_KEY
        # 之前用 \b 开头会漏掉带前缀的变量名——"_" 是单词字符，
        # "YUNCE_ACCESS_TOKEN" 里的 ACCESS_TOKEN 前面没有词边界。
        r"(?i)(?:^|[^A-Za-z0-9])"
        r"([A-Za-z0-9_]*(?:api[_-]?key|secret|passwd|password|access[_-]?token|"
        r"access[_-]?key|private[_-]?key|client[_-]?secret)[A-Za-z0-9_]*)"
        r"\s*[:=]\s*[\"']?([^\"'\s#]+)"
    )
    offenders: list[str] = []
    for path in ROOT.rglob(".env*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if _is_git_ignored(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = secret_name.search(stripped)
            if not match:
                continue
            value = match.group(2)
            if _is_placeholder(value):
                continue
            offenders.append(
                f"{path.relative_to(ROOT)}:{lineno}: {match.group(1)}={value[:12]}..."
            )
    assert not offenders, "以下 .env 文件疑似包含真实密钥：\n" + "\n".join(offenders)


def test_no_real_secrets_in_non_python_source():
    """JS / Vue / YAML 里也不允许出现真实密钥字面量。"""
    secret_pattern = re.compile(
        r"""(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|app[_-]?secret"""
        r"""|client[_-]?secret|private[_-]?key|password)\s*[:=]\s*"""
        r"""["']([^"']{12,})["']"""
    )
    offenders: list[str] = []
    for path in _iter_source_files():
        if path.suffix.lower() == ".py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for match in secret_pattern.finditer(text):
            value = match.group(1)
            if _is_placeholder(value):
                continue
            lineno = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, "以下位置疑似硬编码密钥：\n" + "\n".join(offenders)


def test_gitignore_protects_local_env_files():
    """``.env`` 必须被 git 忽略，否则本地密钥很容易被误提交。"""
    gitignore = ROOT.parent / ".gitignore"
    if not gitignore.exists():
        pytest.skip("仓库根目录没有 .gitignore")
    patterns = {
        line.strip()
        for line in gitignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert ".env" in patterns or "**/.env" in patterns, (
        ".gitignore 必须包含 .env / **/.env，避免本地密钥被提交"
    )


def test_env_example_documents_every_secret_env_var():
    """密钥相关的环境变量必须在 .env.example 里有说明。

    否则部署的人根本不知道要配哪些变量，只能靠猜或者去翻代码。
    """
    example = ROOT / ".env.example"
    if not example.exists():
        pytest.skip("没有 .env.example")
    text = example.read_text(encoding="utf-8")
    for name in ("WB_API_KEY", "YUNCE_ACCESS_TOKEN"):
        assert name in text, f".env.example 里缺少 {name} 的说明"
