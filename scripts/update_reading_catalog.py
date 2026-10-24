"""Build inert RSSHub channel metadata from an official routes.json snapshot.

Usage: python scripts/update_reading_catalog.py INPUT --revision GH_PAGES_SHA
Input: DIYgod/RSSHub gh-pages:build/routes.json (official build-assets output).
This script never imports or executes upstream route handlers.
"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def convert(source, revision):
    namespaces = json.loads(source)
    channels = []
    for namespace, info in sorted(namespaces.items()):
        for route_path, route in sorted(info.get("routes", {}).items()):
            full_path = "/" + namespace + route_path
            channels.append(
                {
                    "id": hashlib.sha256(full_path.encode()).hexdigest()[:24],
                    "namespace": namespace,
                    "site": info.get("name", namespace),
                    "name": route.get("name", full_path),
                    "path": full_path,
                    "example": route.get("example", ""),
                    "description": route.get("description", ""),
                    "parameters": route.get("parameters", {}),
                    "categories": route.get(
                        "categories", info.get("categories", ["other"])
                    ),
                    "features": route.get("features", {}),
                    "docs": "https://docs.rsshub.app/routes/" + namespace,
                }
            )
    return {
        "schema_version": 1,
        "source": "https://github.com/DIYgod/RSSHub",
        "source_revision": revision,
        "source_file": "gh-pages:build/routes.json",
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "namespace_count": len(namespaces),
        "route_count": len(channels),
        "channels": channels,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    result = convert(args.input.read_bytes(), args.revision)
    target = (
        Path(__file__).resolve().parents[1]
        / "src/lantai/app/reading/data/rsshub-catalog.json"
    )
    target.write_text(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    print(
        f"Saved {result['route_count']} routes from {result['namespace_count']} namespaces"
    )
