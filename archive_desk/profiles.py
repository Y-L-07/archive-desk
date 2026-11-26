"""Versioned rule profiles. Import parses data only; it has no file-move capability."""

import json
import sqlite3
from dataclasses import asdict, fields
from pathlib import Path

from .core import Rule, default_rules, validate_rule
from .journal import database_connection


MAX_BYTES = 1024 * 1024
BASE_FIELDS = {"name", "extensions", "target", "enabled"}
V2_FIELDS = {field.name for field in fields(Rule)}


def _text(value, limit, field):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > limit or not value.isprintable()):
        raise ValueError(f"{field}须为 1 至 {limit} 个可显示字符，且首尾没有空白")
    return value


def _validate(data):
    if not isinstance(data, dict) or data.keys() != {"version", "name", "rules"}:
        raise ValueError("方案应包含 version、name、rules 三个字段")
    if type(data["version"]) is not int or data["version"] not in (1, 2):
        raise ValueError("无法识别此规则方案版本")
    name = _text(data["name"], 80, "方案名称")
    raw = data["rules"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= 64:
        raise ValueError("一个方案需要包含 1 至 64 条规则")
    rules = []
    for i, entry in enumerate(raw, 1):
        allowed = BASE_FIELDS if data["version"] == 1 else V2_FIELDS
        if not isinstance(entry, dict) or not BASE_FIELDS <= entry.keys() <= allowed:
            raise ValueError(f"第 {i} 条规则的字段不完整或无法识别")
        values = dict(entry)
        if not isinstance(values["extensions"], (list, tuple)):
            raise ValueError(f"第 {i} 条规则的扩展名必须是列表")
        values["extensions"] = tuple(values["extensions"])
        rule = Rule(**values)
        try:
            validate_rule(rule)
        except ValueError as exc:
            raise ValueError(f"第 {i} 条规则：{exc}") from exc
        rules.append(rule)
    return name, rules


def _encode(name, rules):
    data = {"version": 2, "name": name, "rules": [asdict(rule) for rule in rules]}
    _validate(data)
    body = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if len(body.encode("utf-8")) > MAX_BYTES:
        raise ValueError("方案超过 1 MiB")
    return body


def _decode(body):
    try:
        return _validate(json.loads(body))
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("规则方案包含无效 JSON 或嵌套层级过深") from exc


def import_profile(path):
    with Path(path).open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("只支持 1 MiB 以内的规则方案")
    try:
        body = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("规则方案需使用 UTF-8 编码") from exc
    return _decode(body)


def export_profile(path, name, rules):
    body = _encode(name, rules)
    with Path(path).open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(body)


class ProfileStore:
    def __init__(self, db_path):
        self.path = Path(db_path)
        with database_connection(self.path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS profiles (
                          name TEXT PRIMARY KEY COLLATE NOCASE, spec TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS active_rules (
                          id INTEGER PRIMARY KEY CHECK(id=1), spec TEXT NOT NULL)""")

    def names(self):
        with database_connection(self.path) as db:
            return [row["name"] for row in db.execute("SELECT name FROM profiles ORDER BY name")]

    def load(self, name):
        with database_connection(self.path) as db:
            row = db.execute("SELECT spec FROM profiles WHERE name=?", (name,)).fetchone()
        if row is None:
            raise ValueError("找不到这个规则方案")
        return _decode(row["spec"])[1]

    def save(self, name, rules, *, update_existing=False):
        body = _encode(name, rules)
        try:
            with database_connection(self.path) as db:
                if update_existing:
                    row = db.execute("UPDATE profiles SET spec=? WHERE name=?", (body, name))
                    if row.rowcount != 1:
                        raise ValueError("要更新的方案已经不存在，请重新选择")
                else:
                    db.execute("INSERT INTO profiles (name,spec) VALUES (?,?)", (name, body))
        except sqlite3.IntegrityError as exc:
            raise ValueError("此方案名称已存在，请使用另一个名称") from exc

    def save_active(self, rules):
        body = _encode("当前规则", rules)
        with database_connection(self.path) as db:
            db.execute("""INSERT INTO active_rules(id,spec) VALUES (1,?)
                          ON CONFLICT(id) DO UPDATE SET spec=excluded.spec""", (body,))

    def load_active(self):
        with database_connection(self.path) as db:
            row = db.execute("SELECT spec FROM active_rules WHERE id=1").fetchone()
        return _decode(row["spec"])[1] if row else default_rules()
