"""导出文件名模板：末尾那串数字是序号，前面的部分原样保留。

例：``1110-任金龙-狼人-5451-原切（中）-1``

- 前缀 = ``1110-任金龙-狼人-5451-原切（中）-``（一个字符都不改）
- 起始值 = ``1``，本批第 1 条用它，之后每导出下一条 +1
- 前导零保留位宽：``-001`` → 001、002、003…；``-1`` → 1、2 … 10
- 目标文件夹里已经有同名文件时自动往后顺延，绝不覆盖已有成片

这里只做纯字符串与磁盘占位判断，不碰 ffmpeg，方便单独测试。
"""
from __future__ import annotations

import re
from pathlib import Path

# Windows 文件名里不允许出现的字符（含控制字符）
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# 用户可能顺手把扩展名也写进模板，先剥掉再找末尾数字
_VIDEO_EXT = re.compile(r"\.(mp4|mov|mkv|webm|m4v)$", re.IGNORECASE)
# 前缀上限：给目录和扩展名留足余量，别撞 Windows 的 260 字符路径限制
MAX_PREFIX = 100
# 顺延时最多探多少个号，防止异常目录把请求拖死
MAX_PROBE = 20000


class NameTemplateError(ValueError):
    """模板不合法，消息可以直接展示给用户。"""


def parse(tpl: str) -> tuple[str, int, int]:
    """拆模板 → (前缀, 起始序号, 补零宽度)。

    宽度为 0 表示不补零（``-1`` 就该长成 1、2、10）。
    """
    # strip() 默认按 isspace() 判定，全角空格也在内；中间的空格一律不动
    raw = (tpl or "").strip()
    if not raw:
        raise NameTemplateError("文件名格式不能为空")
    raw = _VIDEO_EXT.sub("", raw).strip()
    if not raw:
        raise NameTemplateError("文件名格式不能只有扩展名")

    m = re.search(r"(\d+)$", raw)
    if not m:
        raise NameTemplateError(
            "文件名格式的末尾必须是数字，例如「1110-任金龙-狼人-5451-原切（中）-1」；"
            "这个数字就是本批的起始序号")

    digits = m.group(1)
    prefix = raw[:m.start()]

    # 非法字符直接剔掉，而不是报错——用户从别处粘贴过来常带 / : 之类
    prefix = _ILLEGAL.sub("", prefix)
    # Windows 不允许文件名以空格或点结尾
    prefix = prefix.rstrip(" .")
    if not prefix.strip():
        raise NameTemplateError(
            "数字前面还需要有别的内容，例如「剧名-1」；只写一个数字没法区分不同剧的成片")
    if len(prefix) > MAX_PREFIX:
        raise NameTemplateError(
            f"文件名格式太长了（数字前面有 {len(prefix)} 个字符，上限 {MAX_PREFIX}）；"
            "建议精简剧名部分")

    start = int(digits)
    if start > 999999:
        raise NameTemplateError("末尾序号太大了，请用 0-999999 之间的数字")
    # 只有明确写了前导零才补零，否则保持自然长度
    width = len(digits) if digits.startswith("0") else 0

    return prefix, start, width


def format_name(prefix: str, num: int, width: int = 0) -> str:
    """按前缀 + 序号拼出不带扩展名的文件名。"""
    s = f"{num:0{width}d}" if width else str(num)
    return prefix + s


class Namer:
    """一次导出批次内负责发号。

    号按任务顺序递增；某条渲染失败，该号作废不回收，避免同一个号被
    下一条复用后与失败残留的半成品撞名。
    """

    def __init__(self, folder: str | Path, tpl: str, ext: str = ".mp4"):
        self.prefix, self.start, self.width = parse(tpl)
        self.folder = Path(folder)
        self.ext = ext if ext.startswith(".") else "." + ext
        self._next_num = self.start
        self._taken: set[str] = set()
        self.issued = 0

    @property
    def next_num(self) -> int:
        return self._next_num

    def name_for(self, num: int) -> str:
        return format_name(self.prefix, num, self.width) + self.ext

    def next(self) -> Path:
        """拿下一个可用文件名（跳过磁盘上已存在的与批内已发过的）。"""
        for _ in range(MAX_PROBE):
            num = self._next_num
            self._next_num += 1
            path = self.folder / self.name_for(num)
            key = path.name.casefold()  # Windows 不区分大小写
            if key in self._taken:
                continue
            if path.exists():
                continue
            self._taken.add(key)
            self.issued += 1
            return path
        raise NameTemplateError(
            f"从 {self.start} 往后找了 {MAX_PROBE} 个序号都被占用了，"
            "请换一个起始序号，或清理一下保存文件夹")


def preview(tpl: str, count: int = 3) -> dict:
    """给前端看的纯格式预览，不查磁盘（真实导出会自动跳过同名文件）。"""
    prefix, start, width = parse(tpl)
    count = max(1, min(20, int(count or 3)))
    return {
        "prefix": prefix,
        "start": start,
        "width": width,
        "names": [format_name(prefix, start + i, width) + ".mp4" for i in range(count)],
    }
