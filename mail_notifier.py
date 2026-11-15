# -*- coding: utf-8 -*-
"""
メール見張り番 (Mail Notifier)

決まった間隔でGmailをチェックし、
「対応が必要」「注意が必要」なメールが届いていたら
画面の右下にポップアップで知らせる常駐ソフト。

- Python標準の機能だけで動く(追加インストール不要)
- 設定は同じフォルダの config.ini
- 使い方は README.txt
"""

import configparser
import datetime as dt
import email
import imaplib
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
import webbrowser
from email.header import decode_header, make_header
from email.utils import parseaddr

APP_NAME = "メール見張り番"
BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0] if sys.argv and sys.argv[0] else __file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "mail_notifier.log")

WEEKDAY_NAMES = ["月", "火", "水", "木", "金", "土", "日"]


# ----------------------------------------------------------------------
# 設定の読み込み
# ----------------------------------------------------------------------
def _lines(text):
    """設定の複数行の値を、空行とコメント(#)を除いたリストにする"""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


class Settings:
    def __init__(self, path=CONFIG_PATH):
        if not os.path.exists(path) and os.path.exists(os.path.join(BASE_DIR, "config.example.ini")):
            raise FileNotFoundError("config.example.ini をコピーして config.ini を作り、アドレスとアプリパスワードを入れてください")
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str
        with open(path, encoding="utf-8-sig") as f:
            cp.read_file(f)

        g = cp["Gmail"]
        self.address = g.get("アドレス", "").strip()
        self.app_password = g.get("アプリパスワード", "").replace(" ", "").strip()

        s = cp["チェック"]
        self.interval_min = max(1, s.getint("間隔_分", 60))
        self.start_hour = s.getint("開始時刻", 8)
        self.end_hour = s.getint("終了時刻", 21)
        days = s.get("チェックする曜日", "月火水木金土")
        self.weekdays = {i for i, n in enumerate(WEEKDAY_NAMES) if n in days}
        self.first_run_hours = s.getint("初回にさかのぼる時間", 3)
        self.sound = s.getboolean("音を鳴らす", True)

        self.senders = [x.lower() for x in _lines(cp.get("要対応の差出人", "一覧", fallback=""))]
        self.keywords = _lines(cp.get("要対応のキーワード", "一覧", fallback=""))
        self.caution_keywords = _lines(cp.get("注意のキーワード", "一覧", fallback=""))
        self.exclude_senders = [x.lower() for x in _lines(cp.get("除外する差出人", "一覧", fallback=""))]
        self.exclude_keywords = _lines(cp.get("除外するキーワード", "一覧", fallback=""))

    def validate(self):
        problems = []
        if not self.address or "@" not in self.address:
            problems.append("config.ini の [Gmail] アドレス が入っていません")
        if not self.app_password or "ここに" in self.app_password:
            problems.append("config.ini の [Gmail] アプリパスワード が入っていません")
        return problems


