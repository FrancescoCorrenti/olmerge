"""olmerge: two-way sync between a local git repository and an Overleaf project, with a real git three-way merge,
on any Overleaf plan (it talks to Overleaf's web endpoints through pyoverleaf, not to the paid git bridge).

Overleaf's state is kept on the local branch `overleaf`. A sync:
    1. commits local edits;
    2. pull:  downloads the project zip and commits it on `overleaf` (parent: the previous snapshot);
    3. merge: merges `overleaf` into the current branch (base = the last synced state);
              on a conflict it stops and uploads nothing;
    4. push:  downloads again to make sure nobody edited Overleaf meanwhile, uploads what differs
              (creates folders, overwrites files, deletes removed ones) and moves `overleaf` to the merged commit.
"""
import getpass
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

__version__ = "0.1.0"
BRANCH = "overleaf"
CONFIG = ".overleaf.json"
EXCLUDE = (".git/", ".vscode/", ".idea/", "build/", "out/", ".overleaf.json", ".gitignore", ".gitattributes", "latexmkrc", ".latexmkrc")
KEYRING = ("olmerge", "overleaf_session2")


class Repo:
    """The git repository the command runs in, with its Overleaf binding (.overleaf.json at the top level)."""

    def __init__(self, path="."):
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True)
        if top.returncode:
            sys.exit("olmerge: not inside a git repository (run `git init` first)")
        self.root = Path(top.stdout.strip())
        cfg = self.root / CONFIG
        self.cfg = json.loads(cfg.read_text()) if cfg.exists() else {}
        self.exclude = EXCLUDE + tuple(self.cfg.get("exclude", []))

    def git(self, *a, env=None, data=None, check=True):
        r = subprocess.run(["git", *a], cwd=self.root, capture_output=True, input=data, env=env)
        if check and r.returncode:
            sys.exit(f"olmerge: git {' '.join(a)} failed:\n{r.stderr.decode(errors='replace')}")
        return r.stdout.decode(errors="replace").strip()

    def ref(self, name):
        return self.git("rev-parse", "--verify", "-q", name, check=False) or None

    def synced(self, path):
        return not any(path == e.rstrip("/") or path.startswith(e) for e in self.exclude)

    def save(self, **kw):
        self.cfg.update(kw)
        (self.root / CONFIG).write_text(json.dumps(self.cfg, indent=1) + "\n")


class Overleaf:
    """The few Overleaf operations olmerge needs (pyoverleaf, logged in with the session cookie)."""

    def __init__(self, host="www.overleaf.com"):
        import pyoverleaf
        cookie = os.environ.get("OVERLEAF_SESSION") or _keyring().get_password(*KEYRING)
        if not cookie:
            sys.exit("olmerge: no Overleaf session; run `olmerge login`")
        self.host, self.api = host, pyoverleaf.Api(host=host)
        self.api.login_from_cookies({"overleaf_session2": cookie})
        self.edit_mode, self.track = "ot", False                   # "upload": replace files (comments are lost)

    def edit_doc(self, pid, doc_id, new):
        """Turn the doc on Overleaf into `new` with in-place edits (the editor's real-time channel), so that comments
        and tracked changes anchored to unchanged text survive -> number of edits."""
        ch = Channel(self.api._open_socket(pid))                   # pylint: disable=protected-access
        try:
            ch.wait_joined()
            lines, version = ch.call("joinDoc", [doc_id, {"encodeRanges": True}])[1:3]
            old = "\n".join(_decode(l) for l in lines)
            op = text_op(old, new)
            if op:
                upd = {"doc": doc_id, "op": op, "v": version}
                if self.track:
                    upd["meta"] = {"tc": os.urandom(12).hex()}
                ch.call("applyOtUpdate", [doc_id, upd])
            ch.call("leaveDoc", [doc_id])
            return len(op)
        finally:
            ch.close()

    def projects(self):
        return self.api.get_projects()

    def download(self, pid):
        z = zipfile.ZipFile(io.BytesIO(self.api.download_project(pid)))
        return {n: z.read(n) for n in z.namelist() if not n.endswith("/")}

    def upload(self, pid, files, deleted):
        """files: {path: bytes} to create or overwrite; deleted: paths to remove."""
        root = self.api.project_get_files(pid)
        folders, entities = {"": root}, {}

        def walk(folder, prefix):
            for c in folder.children:
                if c.type == "folder":
                    folders[prefix + c.name] = c; walk(c, prefix + c.name + "/")
                else:
                    entities[prefix + c.name] = c
        walk(root, "")
        for p in deleted:
            if p in entities:
                self.api.project_delete_entity(pid, entities[p]); print("  deleted ", p)
        for p, data in files.items():
            ent = entities.get(p)
            if ent is not None and ent.type == "doc" and self.edit_mode == "ot":
                try:                                               # edit in place: comments and tracked changes survive
                    n = self.edit_doc(pid, ent.id, data.decode("utf-8"))
                    print("  edited  ", p, f"({n} change{'s' * (n != 1)})"); continue
                except (OTError, UnicodeDecodeError) as e:
                    print(f"  could not edit {p} in place ({e}); replacing the file, its comments are lost")
            parent = ""
            for part in p.split("/")[:-1]:
                q = f"{parent}/{part}" if parent else part
                if q not in folders:
                    folders[q] = self.api.project_create_folder(pid, folders[parent].id, part)
                parent = q
            self.api.project_upload_file(pid, folders[parent].id, p.split("/")[-1], data); print("  uploaded", p)

    def create(self, name, zipped):
        """New project from a zip -> project id (the dashboard's 'Upload project')."""
        from bs4 import BeautifulSoup
        s = self.api._get_session()                                # pylint: disable=protected-access
        page = BeautifulSoup(s.get(f"https://{self.host}/project").content, "html.parser")
        token = page.find("meta", dict(name="ol-csrfToken")).get("content")
        r = s.post(f"https://{self.host}/project/new/upload",
                   files={"name": (None, f"{name}.zip"), "type": (None, "application/zip"),
                          "qqfile": (f"{name}.zip", zipped, "application/zip")},
                   headers={"x-csrf-token": token, "Accept": "application/json", "Referer": f"https://{self.host}/project"})
        r.raise_for_status()
        return r.json()["project_id"]


