"""给 pandoc 生成的中文说明 docx 加「正文两端对齐 + 首行缩进 2 字符」。

只改 word/styles.xml 里的段落样式,正文(document.xml)一字不动:
  - BodyText      : 加 两端对齐(jc=both) + 首行缩进 2 字符(ind firstLineChars=200)
  - FirstParagraph: 继承 BodyText,自动获得上述格式(无需改)
  - Compact(列表) : 重置 firstLineChars=0(列表项不首行缩进;两端对齐继承)
  - BlockText(引用): 重置 firstLineChars=0(保留其左右缩进)
代码块(SourceCode)/标题(Heading*)基于 Normal,不受影响。

firstLineChars 单位为 1/100 字符,故 200 = 2 字符,随字号缩放。

用法:
    python format_docx_indent.py                 # 处理 docs/ 下 4 个说明 docx
    python format_docx_indent.py a.docx b.docx    # 处理指定文件
"""
from __future__ import annotations
import re
import sys
import shutil
import zipfile
from pathlib import Path

DEFAULT_DOCS = [
    "软件说明.docx",
    "使用说明书_V26.5.34.docx",
    "节点位移计算整体位移和旋转_理论公式.docx",
    "软件开发文档.docx",
]

IND_FULL = '<w:ind w:firstLineChars="200"/>'   # 2 字符首行缩进
JC_BOTH = '<w:jc w:val="both"/>'                # 两端对齐
IND_ZERO = '<w:ind w:firstLineChars="0"/>'      # 取消首行缩进


def _style_block(xml: str, style_id: str) -> tuple[int, int, str]:
    """返回指定 styleId 的 <w:style>...</w:style> 块 (起, 止, 文本)。"""
    m = re.search(
        r'<w:style\b[^>]*w:styleId="' + re.escape(style_id) + r'"[^>]*>.*?</w:style>',
        xml, re.S)
    if not m:
        raise KeyError(f'未找到样式 {style_id}')
    return m.start(), m.end(), m.group(0)


def _inject_into_pPr(block: str, insert: str) -> str:
    """在样式块的 <w:pPr> 末尾(</w:pPr> 前)插入 insert;无 pPr 则新建。"""
    if '<w:pPr>' in block:
        return block.replace('</w:pPr>', insert + '</w:pPr>', 1)
    # 无 pPr:在 <w:basedOn .../> 之后(否则开标签之后)插入一个 pPr
    new_pPr = '<w:pPr>' + insert + '</w:pPr>'
    m = re.search(r'<w:basedOn\b[^>]*/>', block)
    if m:
        return block[:m.end()] + new_pPr + block[m.end():]
    m = re.search(r'<w:style\b[^>]*?>', block)
    return block[:m.end()] + new_pPr + block[m.end():]


def patch_styles_xml(xml: str) -> str:
    # 1) BodyText:首行缩进 2 字符 + 两端对齐(ind 必须在 jc 前)
    try:
        s, e, blk = _style_block(xml, 'BodyText')
        xml = xml[:s] + _inject_into_pPr(blk, IND_FULL + JC_BOTH) + xml[e:]
    except KeyError:
        print('  [警告] 无 BodyText 样式,跳过(请确认用当前 pandoc 重渲染)')
    # 2) Compact(列表项):取消首行缩进
    try:
        s, e, blk = _style_block(xml, 'Compact')
        xml = xml[:s] + _inject_into_pPr(blk, IND_ZERO) + xml[e:]
    except KeyError:
        pass
    # 3) BlockText(引用块):给已有 <w:ind ...> 补 firstLineChars="0"
    try:
        s, e, blk = _style_block(xml, 'BlockText')
        if 'w:firstLineChars' not in blk:
            blk2 = re.sub(r'<w:ind\b', '<w:ind w:firstLineChars="0"', blk, count=1)
            if blk2 == blk:                       # BlockText 若无 ind,则插入
                blk2 = _inject_into_pPr(blk, IND_ZERO)
            xml = xml[:s] + blk2 + xml[e:]
    except KeyError:
        pass
    return xml


def process(path: Path) -> None:
    tmp = path.with_suffix('.docx.tmp')
    with zipfile.ZipFile(path) as zin:
        names = zin.namelist()
        styles = zin.read('word/styles.xml').decode('utf-8')
        new_styles = patch_styles_xml(styles)
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zout:
            for n in names:
                data = new_styles.encode('utf-8') if n == 'word/styles.xml' else zin.read(n)
                zout.writestr(zin.getinfo(n), data)
    shutil.move(str(tmp), str(path))
    # 校验
    with zipfile.ZipFile(path) as z:
        sx = z.read('word/styles.xml').decode('utf-8')
    _, _, bt = _style_block(sx, 'BodyText')
    ok = ('firstLineChars="200"' in bt) and ('w:val="both"' in bt)
    print(f'[{"OK" if ok else "??"}] {path.name}  (BodyText 含 缩进200+两端对齐: {ok})')


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    if args:
        targets = [Path(a) for a in args]
    else:
        docs = Path(__file__).resolve().parent.parent / 'docs'
        targets = [docs / n for n in DEFAULT_DOCS]
    for p in targets:
        if not p.exists():
            print(f'[跳过] 不存在: {p}')
            continue
        process(p)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
