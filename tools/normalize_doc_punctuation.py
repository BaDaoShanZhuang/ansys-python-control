"""规范化中文说明文档的文本格式:把中文正文中的半角标点转为全角,
保护代码块/行内代码/数学公式/Markdown 链接与锚点/URL 不被改动。

用法:
    python normalize_doc_punctuation.py            # 试运行,仅报告
    python normalize_doc_punctuation.py --apply     # 写回文件
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

CJK = "一-鿿"
PUNCT = {",": "，", ":": "：", ";": "；", "!": "！", "?": "？"}

DOCS = [
    "软件说明.md",
    "使用说明书_V26.5.34.md",
    "节点位移计算整体位移和旋转_理论公式.md",
    "软件开发文档.md",
    "修改说明_20260619.md",
]


def _protect(text: str) -> tuple[str, list[str]]:
    """把不该改动的片段替换成占位符,返回(文本, 占位列表)。"""
    holders: list[str] = []

    def stash(m: re.Match) -> str:
        holders.append(m.group(0))
        return f"\x00{len(holders) - 1}\x00"

    text = re.sub(r"```.*?```", stash, text, flags=re.S)   # 围栏代码块
    text = re.sub(r"\$\$.*?\$\$", stash, text, flags=re.S)  # 块级公式
    text = re.sub(r"\$[^$\n]+\$", stash, text)               # 行内公式
    text = re.sub(r"`[^`\n]+`", stash, text)                 # 行内代码
    text = re.sub(r"!?\[[^\]]*\]\([^)]*\)", stash, text)     # 链接/图片(含锚点)
    text = re.sub(r"https?://\S+", stash, text)              # 裸 URL
    return text, holders


def _restore(text: str, holders: list[str]) -> str:
    for i, piece in enumerate(holders):
        text = text.replace(f"\x00{i}\x00", piece)
    return text


def _convert_parens(text: str) -> str:
    """栈式匹配:括号对内任意位置含中文 -> 该对转全角(支持嵌套,如 det(R))。"""
    chars = list(text)
    stack: list[int] = []
    to_full: set[int] = set()
    for i, c in enumerate(chars):
        if c == "(":
            stack.append(i)
        elif c == ")" and stack:
            o = stack.pop()
            if re.search(f"[{CJK}]", text[o + 1:i]):
                to_full.add(o)
                to_full.add(i)
    for i in to_full:
        chars[i] = "（" if chars[i] == "(" else "）"
    return "".join(chars)


def normalize(text: str) -> str:
    text, holders = _protect(text)
    # 逗号/冒号/分号/感叹/问号:紧邻中文则转全角
    text = re.sub(f"(?<=[{CJK}])([,:;!?])", lambda m: PUNCT[m.group(1)], text)
    text = re.sub(f"([,:;!?])(?=[{CJK}])", lambda m: PUNCT[m.group(1)], text)
    # 标点夹在 加粗**/全角右括号）/行内代码占位符 等之后,但其前实为中文 -> 也转全角
    skip = "(?:[ \\t]*(?:\\*\\*|\\*|）|\"|'|\\]|\\}|\x00\\d+\x00))+"
    text = re.sub(f"([{CJK}]{skip})([,:;!?])", lambda m: m.group(1) + PUNCT[m.group(2)], text)
    # 全角标点后紧跟的多余 ASCII 空格(其后为中文)删去
    text = re.sub(f"([，：；！？])[ \t]+(?=[{CJK}])", r"\1", text)
    text = _convert_parens(text)
    return _restore(text, holders)


def main() -> int:
    try:  # 避免控制台编码(如 GBK)打印特殊字符(ᵀ 等)时崩溃中断写盘
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    apply = "--apply" in sys.argv
    here = Path(__file__).resolve().parent.parent / "docs"
    total = 0
    for name in DOCS:
        path = here / name
        if not path.exists():
            print(f"[跳过] 不存在: {name}")
            continue
        old = path.read_text(encoding="utf-8")
        new = normalize(old)
        if old == new:
            print(f"[无改动] {name}")
            continue
        old_lines = old.splitlines()
        new_lines = new.splitlines()
        changed = [(i + 1, a, b) for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
        total += len(changed)
        print(f"[{'已写回' if apply else '将改动'}] {name}: {len(changed)} 行")
        for ln, a, b in changed[:6]:
            print(f"    {ln}|- {a}")
            print(f"    {ln}|+ {b}")
        if len(changed) > 6:
            print(f"    ... 其余 {len(changed) - 6} 行")
        if apply:
            path.write_text(new, encoding="utf-8")
    print(f"\n合计 {total} 行" + ("(已写回)" if apply else "(试运行,未写盘;加 --apply 写回)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
