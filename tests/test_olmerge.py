"""olmerge's git logic against a fake Overleaf held in memory (no network): edits on different files merge and upload,
excluded paths stay local, deletions propagate, and a conflicting edit stops before anything is uploaded.
    python -m pytest tests     or     python tests/test_olmerge.py"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import olmerge


class FakeOverleaf:
    def __init__(self, files):
        self.files = dict(files)

    def download(self, pid):
        return dict(self.files)

    def upload(self, pid, files, deleted):
        for p in deleted:
            self.files.pop(p, None)
        self.files.update(files)


def make_repo():
    tmp = Path(tempfile.mkdtemp())
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp, check=True, capture_output=True)
    run("init", "-q"); run("config", "user.name", "t"); run("config", "user.email", "t@example.com")
    for p, d in {"main.tex": b"one\n", "sec/intro.tex": b"two\n", ".vscode/settings.json": b"{}"}.items():
        (tmp / p).parent.mkdir(parents=True, exist_ok=True); (tmp / p).write_bytes(d)
    repo = olmerge.Repo(tmp)
    olmerge.prepare(repo)
    run("add", "-A"); run("commit", "-q", "-m", "init")
    repo.save(project_id="fake")
    run("add", "-A"); run("commit", "-q", "-m", "bind")
    repo.git("update-ref", f"refs/heads/{olmerge.BRANCH}", repo.git("rev-parse", "HEAD"))
    return repo, FakeOverleaf({"main.tex": b"one\n", "sec/intro.tex": b"two\n"})


def test_merge_push_and_conflict():
    repo, ol = make_repo()
    ol.files["main.tex"] = b"one, edited on Overleaf\n"           # different files: merge, then push
    (repo.root / "sec/intro.tex").write_bytes(b"two, edited locally\n")
    (repo.root / "new.tex").write_bytes(b"new\n")
    olmerge.sync(repo, ol)
    assert (repo.root / "main.tex").read_bytes() == b"one, edited on Overleaf\n"
    assert ol.files["sec/intro.tex"] == b"two, edited locally\n" and ol.files["new.tex"] == b"new\n"
    assert ".vscode/settings.json" not in ol.files and ".overleaf.json" not in ol.files

    (repo.root / "new.tex").unlink()                                 # a local deletion reaches Overleaf
    olmerge.sync(repo, ol)
    assert "new.tex" not in ol.files

    ol.files["main.tex"] = b"one, Overleaf again\n"                 # same line on both sides: stop, upload nothing
    (repo.root / "main.tex").write_bytes(b"one, local again\n")
    before = dict(ol.files)
    try:
        olmerge.sync(repo, ol)
        raise AssertionError("conflict not detected")
    except SystemExit as e:
        assert "CONFLICT" in str(e)
    assert ol.files == before


if __name__ == "__main__":
    test_merge_push_and_conflict()
    print("ok")
