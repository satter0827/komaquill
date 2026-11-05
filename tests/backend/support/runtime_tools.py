"""Provide a deterministic bootstrap archive for transport and extraction tests."""

import gzip
import io
import tarfile


def archive() -> bytes:
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w") as bundle:
        member = tarfile.TarInfo("uv-x86_64-unknown-linux-gnu/uv")
        executable = b"verified test executable"
        member.size = len(executable)
        bundle.addfile(member, io.BytesIO(executable))
    return gzip.compress(content.getvalue(), mtime=0)
