"""Local SQLite persistence. Immutable snapshots and versions, transactional pointers."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

from plantrace.assembly_models import ArchitectDecision, BuildingModel, HostAssignment, ReviewWorkspace
from plantrace.calibration import digest
from plantrace.models import DocumentInfo
from plantrace.topology import geometry_version


class PersistenceError(RuntimeError):
    pass


class ReviewConflict(PersistenceError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def database_path() -> Path:
    configured=os.environ.get("PLANTRACE_DB_PATH")
    return Path(configured) if configured else Path(__file__).resolve().parents[1]/"data"/"plantrace.sqlite3"


@dataclass(frozen=True)
class StoredWorkspace:
    revision: int
    snapshot_id: str
    workspace: ReviewWorkspace


class ReviewStore:
    def __init__(self,path: str | Path):
        self.path=Path(path)
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with self.connection() as connection:
                version=connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0,1):
                    raise PersistenceError(f"Unsupported review database schema {version}; the database was not reset.")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript("""
                CREATE TABLE IF NOT EXISTS sources(
                  document_id TEXT PRIMARY KEY, filename TEXT NOT NULL, content BLOB NOT NULL,
                  info_json TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS workspace_snapshots(
                  snapshot_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES sources(document_id),
                  page_number INTEGER NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS workspaces(
                  document_id TEXT NOT NULL REFERENCES sources(document_id), page_number INTEGER NOT NULL,
                  revision INTEGER NOT NULL, snapshot_id TEXT NOT NULL REFERENCES workspace_snapshots(snapshot_id),
                  PRIMARY KEY(document_id,page_number));
                CREATE TABLE IF NOT EXISTS candidates(
                  snapshot_id TEXT NOT NULL REFERENCES workspace_snapshots(snapshot_id), candidate_id TEXT NOT NULL,
                  geometry_version TEXT NOT NULL, payload_json TEXT NOT NULL,
                  PRIMARY KEY(snapshot_id,candidate_id));
                CREATE TABLE IF NOT EXISTS architect_decisions(
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL REFERENCES sources(document_id),
                  page_number INTEGER NOT NULL, decision_id TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS host_assignments(
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL REFERENCES sources(document_id),
                  page_number INTEGER NOT NULL, assignment_id TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS model_versions(
                  document_id TEXT NOT NULL REFERENCES sources(document_id), page_number INTEGER NOT NULL,
                  version INTEGER NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
                  PRIMARY KEY(document_id,page_number,version));
                CREATE TABLE IF NOT EXISTS review_events(
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL REFERENCES sources(document_id),
                  page_number INTEGER NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
                  note TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL);
                PRAGMA user_version=1;
                """)
        except OSError as exc:
            raise PersistenceError(f"Cannot create local review database: {exc}") from exc

    @contextmanager
    def connection(self):
        connection=None
        try:
            connection=sqlite3.connect(str(self.path),timeout=5,isolation_level=None)
            connection.row_factory=sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection
        except sqlite3.Error as exc:
            raise PersistenceError(f"SQLite review storage failed; existing data was not reset: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def transaction(self):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def save_source(self,data: bytes,info: DocumentInfo) -> None:
        if sha256(data).hexdigest()!=info.document_id or len(data)!=info.byte_size:
            raise ValueError("Source bytes do not match the document evidence SHA-256/size.")
        with self.transaction() as connection:
            connection.execute("""INSERT INTO sources VALUES(?,?,?,?,?) ON CONFLICT(document_id)
                                  DO UPDATE SET filename=excluded.filename,info_json=excluded.info_json""",
                               (info.document_id,info.filename,data,info.model_dump_json(),utc_now()))

    def list_sources(self) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute("SELECT document_id,filename,created_at FROM sources ORDER BY created_at,document_id")]

    def load_source(self,document_id: str) -> tuple[bytes,DocumentInfo]:
        with self.connection() as connection:
            row=connection.execute("SELECT content,info_json FROM sources WHERE document_id=?",(document_id,)).fetchone()
        if row is None:
            raise KeyError(f"Source {document_id!r} is not stored.")
        data=bytes(row["content"])
        if sha256(data).hexdigest()!=document_id:
            raise PersistenceError("Stored source integrity check failed; no replacement source was substituted.")
        try:
            info=DocumentInfo.model_validate_json(row["info_json"])
        except ValueError as exc:
            raise PersistenceError("Stored source metadata is invalid; it was not replaced.") from exc
        if info.document_id!=document_id or info.byte_size!=len(data):
            raise PersistenceError("Stored source metadata does not match its original bytes.")
        return data,info

    @staticmethod
    def read_workspace(connection,document_id: str,page_number: int) -> StoredWorkspace | None:
        row=connection.execute("""SELECT w.revision,w.snapshot_id,s.payload_json FROM workspaces w
             JOIN workspace_snapshots s ON s.snapshot_id=w.snapshot_id WHERE w.document_id=? AND w.page_number=?""",
             (document_id,page_number)).fetchone()
        if row is None:
            return None
        try:
            raw=json.loads(row["payload_json"])
            if digest(raw)!=row["snapshot_id"]:
                raise PersistenceError("Stored review snapshot failed its integrity check.")
            workspace=ReviewWorkspace.model_validate_json(row["payload_json"])
        except ValueError as exc:
            raise PersistenceError("Stored review snapshot is invalid; the database was not reset.") from exc
        if workspace.document.document_id!=document_id or workspace.inspection.page.page_number!=page_number:
            raise PersistenceError("Stored review snapshot failed its source/scope/integrity check.")
        return StoredWorkspace(row["revision"],row["snapshot_id"],workspace)

    def load_workspace(self,document_id: str,page_number: int) -> StoredWorkspace | None:
        with self.connection() as connection:
            return self.read_workspace(connection,document_id,page_number)

    def save_workspace(self,workspace: ReviewWorkspace,expected_revision: int | None=None,
                       action: str="workspace_saved",actor: str="Unspecified local editor",note: str="") -> StoredWorkspace:
        workspace=ReviewWorkspace.model_validate(workspace.model_dump())
        document_id=workspace.document.document_id
        page=workspace.inspection.page.page_number
        if any(identity!=document_id for identity in [workspace.inspection.document_id,workspace.candidates.document_id,workspace.calibration.document_id,workspace.measurements.document_id]) or any(number!=page for number in [workspace.candidates.page_number,workspace.calibration.page_number,workspace.measurements.page_number]):
            raise ValueError("Workspace components belong to different documents/pages.")
        payload=workspace.model_dump(mode="json")
        snapshot_id=digest(payload)
        with self.transaction() as connection:
            current=self.read_workspace(connection,document_id,page)
            if current is not None and current.snapshot_id==snapshot_id:
                return current
            revision=current.revision if current else 0
            if expected_revision is not None and expected_revision!=revision:
                raise ReviewConflict("Review changed in another session. Reload the saved review before editing; no concurrent work was overwritten.")
            connection.execute("INSERT OR IGNORE INTO workspace_snapshots VALUES(?,?,?,?,?)",
                               (snapshot_id,document_id,page,json.dumps(payload,sort_keys=True,allow_nan=False),utc_now()))
            connection.executemany("INSERT OR IGNORE INTO candidates VALUES(?,?,?,?)",
                [(snapshot_id,c.id,geometry_version(c),c.model_dump_json()) for c in workspace.candidates.candidates])
            connection.execute("""INSERT INTO workspaces VALUES(?,?,?,?) ON CONFLICT(document_id,page_number)
                                  DO UPDATE SET revision=excluded.revision,snapshot_id=excluded.snapshot_id""",
                                  (document_id,page,revision+1,snapshot_id))
            self.event(connection,document_id,page,action,actor,note,{"snapshot_id":snapshot_id,"previous_snapshot_id":current.snapshot_id if current else None})
            return StoredWorkspace(revision+1,snapshot_id,workspace)

    @staticmethod
    def event(connection,document_id,page_number,action,actor,note,details):
        connection.execute("INSERT INTO review_events(document_id,page_number,action,actor,note,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
                           (document_id,page_number,action,actor,note,json.dumps(details,sort_keys=True,allow_nan=False),utc_now()))

    def history(self,document_id: str,page_number: int) -> list[dict]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM review_events WHERE document_id=? AND page_number=? ORDER BY sequence",(document_id,page_number))]

    @staticmethod
    def decisions(connection,document_id,page_number) -> list[ArchitectDecision]:
        return [ArchitectDecision.model_validate_json(row[0]) for row in connection.execute("SELECT payload_json FROM architect_decisions WHERE document_id=? AND page_number=? ORDER BY sequence",(document_id,page_number))]

    @staticmethod
    def assignments(connection,document_id,page_number) -> list[HostAssignment]:
        return [HostAssignment.model_validate_json(row[0]) for row in connection.execute("SELECT payload_json FROM host_assignments WHERE document_id=? AND page_number=? ORDER BY sequence",(document_id,page_number))]

    @staticmethod
    def read_model(connection,document_id,page_number,version: int | None=None) -> BuildingModel | None:
        if version is None:
            row=connection.execute("SELECT payload_json,payload_hash FROM model_versions WHERE document_id=? AND page_number=? ORDER BY version DESC LIMIT 1",(document_id,page_number)).fetchone()
        else:
            row=connection.execute("SELECT payload_json,payload_hash FROM model_versions WHERE document_id=? AND page_number=? AND version=?",(document_id,page_number,version)).fetchone()
        if row is None:
            return None
        try:
            if digest(json.loads(row["payload_json"]))!=row["payload_hash"]:
                raise PersistenceError("Stored model version failed its integrity check.")
            model=BuildingModel.model_validate_json(row["payload_json"])
        except ValueError as exc:
            raise PersistenceError("Stored model version is invalid; it was not reset.") from exc
        return model

    def list_versions(self,document_id,page_number) -> list[int]:
        with self.connection() as connection:
            return [row[0] for row in connection.execute("SELECT version FROM model_versions WHERE document_id=? AND page_number=? ORDER BY version",(document_id,page_number))]
