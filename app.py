from __future__ import annotations

import argparse
import io
import json
import os
import shutil
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from flask import Flask, jsonify, make_response, render_template, request, send_file

STORAGE_CONNECTION_STR_ENV = (
    "PDF_STORAGE_CONNECTION_STRING",
    "AZURE_STORAGE_CONNECTION_STRING",
)
STORAGE_ACCOUNT_ENV = ("PDF_STORAGE_ACCOUNT", "AZURE_STORAGE_ACCOUNT")
PDFS_CONTAINER_ENV = "PDFS_CONTAINER"
PDF_RESULTS_CONTAINER_ENV = "PDF_RESULTS_CONTAINER"
DEFAULT_PDFS_CONTAINER = "pdfs"
DEFAULT_PDF_RESULTS_CONTAINER = "pdf-results"


def _extract_analysis_pages(data: Any) -> list | None:
    """Resolve the pages array from a Content Understanding result.

    Accepts three shapes produced by the service / SDK:
      A) bare form: top-level "pages": [...]
      B) envelope:  { "kind": "document", "document": { "pages": [...] } }
      C) SDK as_dict() (api 2025-11-01): { "contents": [ { "kind": "document",
           "pages": [...] }, ... ] }  -> pages merged across content entries.
    """
    if not isinstance(data, dict):
        return None

    pages = data.get("pages")
    if isinstance(pages, list):
        return pages

    doc = data.get("document")
    if isinstance(doc, dict):
        pages = doc.get("pages")
        if isinstance(pages, list):
            return pages

    contents = data.get("contents")
    if isinstance(contents, list):
        merged: list[dict[str, Any]] = []
        seen: set[int] = set()
        for entry in contents:
            if not isinstance(entry, dict):
                continue
            entry_pages = entry.get("pages")
            if not isinstance(entry_pages, list):
                continue
            for page in entry_pages:
                if not isinstance(page, dict):
                    continue
                num = page.get("pageNumber")
                key = int(num) if isinstance(num, (int, float)) and num == int(num) else -1
                if key >= 0 and key in seen:
                    continue
                if key >= 0:
                    seen.add(key)
                merged.append(page)
        merged.sort(key=lambda p: p.get("pageNumber", 0))
        return merged or None

    return None


def _looks_like_content_understanding(data: Any) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "Top-level JSON must be an object."

    pages = _extract_analysis_pages(data)
    if not isinstance(pages, list):
        where = (
            "top-level 'pages', inside the 'document' object, "
            "or inside 'contents[].pages' (SDK as_dict output)"
        )
        found = sorted(str(k) for k in data.keys())
        return (
            False,
            f"Expected a Content Understanding document result with a pages array "
            f"({where}). Top-level keys found: {', '.join(found) or '(none)'}. ",
        )

    for idx, page in enumerate(pages):
        if not isinstance(page, dict):
            return False, f"pages[{idx}] must be an object."
        if "pageNumber" not in page:
            return False, f"pages[{idx}] is missing 'pageNumber'."
        if "words" not in page:
            return False, f"pages[{idx}] is missing 'words'."
        words = page.get("words")
        if not isinstance(words, list):
            return False, f"pages[{idx}].words must be an array."
        for w_idx, word in enumerate(words):
            if not isinstance(word, dict):
                return False, f"pages[{idx}].words[{w_idx}] must be an object."
            if "content" not in word:
                return False, f"pages[{idx}].words[{w_idx}] missing 'content'."
            if "confidence" not in word:
                return False, f"pages[{idx}].words[{w_idx}] missing 'confidence'."
    return True, ""


def _sanitize_curated_schema(curated_obj: Any) -> Any:
    """Normalize curated payload for page-level approval schema.

    - Removes legacy per-word `status` fields silently.
    - Ensures page-level status exists.
    """
    if not isinstance(curated_obj, dict):
        return curated_obj

    pages = curated_obj.get("pages")
    if not isinstance(pages, list):
        return curated_obj

    for page in pages:
        if not isinstance(page, dict):
            continue

        if "status" not in page or page.get("status") not in {"pending", "approved"}:
            page["status"] = "pending"

        words = page.get("words")
        if not isinstance(words, list):
            continue

        for word in words:
            if isinstance(word, dict) and "status" in word:
                word.pop("status", None)

    return curated_obj


