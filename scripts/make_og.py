# -*- coding: utf-8 -*-
"""
產生分享預覽圖 og.png（1200x630）。

配色與決賽簡報同一套。資產是產生出來的不是手畫的，之後改標題重跑一次就好：

    py -3 scripts/make_og.py
"""

import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "og.png")

W, H = 1200, 630
PLUM = (42, 36, 56)
SKY = (47, 169, 220)
MINT = (53, 190, 147)
GOLD = (255, 197, 49)
VIOLET = (122, 107, 196)
LILAC = (216, 210, 232)
MUTED = (149, 142, 166)

JHENGHEI = r"C:\Windows\Fonts\msjhbd.ttc"
JHENGHEI_R = r"C:\Windows\Fonts\msjh.ttc"


def font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default(size)


def glow(img, xy, r, color, alpha=120, blur=90):
    """簡報深色頁上那種柔光暈。"""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse(
        [xy[0] - r, xy[1] - r, xy[0] + r, xy[1] + r], fill=color + (alpha,))
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    return Image.alpha_composite(img, layer)


def chip(d, x, y, dot, text, f):
    """圓點 + 文字的分區標籤，對應網站上的三張卡。"""
    r = 9
    d.ellipse([x, y + 7, x + r * 2, y + 7 + r * 2], outline=dot, width=3)
    d.text((x + r * 2 + 13, y), text, font=f, fill=LILAC)
    return x + r * 2 + 13 + d.textlength(text, font=f) + 42


def main():
    img = Image.new("RGBA", (W, H), PLUM + (255,))
    img = glow(img, (1090, 40), 250, SKY, 110, 95)
    img = glow(img, (90, 600), 240, VIOLET, 110, 95)

    d = ImageDraw.Draw(img)

    f_kicker = font(JHENGHEI_R, 25)
    f_title = font(JHENGHEI, 66)
    f_sub = font(JHENGHEI_R, 28)
    f_chip = font(JHENGHEI, 26)
    f_foot = font(JHENGHEI_R, 23)

    x = 84

    d.text((x, 86), "海陸空生態戰情室", font=f_kicker, fill=SKY)

    d.text((x, 138), "風機是鳥類果汁機？", font=f_title, fill=(255, 255, 255))
    d.text((x, 218), "海豚殺手？", font=f_title, fill=(255, 255, 255))

    # 標題底下的漸層螢光筆，跟網站 .hook-q::after 一致
    for i in range(150):
        t = i / 149
        c = tuple(int(SKY[k] + (MINT[k] - SKY[k]) * t) for k in range(3))
        d.rectangle([x + i, 312, x + i + 1, 318], fill=c)

    d.text((x, 352), "三個都市傳說，用政府開放資料一次拆完。", font=f_sub, fill=LILAC)

    cx = x
    for dot, label in ((SKY, "天空　鳥類防護"), (MINT, "水下　鯨豚觀測"),
                       (GOLD, "陸地　低頻噪音")):
        cx = chip(d, cx, 432, dot, label, f_chip)

    d.line([x, 512, W - x, 512], fill=(90, 82, 112), width=1)
    d.text((x, 536), "三鳥牌　│　115 年度大專院校能源開放資料創意競賽「用 AI．懂能源」",
           font=f_foot, fill=MUTED)

    img.convert("RGB").save(OUT, "PNG", optimize=True)
    print("寫入 %s（%dx%d）" % (OUT, W, H))


if __name__ == "__main__":
    main()
