#!/usr/bin/env python3
"""github-readonly プラグインの OS 別 zip をビルドし、リリースを管理するスクリプト。

サブコマンド:
  build               OS 別の zip を作成し、dist/ に出力する
  verify-readonly     github-mcp-server が読み取り専用ツールだけを提供することを確認する
  update-marketplace  marketplace.json に OS 別プラグインのエントリを書き込む
  marketplace-pr-body marketplace.json を更新する PR の本文を出力する
  verify-marketplace  marketplace.json が指す zip の sha256 と中身を確認する

Python の標準ライブラリだけで動作する。
"""

import argparse
import base64
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SOURCE_DIR.parent.parent
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"
UPSTREAM_REPO = "github/github-mcp-server"
MCP_SERVER_NAME = "github"

PLATFORMS = {
    "windows": {
        "plugin_name": "github-readonly-windows",
        "description": "【Windows用】GitHub公式MCPサーバーで、GitHubのリポジトリを読み取り専用で参照する",
        "asset": "github-mcp-server_Windows_x86_64.zip",
        "binary": "github-mcp-server.exe",
        "hook_shell": "powershell",
        "licenses_os": "windows",
    },
    "mac-apple-silicon": {
        "plugin_name": "github-readonly-mac-apple-silicon",
        "description": "【Mac・Appleシリコン用（「このMacについて」に「チップ」と表示される方）】GitHub公式MCPサーバーで、GitHubのリポジトリを読み取り専用で参照する",
        "asset": "github-mcp-server_Darwin_arm64.tar.gz",
        "binary": "github-mcp-server",
        "hook_shell": "bash",
        "licenses_os": "darwin",
    },
    "mac-intel": {
        "plugin_name": "github-readonly-mac-intel",
        "description": "【Mac・Intel用（「このMacについて」に「プロセッサ」と表示される方）】GitHub公式MCPサーバーで、GitHubのリポジトリを読み取り専用で参照する",
        "asset": "github-mcp-server_Darwin_x86_64.tar.gz",
        "binary": "github-mcp-server",
        "hook_shell": "bash",
        "licenses_os": "darwin",
    },
}

# verify-readonly で実行する、CI ランナー（Linux x64）用の実行ファイル
VERIFY_ASSET = "github-mcp-server_Linux_x86_64.tar.gz"

