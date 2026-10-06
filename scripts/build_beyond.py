"""
生成 genshin/beyond 资源：物品数据（GenshinBeyondGachaInfo.json）与物品图标。

数据：Snap.Hutao.Remastered 的 Snap.Metadata（BeyondItem.json）为主，
AnimeGameData 的 BydMaterial / BeyondEmoji 表补齐表情图标与元数据尚未收录的物品。
图标：按 ICON_SOURCES 顺序下载缺失的图标，统一缩放到不超过 256px 的 PNG；
装有 pngquant 时再做有损压缩（达不到画质下限则保留无损）。

用法：
    python scripts/build_beyond.py                 # 输出到 public/，状态写入 data/
    python scripts/build_beyond.py --out X --state Y
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent

HUTAO_BEYOND_URL = "https://raw.githubusercontent.com/SnapHutaoRemasteringProject/Snap.Metadata/main/Genshin/CHS/BeyondItem.json"
AGD_EXCEL_URL = "https://gitlab.com/Dimbreath/animegamedata2/-/raw/main/ExcelBinOutput/{name}.json"

# 按顺序尝试；上游 Starward 为原图 PNG，Nanoka 只有 webp。
ICON_SOURCES = [
    ("starward", "https://starward-static.scighost.com/game-assets/genshin/beyond/{name}.png"),
    ("hutao", "https://static.snaphutaorp.org/static/raw/BeyondItemIcon/{name}.png"),
    ("nanoka", "https://static.nanoka.cc/assets/gi/{name}.webp"),
]

MAX_ICON_SIZE = 256
DOWNLOAD_WORKERS = 6
USER_AGENT = "Moonward-Assets/1.0 (+https://github.com/TurmoilZoom/Moonward-Assets)"

# 图标名来自外部数据，只接受资源名字符，防止路径穿越。
ICON_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")

# 本次物品数低于上次的该比例时视为上游数据异常，拒绝覆盖。
MIN_ITEM_RATIO = 0.9


def fetch(url: str, timeout: int = 60, retries: int = 3) -> bytes | None:
    """
    下载 URL 内容。

    Args:
        url: 地址。
        timeout: 单次请求超时（秒）。
        retries: 失败重试次数（404 不重试）。
    Returns:
        响应内容；404 或多次失败返回 None。
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                return None
        # 连接中断、超时、分块不完整等都按可重试处理。
        except (http.client.HTTPException, OSError):
            pass
        time.sleep(2 * (attempt + 1))
    return None


def fetch_json(url: str):
    """
    下载并解析 JSON；失败直接退出，避免用残缺数据覆盖已发布内容。

    Args:
        url: 地址。
    Returns:
        解析后的对象。
    """
    data = fetch(url, timeout=120)
    if data is None:
        sys.exit(f"下载失败：{url}")
    return json.loads(data)


def emoji_icon_name(prefab_name: str) -> str:
    """
    表情预制体名 → 图标名，例如 Beyd_Eff_UI_Emoji_Common_Rare_18 → UI_UGC_Emoji_Rare_18。

    Args:
        prefab_name: BeyondEmojiExcelConfigData 的 prefabName。
    Returns:
        图标名。
    """
    return re.sub(r"^Beyd_Eff_UI_Emoji_(Common_)?", "UI_UGC_Emoji_", prefab_name)