class OTError(RuntimeError):
    pass


class Channel:
    """Minimal socket.io 0.9 client over pyoverleaf's websocket: numbered calls with acknowledgements, heartbeats."""

    def __init__(self, ws):
        self.ws, self.n = ws, 0

    def recv(self):
        while True:
            m = self.ws.recv()
            if m.startswith("2::"):                                # heartbeat
                self.ws.send("2::")
            elif m.startswith("7:") or m.startswith("0::"):
                raise OTError(f"channel closed: {m[:120]}")
            else:
                return m

    def wait_joined(self):
        while not self.recv().startswith("5:"):                    # joinProjectResponse (or connectionAccepted)
            pass

    def call(self, name, args):
        self.n += 1
        self.ws.send(f"5:{self.n}+::" + json.dumps({"name": name, "args": args}))
        tag = f"6:::{self.n}+"
        while True:
            m = self.recv()
            if m.startswith(tag):
                res = json.loads(m[len(tag):])
                if res and res[0]:
                    raise OTError(f"{name}: {res[0]}")
                return res

    def close(self):
        try:
            self.ws.close()
        except Exception:                                          # pylint: disable=broad-except
            pass


def _decode(line):
    """joinDoc with encodeRanges sends each line as its UTF-8 bytes in a binary string."""
    try:
        return line.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return line


def text_op(old, new):
    """ShareJS text op turning `old` into `new`: delete/insert components, positions in UTF-16 code units (as in the
    browser). Lines are matched first, characters only inside changed blocks, so edits stay small and local; the
    components run from the end of the document backwards, so each position refers to the original text."""
    import difflib
    u16 = lambda s: len(s.encode("utf-16-le")) // 2
    import re
    lines = lambda s: re.findall(r"[^\n]*\n|[^\n]+", s)           # each line keeps its newline; only \n splits
    ol, nl = lines(old), lines(new)
    starts = [0]
    for line in ol:
        starts.append(starts[-1] + len(line))                      # starts[len(ol)] == len(old)
    edits = []                                                     # (char offset in old, deleted text, inserted text)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ol, nl, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        a, b, base = "".join(ol[i1:i2]), "".join(nl[j1:j2]), starts[i1]
        for t, k1, k2, l1, l2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if t != "equal":
                edits.append((base + k1, a[k1:k2], b[l1:l2]))
    op = []
    for off, d, i in sorted(edits, reverse=True):
        p = u16(old[:off])
        if d:
            op.append({"p": p, "d": d})
        if i:
            op.append({"p": p, "i": i})
    return op


def apply_op(text, op):
    """Apply a text_op (UTF-16 positions) to a Python string: the reference used by the tests."""
    b = text.encode("utf-16-le")
    for c in op:
        p = c["p"] * 2
        if "d" in c:
            d = c["d"].encode("utf-16-le")
            assert b[p:p + len(d)] == d, ("delete does not match", c)
            b = b[:p] + b[p + len(d):]
        else:
            b = b[:p] + c["i"].encode("utf-16-le") + b[p:]
    return b.decode("utf-16-le")


def _keyring():
    import keyring
    return keyring


def snapshot(repo, files):
    """Commit Overleaf's files on BRANCH, keeping excluded paths as they are there -> True if Overleaf changed."""
    base = repo.ref(BRANCH)
    env = dict(os.environ, GIT_INDEX_FILE=str(Path(tempfile.mkdtemp()) / "index"))
    if base:
        repo.git("read-tree", base, env=env)
        gone = [p for p in repo.git("ls-files", env=env).splitlines() if repo.synced(p) and p not in files]
        if gone:
            repo.git("rm", "--cached", "-q", "--", *gone, env=env)
    for p, data in files.items():
        sha = repo.git("hash-object", "-w", "--stdin", data=data)
        repo.git("update-index", "--add", "--cacheinfo", f"100644,{sha},{p}", env=env)
    tree = repo.git("write-tree", env=env)
    if base and tree == repo.git("rev-parse", f"{base}^{{tree}}"):
        return False
    commit = repo.git("commit-tree", tree, *(["-p", base] if base else []), "-m", "Overleaf snapshot")
    repo.git("update-ref", f"refs/heads/{BRANCH}", commit)
    return True


