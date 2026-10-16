"""今日学校行く？ — isolated multi-group MVP; never opens the old attendance.db."""

import hashlib
import json
import tempfile
import click
from contextlib import closing
import os
import re
import secrets
import smtplib
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

JST = timezone(timedelta(hours=9))
BASE = Path(__file__).resolve().parent


def now():
    return datetime.now(JST)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def create_app(config=None):
    app = Flask(__name__, instance_path=str(BASE / "instance"))
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        DATABASE=str(BASE / "instance" / "school.sqlite3"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        MAX_CONTENT_LENGTH=16384,
        PUBLIC_URL=os.environ.get("PUBLIC_URL", "http://127.0.0.1:5000"),
        MAIL_MODE=os.environ.get("MAIL_MODE", "disabled"),
    )
    if config:
        app.config.update(config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    if not app.config["SECRET_KEY"]:
        keyfile = Path(app.instance_path) / ".secret"
        try:
            with keyfile.open("x", encoding="ascii") as f:
                f.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        app.config["SECRET_KEY"] = keyfile.read_text(encoding="ascii")

    if len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("SECRET_KEY は32文字以上にしてください。")

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"], timeout=15)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys=ON")
        return g.db

    @app.teardown_appcontext
    def close_db(error=None):
        conn = g.pop("db", None)
        if conn:
            conn.close()

    with app.app_context():
        existing = db().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r[0] for r in existing}

        if names and "school_schema" not in names:
            raise RuntimeError(
                "既存DBは自動変更しません。新しい保存先を指定してください。"
            )

        if (
            "school_schema" in names
            and db().execute(
                "SELECT version FROM school_schema"
            ).fetchone()[0] != 1
        ):
            raise RuntimeError("未対応のDBバージョンです。")

        db().executescript("""
        CREATE TABLE IF NOT EXISTS school_schema(version INTEGER NOT NULL);
        INSERT INTO school_schema SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM school_schema);

        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY, login_id TEXT UNIQUE NOT NULL, email TEXT UNIQUE NOT NULL,
          password TEXT NOT NULL, auth_version INTEGER NOT NULL DEFAULT 1);

        CREATE TABLE IF NOT EXISTS groups(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, deadline TEXT NOT NULL DEFAULT '08:30',
          weekdays TEXT NOT NULL DEFAULT '0,1,2,3,4', holidays TEXT NOT NULL DEFAULT '',
          invite TEXT UNIQUE NOT NULL, invite_until TEXT NOT NULL);

        CREATE TABLE IF NOT EXISTS members(
          group_id INTEGER REFERENCES groups(id), user_id INTEGER REFERENCES users(id),
          nickname TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','member')),
          PRIMARY KEY(group_id,user_id));

        CREATE TABLE IF NOT EXISTS answers(
          group_id INTEGER, user_id INTEGER, day TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('出席','欠席')), comment TEXT NOT NULL,
          PRIMARY KEY(group_id,user_id,day),
          FOREIGN KEY(group_id,user_id) REFERENCES members(group_id,user_id) ON DELETE CASCADE);

        CREATE TABLE IF NOT EXISTS notices(
          id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), message TEXT NOT NULL,
          created TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0);

        CREATE TABLE IF NOT EXISTS resets(
          token TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id), expires TEXT NOT NULL);

        CREATE TABLE IF NOT EXISTS attempts(
          key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires TEXT NOT NULL);
        """)

    def json_path():
        # JSONはstaticフォルダーの外に保存する。
        # SQLiteを正本として扱う。
        return Path(
            app.config.get("JSON_EXPORT")
            or Path(app.config["DATABASE"]).with_name("attendance.json")
        )

    def export_json():
        target = json_path()
        if target.resolve() == Path(app.config["DATABASE"]).resolve():
            raise ValueError("JSON_EXPORT must not point to the SQLite database")

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None

        try:
            # 元のDB更新がコミットされた後に実行する。
            with closing(
                sqlite3.connect(app.config["DATABASE"], timeout=15)
            ) as conn, conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")

                data = {
                    "schema_version": 1,
                    "exported_at": now().isoformat(),
                    "groups": [
                        dict(r)
                        for r in conn.execute(
                            "SELECT id,name,deadline,weekdays,holidays "
                            "FROM groups ORDER BY id"
                        )
                    ],
                    "members": [
                        dict(r)
                        for r in conn.execute(
                            "SELECT group_id,user_id,nickname,role "
                            "FROM members ORDER BY group_id,user_id"
                        )
                    ],
                    "answers": [
                        dict(r)
                        for r in conn.execute(
                            "SELECT group_id,user_id,day,status,comment "
                            "FROM answers ORDER BY group_id,user_id,day"
                        )
                    ],
                }

                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=target.parent,
                    prefix=".attendance-",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    json.dump(data, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())

                os.replace(temporary, target)
                temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

        return target

    def sync_json():
        try:
            export_json()
        except (OSError, sqlite3.Error, ValueError):
            app.logger.exception(
                "JSONへの保存に失敗しました。SQLiteのデータは保存済みです。"
            )
            flash(
                "SQLiteには保存しましたが、JSON保存に失敗しました。管理者に連絡してください。"
            )

    @app.cli.command("export-json")
    def export_json_command():
        """SQLiteからJSONを保存（失敗後の再実行にも使用）。"""
        try:
            click.echo(str(export_json()))
        except (OSError, sqlite3.Error, ValueError) as error:
            raise click.ClickException(str(error)) from error

    @app.cli.command("read-json")
    def read_json_command():
        """保存済みJSONを読み出して表示。SQLiteへは書き戻さない。"""
        try:
            with json_path().open(encoding="utf-8") as handle:
                data = json.load(handle)
            click.echo(json.dumps(data, ensure_ascii=False, indent=2))
        except (OSError, ValueError) as error:
            raise click.ClickException(str(error)) from error

    def csrf():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    app.jinja_env.globals.update(csrf=csrf)

    @app.before_request
    def prepare():
        g.user = db().execute(
            "SELECT * FROM users WHERE id=?",
            (session.get("uid"),),
        ).fetchone()

        if g.user and session.get("version") != g.user["auth_version"]:
            session.clear()
            g.user = None

        if request.method == "POST":
            token = request.form.get("csrf", "")
            if not token or not secrets.compare_digest(
                token, session.get("csrf", "")
            ):
                abort(400, "画面を再読み込みして送信してください。")

        public = {
            "index",
            "login",
            "register",
            "forgot",
            "reset",
            "static",
            "favicon",
        }
        if not g.user and request.endpoint not in public:
            return redirect(url_for("index"))

    @app.after_request
    def headers(response):
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'self'; style-src 'self'; "
                "form-action 'self'; frame-ancestors 'none'"
            ),
        })
        return response

    def go(message, endpoint="index", **kwargs):
        flash(message)
        return redirect(url_for(endpoint, **kwargs))

    def member(gid, admin=False):
        row = db().execute(
            """SELECT groups.*,members.nickname,members.role FROM groups
               JOIN members ON members.group_id=groups.id
               WHERE groups.id=? AND members.user_id=?""",
            (gid, g.user["id"]),
        ).fetchone()

        if not row or (admin and row["role"] != "admin"):
            abort(403, "このグループを操作する権限がありません。")

        return row

    def my_answer(gid):
        return db().execute(
            "SELECT * FROM answers "
            "WHERE group_id=? AND user_id=? AND day=?",
            (gid, g.user["id"], now().date().isoformat()),
        ).fetchone()

    def schedule(group):
        moment = now()
        active = (
            str(moment.weekday()) in group["weekdays"].split(",")
            and moment.date().isoformat() not in group["holidays"].split()
        )
        return (
            "休止日"
            if not active
            else (
                "受付中"
                if moment.strftime("%H:%M") < group["deadline"]
                else "締切済み"
            )
        )

    def notify(gid, message):
        db().execute(
            """INSERT INTO notices(user_id,message,created)
               SELECT user_id,?,? FROM members WHERE group_id=?""",
            (message, now().isoformat(), gid),
        )

    def limited(scope, limit):
        key = digest(scope + ":" + (request.remote_addr or "local"))

        with db():
            db().execute("BEGIN IMMEDIATE")
            db().execute(
                "DELETE FROM attempts WHERE expires<=?",
                (now().isoformat(),),
            )
            db().execute(
                "INSERT INTO attempts VALUES (?,1,?) "
                "ON CONFLICT(key) DO UPDATE SET count=count+1",
                (key, (now() + timedelta(minutes=15)).isoformat()),
            )
            count = db().execute(
                "SELECT count FROM attempts WHERE key=?",
                (key,),
            ).fetchone()[0]

        if count > limit:
            abort(429, "試行回数が多いため、15分ほど待ってからお試しください。")

    def signin(user):
        session.clear()
        session.update(uid=user["id"], version=user["auth_version"])
        session.permanent = True

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

    @app.get("/")
    def index():
        cards = []
        if g.user:
            for row in db().execute(
                "SELECT groups.*,members.nickname "
                "FROM groups JOIN members ON groups.id=members.group_id "
                "WHERE members.user_id=? ORDER BY groups.id",
                (g.user["id"],),
            ):
                cards.append(
                    dict(
                        row,
                        answer=my_answer(row["id"]),
                        state=schedule(row),
                    )
                )
        return render_template(
            "index.html",
            page="home",
            cards=cards,
            today=now().strftime("%m月%d日"),
        )

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if g.user:
            return redirect(url_for("index"))

        if request.method == "GET":
            return render_template("index.html", page="register", values={})

        limited("register", 10)
        login_id = "user_" + secrets.token_hex(16)
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        def invalid(message):
            return render_template(
                "index.html",
                page="register",
                registration_error=message,
                values={"email": email},
            ), 400

        if (
            not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
            or len(email) > 254
        ):
            return invalid(
                "まだ登録されていません。メールアドレスを確認してください（例：name@example.com）。"
            )

        if (
            not 12 <= len(password) <= 128
            or password != request.form.get("confirm")
        ):
            return invalid(
                "まだ登録されていません。パスワードは12〜128文字で、確認欄にも同じものを入力してください。"
            )

        try:
            with db():
                cur = db().execute(
                    "INSERT INTO users(login_id,email,password) VALUES (?,?,?)",
                    (login_id, email, generate_password_hash(password)),
                )
            signin(
                db().execute(
                    "SELECT * FROM users WHERE id=?",
                    (cur.lastrowid,),
                ).fetchone()
            )
        except sqlite3.IntegrityError:
            return invalid(
                "このメールアドレスは登録済みです。ログインするか、パスワードを再設定してください。"
            )

        return go("登録しました。グループを作るか、招待コードで参加しましょう。")

    dummy = generate_password_hash(secrets.token_urlsafe(32))

    @app.post("/login")
    def login():
        limited("login", 30)
        user = db().execute(
            "SELECT * FROM users WHERE email=?",
            (request.form.get("email", "").strip().lower(),),
        ).fetchone()
        valid = check_password_hash(
            user["password"] if user else dummy,
            request.form.get("password", "")[:129],
        )
        if not user or not valid:
            return go("メールアドレスまたはパスワードが違います。")

        signin(user)
        return redirect(url_for("index"))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.route("/forgot", methods=["GET", "POST"])
    def forgot():
        if request.method == "POST":
            limited("reset", 5)
            user = db().execute(
                "SELECT * FROM users WHERE email=?",
                (request.form.get("email", "").strip().lower(),),
            ).fetchone()

            if user:
                token = secrets.token_urlsafe(32)
                with db():
                    db().execute(
                        "DELETE FROM resets WHERE user_id=?",
                        (user["id"],),
                    )
                    db().execute(
                        "INSERT INTO resets VALUES (?,?,?)",
                        (
                            digest(token),
                            user["id"],
                            (now() + timedelta(minutes=30)).isoformat(),
                        ),
                    )

                link = (
                    app.config["PUBLIC_URL"].rstrip("/")
                    + url_for("reset", token=token)
                )
                msg = EmailMessage()
                msg["Subject"] = "今日学校行く？ パスワードの再設定"
                msg["From"] = os.environ.get("MAIL_FROM", "noreply@localhost")
                msg["To"] = user["email"]
                msg.set_content(
                    "30分以内に次のURLからパスワードを再設定してください。\n"
                    + link
                )

                try:
                    if app.config["MAIL_MODE"] == "file":
                        outbox = Path(app.instance_path) / "outbox"
                        outbox.mkdir(exist_ok=True)
                        (outbox / (secrets.token_hex(12) + ".eml")).write_bytes(
                            bytes(msg)
                        )
                    elif app.config["MAIL_MODE"] == "smtp":
                        with smtplib.SMTP(
                            os.environ["SMTP_HOST"],
                            int(os.environ.get("SMTP_PORT", "587")),
                            timeout=10,
                        ) as smtp:
                            smtp.starttls()
                            if os.environ.get("SMTP_USER"):
                                smtp.login(
                                    os.environ["SMTP_USER"],
                                    os.environ["SMTP_PASSWORD"],
                                )
                            smtp.send_message(msg)
                    else:
                        app.logger.warning(
                            "メール送信は未設定です。MAIL_MODEを設定してください。"
                        )
                except (OSError, smtplib.SMTPException, KeyError):
                    app.logger.error(
                        "再設定メールを送信できませんでした。SMTP設定を確認してください。"
                    )

            return go(
                "登録済みの場合は再設定メールを送信します。届かない場合は管理者にご連絡ください。",
                "forgot",
            )

        return render_template("index.html", page="forgot")

    @app.route("/reset/<token>", methods=["GET", "POST"])
    def reset(token):
        if request.method == "POST":
            db().execute("BEGIN IMMEDIATE")

        row = db().execute(
            "SELECT * FROM resets WHERE token=? AND expires>?",
            (digest(token), now().isoformat()),
        ).fetchone()

        if not row:
            abort(
                400,
                "このリンクは期限切れ、または使用済みです。再設定をやり直してください。",
            )

        if request.method == "POST":
            password = request.form.get("password", "")
            if (
                not 12 <= len(password) <= 128
                or password != request.form.get("confirm")
            ):
                db().rollback()
                return go(
                    "12〜128文字の同じパスワードを入力してください。",
                    "reset",
                    token=token,
                )

            with db():
                db().execute(
                    "UPDATE users SET password=?,auth_version=auth_version+1 "
                    "WHERE id=?",
                    (generate_password_hash(password), row["user_id"]),
                )
                db().execute(
                    "DELETE FROM resets WHERE user_id=?",
                    (row["user_id"],),
                )

            session.clear()
            return go("パスワードを変更しました。ログインしてください。")

        return render_template("index.html", page="reset")

    @app.route("/groups", methods=["GET", "POST"])
    def groups():
        if request.method == "POST":
            nickname = request.form.get("nickname", "").strip()
            if not 1 <= len(nickname) <= 40:
                return go("ニックネームを1〜40文字で入力してください。", "groups")

            with db():
                db().execute("BEGIN IMMEDIATE")

                if request.form.get("action") == "create":
                    name = request.form.get("name", "").strip()
                    if not 1 <= len(name) <= 60:
                        return go(
                            "グループ名を1〜60文字で入力してください。",
                            "groups",
                        )

                    cur = db().execute(
                        "INSERT INTO groups(name,invite,invite_until) "
                        "VALUES (?,?,?)",
                        (
                            name,
                            secrets.token_urlsafe(18),
                            (now() + timedelta(days=7)).isoformat(),
                        ),
                    )
                    gid, role = cur.lastrowid, "admin"
                else:
                    limited_code = request.form.get("invite", "").strip()
                    row = db().execute(
                        "SELECT * FROM groups WHERE invite=? AND invite_until>?",
                        (limited_code, now().isoformat()),
                    ).fetchone()

                    if not row:
                        return go(
                            "招待コードが無効、または期限切れです。",
                            "groups",
                        )

                    gid, role = row["id"], "member"

                cur = db().execute(
                    "INSERT INTO members VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                    (gid, g.user["id"], nickname, role),
                )
                if cur.rowcount:
                    notify(gid, "グループに新しいメンバーが参加しました。")

            sync_json()
            return go("グループを開きました。", "group", gid=gid)

        return render_template("index.html", page="groups")

    @app.get("/groups/<int:gid>")
    def group(gid):
        row = member(gid)
        answer = my_answer(gid)
        group_members = db().execute(
            """SELECT nickname, role FROM members
               WHERE group_id=?
               ORDER BY CASE WHEN role='admin' THEN 0 ELSE 1 END, user_id""",
            (gid,),
        ).fetchall()
        people = []

        if answer:
            people = db().execute(
                """SELECT m.nickname,COALESCE(a.status,'未回答') status,a.comment
                   FROM members m
                   LEFT JOIN answers a
                     ON a.group_id=m.group_id
                    AND a.user_id=m.user_id
                    AND a.day=?
                   WHERE m.group_id=?
                   ORDER BY m.user_id""",
                (now().date().isoformat(), gid),
            ).fetchall()

        return render_template(
            "index.html",
            page="group",
            group=row,
            answer=answer,
            people=people,
            group_members=group_members,
            state=schedule(row),
        )

    @app.post("/groups/<int:gid>/answer")
    def answer(gid):
        with db():
            db().execute("BEGIN IMMEDIATE")
            row = member(gid)
            comment = request.form.get("comment", "").strip()

            if len(comment) > 500:
                abort(400, "コメントは500文字以内です。")

            old = my_answer(gid)
            if old:
                db().execute(
                    "UPDATE answers SET comment=? "
                    "WHERE group_id=? AND user_id=? AND day=?",
                    (comment, gid, g.user["id"], now().date().isoformat()),
                )
            else:
                if schedule(row) != "受付中":
                    abort(403, "本日は回答受付時間外です。")

                status = request.form.get("status")
                if status not in ("出席", "欠席"):
                    abort(400, "出席か欠席を選んでください。")

                db().execute(
                    "INSERT INTO answers VALUES (?,?,?,?,?)",
                    (
                        gid,
                        g.user["id"],
                        now().date().isoformat(),
                        status,
                        comment,
                    ),
                )

        sync_json()
        return go(
            "回答を保存しました。" if not old else "コメントを更新しました。",
            "group",
            gid=gid,
        )

    @app.post("/groups/<int:gid>/delete")
    def delete_answer(gid):
        with db():
            db().execute("BEGIN IMMEDIATE")
            member(gid)

            db().execute(
                "DELETE FROM answers WHERE group_id=? AND user_id=? AND day=?",
                (gid, g.user["id"], now().date().isoformat()),
            )

        sync_json()
        return go("今日の出欠データを削除しました。", "group", gid=gid)

    @app.post("/groups/<int:gid>/nickname")
    def nickname(gid):
        with db():
            db().execute("BEGIN IMMEDIATE")
            member(gid)
            name = request.form.get("nickname", "").strip()

            if my_answer(gid) or not 1 <= len(name) <= 40:
                abort(400, "回答前に1〜40文字で変更してください。")

            db().execute(
                "UPDATE members SET nickname=? WHERE group_id=? AND user_id=?",
                (name, gid, g.user["id"]),
            )

        sync_json()
        return go("ニックネームを変更しました。", "group", gid=gid)

    @app.route("/groups/<int:gid>/admin", methods=["GET", "POST"])
    def admin(gid):
        row = member(gid, admin=True)

        if request.method == "POST":
            with db():
                db().execute("BEGIN IMMEDIATE")
                row = member(gid, admin=True)
                action = request.form.get("action")

                if action == "invite":
                    db().execute(
                        "UPDATE groups SET invite=?,invite_until=? WHERE id=?",
                        (
                            secrets.token_urlsafe(18),
                            (now() + timedelta(days=7)).isoformat(),
                            gid,
                        ),
                    )
                elif action == "remove":
                    uid = request.form.get("uid", type=int)
                    db().execute(
                        "DELETE FROM members "
                        "WHERE group_id=? AND user_id=? AND role='member'",
                        (gid, uid),
                    )
                elif action == "settings":
                    deadline = request.form.get("deadline", "")
                    days = sorted(set(request.form.getlist("weekdays")))
                    holidays = request.form.get("holidays", "").split()

                    try:
                        if (
                            not re.fullmatch(
                                r"(?:[01]\d|2[0-3]):[0-5]\d",
                                deadline,
                            )
                            or any(d not in list("0123456") for d in days)
                        ):
                            raise ValueError()

                        for day in holidays:
                            if (
                                datetime.strptime(day, "%Y-%m-%d").strftime(
                                    "%Y-%m-%d"
                                )
                                != day
                            ):
                                raise ValueError()
                    except ValueError:
                        abort(400, "時刻・曜日・休止日の形式を確認してください。")

                    db().execute(
                        "UPDATE groups SET deadline=?,weekdays=?,holidays=? "
                        "WHERE id=?",
                        (
                            deadline,
                            ",".join(days),
                            "\n".join(sorted(set(holidays))),
                            gid,
                        ),
                    )
                    notify(
                        gid,
                        f"「{row['name']}」の設定が更新されました。"
                        f"締切 {row['deadline']} → {deadline}。"
                        "実施曜日・休止日はグループ画面で確認できます。",
                    )
                else:
                    abort(400)

            sync_json()
            return go("更新しました。", "admin", gid=gid)

        people = db().execute(
            "SELECT user_id,nickname,role FROM members WHERE group_id=?",
            (gid,),
        ).fetchall()

        return render_template(
            "index.html",
            page="admin",
            group=row,
            people=people,
        )

    @app.route("/notifications", methods=["GET", "POST"])
    def notifications():
        if request.method == "POST":
            with db():
                db().execute(
                    "UPDATE notices SET seen=1 WHERE user_id=?",
                    (g.user["id"],),
                )
            return redirect(url_for("notifications"))

        rows = db().execute(
            "SELECT * FROM notices WHERE user_id=? ORDER BY id DESC LIMIT 100",
            (g.user["id"],),
        ).fetchall()

        return render_template(
            "index.html",
            page="notifications",
            notices=rows,
        )

    @app.get("/account")
    def account():
        return render_template("index.html", page="account")

    @app.context_processor
    def shared():
        unread = (
            db().execute(
                "SELECT COUNT(*) FROM notices WHERE user_id=? AND seen=0",
                (g.user["id"],),
            ).fetchone()[0]
            if g.get("user")
            else 0
        )
        return dict(
            unread=unread,
            weekdays=["月", "火", "水", "木", "金", "土", "日"],
        )

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(429)
    def error(err):
        return render_template(
            "index.html",
            page="error",
            error=err,
        ), err.code

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", debug=False)