# zip 内のタイムスタンプを固定し、同じ入力から同じ zip が作られるようにする
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def upstream_version() -> str:
    for line in (SOURCE_DIR / "versions.env").read_text(encoding="utf-8").splitlines():
        if line.startswith("GITHUB_MCP_SERVER_VERSION="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("versions.env に GITHUB_MCP_SERVER_VERSION がありません")


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as res:
        return res.read()


def fetch_checksums(version: str) -> dict:
    """公式リリースのチェックサムファイルを取得し、{ファイル名: sha256} を返す。"""
    name = f"github-mcp-server_{version.lstrip('v')}_checksums.txt"
    text = download(f"https://github.com/{UPSTREAM_REPO}/releases/download/{version}/{name}").decode()
    checksums = {}
    for line in text.splitlines():
        if line.strip():
            digest, filename = line.split()
            checksums[filename] = digest
    return checksums


def fetch_upstream_files(version: str, asset: str, checksums: dict) -> dict:
    """公式リリースのアーカイブを取得してチェックサムを照合し、{ファイル名: 中身} を返す。"""
    data = download(f"https://github.com/{UPSTREAM_REPO}/releases/download/{version}/{asset}")
    actual = hashlib.sha256(data).hexdigest()
    if checksums.get(asset) != actual:
        raise SystemExit(f"{asset} のチェックサムが公式の値と一致しません（取得値: {actual}）")

    files = {}
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if not info.is_dir():
                    files[Path(info.filename).name] = zf.read(info)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for member in tf.getmembers():
                if member.isfile():
                    files[Path(member.name).name] = tf.extractfile(member).read()
    return files


def fetch_third_party_licenses(version: str) -> dict:
    """github-mcp-server の該当タグから、同梱ライブラリのライセンス一式を取得し、{パス: 中身} を返す。"""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        subprocess.run(["git", "-c", "advice.detachedHead=false", "clone", "--quiet", "--depth", "1", "--branch", version, "--filter=blob:none",
                        "--sparse", f"https://github.com/{UPSTREAM_REPO}.git", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "sparse-checkout", "set", "--no-cone",
                        "/third-party/", "/third-party-licenses.*.md"], check=True)
        files = {}
        for path in sorted(repo.rglob("*")):
            if path.is_file() and ".git" not in path.relative_to(repo).parts:
                files[path.relative_to(repo).as_posix()] = path.read_bytes()
    if not any(name.startswith("third-party/") for name in files):
        raise SystemExit(f"{UPSTREAM_REPO} {version} に third-party ディレクトリがありません")
    return files


def hooks_config(shell: str) -> dict:
    """deny-rules.json から、指定したシェル向けの hooks.json の内容を作る。"""
    rules = json.loads((SOURCE_DIR / "hooks" / "deny-rules.json").read_text(encoding="utf-8"))
    reason = rules["reason"]
    if "'" in reason:
        raise SystemExit("deny-rules.json の reason にシングルクォートは使えません")
    if shell == "powershell":
        # PowerShell のスクリプトやコンソールの文字コードで日本語が化けないよう、
        # メッセージは UTF-8 を Base64 にして埋め込み、バイト列のまま標準エラー出力に書き込む
        encoded = base64.b64encode(reason.encode("utf-8")).decode("ascii")
        command = (f"$b = [Convert]::FromBase64String('{encoded}'); "
                   "[Console]::OpenStandardError().Write($b, 0, $b.Length); exit 2")
    else:
        command = f"echo '{reason}' >&2; exit 2"

    handlers = []
    for tool in ("Bash", "PowerShell"):
        for pattern in rules["commands"]:
            handlers.append({
                "type": "command",
                "if": f"{tool}({pattern})",
                "command": command,
                "shell": shell,
            })
    return {"hooks": {"PreToolUse": [{"matcher": "Bash|PowerShell", "hooks": handlers}]}}


def plugin_files(platform: dict, upstream: dict, third_party: dict, version: str) -> dict:
    """zip に格納する {パス: (中身, 実行可能か)} を返す。"""
    binary = platform["binary"]
    files = {
        ".claude-plugin/plugin.json": json.dumps({
            "name": platform["plugin_name"],
            "version": version,
            "description": platform["description"],
            "author": {"name": "Ryo"},
            "license": "MIT",
        }, ensure_ascii=False, indent=2),
        ".mcp.json": json.dumps({
            "mcpServers": {
                MCP_SERVER_NAME: {
                    "command": f"${{CLAUDE_PLUGIN_ROOT}}/mcp-server/{binary}",
                    "args": ["stdio", "--read-only"],
                },
            },
        }, indent=2),
        "hooks/hooks.json": json.dumps(hooks_config(platform["hook_shell"]), indent=2),
        f"mcp-server/{binary}": (upstream[binary], True),
        "mcp-server/LICENSE": upstream["LICENSE"],
        "mcp-server/third-party-licenses.md": third_party[f"third-party-licenses.{platform['licenses_os']}.md"],
    }
    for name, content in third_party.items():
        if name.startswith("third-party/"):
            files[f"mcp-server/{name}"] = content
    for path in sorted((SOURCE_DIR / "skills").rglob("*")):
        if path.is_file():
            files[path.relative_to(SOURCE_DIR).as_posix()] = path.read_bytes()

    normalized = {}
    for name, content in files.items():
        if not isinstance(content, tuple):
            content = (content, False)
        data, executable = content
        if isinstance(data, str):
            data = (data + "\n").encode("utf-8")
        normalized[name] = (data, executable)
    return normalized


def write_zip(path: Path, files: dict) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            data, executable = files[name]
            info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
            info.create_system = 3  # Unix。実行権限を保持させるため
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)


def cmd_build(args: argparse.Namespace) -> None:
    version = upstream_version()
    checksums = fetch_checksums(version)
    third_party = fetch_third_party_licenses(version)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    builds = []
    for key, platform in PLATFORMS.items():
        upstream = fetch_upstream_files(version, platform["asset"], checksums)
        zip_path = out_dir / f"{platform['plugin_name']}.zip"
        write_zip(zip_path, plugin_files(platform, upstream, third_party, args.version))
        digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        builds.append({"platform": key, "file": zip_path.name, "sha256": digest})
        print(f"{zip_path.name}  sha256={digest}")

    (out_dir / "builds.json").write_text(json.dumps(builds, indent=2) + "\n", encoding="utf-8")