# ----------------------------------------------------------------------
# メールの読み取り
# ----------------------------------------------------------------------
def decode_str(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def get_body_text(msg, limit=3000):
    """本文(テキスト部分)を取り出す。HTMLしかない場合はタグを外す"""
    texts, htmls = [], []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = part.get_content_type()
        if part.get_content_maintype() == "multipart":
            continue
        if part.get("Content-Disposition", "").lower().startswith("attachment"):
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/plain":
            texts.append(text)
        elif ctype == "text/html":
            htmls.append(text)
    if texts:
        body = "\n".join(texts)
    elif htmls:
        body = re.sub(r"<[^>]+>", " ", "\n".join(htmls))
        body = re.sub(r"&nbsp;", " ", body)
    else:
        body = ""
    body = re.sub(r"[ \t　]+", " ", body)
    return body[:limit]


class MailItem:
    def __init__(self, uid, from_name, from_addr, subject, date, message_id, body):
        self.uid = uid
        self.from_name = from_name
        self.from_addr = from_addr
        self.subject = subject
        self.date = date
        self.message_id = message_id
        self.body = body
        self.level = None      # "要対応" / "注意"
        self.reason = ""

    def gmail_url(self):
        if self.message_id:
            q = "rfc822msgid:" + self.message_id.strip("<>")
        else:
            q = 'subject:"%s"' % self.subject[:40]
        return "https://mail.google.com/mail/u/0/#search/" + urllib.parse.quote(q)


def parse_message(uid, raw_bytes):
    msg = email.message_from_bytes(raw_bytes)
    name, addr = parseaddr(decode_str(msg.get("From", "")))
    return MailItem(
        uid=uid,
        from_name=name or addr,
        from_addr=addr.lower(),
        subject=decode_str(msg.get("Subject", "(件名なし)")),
        date=msg.get("Date", ""),
        message_id=(msg.get("Message-ID", "") or "").strip(),
        body=get_body_text(msg),
    )


# ----------------------------------------------------------------------
# 判定ルール
# ----------------------------------------------------------------------
def classify(item, st):
    """要対応 / 注意 / 対象外(None) を決める"""
    sender = (item.from_name + " " + item.from_addr).lower()
    subject = item.subject
    text = subject + "\n" + item.body

    for x in st.exclude_senders:
        if x and x in sender:
            return None
    for k in st.exclude_keywords:
        if k and k in subject:
            return None

    sender_hit = next((x for x in st.senders if x and x in sender), None)
    kw_subject = next((k for k in st.keywords if k in subject), None)
    kw_body = next((k for k in st.keywords if k in item.body), None)
    caution = next((k for k in st.caution_keywords if k in text), None)

    if sender_hit and (kw_subject or kw_body):
        item.level = "要対応"
        item.reason = "差出人「%s」+「%s」" % (sender_hit, kw_subject or kw_body)
    elif kw_subject:
        item.level = "要対応"
        item.reason = "件名に「%s」" % kw_subject
    elif caution:
        item.level = "注意"
        item.reason = "「%s」を含む" % caution
    elif sender_hit:
        item.level = "注意"
        item.reason = "差出人「%s」" % sender_hit
    else:
        return None
    return item


# ----------------------------------------------------------------------
# Gmail(IMAP)へのアクセス
# ----------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def find_all_mail_folder(imap):
    """「すべてのメール」フォルダ(\\All)を探す。受信トレイから外れたメールも見るため"""
    typ, data = imap.list()
    if typ == "OK":
        for raw in data:
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            if "\\All" in line:
                m = re.search(r'"([^"]+)"\s*$', line)
                if m:
                    return '"%s"' % m.group(1)
                return line.rsplit(" ", 1)[-1]
    return "INBOX"


def check_mail(st, state, log):
    """新着メールを確認し、知らせるべきメールのリストを返す"""
    imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        imap.login(st.address, st.app_password)
        folder = find_all_mail_folder(imap)
        typ, _ = imap.select(folder, readonly=True)
        if typ != "OK":
            raise RuntimeError("フォルダを開けませんでした: %s" % folder)

        uidvalidity = None
        resp = imap.response("UIDVALIDITY")
        if resp and resp[1] and resp[1][0]:
            uidvalidity = resp[1][0].decode() if isinstance(resp[1][0], bytes) else str(resp[1][0])

        last_uid = state.get("last_uid")
        if state.get("uidvalidity") != uidvalidity:
            last_uid = None

        if last_uid is None:
            # 初回: 指定時間だけさかのぼって確認する
            since = (dt.datetime.now() - dt.timedelta(hours=st.first_run_hours)).strftime("%d-%b-%Y")
            typ, data = imap.uid("search", None, "SINCE", since)
            uids = [int(x) for x in data[0].split()] if typ == "OK" and data and data[0] else []
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=st.first_run_hours)
        else:
            typ, data = imap.uid("search", None, "UID", "%d:*" % (int(last_uid) + 1))
            uids = [int(x) for x in data[0].split()] if typ == "OK" and data and data[0] else []
            uids = [u for u in uids if u > int(last_uid)]
            cutoff = None

        found = []
        for uid in sorted(uids)[-200:]:
            typ, data = imap.uid("fetch", str(uid), "(BODY.PEEK[]<0.60000>)")
            if typ != "OK" or not data:
                continue
            raw = None
            for part in data:
                if isinstance(part, tuple) and len(part) >= 2:
                    raw = part[1]
                    break
            if not raw:
                continue
            item = parse_message(uid, raw)
            if cutoff is not None:
                try:
                    d = email.utils.parsedate_to_datetime(item.date)
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=dt.timezone.utc)
                    if d < cutoff:
                        continue
                except Exception:
                    pass
            if classify(item, st):
                found.append(item)

        max_uid = max(uids) if uids else last_uid
        if max_uid is None:
            # 新着がなかった初回は、今の最新UIDを記録しておく
            typ, data = imap.uid("search", None, "ALL")
            all_uids = [int(x) for x in data[0].split()] if typ == "OK" and data and data[0] else []
            max_uid = max(all_uids) if all_uids else 0

        state["last_uid"] = int(max_uid)
        state["uidvalidity"] = uidvalidity
        state["last_check"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        save_state(state)
        log.info("チェック完了: 新着%d通 / 通知%d通", len(uids), len(found))
        return found
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def in_active_time(st, now=None):
    now = now or dt.datetime.now()
    if now.weekday() not in st.weekdays:
        return False
    return st.start_hour <= now.hour < st.end_hour


# ----------------------------------------------------------------------
# ポップアップ表示 (tkinter)
# ----------------------------------------------------------------------
class PopupApp:
    def __init__(self, st, log, once=False):
        import tkinter as tk
        self.tk = tk
        self.st = st
        self.log = log
        self.once = once
        self.q = queue.Queue()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.windows = []
        self.root.after(500, self._poll_queue)

    def notify(self, items, title=None):
        self.q.put((items, title))

    def _poll_queue(self):
        try:
            while True:
                items, title = self.q.get_nowait()
                self._show(items, title)
        except queue.Empty:
            pass
        if self.once and not self.windows and self.q.empty() and getattr(self, "_done", False):
            self.root.quit()
            return
        self.root.after(500, self._poll_queue)

    def _show(self, items, title=None):
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title(title or APP_NAME)
        win.attributes("-topmost", True)
        win.configure(bg="#1f2a44")

        n_action = sum(1 for i in items if i.level == "要対応")
        n_caution = len(items) - n_action
        head = "📬 要対応 %d件 / 注意 %d件" % (n_action, n_caution)
        tk.Label(win, text=head, font=("Meiryo UI", 12, "bold"), fg="white",
                 bg="#1f2a44", anchor="w").pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(win, text=dt.datetime.now().strftime("%m/%d %H:%M のチェック"),
                 font=("Meiryo UI", 9), fg="#c8d0e0", bg="#1f2a44", anchor="w").pack(fill="x", padx=12)

        frame = tk.Frame(win, bg="#1f2a44")
        frame.pack(fill="both", expand=True, padx=8, pady=6)

        shown = sorted(items, key=lambda i: 0 if i.level == "要対応" else 1)[:10]
        for it in shown:
            color = "#ffb4a8" if it.level == "要対応" else "#ffe08a"
            row = tk.Frame(frame, bg="#2c3a5c")
            row.pack(fill="x", pady=3)
            tk.Label(row, text="【%s】" % it.level, font=("Meiryo UI", 9, "bold"),
                     fg=color, bg="#2c3a5c").grid(row=0, column=0, sticky="w", padx=6, pady=(4, 0))
            tk.Label(row, text=it.from_name[:30], font=("Meiryo UI", 9),
                     fg="#c8d0e0", bg="#2c3a5c").grid(row=0, column=1, sticky="w", pady=(4, 0))
            tk.Label(row, text=it.subject[:60], font=("Meiryo UI", 10), fg="white",
                     bg="#2c3a5c", anchor="w", justify="left", wraplength=380).grid(
                row=1, column=0, columnspan=2, sticky="w", padx=6)
            tk.Label(row, text="理由: " + it.reason, font=("Meiryo UI", 8), fg="#9fb0d0",
                     bg="#2c3a5c").grid(row=2, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 4))
            tk.Button(row, text="開く", font=("Meiryo UI", 9),
                      command=lambda u=it.gmail_url(): webbrowser.open(u)).grid(
                row=0, column=2, rowspan=3, padx=6)
            row.grid_columnconfigure(1, weight=1)

        if len(items) > len(shown):
            tk.Label(win, text="ほか %d件" % (len(items) - len(shown)), font=("Meiryo UI", 9),
                     fg="#c8d0e0", bg="#1f2a44").pack(anchor="w", padx=12)

        btns = tk.Frame(win, bg="#1f2a44")
        btns.pack(fill="x", padx=8, pady=(0, 10))
        tk.Button(btns, text="Gmailを開く", font=("Meiryo UI", 9),
                  command=lambda: webbrowser.open("https://mail.google.com/mail/u/0/#all")).pack(side="left")
        tk.Button(btns, text="閉じる", font=("Meiryo UI", 9),
                  command=lambda: self._close(win)).pack(side="right")

        # 画面右下に出す
        win.update_idletasks()
        w, h = max(win.winfo_reqwidth(), 460), win.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry("%dx%d+%d+%d" % (w, h, sw - w - 20, sh - h - 60))
        win.protocol("WM_DELETE_WINDOW", lambda: self._close(win))
        self.windows.append(win)

        if self.st.sound:
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                self.root.bell()

    def _close(self, win):
        try:
            self.windows.remove(win)
        except ValueError:
            pass
        win.destroy()

    def run(self):
        self.root.mainloop()