def _build_curated(raw: dict[str, Any], pdf_name: str, source_json_name: str) -> dict[str, Any]:
    pages = _extract_analysis_pages(raw) or []
    curated_pages: list[dict[str, Any]] = []

    for page in pages:
        words = page.get("words") or []
        normalized_words = []
        text_tokens: list[str] = []
        for word in words:
            content = str(word.get("content", ""))
            text_tokens.append(content)
            confidence = word.get("confidence")
            try:
                confidence_val = float(confidence) if confidence is not None else None
            except (TypeError, ValueError):
                confidence_val = None

            normalized_words.append(
                {
                    "text": content,
                    "original": content,
                    "confidence": confidence_val,
                    "note": "",
                }
            )

        curated_pages.append(
            {
                "pageNumber": int(page.get("pageNumber", len(curated_pages) + 1)),
                "kind": "blank" if len(normalized_words) == 0 else "other",
                "text": " ".join([t for t in text_tokens if t]).strip(),
                "status": "pending",
                "words": normalized_words,
                "issues": [],
            }
        )

    return {
        "schema": "curated/1",
        "document": {
            "fileName": pdf_name,
            "sourceJson": source_json_name,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        },
        "pages": curated_pages,
    }


class StorageBackend(ABC):
    """Abstract document store for the review tool.

    Keys are storage-relative paths. Two flavours exist:

    * ``LocalStorage``  - a folder on disk (legacy behaviour).
    * ``BlobStorage``   - Azure Blob Storage. Source PDFs live in one container
      (default ``pdfs``); the results container (default ``pdf-results``) holds
      the Content Understanding analysis JSON for each PDF, curated output
      (``<stem>.curated.json``) and timestamped backups.
    """

    name: str = "local"

    @abstractmethod
    def display_base(self) -> str:
        """Human-readable root, shown in /api/config."""

    @abstractmethod
    def list_pdf_keys(self) -> list[str]:
        """All PDF keys, sorted, with a discoverable sibling JSON."""

    @abstractmethod
    def sibling_json_keys(self, pdf_key: str) -> list[str]:
        """Candidate analysis-JSON keys for a PDF key, in priority order."""

    @abstractmethod
    def curated_key_for(self, pdf_key: str) -> str:
        """Key under which curated output for a PDF is stored/retrieved."""

    @abstractmethod
    def read_bytes(self, key: str) -> bytes | None:
        """Read raw bytes; None if missing."""

    @abstractmethod
    def write_bytes(self, key: str, data: bytes) -> None:
        """Create or overwrite a blob/file."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Whether the key exists."""


def _safe_resolve(base_folder: Path, user_path: str) -> Path:
    candidate = (base_folder / user_path).resolve()
    base_resolved = base_folder.resolve()
    if base_resolved not in [candidate, *candidate.parents]:
        raise ValueError("Requested path is outside the server folder.")
    return candidate


class LocalStorage(StorageBackend):
    name = "local"

    def __init__(self, base_folder: Path):
        self.base = base_folder.expanduser().resolve()
        if not self.base.exists() or not self.base.is_dir():
            raise SystemExit(f"Folder does not exist or is not a directory: {self.base}")

    def display_base(self) -> str:
        return str(self.base)

    def list_pdf_keys(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.base))
            for p in self.base.rglob("*.pdf")
            if p.is_file()
        )

    def sibling_json_keys(self, pdf_key: str) -> list[str]:
        pdf_path = self.base / pdf_key
        return [
            str(Path(f"{pdf_path}.json").relative_to(self.base)),
            str(pdf_path.with_suffix(".json").relative_to(self.base)),
        ]

    def curated_key_for(self, pdf_key: str) -> str:
        return str(Path(pdf_key).with_suffix(".curated.json"))

    def _raw_path(self, key: str) -> Path:
        return _safe_resolve(self.base, key)

    def read_bytes(self, key: str) -> bytes | None:
        p = self._raw_path(key)
        return p.read_bytes() if p.exists() and p.is_file() else None

    def write_bytes(self, key: str, data: bytes) -> None:
        p = self._raw_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def exists(self, key: str) -> bool:
        p = self._raw_path(key)
        return p.exists() and p.is_file()


