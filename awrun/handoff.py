"""Move a suspended run to another machine, and refuse one that was altered.

A suspended run is two things: the queue item, and -- for a workflow -- the
journal it will replay. `export_run` puts both in one bundle; `import_run` puts
them back under the SAME run id on another queue, where `awrun resume` continues
from the journal.

The bundle is trusted input to a replay: a forged journal would be replayed as
the run's own past. So import verifies before it unpacks, and fails closed:

* a bundle sealed with a signing key is accepted only against an EXPECTED public
  key -- a seal that merely verifies against itself proves nobody's identity;
* an unsealed bundle is accepted only against the SHA-256 the exporter printed.

Neither given -> refused. A run lives in one place: export closes the local copy.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from awrun.store import STATUS_SUSPENDED, RunError, RunItem, RunStore

_MEMBERS = ("run.json", "journal.jsonl", "awseal.json")
BUNDLE_SUFFIX = ".awrun.zip"


def sha256_file(path: Path) -> str:
    try:
        from awshare.store import digest_file
        digest = digest_file(path)
        return digest.split(":", 1)[-1]
    except ImportError:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()


def journal_file(store: RunStore, item: RunItem) -> Optional[Path]:
    if item.kind != "flow":
        return None
    root = item.spec.get("journal") or (item.checkpoint or {}).get("journal") \
        or str(store.path / "journals")
    path = Path(root) / item.id / "journal.jsonl"
    return path if path.is_file() else None


def export_run(store: RunStore, item_id: str, out_dir: Path, *,
               sign: bool = True) -> dict:
    item = store.get(item_id)
    if item is None:
        raise RunError(f"no such run: {item_id}")
    if item.status != STATUS_SUSPENDED:
        raise RunError(f"run {item_id} is {item.status}; only a suspended run can be "
                       f"exported (suspend it first)")
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = out_dir / f"{item.id}{BUNDLE_SUFFIX}"
    sealed_by = ""
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td)
        (stage / "run.json").write_text(json.dumps(item.to_dict(), indent=2),
                                        encoding="utf-8")
        journal = journal_file(store, item)
        if journal is not None:
            shutil.copyfile(journal, stage / "journal.jsonl")
        if sign:
            try:
                import awseal
                from awseal import seal as _seal
                _seal.write(_seal.sign(stage, subject=f"awrun:{item.id}"), stage)
                sealed_by = awseal.keys.public_key_hex()
            except Exception:  # noqa: BLE001 - no key / no brick: an unsealed bundle,
                sealed_by = ""                  # which import accepts only by digest
                (stage / "awseal.json").unlink(missing_ok=True)
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in _MEMBERS:
                if (stage / name).is_file():
                    zf.write(stage / name, name)
    digest = sha256_file(bundle)
    store.cancel(item.id)          # a run lives in one place
    return {"bundle": str(bundle), "sha256": digest, "sealed_by": sealed_by,
            "journal": journal is not None}


def import_run(store: RunStore, bundle: Path, *, expect_sha256: str = "",
               expect_key: str = "") -> RunItem:
    if not expect_sha256 and not expect_key:
        raise RunError("refusing to import an unverified bundle: give --sha256 (what the "
                       "exporter printed) or --expect-key (the exporter's public key)")
    if not bundle.is_file():
        raise RunError(f"no such bundle: {bundle}")
    if expect_sha256:
        actual = sha256_file(bundle)
        if actual != expect_sha256.strip().lower():
            raise RunError(f"bundle digest mismatch: expected {expect_sha256}, got {actual}")
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td)
        with zipfile.ZipFile(bundle) as zf:
            names = zf.namelist()
            stray = sorted(set(names) - set(_MEMBERS))
            if stray or len(names) != len(set(names)):
                raise RunError(f"bundle carries unexpected members {stray or names}")
            for name in names:       # fixed names only: nothing in the zip picks a path
                (stage / name).write_bytes(zf.read(name))
        if expect_key:
            if not (stage / "awseal.json").is_file():
                raise RunError("--expect-key given but the bundle is not sealed")
            try:
                from awseal import seal as _seal
            except ImportError:
                raise RunError("verifying a sealed bundle needs awseal: "
                               "pip install awseal") from None
            verdict = _seal.verify(stage, expect_key=expect_key)
            if not (verdict.get("ok") and verdict.get("key_trusted") is True):
                raise RunError(f"seal verification failed: signature_ok="
                               f"{verdict.get('signature_ok')} content_ok="
                               f"{verdict.get('content_ok')} key_trusted="
                               f"{verdict.get('key_trusted')}")
        try:
            item = RunItem.from_dict(json.loads((stage / "run.json").read_text("utf-8")))
        except (OSError, ValueError, TypeError) as exc:
            raise RunError(f"bundle has no readable run.json: {exc}") from exc
        if store.get(item.id) is not None:
            raise RunError(f"run {item.id} already exists in this queue")
        if (stage / "journal.jsonl").is_file():
            target = store.path / "journals" / item.id / "journal.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(stage / "journal.jsonl", target)
            item.spec.pop("journal", None)     # the journal lives HERE now
            item.checkpoint = dict(item.checkpoint or {}, journal=str(store.path / "journals"),
                                   journal_sha256=sha256_file(target))
    return store.adopt(item)
