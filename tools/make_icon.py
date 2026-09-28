"""アプリアイコン (PNG / ICO) を生成する.

CTガントリーのリングと体の断面。断面の左半分は通常線量、右半分は低線量のノイズ.
    python tools/make_icon.py
"""
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

S = 1024
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "src", "ctlowdose", "resources")


def vertical_gradient(size, top, bottom):
    t = np.linspace(0, 1, size)[:, None, None]
    g = (1 - t) * np.array(top) + t * np.array(bottom)
    return Image.fromarray(np.broadcast_to(g, (size, size, 3)).astype(np.uint8), "RGB")


def ellipse_mask(box):
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).ellipse(box, fill=255)
    return m


def main():
    rng = np.random.default_rng(3)
    c = S // 2

    #背景: 角丸の紺グラデーション
    bg = vertical_gradient(S, (30, 64, 104), (12, 26, 46)).convert("RGBA")
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((24, 24, S - 24, S - 24), radius=210, fill=255)
    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    icon.paste(bg, (0, 0), mask)
    d = ImageDraw.Draw(icon)

    #ガントリー (リング) と X線管
    d.ellipse((c - 420, c - 420, c + 420, c + 420), fill=(226, 234, 243, 255))
    d.ellipse((c - 352, c - 352, c + 352, c + 352), fill=(10, 18, 30, 255))
    d.rounded_rectangle((c - 70, c - 452, c + 70, c - 388), radius=22, fill=(45, 212, 191, 255))

    #体の断面 (楕円) + 臓器 + 脊椎
    body_box = (c - 285, c - 200, c + 285, c + 215)
    body = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bd = ImageDraw.Draw(body)
    bd.ellipse(body_box, fill=(128, 138, 150, 255))
    bd.ellipse((c - 250, c - 150, c - 10, c + 110), fill=(176, 184, 194, 255))    #肝臓
    bd.ellipse((c + 40, c - 130, c + 220, c + 60), fill=(92, 100, 112, 255))      #胃
    bd.ellipse((c - 52, c + 95, c + 52, c + 190), fill=(245, 247, 250, 255))      #脊椎

    #右半分に低線量ノイズ (粗い粒で小サイズでも見えるようにする)
    arr = np.asarray(body).astype(np.float32)
    grain = rng.normal(0, 1, (S // 16, S // 16))
    grain = np.kron(grain, np.ones((16, 16)))
    fine = rng.normal(0, 1, (S // 6 + 1, S // 6 + 1))
    fine = np.kron(fine, np.ones((6, 6)))[:S, :S]
    noise = 20 * grain + 12 * fine
    right = np.zeros((S, S), bool)
    right[:, c:] = True
    inside = np.asarray(ellipse_mask(body_box)) > 0
    sel = right & inside
    for ch in range(3):
        arr[..., ch][sel] = np.clip(arr[..., ch][sel] + noise[sel], 0, 255)
    body = Image.fromarray(arr.astype(np.uint8), "RGBA")
    icon.alpha_composite(body)

    #境界線 (通常線量 | 低線量)
    d = ImageDraw.Draw(icon)
    d.line((c, c - 230, c, c + 245), fill=(45, 212, 191, 255), width=14)

    #わずかに滑らかにして縮小
    icon = icon.filter(ImageFilter.SMOOTH)
    os.makedirs(OUT_DIR, exist_ok=True)
    icon.resize((256, 256), Image.LANCZOS).save(os.path.join(OUT_DIR, "icon.png"))
    icon.save(os.path.join(OUT_DIR, "icon.ico"),
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    icon.resize((512, 512), Image.LANCZOS).save(os.path.join(ROOT, "docs", "icon.png"))
    print("saved to", OUT_DIR)


if __name__ == "__main__":
    main()
