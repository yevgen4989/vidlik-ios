"""Publish the IPA submitted from the private build workflow."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
from shutil import copyfileobj
import stat
import subprocess
import sys
import tempfile
from urllib.request import urlopen
from zipfile import ZipFile


REPO = "yevgen4989/vidlik-ios"
BUNDLE = "com.pisotsky.vidlik"
RUNNER = "Payload/Runner.app"


def validate(ipa, version, build):
    with ZipFile(ipa) as archive:
        paths = [PurePosixPath(entry.filename) for entry in archive.infolist()]
        if (
            any(path.parts[:1] != ("Payload",) for path in paths)
            or {p.parts[1] for p in paths if len(p.parts) > 1} != {"Runner.app"}
            or any(part.startswith("._") for path in paths for part in path.parts)
            or any(path.name == "embedded.mobileprovision" for path in paths)
            or archive.testzip() is not None
        ):
            raise ValueError("Unexpected IPA layout or corrupt archive")
        info = plistlib.loads(archive.read(f"{RUNNER}/Info.plist"))
        if (
            info["CFBundleIdentifier"] != BUNDLE
            or info["CFBundleSupportedPlatforms"] != ["iPhoneOS"]
            or info["CFBundleShortVersionString"] != version
            or info["CFBundleVersion"] != build
            or info["CFBundleDisplayName"] != "Vidlik"
        ):
            raise ValueError("Unexpected iOS bundle metadata")
        executable = archive.getinfo(f"{RUNNER}/{info['CFBundleExecutable']}")
        if not (executable.external_attr >> 16) & stat.S_IXUSR:
            raise ValueError("The bundle executable is not marked executable")
        for locale in ("en", "uk"):
            strings = plistlib.loads(
                archive.read(f"{RUNNER}/{locale}.lproj/InfoPlist.strings")
            )
            if strings["CFBundleDisplayName"] != "Відлік":
                raise ValueError(f"Incorrect Home Screen name for {locale}")
    return info


def version_parts(value):
    return tuple(int(part) for part in value.split("."))


def publish(incoming, catalog):
    build = (incoming / "build-number").read_text().strip()
    if not re.fullmatch(r"[1-9][0-9]*", build):
        raise ValueError("Invalid build number")
    version = f"1.1.{build}"
    source = json.loads(catalog.read_text())
    app = next(app for app in source["apps"] if app["bundleIdentifier"] == BUNDLE)
    if version_parts(app["versions"][0]["version"]) > version_parts(version):
        print("A newer release is already listed.")
        return

    ipa = incoming / f"Vidlik-{version}-{build}.ipa"
    validate(ipa, version, build)
    tag = f"ios-{version}"
    release = subprocess.run(
        ["gh", "api", f"repos/{REPO}/releases/tags/{tag}"],
        capture_output=True, text=True,
    )
    created = release.returncode != 0
    if created:
        if "HTTP 404" not in release.stderr:
            raise RuntimeError("Cannot check the existing GitHub release")
        subprocess.run(
            [
                "gh", "release", "create", tag, str(ipa), "--repo", REPO,
                "--target", "main", "--prerelease", "--title", f"Відлік для iPhone {version}",
                "--notes", f"Автоматична збірка Відліку для SideStore, версія {version}.",
            ],
            check=True,
        )
    else:
        details = json.loads(release.stdout)
        if details["draft"] or not any(asset["name"] == ipa.name for asset in details["assets"]):
            raise ValueError("Existing release is incomplete")

    url = f"https://github.com/{REPO}/releases/download/{tag}/{ipa.name}"
    with tempfile.TemporaryDirectory() as folder:
        downloaded = Path(folder) / ipa.name
        with urlopen(url, timeout=30) as response, downloaded.open("wb") as output:
            copyfileobj(response, output)
        info = validate(downloaded, version, build)
        if created and hashlib.sha256(downloaded.read_bytes()).digest() != hashlib.sha256(ipa.read_bytes()).digest():
            raise ValueError("Published IPA does not match the submitted IPA")
        size = downloaded.stat().st_size

    if app["versions"][0]["version"] == version:
        if app["versions"][0]["downloadURL"] != url or app["versions"][0]["size"] != size:
            raise ValueError("Existing catalog entry disagrees with the published IPA")
        return

    app["versions"].insert(0, {
        "version": version,
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "downloadURL": url,
        "size": size,
        "minOSVersion": info["MinimumOSVersion"],
        "localizedDescription": f"Автоматична збірка {version}.",
    })
    catalog.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n")
    print(f"Published {version}: {url}")


if __name__ == "__main__":
    if os.environ.get("GITHUB_REPOSITORY") != REPO:
        raise SystemExit("Publish only from the Vidlik distribution repository")
    publish(Path(sys.argv[1]), Path(sys.argv[2]))
