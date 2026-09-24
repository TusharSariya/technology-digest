"""Produce a deterministic content-addressed Lambda artifact."""

import base64
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def main():
    target = Path("build/release.zip")
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        for root, prefix in [
            (Path("build/package"), ""),
            (Path("digest"), "digest/"),
            (Path("config"), "config/"),
        ]:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                info = ZipInfo(prefix + path.relative_to(root).as_posix())
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, path.read_bytes())
    digest = hashlib.sha256(target.read_bytes()).digest()
    manifest = {
        "key": "stable/technology-digest/" + digest.hex() + ".zip",
        "sha256": base64.b64encode(digest).decode(),
        "file": str(target),
    }
    Path("build/artifact.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
