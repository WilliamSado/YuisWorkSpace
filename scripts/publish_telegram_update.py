#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import html
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx


BASE = Path(__file__).resolve().parent.parent


def read_props(product_dir: Path) -> dict[str, str]:
    props: dict[str, str] = {}
    candidates = [
        product_dir / "system" / "build.prop",
        product_dir / "system" / "system" / "build.prop",
        product_dir / "product" / "build.prop",
        product_dir / "system_ext" / "build.prop",
        product_dir / "vendor" / "build.prop",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(errors="replace").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                props.setdefault(key.strip(), value.strip())
    return props


def safe_tag(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value.replace(" ", "_"))


def render_banner(kind: str, model: str, device: str, output: Path) -> None:
    source = BASE / "assets" / f"{kind}-banner.jpg"
    if not source.is_file():
        raise RuntimeError(f"banner asset not found: {source}")
    if kind == "aviumui":
        shutil.copyfile(source, output)
        return
    image_tool = shutil.which("magick") or shutil.which("convert")
    if not image_tool:
        raise RuntimeError("ImageMagick is required to render the LineageOS banner")
    font = next(
        (
            path for path in (
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/noto/NotoSans-Regular.ttf",
            )
            if Path(path).is_file()
        ),
        None,
    )
    if not font:
        raise RuntimeError("DejaVu Sans or Noto Sans is required to render the banner")
    label = f"For {model.replace('_', ' ')} ({device})"
    point_size = "27" if len(label) <= 25 else "23"
    subprocess.run(
        [
            image_tool, str(source),
            "-fill", "white", "-draw", "rectangle 145,372 507,440",
            "-font", font, "-pointsize", point_size, "-fill", "#111111",
            "-gravity", "NorthWest", "-annotate", "+174+390", label,
            "-quality", "94", str(output),
        ],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rom", required=True)
    parser.add_argument("--family", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--release-type", choices=("nightly", "weekly", "monthly"), required=True)
    parser.add_argument("--rom-version", help="Explicit ROM version, for example 23.2 or 16.2")
    parser.add_argument("--artifact", required=True, help="Trusted artifact glob")
    parser.add_argument("--banner", choices=("lineageos", "aviumui"), required=True)
    parser.add_argument("--edit-message-id", type=int, help="Edit an existing Telegram caption instead of posting again")
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_RELEASE_CHAT", "@YuiChanelUpdate")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    matches = [Path(path) for path in glob.glob(args.artifact)]
    if not matches:
        raise RuntimeError(f"no release artifact matches {args.artifact}")
    artifact = max(matches, key=lambda path: path.stat().st_mtime)
    source_root = Path.cwd()
    product_dir = source_root / "out" / "target" / "product" / args.device
    props = read_props(product_dir)

    model_files = [artifact.parent / "LINEAGE_CUSTOM_MODEL", product_dir / "LINEAGE_CUSTOM_MODEL"]
    model = os.environ.get("LINEAGE_CUSTOM_MODEL", "").strip()
    model = model or next((p.read_text().strip() for p in model_files if p.is_file() and p.read_text().strip()), "")
    model = model or props.get("ro.product.model") or args.device
    model = model.replace(" ", "_")

    full_rom_version = (
        props.get("ro.avium.version")
        or props.get("ro.aviumui.version")
        or props.get("ro.lineage.version")
        or props.get("ro.build.version.incremental")
        or "unknown"
    )
    rom_version = args.rom_version or full_rom_version.split("-")[0]
    android_version = props.get("ro.build.version.release", "16")
    security_patch = props.get("ro.build.version.security_patch", "unknown")
    build_variant = props.get("ro.build.type", "userdebug")
    build_date = datetime.now().strftime("%Y-%m-%d")
    path_date = datetime.now().strftime("%Y%m%d")
    download = (
        "https://sourceforge.net/projects/yuis-release/files/"
        f"{quote(args.family)}/{quote(args.device)}/{path_date}/"
    )

    caption = (
        f"#{safe_tag(args.rom)} #{args.release_type} #Android{safe_tag(android_version)} #{safe_tag(model)}\n"
        f"<b>{html.escape(args.rom)} {html.escape(rom_version)} | {args.release_type} | "
        f"Android {html.escape(android_version)}</b>\n\n"
        f"📅 Build date: {build_date}\n"
        f"🛡 Security patch: {html.escape(security_patch)}\n"
        f"💬 Variant: {html.escape(build_variant)}\n\n"
        f"◾️ <a href=\"{download}\">Download</a>\n\n"
        "Notes:\n"
        "• Selinux is Enforcing.\n"
        "• Release key\n"
        "• CI Build\n\n"
        "Bugs:\n"
        "- U tell me\n\n"
        "By: @William_sadoyui\n"
        "Follow @YuiChanelUpdate\n"
        "Join @YuiChanel"
    )

    if args.edit_message_id:
        with httpx.Client(timeout=90) as client:
            response = client.post(
                f"https://api.telegram.org/bot{token}/editMessageCaption",
                json={
                    "chat_id": chat,
                    "message_id": args.edit_message_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                },
            )
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError(result.get("description", "Telegram rejected caption edit"))
            print(f"Telegram update caption edited in {chat}: message {args.edit_message_id}")
        return

    temp_root = Path(os.environ.get("ANDROID_SIGNING_TMPDIR", "/home/ubuntu/aosp/.tmp/signing"))
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="telegram-update-", dir=temp_root) as temp:
        banner = Path(temp) / "banner.jpg"
        render_banner(args.banner, model, args.device, banner)
        with banner.open("rb") as photo, httpx.Client(timeout=90) as client:
            response = client.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat, "caption": caption, "parse_mode": "HTML"},
                files={"photo": (banner.name, photo, "image/jpeg")},
            )
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError(result.get("description", "Telegram rejected update post"))
            print(f"Telegram update published to {chat}: message {result['result']['message_id']}")


if __name__ == "__main__":
    main()
