"""Prepare a fresh, offline job-search project from this skill's runtime assets."""
import argparse
import ast
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--hermes-repo", type=Path)
    parser.add_argument("--hermes-cli", type=Path, default=Path.home() / ".local/bin/hermes")
    parser.add_argument("--job-id")
    parser.add_argument("--slack-channel")
    args = parser.parse_args()
    root = args.workspace.expanduser().resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        parser.error("Workspace must be absent or empty; existing files are preserved")
    hermes_home = args.hermes_home.expanduser().resolve()
    config = {
        "app_dir": str(root), "hermes_home": str(hermes_home),
        "hermes_repo": str((args.hermes_repo or hermes_home / "hermes-agent").expanduser().resolve()),
        "hermes_cli": str(args.hermes_cli.expanduser().resolve()),
        "python": sys.executable, "job_id": args.job_id,
        "slack_channel_id": args.slack_channel,
        "schedule": "0 9,18 * * *", "timezone": "Asia/Seoul",
        "status": "prepared_not_scheduled",
    }
    assets = Path(__file__).resolve().parents[1] / "assets/runtime"
    rendered = {}
    for source in sorted(assets.iterdir()):
        if not source.is_file():
            continue
        name = source.name.removesuffix(".tpl")
        text = source.read_text().replace("__APP_DIR_PY__", repr(str(root))).replace("__APP_DIR_TEXT__", str(root))
        if name.endswith(".py"):
            ast.parse(text, filename=name)
        rendered[name] = text
    if "collect_jobs.py" not in rendered:
        raise RuntimeError("Bundled runtime is missing")
    root.mkdir(parents=True, exist_ok=True)
    for name, text in rendered.items():
        (root / name).write_text(text)
    (root / "deployment.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"workspace": str(root), "files": sorted([*rendered, "deployment.json"]),
                      "scheduled": False, "slack_sent": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