# ----------------------------------------------------------------------
# 見張りの本体
# ----------------------------------------------------------------------
def watcher_loop(st, app, log, once=False):
    state = load_state()
    while True:
        now = dt.datetime.now()
        if once or in_active_time(st, now):
            try:
                found = check_mail(st, state, log)
                if found:
                    app.notify(found)
            except imaplib.IMAP4.error as e:
                log.error("ログインエラー: %s", e)
                err = MailItem(0, APP_NAME, "", "Gmailにログインできません。config.ini のアドレスとアプリパスワードを確認してください。", "", "", "")
                err.level, err.reason = "注意", str(e)[:80]
                app.notify([err], title=APP_NAME + "(エラー)")
            except Exception as e:
                log.exception("チェック中のエラー: %s", e)
        else:
            log.info("お休み時間のためスキップ")
        if once:
            app._done = True
            return
        time.sleep(st.interval_min * 60)


def setup_log():
    log = logging.getLogger("mail_notifier")
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)
    return log


def main():
    log = setup_log()
    args = sys.argv[1:]

    try:
        st = Settings()
    except Exception as e:
        log.exception("設定の読み込みに失敗")
        print("config.ini を読み込めませんでした:", e)
        return

    if "--test" in args:
        app = PopupApp(st, log)
        a = MailItem(0, "見本の仕事サイト", "", "【見本】サンプル商事さんからメッセージが届いています", "", "", "")
        a.level, a.reason = "要対応", "差出人「見本の仕事サイト」+「メッセージが届いています」"
        b = MailItem(0, "見本銀行", "", "【見本】お取引エラーのお知らせ", "", "", "")
        b.level, b.reason = "注意", "「エラー」を含む"
        app.notify([a, b], title=APP_NAME + "(テスト表示)")
        app.run()
        return

    problems = st.validate()
    if problems:
        log.error(" / ".join(problems))
        try:
            app = PopupApp(st, log)
            e = MailItem(0, APP_NAME, "", "設定が足りません: " + " / ".join(problems), "", "", "")
            e.level, e.reason = "注意", "config.ini を開いて入力してください"
            app.notify([e], title=APP_NAME + "(設定エラー)")
            app.run()
        except Exception:
            print("\n".join(problems))
        return

    once = "--once" in args
    app = PopupApp(st, log, once=once)
    th = threading.Thread(target=watcher_loop, args=(st, app, log, once), daemon=True)
    th.start()
    log.info("%s 起動 (間隔%d分, %d時〜%d時)", APP_NAME, st.interval_min, st.start_hour, st.end_hour)
    app.run()


if __name__ == "__main__":
    main()