def resolve_items(hutao: list, materials: list, emojis: list) -> tuple[list[dict], list[str]]:
    """
    合并元数据，得到每个物品的 Id / 名称 / 星级 / 图标名。

    Args:
        hutao: BeyondItem.json。
        materials: BydMaterialExcelConfigData.json。
        emojis: BeyondEmojiExcelConfigData.json。
    Returns:
        物品列表（Icon 为图标名，可能为空）与日志行。
    """
    material_by_id = {x["id"]: x for x in materials if "id" in x}
    prefab_by_emoji = {x["id"]: x.get("prefabName", "") for x in emojis if "id" in x}
    logs: list[str] = []

    def fallback_icon(item_id: int) -> str:
        material = material_by_id.get(item_id)
        if material is None:
            return ""
        if material.get("icon"):
            return material["icon"]
        # 表情物品没有 icon 字段，需经表情表的预制体名推出图标名。
        for use in material.get("itemUse", []):
            if use.get("useOp") == "BYD_MATERIAL_USE_GAIN_EMOJI":
                params = use.get("useParam") or [""]
                prefab = prefab_by_emoji.get(int(params[0])) if params[0].isdigit() else None
                if prefab:
                    return emoji_icon_name(prefab)
        return ""

    items: dict[int, dict] = {}
    filled = 0
    for x in hutao:
        item_id = int(x["Id"])
        icon = x.get("Icon") or ""
        if not icon:
            icon = fallback_icon(item_id)
            filled += bool(icon)
        items[item_id] = {"Id": item_id, "Name": x.get("Name") or "", "Rank": int(x.get("RankLevel") or 0), "Icon": icon}

    # 元数据滞后时，用游戏数据补上能直接确定图标的新物品（名称留空，客户端显示用的是接口返回的名称）。
    supplemented = 0
    for item_id, material in material_by_id.items():
        if item_id in items:
            continue
        icon = fallback_icon(item_id)
        if icon:
            items[item_id] = {"Id": item_id, "Name": "", "Rank": int(material.get("rankLevel") or 0), "Icon": icon}
            supplemented += 1

    for item in items.values():
        if item["Icon"] and not ICON_NAME_PATTERN.match(item["Icon"]):
            logs.append(f"非法图标名已忽略：{item['Id']} {item['Icon']!r}")
            item["Icon"] = ""

    logs.append(f"元数据 {len(hutao)} 条，补全表情图标 {filled} 个，从游戏数据补充物品 {supplemented} 个")
    return sorted(items.values(), key=lambda x: x["Id"]), logs


def download_icon(name: str, target: Path) -> str | None:
    """
    按来源顺序下载图标，缩放后保存为 PNG。

    Args:
        name: 图标名。
        target: 保存路径。
    Returns:
        成功的来源名；全部失败返回 None。
    """
    for source, template in ICON_SOURCES:
        data = fetch(template.format(name=name), timeout=30, retries=2)
        if not data:
            continue
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except Exception:
            continue
        image = image.convert("RGBA")
        if max(image.size) > MAX_ICON_SIZE:
            image.thumbnail((MAX_ICON_SIZE, MAX_ICON_SIZE), Image.Resampling.LANCZOS)
        temp = target.with_suffix(".tmp")
        image.save(temp, "PNG", optimize=True)
        temp.replace(target)
        return source
    return None


def optimize_icon(path: Path, pngquant: str) -> None:
    """
    用 pngquant 有损压缩；达不到画质下限或压缩后更大时保留原文件。

    Args:
        path: PNG 路径。
        pngquant: pngquant 可执行文件路径。
    """
    temp = path.with_suffix(".tmp")
    result = subprocess.run(
        [pngquant, "--quality=80-100", "--speed=1", "--strip", "--skip-if-larger", "--force", "--output", str(temp), str(path)],
        capture_output=True,
    )
    # 0 = 已压缩；98 = 压缩后更大；99 = 达不到画质下限。后两种保留无损原图。
    if result.returncode == 0 and temp.exists():
        temp.replace(path)
    elif result.returncode not in (98, 99):
        raise RuntimeError(f"pngquant 失败（{result.returncode}）：{path.name} {result.stderr.decode(errors='ignore')}")
    if temp.exists():
        temp.unlink()


def write_text_atomic(path: Path, text: str) -> None:
    """
    原子写入文本（LF 换行）。

    Args:
        path: 目标路径。
        text: 内容。
    """
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    temp.replace(path)