class BlobStorage(StorageBackend):
    name = "blob"

    def __init__(
        self,
        service: BlobServiceClient,
        pdfs_container: str = DEFAULT_PDFS_CONTAINER,
        results_container: str = DEFAULT_PDF_RESULTS_CONTAINER,
    ):
        self.pdfs = self._ensure_container(service, pdfs_container)
        self.results = self._ensure_container(service, results_container)

    @staticmethod
    def _ensure_container(service: BlobServiceClient, name: str):
        client = service.get_container_client(name)
        if not client.exists():
            client.create_container()
        return client

    def display_base(self) -> str:
        return f"az://{self.pdfs.account_name}/{self.pdfs.container_name} + {self.results.container_name}"

    def list_pdf_keys(self) -> list[str]:
        names = [b.name for b in self.pdfs.list_blobs() if b.name.lower().endswith(".pdf")]
        return sorted(names, key=str.lower)

    def sibling_json_keys(self, pdf_key: str) -> list[str]:
        stem = pdf_key[:-4] if pdf_key.lower().endswith(".pdf") else pdf_key
        # Analysis JSON outputs live in the results container (pdf-results),
        # named either "<stem>.json" or "<pdf>.json".
        return [f"{stem}.json", f"{pdf_key}.json"]

    def curated_key_for(self, pdf_key: str) -> str:
        stem = pdf_key[:-4] if pdf_key.lower().endswith(".pdf") else pdf_key
        return f"{stem}.curated.json"

    def _container_for(self, key: str):
        # PDFs are the only blobs in the pdfs container; every other key
        # (analysis JSON, curated JSON, backups) belongs to pdf-results.
        if key.lower().endswith(".pdf"):
            return self.pdfs
        return self.results

    def read_bytes(self, key: str) -> bytes | None:
        blob = self._container_for(key).get_blob_client(key)
        if not blob.exists():
            return None
        stream = blob.download_blob()
        return stream.readall()

    def write_bytes(self, key: str, data: bytes) -> None:
        self._container_for(key).get_blob_client(key).upload_blob(data, overwrite=True)

    def exists(self, key: str) -> bool:
        return self._container_for(key).get_blob_client(key).exists()


def _storage_from_env() -> StorageBackend:
    """Build the backend from environment variables.

    * ``PDF_STORAGE_CONNECTION_STRING`` (or ``AZURE_STORAGE_CONNECTION_STRING``)
      -> Blob Storage using the connection string (dev / simple deploys).
    * ``PDF_STORAGE_ACCOUNT`` (or ``AZURE_STORAGE_ACCOUNT``) -> Blob Storage
      using ``DefaultAzureCredential`` (managed identity on Azure App Service).
    * Neither -> local folder mode, rooted at ``BASE_FOLDER`` or the CWD.
    """
    conn_str = next((os.environ.get(v) for v in STORAGE_CONNECTION_STR_ENV if os.environ.get(v)), None)
    account = next((os.environ.get(v) for v in STORAGE_ACCOUNT_ENV if os.environ.get(v)), None)
    pdfs_container = os.environ.get(PDFS_CONTAINER_ENV, DEFAULT_PDFS_CONTAINER)
    results_container = os.environ.get(PDF_RESULTS_CONTAINER_ENV, DEFAULT_PDF_RESULTS_CONTAINER)

    if not conn_str and not account:
        return LocalStorage(Path(os.environ.get("BASE_FOLDER", ".")))

    if conn_str:
        service = BlobServiceClient.from_connection_string(conn_str)
    else:
        service = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=DefaultAzureCredential(),
        )
    return BlobStorage(service, pdfs_container, results_container)