def mcp_tools(binary: Path) -> list:
    """MCP の initialize と tools/list を送り、提供されるツール一覧を返す。"""
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "verify-readonly", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    env = {k: v for k, v in os.environ.items()
           if k not in ("GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")}
    proc = subprocess.Popen([str(binary), "stdio", "--read-only"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True)
    try:
        for req in requests:
            proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()
        for line in proc.stdout:
            msg = json.loads(line)
            if msg.get("id") == 2:
                return msg["result"]["tools"]
    finally:
        proc.kill()
    raise SystemExit("tools/list の応答を受け取れませんでした")


def cmd_verify_readonly(args: argparse.Namespace) -> None:
    version = upstream_version()
    upstream = fetch_upstream_files(version, VERIFY_ASSET, fetch_checksums(version))
    with tempfile.TemporaryDirectory() as tmp:
        binary = Path(tmp) / "github-mcp-server"
        binary.write_bytes(upstream["github-mcp-server"])
        binary.chmod(0o755)
        tools = mcp_tools(binary)

    writable = [t["name"] for t in tools if not t.get("annotations", {}).get("readOnlyHint")]
    print(f"github-mcp-server {version}: {len(tools)} tools")
    if not tools:
        raise SystemExit("ツールが 1 つも提供されていません")
    if writable:
        raise SystemExit(f"読み取り専用ではないツールが含まれています: {', '.join(writable)}")
    print("すべてのツールが読み取り専用です")


def cmd_update_marketplace(args: argparse.Namespace) -> None:
    builds = json.loads((Path(args.dist) / "builds.json").read_text(encoding="utf-8"))
    marketplace = json.loads(MARKETPLACE_JSON.read_text(encoding="utf-8"))
    entries = {p["name"]: p for p in marketplace["plugins"]}

    for build in builds:
        platform = PLATFORMS[build["platform"]]
        entries[platform["plugin_name"]] = {
            "name": platform["plugin_name"],
            "description": platform["description"],
            "source": {
                "source": "archive",
                "url": f"https://github.com/{args.repo}/releases/download/{args.tag}/{build['file']}",
                "sha256": build["sha256"],
            },
        }

    marketplace["plugins"] = list(entries.values())
    MARKETPLACE_JSON.write_text(json.dumps(marketplace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_marketplace_pr_body(args: argparse.Namespace) -> None:
    builds = json.loads((Path(args.dist) / "builds.json").read_text(encoding="utf-8"))
    release_url = f"https://github.com/{args.repo}/releases/tag/{args.tag}"
    lines = [
        f"[{args.tag}]({release_url}) の zip を marketplace.json に登録します。",
        "",
        "このPRをマージすると、自動更新を ON にしている利用者に新しいバージョンが配信されます。",
        "承認する前に、リリースノートで今回のリリースに含まれる変更を確認してください。",
        "",
        "| プラグイン | zip | sha256 |",
        "| --- | --- | --- |",
    ]
    for build in builds:
        name = PLATFORMS[build["platform"]]["plugin_name"]
        url = f"https://github.com/{args.repo}/releases/download/{args.tag}/{build['file']}"
        lines.append(f"| `{name}` | [{build['file']}]({url}) | `{build['sha256']}` |")
    print("\n".join(lines))


def cmd_verify_marketplace(args: argparse.Namespace) -> None:
    marketplace = json.loads(MARKETPLACE_JSON.read_text(encoding="utf-8"))
    names = {p["plugin_name"] for p in PLATFORMS.values()}
    entries = [p for p in marketplace["plugins"] if p["name"] in names]
    for entry in entries:
        source = entry["source"]
        data = download(source["url"])
        actual = hashlib.sha256(data).hexdigest()
        if actual != source["sha256"]:
            raise SystemExit(f"{entry['name']}: sha256 が一致しません（取得値: {actual}）")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            manifest = json.loads(zf.read(".claude-plugin/plugin.json"))
        if manifest["name"] != entry["name"]:
            raise SystemExit(f"{entry['name']}: zip 内のプラグイン名が {manifest['name']} になっています")
        print(f"{entry['name']}: OK")
    print(f"{len(entries)} 件のエントリを確認しました")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="OS 別の zip を作成する")
    build.add_argument("--out", default="dist", help="出力先ディレクトリ")
    build.add_argument("--version", default="0.0.0-dev", help="プラグインのバージョン")
    build.set_defaults(func=cmd_build)

    verify = sub.add_parser("verify-readonly", help="読み取り専用ツールだけが提供されることを確認する")
    verify.set_defaults(func=cmd_verify_readonly)

    update = sub.add_parser("update-marketplace", help="marketplace.json を更新する")
    update.add_argument("--dist", default="dist", help="build の出力先ディレクトリ")
    update.add_argument("--repo", required=True, help="このリポジトリ（owner/repo）")
    update.add_argument("--tag", required=True, help="zip を添付したリリースのタグ")
    update.set_defaults(func=cmd_update_marketplace)

    pr_body = sub.add_parser("marketplace-pr-body", help="marketplace.json を更新する PR の本文を出力する")
    pr_body.add_argument("--dist", default="dist", help="build の出力先ディレクトリ")
    pr_body.add_argument("--repo", required=True, help="このリポジトリ（owner/repo）")
    pr_body.add_argument("--tag", required=True, help="zip を添付したリリースのタグ")
    pr_body.set_defaults(func=cmd_marketplace_pr_body)

    verify_mp = sub.add_parser("verify-marketplace", help="marketplace.json が指す zip を確認する")
    verify_mp.set_defaults(func=cmd_verify_marketplace)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
