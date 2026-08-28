"""素材パイプライン(クロマキー→パーツ分離→サイズ予算検査→WebP再エンコード)。

assets/raw/ に置かれた単色緑背景の画像を処理し、assets/parts/ に透過WebPと
manifest.json を出力する。Pillow+numpy のみに依存(scipyがあれば連結成分に利用)。

命名規則(DECISIONS.md):
  background* / bg* → 背景(クロマキーなし・そのまま使用)
  part* / wing*     → 可動パーツ(クロマキー+連結成分分離。大きい順最大2個)
  それ以外           → 胴体(クロマキー・最大成分のみ)

サイズ予算: シーンSVGに内包した合計(base64換算)が上限1MB・目標500KB。
超過時は品質→縮尺の梯子で自動段階ダウンする。
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

try:  # 画像処理系はパイプライン実行時のみ必要(シーン生成はmanifest+バイト列だけで動く)
    import numpy as np
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - CI/実行環境では常に入っている
    np = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]

RAW_DIR = "assets/raw"
PARTS_DIR = "assets/parts"
MANIFEST = "manifest.json"

SCENE_BUDGET_TARGET = 500 * 1024  # base64換算の目標
MARKUP_HEADROOM = 64 * 1024  # SVGマークアップ+プレースホルダ分の余白
SCENE_BUDGET_MAX = 1024 * 1024 - MARKUP_HEADROOM  # 素材合計の上限(シーン全体で1MBを守る)
QUALITY_LADDER = (88, 80, 70, 60)
SCALE_LADDER = (1.0, 0.85, 0.7, 0.55)

# 表示サイズ(シーンSVG内での最大描画幅)。素材はこの1.0〜1.5倍に制限する
DISPLAY_W = {"background": 760, "body": 340, "part": 220}

RAW_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def _b64_size(raw_bytes: int) -> int:
    return (raw_bytes + 2) // 3 * 4


def classify_raw(name: str) -> str:
    lower = name.lower()
    if lower.startswith(("background", "bg")):
        return "background"
    if lower.startswith(("part", "wing")):
        return "part"
    return "body"


def chroma_key(img: Image.Image) -> Image.Image:
    """緑背景を透過化する。エッジの緑にじみも抑える。"""
    rgba = np.asarray(img.convert("RGBA")).astype(np.int16)
    r, g, b, a = rgba[..., 0], rgba[..., 1], rgba[..., 2], rgba[..., 3]
    green_mask = (g > r * 1.25) & (g > b * 1.25) & (g > 90)
    a = np.where(green_mask, 0, a)
    # にじみ除去: 残った画素の過剰なG成分をmax(R,B)にクランプ
    spill = (~green_mask) & (g > np.maximum(r, b))
    g = np.where(spill, np.maximum(r, b), g)
    out = np.stack([r, g, b, a], axis=-1).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def _label_components(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """連結成分ラベリング。scipyがあれば利用、なければ簡易2パス(4近傍)。"""
    try:
        from scipy import ndimage  # type: ignore

        labels, count = ndimage.label(mask)
        return labels, int(count)
    except ImportError:
        pass
    labels = np.zeros(mask.shape, dtype=np.int32)
    current = 0
    stack: list[tuple[int, int]] = []
    h, w = mask.shape
    for sy in range(h):
        for sx in range(w):
            if mask[sy, sx] and labels[sy, sx] == 0:
                current += 1
                stack.append((sy, sx))
                labels[sy, sx] = current
                while stack:
                    y, x = stack.pop()
                    for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                        if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and labels[ny, nx] == 0:
                            labels[ny, nx] = current
                            stack.append((ny, nx))
    return labels, current


def split_components(img: Image.Image, max_parts: int) -> list[Image.Image]:
    """透過画像から不透明の連結成分を大きい順に最大 max_parts 個切り出す。"""
    alpha = np.asarray(img)[..., 3]
    mask = alpha > 16
    if not mask.any():
        return []
    labels, count = _label_components(mask)
    if count == 0:
        return []
    sizes = [(int((labels == i).sum()), i) for i in range(1, count + 1)]
    sizes.sort(reverse=True)
    min_pixels = max(64, int(mask.sum() * 0.02))  # ノイズ成分は捨てる
    out: list[Image.Image] = []
    for size, idx in sizes[:max_parts]:
        if size < min_pixels:
            continue
        ys, xs = np.where(labels == idx)
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        piece = np.asarray(img).copy()
        piece[..., 3] = np.where(labels == idx, piece[..., 3], 0)
        out.append(Image.fromarray(piece[y0:y1, x0:x1], "RGBA"))
    return out


def _limit_resolution(img: Image.Image, kind: str) -> Image.Image:
    limit = int(DISPLAY_W.get(kind, 340) * 1.5)
    if img.width <= limit:
        return img
    ratio = limit / img.width
    return img.resize((limit, max(1, int(img.height * ratio))), Image.LANCZOS)


def _encode(img: Image.Image, kind: str, quality: int, scale: float) -> bytes:
    work = img
    if scale < 1.0:
        floor = int(DISPLAY_W.get(kind, 340) * 1.0)  # 表示サイズ×1.0を下限
        new_w = max(min(img.width, floor), int(img.width * scale))
        if new_w < img.width:
            work = img.resize((new_w, max(1, int(img.height * new_w / img.width))), Image.LANCZOS)
    buf = io.BytesIO()
    if kind == "background":
        work.convert("RGB").save(buf, "WEBP", quality=quality, method=6)
    else:
        work.save(buf, "WEBP", quality=quality, method=6)
    return buf.getvalue()


def _part_pivot(img: Image.Image) -> tuple[int, int]:
    """可動パーツの付け根(pivot)推定: 不透明画素の被覆が厚い側の端の中央。

    翼などは付け根側の端に向かって太くなるため、左右端3列のアルファ量を比べて
    厚い側を付け根とみなす(manifest.jsonを手で編集すれば上書きできる)。
    """
    alpha = np.asarray(img)[..., 3]
    mask = alpha > 16
    h, w = mask.shape
    edge = max(1, min(3, w))
    left_cov = int(mask[:, :edge].sum())
    right_cov = int(mask[:, -edge:].sum())
    if right_cov >= left_cov and right_cov > 0:
        ys = np.where(mask[:, -edge:].any(axis=1))[0]
        return w - 1, int(ys.mean()) if len(ys) else h // 2
    if left_cov > 0:
        ys = np.where(mask[:, :edge].any(axis=1))[0]
        return 0, int(ys.mean()) if len(ys) else h // 2
    return w // 2, h // 2


def has_raw_assets(root: str | Path = ".") -> bool:
    raw_dir = Path(root) / RAW_DIR
    return raw_dir.is_dir() and any(p.suffix.lower() in RAW_EXTS for p in raw_dir.iterdir())


def process_raw_assets(root: str | Path = ".") -> dict[str, Any] | None:
    """assets/raw/ を処理して assets/parts/ を生成し、manifestを返す。素材が無ければ None。"""
    if Image is None or np is None:
        raise RuntimeError("Pillow/numpy が必要です(pip install Pillow numpy)")
    root_path = Path(root)
    raw_dir = root_path / RAW_DIR
    if not raw_dir.is_dir():
        return None
    raw_files = sorted(p for p in raw_dir.iterdir() if p.suffix.lower() in RAW_EXTS)
    if not raw_files:
        return None

    background: Image.Image | None = None
    body: Image.Image | None = None
    part_images: list[Image.Image] = []
    for f in raw_files:
        kind = classify_raw(f.name)
        img = Image.open(f)
        img.load()
        img = ImageOps.exif_transpose(img)  # スマホ縦写真の向きを正す
        if kind == "background":
            background = _limit_resolution(img.convert("RGB"), "background")
        elif kind == "part":
            keyed = chroma_key(img)
            for piece in split_components(keyed, max_parts=2):
                part_images.append(_limit_resolution(piece, "part"))
        else:
            keyed = chroma_key(img)
            pieces = split_components(keyed, max_parts=1)
            if pieces:
                body = _limit_resolution(pieces[0], "body")

    # 品質→縮尺の梯子で予算に収める
    chosen: dict[str, Any] | None = None
    for scale in SCALE_LADDER:
        for quality in QUALITY_LADDER:
            encoded: dict[str, bytes] = {}
            if background is not None:
                encoded["background.webp"] = _encode(background, "background", quality, scale)
            if body is not None:
                encoded["body.webp"] = _encode(body, "body", quality, scale)
            for i, part in enumerate(part_images):
                encoded[f"part{i + 1}.webp"] = _encode(part, "part", quality, scale)
            total_b64 = sum(_b64_size(len(v)) for v in encoded.values())
            if total_b64 <= SCENE_BUDGET_TARGET or (
                scale == SCALE_LADDER[-1] and quality == QUALITY_LADDER[-1]
            ):
                chosen = {"encoded": encoded, "quality": quality, "scale": scale, "total_b64": total_b64}
                break
        if chosen:
            break
    assert chosen is not None
    if chosen["total_b64"] > SCENE_BUDGET_MAX:
        raise ValueError(f"素材が大きすぎます(最小設定でも{chosen['total_b64']}B > 1MB)")

    parts_dir = root_path / PARTS_DIR
    parts_dir.mkdir(parents=True, exist_ok=True)
    for stale in parts_dir.glob("*"):  # 前回の敵の残骸(孤児WebP・旧manifest)を残さない
        if stale.suffix.lower() in (".webp", ".json"):
            stale.unlink()
    for name, data in chosen["encoded"].items():
        (parts_dir / name).write_bytes(data)

    def dims(name: str) -> tuple[int, int]:
        with Image.open(io.BytesIO(chosen["encoded"][name])) as im:
            return im.width, im.height

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "quality": chosen["quality"],
        "scale": chosen["scale"],
        "total_b64_bytes": chosen["total_b64"],
    }
    if background is not None:
        w, h = dims("background.webp")
        manifest["background"] = {"file": "background.webp", "w": w, "h": h}
    if body is not None:
        w, h = dims("body.webp")
        manifest["body"] = {"file": "body.webp", "w": w, "h": h}
    manifest["parts"] = []
    for i, part_img in enumerate(part_images):
        name = f"part{i + 1}.webp"
        w, h = dims(name)
        px, py = _part_pivot(part_img)
        # pivotは元解像度基準→エンコード後の寸法にスケールする
        px = int(px * w / max(1, part_img.width))
        py = int(py * h / max(1, part_img.height))
        manifest["parts"].append({"file": name, "w": w, "h": h, "pivot": [px, py], "z": "back" if i % 2 else "front"})
    with open(parts_dir / MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def load_manifest(root: str | Path = ".") -> dict[str, Any] | None:
    path = Path(root) / PARTS_DIR / MANIFEST
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def part_data_uri(root: str | Path, filename: str) -> str:
    data = (Path(root) / PARTS_DIR / filename).read_bytes()
    return "data:image/webp;base64," + base64.b64encode(data).decode()
