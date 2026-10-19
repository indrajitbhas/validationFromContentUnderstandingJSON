from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request, send_file


def _extract_analysis_pages(data: Any) -> list | None:
    """Resolve the pages array from a Content Understanding result.

    Accepts both the bare form (top-level "pages") and the standard envelope
    ("kind": "document" with "document": { "pages": [...] }).
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
    return None


def _looks_like_content_understanding(data: Any) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "Top-level JSON must be an object."

    pages = _extract_analysis_pages(data)
    if not isinstance(pages, list):
        where = "top-level 'pages' or inside the 'document' object"
        found = sorted(str(k) for k in data.keys())
        return (
            False,
            f"Expected a Content Understanding document result with a pages array "
            f"({where}). Top-level keys found: {', '.join(found) or '(none)'}.",
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


def _safe_resolve(base_folder: Path, user_path: str) -> Path:
    candidate = (base_folder / user_path).resolve()
    base_resolved = base_folder.resolve()
    if base_resolved not in [candidate, *candidate.parents]:
        raise ValueError("Requested path is outside the server folder.")
    return candidate


def _curated_path_for_pdf(pdf_path: Path) -> Path:
    return pdf_path.with_suffix(".curated.json")


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
                    "status": "pending",
                    "note": "",
                }
            )

        curated_pages.append(
            {
                "pageNumber": int(page.get("pageNumber", len(curated_pages) + 1)),
                "kind": "blank" if len(normalized_words) == 0 else "unknown",
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


def create_app(base_folder: Path) -> Flask:
    app = Flask(__name__)
    app.config["BASE_FOLDER"] = base_folder.resolve()

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/api/config")
    def config() -> Any:
        return jsonify({"baseFolder": str(app.config["BASE_FOLDER"])})

    @app.get("/api/list-documents")
    def list_documents() -> Any:
        base: Path = app.config["BASE_FOLDER"]
        docs: list[dict[str, Any]] = []

        for pdf_path in sorted(base.rglob("*.pdf")):
            if not pdf_path.is_file():
                continue

            siblings = [
                Path(f"{pdf_path}.json"),
                pdf_path.with_suffix(".json"),
            ]
            json_path = next((s for s in siblings if s.exists() and s.is_file()), None)
            if json_path is None:
                continue

            curated_path = _curated_path_for_pdf(pdf_path)

            docs.append(
                {
                    "label": str(pdf_path.relative_to(base)),
                    "pdfPath": str(pdf_path.relative_to(base)),
                    "jsonPath": str(json_path.relative_to(base)),
                    "curatedPath": str(curated_path.relative_to(base)),
                    "curatedExists": curated_path.exists(),
                }
            )

        return jsonify({"documents": docs})

    @app.post("/api/load-server-document")
    def load_server_document() -> Any:
        payload = request.get_json(silent=True) or {}
        pdf_rel = payload.get("pdfPath")
        json_rel = payload.get("jsonPath")
        if not pdf_rel or not json_rel:
            return jsonify({"ok": False, "message": "pdfPath and jsonPath are required."}), 400

        base: Path = app.config["BASE_FOLDER"]
        try:
            pdf_path = _safe_resolve(base, pdf_rel)
            json_path = _safe_resolve(base, json_rel)
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400

        if not pdf_path.exists() or not pdf_path.is_file():
            return jsonify({"ok": False, "message": f"PDF not found: {pdf_rel}"}), 404
        if not json_path.exists() or not json_path.is_file():
            return jsonify({"ok": False, "message": f"JSON not found: {json_rel}"}), 404

        try:
            raw_text = json_path.read_text(encoding="utf-8")
            raw_obj = json.loads(raw_text)
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

        curated_path = _curated_path_for_pdf(pdf_path)
        if curated_path.exists():
            try:
                curated_obj = json.loads(curated_path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                return jsonify(
                    {
                        "ok": False,
                        "message": f"Existing curated JSON is invalid and could not be loaded: {curated_path.name}. Error: {exc}",
                    }
                ), 400
        else:
            curated_obj = _build_curated(raw_obj, pdf_path.name, json_path.name)

        return jsonify(
            {
                "ok": True,
                "mode": "server",
                "pdfUrl": f"/api/pdf?path={pdf_rel}",
                "pdfPath": pdf_rel,
                "jsonPath": json_rel,
                "curatedPath": str(curated_path.relative_to(base)),
                "rawJson": raw_obj,
                "curatedJson": curated_obj,
                "message": "Loaded PDF+JSON from server folder.",
            }
        )

    @app.get("/api/pdf")
    def get_pdf() -> Any:
        rel_path = request.args.get("path", "")
        if not rel_path:
            return jsonify({"ok": False, "message": "Missing 'path' query parameter."}), 400

        base: Path = app.config["BASE_FOLDER"]
        try:
            abs_path = _safe_resolve(base, rel_path)
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400

        if not abs_path.exists() or not abs_path.is_file() or abs_path.suffix.lower() != ".pdf":
            return jsonify({"ok": False, "message": "PDF file not found."}), 404

        return send_file(abs_path, mimetype="application/pdf")

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

        base: Path = app.config["BASE_FOLDER"]

        try:
            if mode == "server":
                pdf_rel = payload.get("pdfPath")
                if not pdf_rel:
                    return jsonify({"ok": False, "message": "pdfPath is required for server mode."}), 400
                pdf_path = _safe_resolve(base, pdf_rel)
                target_path = _curated_path_for_pdf(pdf_path)
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

                target_path = (output_dir / filename).resolve()
                save_origin = "file-picker mode"
            else:
                return jsonify({"ok": False, "message": "Invalid mode. Use 'server' or 'picker'."}), 400

            target_path.parent.mkdir(parents=True, exist_ok=True)

            backup_path = None
            if target_path.exists():
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup_path = target_path.with_suffix(target_path.suffix + f".{ts}.bak")
                shutil.copy2(target_path, backup_path)

            curated_obj.setdefault("schema", "curated/1")
            curated_obj.setdefault("document", {})
            curated_obj["document"]["updatedAt"] = datetime.now(timezone.utc).isoformat()

            target_path.write_text(json.dumps(curated_obj, ensure_ascii=False, indent=2), encoding="utf-8")

            msg = f"Saved curated JSON via {save_origin}: {target_path}"
            if backup_path:
                msg += f" (backup created: {backup_path.name})"

            return jsonify(
                {
                    "ok": True,
                    "message": msg,
                    "savedPath": str(target_path),
                    "backupPath": str(backup_path) if backup_path else None,
                }
            )

        except ValueError as exc:
            return jsonify({"ok": False, "message": f"Save failed: {exc}"}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "message": f"Save failed with unexpected error: {exc}"}), 500

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Local PDF review tool for Azure Content Understanding output.")
    parser.add_argument(
        "--folder",
        default=".",
        help="Server-exposed folder for PDF/JSON discovery (default: current working directory).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", default=8000, type=int, help="Port to bind.")
    parser.add_argument("--debug", action="store_true", help="Run Flask in debug mode.")
    args = parser.parse_args()

    base_folder = Path(args.folder).expanduser().resolve()
    if not base_folder.exists() or not base_folder.is_dir():
        raise SystemExit(f"Folder does not exist or is not a directory: {base_folder}")

    app = create_app(base_folder)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
