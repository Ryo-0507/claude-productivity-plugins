---
name: github-access
description: GitHub 上のリポジトリ、ファイル、コミット、ブランチ、タグ、リリース、イシュー、プルリクエストを参照するときに使う。GitHub の URL やリポジトリ名が出てきたとき、GitHub 上のコードについて質問されたときは必ずこのスキルに従う。
---

# GitHub の参照

このプラグインは、GitHub 公式の MCP サーバー（github-mcp-server）を読み取り専用モードで提供します。
GitHub の情報は、必ずこのプラグインの `github` MCP サーバーのツールを使って取得してください。

## 守ること

- GitHub へのアクセスに `git push`、`gh` コマンド、GitHub API への直接リクエスト（`curl` など）を使わない。
- GitHub への書き込み（push、ファイルの作成・更新・削除、ブランチやリポジトリの作成、フォーク、イシューの起票、コメントなど）を頼まれた場合は、このプラグインが読み取り専用であることを伝え、実行しない。
- 利用者にターミナルでの操作を求めない。

## ログイン

初めてツールを使うときに、github-mcp-server がブラウザを開いて GitHub へのログインを求めます。
ツールの呼び出しが認証待ちやエラーになった場合は、ブラウザに表示された画面で GitHub にログインし、アクセスを許可するよう利用者に案内してください。
ログイン状態は Claude Code を再起動すると消えるため、再起動後は再度ログインが必要です。

## ツールの使い分け

| やりたいこと                     | 使うツール                                             |
| -------------------------------- | ------------------------------------------------------ |
| ファイルやディレクトリの中身を見る | `get_file_contents`                                    |
| コードを検索する                 | `search_code`                                          |
| リポジトリを探す                 | `search_repositories`                                  |
| コミットを見る                   | `list_commits`、`get_commit`、`search_commits`         |
| ブランチ・タグを見る             | `list_branches`、`list_tags`、`get_tag`                |
| リリースを見る                   | `list_releases`、`get_latest_release`、`get_release_by_tag` |
| イシューを見る                   | `list_issues`、`issue_read`、`search_issues`           |
| プルリクエストを見る             | `list_pull_requests`、`pull_request_read`、`search_pull_requests` |

リポジトリ名が分からないときは、`search_repositories` で候補を探してから利用者に確認してください。
