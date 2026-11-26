"""Bridge verified V2 file transfers to the catalog in the journal transaction."""
import json
from pathlib import Path

from .catalog import touch_roots, upsert_file
from .core import FileInfo


def _matches(row, fingerprint, digest):
    if not fingerprint:
        return False
    dev, ino, size, mtime_ns = fingerprint
    return ((row["dev"], row["ino"], row["size"], row["mtime_ns"])
            == (str(dev), str(ino), size, mtime_ns)
            and not (digest and row["digest"] and row["digest"] != digest))


def _bindings(roots, directories, path):
    pinned = {Path(p): (str(dev), str(ino)) for p, dev, ino in directories}
    for root in roots:
        base = Path(root["path"])
        if pinned.get(base) == (root["dev"], root["ino"]) and path.is_relative_to(base):
            yield root["id"], path.relative_to(base).as_posix()


def committed_transfer(db, item_id, transfer):
    roots = db.execute("SELECT id,path,dev,ino FROM catalog_roots").fetchall()
    if not roots:
        return
    dirs = transfer["directories"]
    source, target = Path(transfer["source"]), Path(transfer["target"])
    before = dict(_bindings(roots, dirs, source))
    after = dict(_bindings(roots, dirs, target))
    if not (before or after):
        return
    touch_roots(db, before.keys() | after.keys())
    digest = transfer["sha256"]
    original = {}
    for root_id, relpath in before.items():
        row = db.execute("SELECT * FROM catalog_files WHERE root_id=? AND relpath=? AND active=1",
                         (root_id, relpath)).fetchone()
        if row:
            # A successful move establishes vacancy even if the old index was stale.
            db.execute("UPDATE catalog_files SET active=0 WHERE id=?", (row["id"],))
            if _matches(row, transfer["file"], digest):
                original[root_id] = row["id"]
                db.execute("UPDATE catalog_files SET digest=? WHERE id=?", (digest, row["id"]))
    dev, ino, size, mtime_ns = transfer.get("result", transfer["file"])
    info = FileInfo(target, size, mtime_ns, dev, ino, digest=digest)
    links = set(original.values())
    unmatched = original.keys() - after.keys()
    destinations = after.keys() - original.keys()
    for root_id, relpath in after.items():
        prior = original.get(root_id)
        if root_id in destinations and len(unmatched) == len(destinations) == 1:
            prior = original[next(iter(unmatched))]
        if prior is None:
            # An undo from an unindexed location may return a cross-volume
            # copy with a new file ID to its previously annotated catalog entry.
            row = db.execute("""SELECT f.id FROM catalog_history_links l JOIN catalog_files f
                ON f.id=l.entry_id WHERE l.item_id=? AND f.root_id=? AND f.relpath=?
                AND f.active=0 AND f.digest=? ORDER BY f.id DESC LIMIT 1""",
                (item_id, root_id, relpath, digest)).fetchone()
            prior = row["id"] if row else None
        links.add(upsert_file(db, root_id, relpath, info, digest=digest, prior_id=prior))
    db.executemany("INSERT OR IGNORE INTO catalog_history_links(item_id,entry_id) VALUES(?,?)",
                   ((item_id, entry_id) for entry_id in links))


def _legacy_entries(db, row):
    # Pre-catalog history can be joined after an initial index only with
    # matching recorded directory and file identities at the current location.
    if row["status"] == "moved":
        ev = json.loads(row["evidence"] or "{}")
        location, signature = Path(row["target"]), ev.get("file")
    elif row["status"] == "undone":
        ev = json.loads(row["transfer"] or "{}")
        location, signature = Path(row["source"]), ev.get("result", ev.get("file"))
    else:
        return []
    entries = []
    roots = db.execute("SELECT id,path,dev,ino FROM catalog_roots")
    for root_id, path in _bindings(roots, ev.get("directories", []), location):
        entry = db.execute("SELECT * FROM catalog_files WHERE root_id=? AND relpath=? AND active=1",
                           (root_id, path)).fetchone()
        if entry and _matches(entry, signature, ev.get("sha256")):
            entries.append(entry["id"])
    return entries


def matching_entries(db, item_id):
    ids = [r[0] for r in db.execute("SELECT entry_id FROM catalog_history_links WHERE item_id=?",
                                    (item_id,))]
    row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return sorted(set(ids) | set(_legacy_entries(db, row))) if row else []


def matching_history(db, entry_id):
    rows = {r["id"]: dict(r) for r in db.execute("""SELECT i.* FROM items i
        JOIN catalog_history_links l ON i.id=l.item_id WHERE l.entry_id=?""", (entry_id,))}
    entry = db.execute("""SELECT r.path,f.relpath FROM catalog_files f JOIN catalog_roots r ON
        r.id=f.root_id WHERE f.id=? AND f.active=1""", (entry_id,)).fetchone()
    if entry:
        path = str(Path(entry["path"]) / entry["relpath"])
        for row in db.execute("""SELECT * FROM items WHERE (status='moved' AND target=? COLLATE NOCASE)
                OR (status='undone' AND source=? COLLATE NOCASE)""", (path, path)):
            if row["id"] not in rows and entry_id in _legacy_entries(db, row):
                rows[row["id"]] = dict(row)
    return [rows[key] for key in sorted(rows, reverse=True)]
