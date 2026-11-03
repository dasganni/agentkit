"""Download routes for the agent CLI binaries built by scripts/cli-dist.sh."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response

TARGETS = frozenset(
    f"{goos}-{arch}" for goos in ("linux", "darwin", "windows") for arch in ("amd64", "arm64")
)


def cli_router(dist_dir: Path) -> APIRouter:
    """Serve `version`, `<os>-<arch>` (gzip, no extension) and `<os>-<arch>.sha256` from `dist_dir`.

    Every route answers 404 while the directory or the requested file is missing.
    """
    router = APIRouter(prefix="/cli")

    def existing(name: str) -> Path:
        path = dist_dir / name
        if not dist_dir.is_dir() or not path.is_file():
            raise HTTPException(404, "not found")
        return path

    @router.get("/version")
    async def version() -> Response:
        return PlainTextResponse(existing("version").read_text().strip())

    @router.get("/{name}")
    async def download(name: str) -> Response:
        target, is_sha = (
            (name.removesuffix(".sha256"), True) if name.endswith(".sha256") else (name, False)
        )
        if target not in TARGETS:
            raise HTTPException(404, "not found")
        if is_sha:
            return PlainTextResponse(existing(f"{target}.sha256").read_text().strip())
        return FileResponse(existing(target), media_type="application/gzip")

    return router
