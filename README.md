# 今日学校行く？

Flaskで作成したグループごとの出欠共有アプリです。

## 起動

```sh
python -m venv .venv
```

Windows PowerShellで仮想環境のPythonを使い、必要なライブラリをインストールして起動します。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

ブラウザーで http://127.0.0.1:5000 を開きます。

## 主な機能

- アカウント登録・ログイン
- グループの作成・招待コードによる参加
- 今日の出欠回答・コメント編集
- 今日の自分の回答の削除（確認ダイアログ・キャンセル付き）
- SQLite保存とJSONへの同期

## データ

実行時に `instance/` 内へSQLite、JSON、秘密鍵が作成されます。
利用者のデータと秘密鍵はこのリポジトリに含めません。
新しくダウンロードした場所では、新しいデータベースで起動します。
既存の利用者データは元のアプリフォルダーに保存されています。