def create_app(storage: StorageBackend | None = None) -> Flask:
    storage = storage or _storage_from_env()
    app = Flask(__name__)
    app.config["STORAGE"] = storage

    @app.get("/")
    def index() -> str:
        resp = make_response(render_template("index.html"))
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/api/config")
    def config() -> Any:
        return jsonify(
            {
                "baseFolder": storage.display_base(),
                "storage": storage.name,
            }
        )

    @app.get("/api/list-documents")
    def list_documents() -> Any:
        docs: list[dict[str, Any]] = []

        for pdf_key in storage.list_pdf_keys():
            json_key = next((k for k in storage.sibling_json_keys(pdf_key) if storage.exists(k)), None)
            if json_key is None:
                continue

            curated_key = storage.curated_key_for(pdf_key)

            docs.append(
                {
                    "label": pdf_key,
                    "pdfPath": pdf_key,
                    "jsonPath": json_key,
                    "curatedPath": curated_key,
                    "curatedExists": storage.exists(curated_key),
                }
            )

        return jsonify({"documents": docs})

    @app.post("/api/load-server-document")
    def load_server_document() -> Any:
        payload = request.get_json(silent=True) or {}
        pdf_key = payload.get("pdfPath")
        json_key = payload.get("jsonPath")
        if not pdf_key or not json_key:
            return jsonify({"ok": False, "message": "pdfPath and jsonPath are required."}), 400

        try:
            raw_bytes = storage.read_bytes(json_key)
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        if raw_bytes is None:
            return jsonify({"ok": False, "message": f"JSON not found: {json_key}"}), 404

        try:
            raw_obj = json.loads(raw_bytes.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "message": f"Failed to read/parse JSON: {exc}"}), 400

        valid, reason = _looks_like_content_understanding(raw_obj)
        if not valid:
            return jsonify(
                {
                    "ok": False,
                    "message": "Input JSON is not Azure Content Understanding document output. "
                    f"Details: {reason}",
                }
            ), 400

        curated_key = storage.curated_key_for(pdf_key)
        if storage.exists(curated_key):
            try:
                curated_obj = json.loads(storage.read_bytes(curated_key).decode("utf-8"))
                curated_obj = _sanitize_curated_schema(curated_obj)
            except Exception as exc:  # noqa: BLE001
                return jsonify(
                    {
                        "ok": False,
                        "message": f"Existing curated JSON is invalid and could not be loaded: {curated_key}. Error: {exc}",
                    }
                ), 400
        else:
            curated_obj = _build_curated(raw_obj, Path(pdf_key).name, Path(json_key).name)

        return jsonify(
            {
                "ok": True,
                "mode": "server",
                "pdfUrl": f"/api/pdf?path={quote(pdf_key, safe='')}",
                "pdfPath": pdf_key,
                "jsonPath": json_key,
                "curatedPath": curated_key,
                "rawJson": raw_obj,
                "curatedJson": curated_obj,
                "message": "Loaded PDF+JSON from server storage.",
            }
        )

    @app.get("/api/pdf")
    def get_pdf() -> Any:
        key = request.args.get("path", "")
        if not key:
            return jsonify({"ok": False, "message": "Missing 'path' query parameter."}), 400

        if storage.name == "blob":
            data = storage.read_bytes(key)
            if data is None:
                return jsonify({"ok": False, "message": "PDF blob not found."}), 404
            return send_file(io.BytesIO(data), mimetype="application/pdf", download_name=Path(key).name)

        try:
            data = storage.read_bytes(key)
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        if data is None or not key.lower().endswith(".pdf"):
            return jsonify({"ok": False, "message": "PDF file not found."}), 404

        return send_file(storage._raw_path(key), mimetype="application/pdf")

    @app.post("/api/save-curated")
    def save_curated() -> Any:
        payload = request.get_json(silent=True) or {}
        mode = payload.get("mode")
        curated_payload = payload.get("curatedJson")

        if curated_payload is None:
            return jsonify({"ok": False, "message": "Missing curatedJson in request."}), 400

        try:
            if isinstance(curated_payload, str):
                curated_obj = json.loads(curated_payload)
            else:
                curated_obj = curated_payload
            if not isinstance(curated_obj, dict):
                raise ValueError("Curated JSON must be an object.")
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "message": f"Curated JSON parse error: {exc}"}), 400

        if storage.name == "blob":
            try:
                pdf_key = payload.get("pdfPath")
                if not pdf_key:
                    doc_name = str(curated_obj.get("document", {}).get("fileName", "document.pdf"))
                    pdf_key = doc_name
                target_key = storage.curated_key_for(pdf_key)
                return _save_curated_blob(storage, curated_obj, target_key)
            except ValueError as exc:
                return jsonify({"ok": False, "message": f"Save failed: {exc}"}), 400
            except Exception as exc:  # noqa: BLE001
                return jsonify({"ok": False, "message": f"Save failed with unexpected error: {exc}"}), 500

        base = storage.base

        try:
            if mode == "server":
                pdf_rel = payload.get("pdfPath")
                if not pdf_rel:
                    return jsonify({"ok": False, "message": "pdfPath is required for server mode."}), 400
                abs_target = storage._raw_path(storage.curated_key_for(pdf_rel))
                save_origin = "server-folder mode"
            elif mode == "picker":
                output_dir_raw = payload.get("outputDir") or str(base)
                output_dir = Path(output_dir_raw).expanduser().resolve()
                if not output_dir.exists() or not output_dir.is_dir():
                    return jsonify(
                        {
                            "ok": False,
                            "message": f"Picker save failed: output directory does not exist: {output_dir}",
                        }
                    ), 400

                output_file = payload.get("outputFile")
                if output_file:
                    filename = Path(output_file).name
                else:
                    doc_name = str(curated_obj.get("document", {}).get("fileName", "document.pdf"))
                    filename = f"{Path(doc_name).stem}.curated.json"

                if not filename.lower().endswith(".json"):
                    filename += ".json"

                abs_target = (output_dir / filename).resolve()
                save_origin = "file-picker mode"
            else:
                return jsonify({"ok": False, "message": "Invalid mode. Use 'server' or 'picker'."}), 400

            abs_target.parent.mkdir(parents=True, exist_ok=True)

            backup_path = None
            if abs_target.exists():
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup_path = abs_target.with_suffix(abs_target.suffix + f".{ts}.bak")
                shutil.copy2(abs_target, backup_path)

            curated_obj = _sanitize_curated_schema(curated_obj)
            curated_obj.setdefault("schema", "curated/1")
            curated_obj.setdefault("document", {})
            curated_obj["document"]["updatedAt"] = datetime.now(timezone.utc).isoformat()

            abs_target.write_text(json.dumps(curated_obj, ensure_ascii=False, indent=2), encoding="utf-8")

            msg = f"Saved curated JSON via {save_origin}: {abs_target}"
            if backup_path:
                msg += f" (backup created: {backup_path.name})"

            return jsonify(
                {
                    "ok": True,
                    "message": msg,
                    "savedPath": str(abs_target),
                    "backupPath": str(backup_path) if backup_path else None,
                }
            )

        except ValueError as exc:
            return jsonify({"ok": False, "message": f"Save failed: {exc}"}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "message": f"Save failed with unexpected error: {exc}"}), 500

    return app


