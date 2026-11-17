"""
Genera l'icona dell'app (1024x1024, senza trasparenza come richiesto da iOS).
Uso: python ios/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024
K = 4  # disegno a risoluzione 4x e poi riduco, per bordi lisci
W = S * K
OUT = Path(__file__).parent / "SecondScreen" / "Assets.xcassets" / "AppIcon.appiconset" / "icon-1024.png"


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def main():
    # sfondo: gradiente diagonale blu notte -> blu -> turchese
    top, mid, bottom = (17, 24, 64), (32, 76, 190), (36, 178, 196)
    grad = Image.new("RGB", (256, 256))
    px = grad.load()
    for y in range(256):
        for x in range(256):
            t = (x * 0.35 + y * 0.65) / 255
            px[x, y] = lerp(top, mid, t / 0.55) if t < 0.55 else lerp(mid, bottom, (t - 0.55) / 0.45)
    img = grad.resize((W, W), Image.BICUBIC)

    def r(v):
        return int(v * K)

    # ombra morbida sotto i dispositivi
    shadow = Image.new("L", (W, W), 0)
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle([r(150), r(250), r(700), r(640)], radius=r(40), fill=150)
    sd.rounded_rectangle([r(560), r(380), r(880), r(820)], radius=r(46), fill=170)
    shadow = shadow.filter(ImageFilter.GaussianBlur(r(28)))
    img.paste((8, 14, 40), (r(14), r(22)), shadow)

    d = ImageDraw.Draw(img)
    white = (245, 248, 255)
    screen = (24, 42, 96)

    # monitor del PC
    d.rounded_rectangle([r(140), r(220), r(690), r(600)], radius=r(36), fill=white)
    d.rounded_rectangle([r(168), r(248), r(662), r(560)], radius=r(16), fill=screen)
    d.rounded_rectangle([r(375), r(600), r(455), r(680)], radius=r(8), fill=white)       # collo
    d.rounded_rectangle([r(290), r(670), r(540), r(712)], radius=r(21), fill=white)      # base

    # iPad
    d.rounded_rectangle([r(560), r(360), r(860), r(800)], radius=r(44), fill=white)
    d.rounded_rectangle([r(584), r(384), r(836), r(776)], radius=r(24), fill=screen)

    # "finestra" che passa dal monitor all'iPad: lo schermo esteso
    acc = (130, 236, 230)
    d.rounded_rectangle([r(210), r(300), r(470), r(470)], radius=r(14), fill=(66, 110, 220))
    d.rounded_rectangle([r(210), r(300), r(470), r(336)], radius=r(14), fill=acc)
    d.rounded_rectangle([r(606), r(460), r(814), r(620)], radius=r(14), fill=(66, 110, 220))
    d.rounded_rectangle([r(606), r(460), r(814), r(494)], radius=r(14), fill=acc)

    # freccia del puntatore sull'iPad
    arrow = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]
    ax, ay, k = 700, 560, 7.5
    d.polygon([(r(ax + x * k), r(ay + y * k)) for x, y in arrow], fill=white, outline=screen,
              width=r(5))

    img = img.resize((S, S), Image.LANCZOS)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, "PNG")
    print("icona salvata in", OUT)


if __name__ == "__main__":
    main()
