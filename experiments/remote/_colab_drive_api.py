# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any

import requests


DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveApiError(RuntimeError):
    pass


def _escape_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveApi:
    def __init__(self, access_token: str, *, timeout: int = 120) -> None:
        if not access_token.strip():
            raise DriveApiError("Google Drive access token is empty.")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {access_token.strip()}"})

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        response = self.session.request(method, url, **kwargs)
        if response.status_code >= 400:
            body = response.text[-2000:]
            raise DriveApiError(f"Drive API {method} {response.status_code}: {body}")
        return response

    def list_children(self, parent_id: str, *, name: str | None = None) -> list[dict[str, Any]]:
        clauses = [f"'{_escape_query(parent_id)}' in parents", "trashed=false"]
        if name is not None:
            clauses.append(f"name='{_escape_query(name)}'")
        params = {
            "q": " and ".join(clauses),
            "fields": "nextPageToken,files(id,name,mimeType,size,md5Checksum,modifiedTime,parents)",
            "pageSize": 1000,
            "orderBy": "name",
        }
        rows: list[dict[str, Any]] = []
        while True:
            response = self._request("GET", f"{DRIVE_API}/files", params=params)
            payload = response.json()
            rows.extend(payload.get("files") or [])
            token = payload.get("nextPageToken")
            if not token:
                return rows
            params["pageToken"] = token

    def unique_child(
        self,
        parent_id: str,
        name: str,
        *,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        rows = self.list_children(parent_id, name=name)
        if mime_type is not None:
            rows = [row for row in rows if row.get("mimeType") == mime_type]
        if len(rows) != 1:
            raise DriveApiError(
                f"Expected one Drive child {name!r} under {parent_id}, found {len(rows)}."
            )
        return rows[0]

    def resolve_path(self, parts: list[str], *, start_id: str = "root") -> dict[str, Any]:
        current = {"id": start_id, "name": "root", "mimeType": FOLDER_MIME}
        for index, name in enumerate(parts):
            expected = FOLDER_MIME if index < len(parts) - 1 else None
            current = self.unique_child(str(current["id"]), name, mime_type=expected)
        return current

    def ensure_folder(self, parent_id: str, name: str) -> dict[str, Any]:
        rows = [
            row
            for row in self.list_children(parent_id, name=name)
            if row.get("mimeType") == FOLDER_MIME
        ]
        if len(rows) > 1:
            raise DriveApiError(f"Duplicate Drive folder {name!r} under {parent_id}.")
        if rows:
            return rows[0]
        response = self._request(
            "POST",
            f"{DRIVE_API}/files",
            params={"fields": "id,name,mimeType,parents"},
            json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        )
        return response.json()

    def download_file(self, file_id: str, destination: Path, *, expected: dict[str, Any] | None = None) -> dict[str, Any]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + ".part")
        temp.unlink(missing_ok=True)
        digest = hashlib.md5()
        total = 0
        try:
            with self._request(
                "GET",
                f"{DRIVE_API}/files/{file_id}",
                params={"alt": "media"},
                stream=True,
                timeout=max(self.timeout, 600),
            ) as response, temp.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=8 * 1024 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
            if expected:
                expected_size = int(expected.get("size") or 0)
                expected_md5 = str(expected.get("md5Checksum") or "")
                if expected_size and total != expected_size:
                    raise DriveApiError(f"Drive download size mismatch: {total} != {expected_size}")
                if expected_md5 and digest.hexdigest() != expected_md5:
                    raise DriveApiError("Drive download MD5 mismatch.")
            temp.replace(destination)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        return {"path": str(destination), "bytes": total, "md5": digest.hexdigest()}

    def download_bytes(self, file_id: str, *, max_bytes: int = 8 * 1024 * 1024) -> bytes:
        response = self._request(
            "GET",
            f"{DRIVE_API}/files/{file_id}",
            params={"alt": "media"},
            timeout=max(self.timeout, 300),
        )
        if len(response.content) > max_bytes:
            raise DriveApiError(f"Drive object exceeds in-memory limit: {len(response.content)}")
        return response.content

    def upload_file(
        self,
        source: Path,
        *,
        parent_id: str | None = None,
        name: str | None = None,
        existing_id: str | None = None,
        chunk_size: int = 32 * 1024 * 1024,
    ) -> dict[str, Any]:
        if not source.is_file():
            raise DriveApiError(f"Upload source is missing: {source}")
        total = source.stat().st_size
        mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        metadata: dict[str, Any] = {"name": name or source.name}
        if existing_id is None:
            if not parent_id:
                raise DriveApiError("parent_id is required for a new Drive file.")
            metadata["parents"] = [parent_id]
            method = "POST"
            url = f"{DRIVE_UPLOAD_API}/files"
        else:
            method = "PATCH"
            url = f"{DRIVE_UPLOAD_API}/files/{existing_id}"
        response = self._request(
            method,
            url,
            params={"uploadType": "resumable", "fields": "id,name,size,md5Checksum,parents"},
            headers={
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(total),
                "Content-Type": "application/json; charset=UTF-8",
            },
            data=json.dumps(metadata, ensure_ascii=False).encode("utf-8"),
        )
        location = response.headers.get("Location")
        if not location:
            raise DriveApiError("Drive resumable upload did not return a Location header.")

        if total == 0:
            final = self._request(
                "PUT",
                location,
                headers={"Content-Length": "0", "Content-Range": "bytes */0"},
                data=b"",
            )
            return final.json()

        offset = 0
        with source.open("rb") as handle:
            while offset < total:
                chunk = handle.read(min(chunk_size, total - offset))
                end = offset + len(chunk) - 1
                upload = self.session.put(
                    location,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Type": mime_type,
                        "Content-Range": f"bytes {offset}-{end}/{total}",
                    },
                    data=chunk,
                    timeout=max(self.timeout, 600),
                )
                if upload.status_code in (200, 201):
                    return upload.json()
                if upload.status_code != 308:
                    raise DriveApiError(
                        f"Drive upload failed at {offset}: HTTP {upload.status_code} {upload.text[-2000:]}"
                    )
                offset = end + 1
        raise DriveApiError("Drive resumable upload ended without a final response.")

    def delete_file(self, file_id: str) -> None:
        self._request("DELETE", f"{DRIVE_API}/files/{file_id}")
