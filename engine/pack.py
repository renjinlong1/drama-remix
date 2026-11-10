"""包装素材生成：角标 logo、全屏模板叠加、引导尾帧适配。

（`make_title_card` / `make_end_card` 是早期的片头剧名卡 / 结尾追更卡生成器，
成片已经不用了——留着是为了以后想恢复文字卡时能直接接回来。）

全部用 Pillow 现画，不依赖任何模板文件；你上传自己的 png 时直接叠加。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_BOLD = ["C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyhbd.ttf",
             "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/msyh.ttc"]
FONT_REG = ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf",
            "C:/Windows/Fonts/simsun.ttc"]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for p in (FONT_BOLD if bold else FONT_REG):
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _fit(text: str, max_w: int, start: int, bold: bool = True, min_size: int = 28) -> ImageFont.FreeTypeFont:
    size = start
    while size > min_size:
        f = _font(size, bold)
        if _text_w(text, f) <= max_w:
            return f
        size -= 4
    return _font(min_size, bold)


def _text_w(text: str, font) -> int:
    try:
        return int(font.getlength(text))
    except Exception:
        return font.getsize(text)[0]


def _wrap(text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        if _text_w(cur + ch, font) <= max_w:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def _wrap_balanced(text: str, font, max_w: int, max_lines: int = 3) -> list[str]:
    """折行后如果末行太短（只剩两三个字），按字数重新均分，视觉更稳。"""
    lines = _wrap(text, font, max_w)
    if len(lines) <= 1:
        return lines
    last_w = _text_w(lines[-1], font)
    if last_w >= max_w * 0.45:
        return lines
    n = len(lines)
    total = len(text)
    per = (total + n - 1) // n
    trial = [text[i * per:(i + 1) * per] for i in range(n)]
    trial = [t for t in trial if t]
    if len(trial) <= 1:
        return lines
    if all(_text_w(t, font) <= max_w for t in trial):
        return trial
    return lines


def _bg_from(img_path: str | None, size: tuple[int, int]) -> Image.Image:
    W, H = size
    if img_path and Path(img_path).exists():
        im = Image.open(img_path).convert("RGB")
        # 等比铺满
        s = max(W / im.width, H / im.height)
        im = im.resize((int(im.width * s + 1), int(im.height * s + 1)), Image.LANCZOS)
        x = (im.width - W) // 2
        y = (im.height - H) // 2
        im = im.crop((x, y, x + W, y + H))
        im = im.filter(ImageFilter.GaussianBlur(radius=max(6, W // 90)))
        dark = Image.new("RGB", size, (8, 10, 16))
        return Image.blend(im, dark, 0.62)
    return Image.new("RGB", size, (10, 12, 18))


def _vignette(im: Image.Image, strength: float = 0.55) -> Image.Image:
    W, H = im.size
    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    for i in range(60):
        a = int(255 * (1 - i / 60) * strength)
        d.rectangle([-i * 6, -i * 6, W + i * 6, H + i * 6], outline=a, width=12)
    mask = mask.filter(ImageFilter.GaussianBlur(60))
    dark = Image.new("RGB", (W, H), (0, 0, 0))
    return Image.composite(dark, im, mask.point(lambda v: min(220, int(v * 0.9))))


def make_title_card(out_path: str | Path, size: tuple[int, int], title: str,
                    subtitle: str = "", badge: str = "", bg_image: str | None = None,
                    accent: str = "#e8b754") -> str:
    W, H = size
    im = _vignette(_bg_from(bg_image, size), 0.5)
    d = ImageDraw.Draw(im, "RGBA")

    max_w = int(W * 0.74)
    # 优先一行放下；放不下再缩字号，最多两行
    fsz = int(H * 0.060)
    while fsz > int(H * 0.030):
        f = _font(fsz, True)
        if len(_wrap(title, f, max_w)) <= 2:
            break
        fsz -= 4
    f = _font(fsz, True)
    lines = _wrap_balanced(title, f, max_w, 2)[:2]
    lh = int(fsz * 1.30)
    block_h = lh * len(lines)

    ty = int(H * 0.44) - block_h // 2
    bar_w = max(8, W // 80)
    bx = int(W * 0.095)
    d.rectangle([bx, ty + int(fsz * 0.06), bx + bar_w, ty + block_h - int(fsz * 0.06)],
                fill=accent)

    tx = bx + bar_w + int(W * 0.032)
    y = ty
    for ln in lines:
        d.text((tx, y), ln, font=f, fill=(255, 255, 255, 255))
        y += lh

    if subtitle:
        fs = _fit(subtitle, max_w, int(H * 0.028), False, int(H * 0.017))
        d.text((tx, y + int(H * 0.006)), subtitle, font=fs, fill=accent)

    if badge:
        fb = _font(int(H * 0.021), True)
        bw = _text_w(badge, fb) + int(W * 0.055)
        bh = int(H * 0.043)
        by = ty - bh - int(H * 0.026)
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh // 2,
                            fill=(255, 255, 255, 26), outline=(255, 255, 255, 60), width=2)
        d.text((bx + (bw - _text_w(badge, fb)) // 2, by + (bh - int(H * 0.021)) // 2 - 2),
               badge, font=fb, fill=(255, 255, 255, 230))
    im.save(out_path, quality=95)
    return str(out_path)


def make_end_card(out_path: str | Path, size: tuple[int, int], app_name: str = "",
                  headline: str = "后续更精彩", sub: str = "全集已更新 · 点击下方立即观看",
                  foot: str = "", accent: str = "#e8b754", bg_image: str | None = None,
                  qr_image: str | None = None, episodes_text: str = "") -> str:
    W, H = size
    im = _bg_from(bg_image, size)
    gr = Image.new("L", (1, H))
    for y in range(H):
        gr.putpixel((0, y), int(150 + 90 * (1 - abs(y / H - 0.45) * 2)))
    gr = gr.resize((W, H))
    im = Image.composite(Image.new("RGB", size, (14, 16, 24)), im, gr.point(lambda v: int(v * 0.7)))
    d = ImageDraw.Draw(im, "RGBA")

    y = int(H * 0.255)
    f1 = _fit(headline, int(W * 0.78), int(H * 0.056), True, int(H * 0.030))
    d.text((W // 2 - _text_w(headline, f1) // 2, y), headline, font=f1, fill=(255, 255, 255, 255))
    y += int(f1.size * 1.55)

    if sub:
        f2 = _fit(sub, int(W * 0.80), int(H * 0.026), False, int(H * 0.016))
        for ln in _wrap(sub, f2, int(W * 0.80))[:2]:
            d.text((W // 2 - _text_w(ln, f2) // 2, y), ln, font=f2, fill=(222, 227, 238, 235))
            y += int(f2.size * 1.5)
    y += int(H * 0.020)

    # 白卡片：有二维码贴码，没有就画成"按钮"样，不糊假码
    card_w = int(W * 0.64)
    pad = int(W * 0.035)
    if qr_image and Path(qr_image).exists():
        qr_size = int(card_w - pad * 2)
        card_h = qr_size + pad * 2 + (int(H * 0.070) if app_name else 0)
    else:
        card_h = int(H * 0.135) + (int(H * 0.062) if app_name else 0)
    cx = (W - card_w) // 2
    cy = y
    d.rounded_rectangle([cx, cy, cx + card_w, cy + card_h], radius=int(W * 0.030),
                        fill=(255, 255, 255, 246))

    if qr_image and Path(qr_image).exists():
        try:
            qr = Image.open(qr_image).convert("RGBA")
            qr = qr.resize((card_w - pad * 2, card_w - pad * 2), Image.LANCZOS)
            im.paste(qr, (cx + pad, cy + pad), qr)
        except Exception:
            pass
    else:
        bw2, bh2 = int(card_w * 0.78), int(H * 0.062)
        bx2 = cx + (card_w - bw2) // 2
        by2 = cy + int(H * 0.030)
        d.rounded_rectangle([bx2, by2, bx2 + bw2, by2 + bh2], radius=bh2 // 2,
                            fill=(18, 21, 30, 255))
        fb = _font(int(H * 0.024), True)
        t = "立即观看全集"
        d.text((bx2 + bw2 // 2 - _text_w(t, fb) // 2, by2 + (bh2 - int(H * 0.024)) // 2 - 1),
               t, font=fb, fill=(255, 255, 255, 245))
        fs2 = _font(int(H * 0.017), False)
        t2 = "打开 App 搜索剧名"
        d.text((cx + card_w // 2 - _text_w(t2, fs2) // 2, by2 + bh2 + int(H * 0.016)), t2,
               font=fs2, fill=(120, 126, 140, 255))

    if app_name:
        fa = _fit(app_name, card_w - int(W * 0.06), int(H * 0.026), True, int(H * 0.016))
        d.text((W // 2 - _text_w(app_name, fa) // 2, cy + card_h - int(H * 0.048)), app_name,
               font=fa, fill=(18, 20, 26, 255))

    y = cy + card_h + int(H * 0.026)
    if episodes_text:
        fe = _font(int(H * 0.021), True)
        d.text((W // 2 - _text_w(episodes_text, fe) // 2, y), episodes_text, font=fe,
               fill=accent)
        y += int(H * 0.034)

    if foot:
        ff = _fit(foot, int(W * 0.78), int(H * 0.017), False, 14)
        d.text((W // 2 - _text_w(foot, ff) // 2, int(H * 0.935)), foot, font=ff,
               fill=(140, 146, 162, 235))
    im.save(out_path, quality=95)
    return str(out_path)


def prepare_logo(src: str | Path, out_path: str | Path, out_w: int,
                 width_ratio: float = 0.14) -> str:
    """裁掉透明边、按输出**宽度**比例缩放（角标不该抢画面），输出带 alpha 的 png。"""
    im = Image.open(src).convert("RGBA")
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    target_w = max(36, int(out_w * width_ratio))
    s = target_w / im.width
    im = im.resize((target_w, max(1, int(im.height * s))), Image.LANCZOS)
    im.save(out_path)
    return str(out_path)


def prepare_overlay(src: str | Path, out_path: str | Path, w: int, h: int,
                    opacity: float = 1.0) -> str:
    """全屏包装模板（含 logo/剧名等设计的透明 png）：拉伸到输出尺寸并整体调不透明度。"""
    im = Image.open(src).convert("RGBA")
    im = im.resize((w, h), Image.LANCZOS)
    opacity = max(0.05, min(1.0, opacity))
    if opacity < 0.999:
        alpha = im.getchannel("A").point(lambda v: int(v * opacity))
        im.putalpha(alpha)
    im.save(out_path)
    return str(out_path)


def prepare_end_frame(src: str | Path, out_path: str | Path, w: int, h: int) -> str:
    """引导尾帧：等比铺满 + 居中裁切到输出尺寸，不留黑边。"""
    im = Image.open(src).convert("RGB")
    s = max(w / im.width, h / im.height)
    im = im.resize((int(im.width * s + 1), int(im.height * s + 1)), Image.LANCZOS)
    x = (im.width - w) // 2
    y = (im.height - h) // 2
    im = im.crop((x, y, x + w, y + h))
    im.save(out_path, quality=95)
    return str(out_path)


def compose_preview(bg_path: str | None, overlay_src: str | Path,
                    out_path: str | Path, w: int, h: int,
                    opacity: float = 1.0) -> str:
    """预览：把模板叠到一帧原片画面上，看包装效果（不跑整片）。"""
    if bg_path and Path(bg_path).exists():
        base = Image.open(bg_path).convert("RGB")
        s = max(w / base.width, h / base.height)
        base = base.resize((int(base.width * s + 1), int(base.height * s + 1)), Image.LANCZOS)
        base = base.crop(((base.width - w) // 2, (base.height - h) // 2,
                          (base.width - w) // 2 + w, (base.height - h) // 2 + h))
    else:
        base = Image.new("RGB", (w, h), (14, 17, 25))
    ov = Image.open(overlay_src).convert("RGBA").resize((w, h), Image.LANCZOS)
    opacity = max(0.05, min(1.0, opacity))
    if opacity < 0.999:
        ov.putalpha(ov.getchannel("A").point(lambda v: int(v * opacity)))
    out = Image.alpha_composite(base.convert("RGBA"), ov).convert("RGB")
    out.save(out_path, quality=92)
    return str(out_path)


def frame(dir_path: str | Path, name: str) -> str:
    return str(Path(dir_path) / name)