def sync(repo, overleaf, dry_run=False):
    pid = repo.cfg.get("project_id") or sys.exit("olmerge: not bound to a project; run `olmerge create` or `olmerge bind`")
    if repo.git("status", "--porcelain"):
        repo.git("add", "-A"); repo.git("commit", "-q", "-m", "Local edits before Overleaf sync"); print("committed local edits")
    for _ in range(3):
        remote = {p: d for p, d in overleaf.download(pid).items() if repo.synced(p)}
        if snapshot(repo, remote):
            print("pulled changes from Overleaf")
        ahead = subprocess.run(["git", "merge-base", "--is-ancestor", BRANCH, "HEAD"], cwd=repo.root).returncode != 0
        if ahead:
            r = subprocess.run(["git", "merge", "--no-edit", "--allow-unrelated-histories", BRANCH], cwd=repo.root,
                               capture_output=True, text=True)
            if r.returncode:
                print(r.stdout)
                sys.exit("olmerge: CONFLICT. Resolve the files above, commit, and sync again (nothing was uploaded).")
            print("merged Overleaf into", repo.git("branch", "--show-current"))
        diff = repo.git("diff", "--name-status", "--no-renames", BRANCH, "HEAD")
        todo = [(line.split("\t", 1)[1], line[0]) for line in diff.splitlines() if line and repo.synced(line.split("\t", 1)[1])]
        if not todo:
            print("in sync"); return
        print(f"{len(todo)} file(s) to push:", ", ".join(f"{s} {p}" for p, s in todo))
        if dry_run:
            return
        if {p: d for p, d in overleaf.download(pid).items() if repo.synced(p)} != remote:
            print("Overleaf changed during the sync, starting over"); continue
        files = {p: subprocess.run(["git", "show", f"HEAD:{p}"], cwd=repo.root, capture_output=True, check=True).stdout
                 for p, s in todo if s != "D"}
        overleaf.upload(pid, files, [p for p, s in todo if s == "D"])
        repo.git("update-ref", f"refs/heads/{BRANCH}", repo.git("rev-parse", "HEAD"))
        print("pushed: Overleaf now matches", repo.git("rev-parse", "--short", "HEAD")); return
    sys.exit("olmerge: Overleaf kept changing during the sync; try again in a minute")


def prepare(repo):
    """Overleaf stores LF: keep LF in the repository so snapshots compare byte for byte."""
    repo.git("config", "core.autocrlf", "false")
    ga = repo.root / ".gitattributes"
    if not ga.exists():
        ga.write_text("* text=auto eol=lf\n*.pdf binary\n*.png binary\n*.jpg binary\n")
        repo.git("add", ".gitattributes"); repo.git("add", "--renormalize", ".")
        repo.git("commit", "-q", "-m", "LF line endings for Overleaf", check=False)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="olmerge", description="Two-way git merge with an Overleaf project, on any plan.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("login", help="store your Overleaf session cookie in the OS keychain")
    c = sub.add_parser("create", help="create an Overleaf project from this repository and bind it"); c.add_argument("name")
    b = sub.add_parser("bind", help="bind this repository to an existing Overleaf project"); b.add_argument("project")
    s = sub.add_parser("sync", help="pull, merge and push"); s.add_argument("--dry-run", action="store_true")
    sub.add_parser("projects", help="list your Overleaf projects")
    a = ap.parse_args(argv)
    if a.cmd == "login":
        _keyring().set_password(*KEYRING, getpass.getpass("overleaf_session2 cookie: ").strip())
        print("saved;", len(Overleaf().projects()), "projects visible")
        return
    if a.cmd == "projects":
        for p in Overleaf().projects():
            print(p.id, p.name)
        return
    repo = Repo()
    ol = Overleaf(repo.cfg.get("host", "www.overleaf.com"))
    ol.edit_mode, ol.track = repo.cfg.get("edit", "ot"), bool(repo.cfg.get("track_changes", False))
    if a.cmd == "create":
        prepare(repo)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in repo.git("ls-files").splitlines():
                if repo.synced(p):
                    z.writestr(p, (repo.root / p).read_bytes())
        pid = ol.create(a.name, buf.getvalue())
        repo.save(project_id=pid)
        repo.git("update-ref", f"refs/heads/{BRANCH}", repo.git("rev-parse", "HEAD"))
        print(f"created https://{ol.host}/project/{pid}")
    elif a.cmd == "bind":
        prepare(repo)
        repo.save(project_id=a.project.rstrip("/").split("/")[-1])
        print("bound; the first sync merges the two histories")
    else:
        sync(repo, ol, a.dry_run)


if __name__ == "__main__":
    main()