def dump_items(items: list[dict]) -> str:
    """
    每个物品一行，便于在 git 里看差异。

    Args:
        items: 物品列表。
    Returns:
        JSON 文本。
    """
    lines = [json.dumps(x, ensure_ascii=False, separators=(",", ":")) for x in items]
    return "[\n" + ",\n".join(lines) + "\n]\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=ROOT / "public", help="发布目录")
    parser.add_argument("--state", type=Path, default=ROOT / "data", help="图标清单等不发布的状态文件目录")
    args = parser.parse_args()

    beyond_dir = args.out / "genshin" / "beyond"
    icon_dir = beyond_dir / "icons"
    json_path = beyond_dir / "GenshinBeyondGachaInfo.json"
    manifest_path = args.state / "beyond-icons.json"
    icon_dir.mkdir(parents=True, exist_ok=True)
    args.state.mkdir(parents=True, exist_ok=True)

    print("下载元数据…")
    hutao = fetch_json(HUTAO_BEYOND_URL)
    materials = fetch_json(AGD_EXCEL_URL.format(name="BydMaterialExcelConfigData"))
    emojis = fetch_json(AGD_EXCEL_URL.format(name="BeyondEmojiExcelConfigData"))

    items, logs = resolve_items(hutao, materials, emojis)
    if json_path.exists():
        previous = len(json.loads(json_path.read_text(encoding="utf-8")))
        if len(items) < previous * MIN_ITEM_RATIO:
            sys.exit(f"物品数从 {previous} 降到 {len(items)}，疑似上游数据异常，已中止")

    # 清单记录每张图标的来源与是否已压缩，避免重复下载、重复压缩。
    manifest: dict[str, dict] = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    needed = sorted({x["Icon"] for x in items if x["Icon"]})
    missing = [n for n in needed if not (icon_dir / f"{n}.png").exists()]
    print(f"物品 {len(items)} 个，图标 {len(needed)} 张，需下载 {len(missing)} 张")

    failed: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(DOWNLOAD_WORKERS) as pool:
        futures = {pool.submit(download_icon, n, icon_dir / f"{n}.png"): n for n in missing}
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            name = futures[future]
            try:
                source = future.result()
            except Exception as ex:
                # 单张图标出错只记失败，不影响其余图标与数据发布。
                print(f"  下载出错：{name} {ex!r}")
                source = None
            if source:
                manifest[name] = {"source": source, "optimized": False}
            else:
                failed.append(name)
            if done % 100 == 0:
                print(f"  已处理 {done}/{len(missing)}")

    pngquant = shutil.which("pngquant")
    pending = [n for n in needed if (icon_dir / f"{n}.png").exists() and not manifest.get(n, {}).get("optimized")]
    if pngquant:
        for name in pending:
            optimize_icon(icon_dir / f"{name}.png", pngquant)
            manifest.setdefault(name, {"source": "unknown"})["optimized"] = True
    elif pending:
        logs.append(f"未找到 pngquant，{len(pending)} 张图标保持无损")

    # 不再被引用的图标一并删除，保持发布目录干净。
    needed_set = set(needed)
    removed = 0
    for file in icon_dir.glob("*.png"):
        if file.stem not in needed_set:
            file.unlink()
            removed += 1
    manifest = {k: v for k, v in sorted(manifest.items()) if k in needed_set and (icon_dir / f"{k}.png").exists()}

    for item in items:
        name = item["Icon"]
        item["Icon"] = f"icons/{name}.png" if name and (icon_dir / f"{name}.png").exists() else ""

    write_text_atomic(json_path, dump_items(items))
    write_text_atomic(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")

    total_bytes = sum(f.stat().st_size for f in icon_dir.glob("*.png"))
    no_icon = sum(1 for x in items if not x["Icon"])
    summary = [
        "## genshin/beyond",
        "",
        *[f"- {line}" for line in logs],
        f"- 物品 {len(items)} 个（无图标 {no_icon} 个），图标 {len(manifest)} 张，共 {total_bytes / 1048576:.1f} MiB",
        f"- 本次下载 {len(missing) - len(failed)} 张，删除 {removed} 张",
    ]
    if failed:
        summary += ["", f"### 所有来源都缺失的图标（{len(failed)}）", "", *[f"- `{n}`" for n in failed]]
    text = "\n".join(summary) + "\n"
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