def _save_curated_blob(storage: BlobStorage, curated_obj: Any, target_key: str):
    curated_obj = _sanitize_curated_schema(curated_obj)
    curated_obj.setdefault("schema", "curated/1")
    curated_obj.setdefault("document", {})
    curated_obj["document"]["updatedAt"] = datetime.now(timezone.utc).isoformat()

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_key = None
    if storage.exists(target_key):
        existing = storage.read_bytes(target_key)
        backup_key = f"{target_key}.{ts}.bak"
        storage.write_bytes(backup_key, existing)

    payload = json.dumps(curated_obj, ensure_ascii=False, indent=2).encode("utf-8")
    storage.write_bytes(target_key, payload)

    msg = f"Saved curated JSON to blob: {target_key}"
    if backup_key:
        msg += f" (backup created: {backup_key})"

    return jsonify(
        {
            "ok": True,
            "message": msg,
            "savedPath": target_key,
            "backupPath": backup_key,
        }
    )


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PDF review tool for Azure Content Understanding output. "
        "Uses Azure Blob Storage when connection string / account env vars are set, "
        "otherwise serves a local folder."
    )
    parser.add_argument(
        "--folder",
        default=".",
        help="Server-exposed folder for PDF/JSON discovery (local mode only).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", default=8000, type=int, help="Port to bind.")
    parser.add_argument("--debug", action="store_true", help="Run Flask in debug mode.")
    args = parser.parse_args()

    conn_str = next((os.environ.get(v) for v in STORAGE_CONNECTION_STR_ENV if os.environ.get(v)), None)
    account = next((os.environ.get(v) for v in STORAGE_ACCOUNT_ENV if os.environ.get(v)), None)

    if conn_str or account:
        storage = _storage_from_env()
    else:
        storage = LocalStorage(Path(args.folder))

    create_app(storage).run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
