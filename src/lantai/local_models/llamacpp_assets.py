"""Reviewed llama.cpp asset pins. Network metadata never grants install trust.

SHA256: https://github.com/ggml-org/llama.cpp/releases/expanded_assets/b8744
Exact sizes: official release-asset HEAD responses, verified 2026-10-04.
These pins bind bytes to the reviewed HTTPS release; they are not signatures.
New versions/platforms require a separately reviewed catalog update.
"""
from __future__ import annotations

from dataclasses import dataclass

OFFICIAL_RELEASE_BASE = "https://github.com/ggml-org/llama.cpp/releases/download"


@dataclass(frozen=True)
class TrustedLlamaCppAsset:
    tag: str
    os_name: str
    arch: str
    backend: str
    cuda_version: str | None
    file_name: str
    url: str
    sha256: str
    size_bytes: int


TRUSTED_LLAMA_CPP_ASSETS: tuple[TrustedLlamaCppAsset, ...] = (
    TrustedLlamaCppAsset(
        'b8744',
        'macos',
        'x64',
        'cpu',
        None,
        'llama-b8744-bin-macos-x64.tar.gz',
        'https://github.com/ggml-org/llama.cpp/releases/download/b8744/llama-b8744-bin-macos-x64.tar.gz',
        '6215dc2cb105a6577167b3458b1add4bdde9bf06c9bc064312c541674f0bb435',
        40654119,
    ),
    TrustedLlamaCppAsset(
        'b8744',
        'macos',
        'arm64',
        'cpu',
        None,
        'llama-b8744-bin-macos-arm64.tar.gz',
        'https://github.com/ggml-org/llama.cpp/releases/download/b8744/llama-b8744-bin-macos-arm64.tar.gz',
        '4d6db38e68993c0f338a52f5ae6887f5b18c5b2c602c488470aa52e8ca96021f',
        40582913,
    ),
    TrustedLlamaCppAsset(
        'b8744',
        'linux',
        'x64',
        'cpu',
        None,
        'llama-b8744-bin-ubuntu-x64.tar.gz',
        'https://github.com/ggml-org/llama.cpp/releases/download/b8744/llama-b8744-bin-ubuntu-x64.tar.gz',
        '00f649740be0a5364e011053b874fe76dbda105629c087d024e221f963a93677',
        31974603,
    ),
    TrustedLlamaCppAsset(
        'b8744',
        'linux',
        'arm64',
        'cpu',
        None,
        'llama-b8744-bin-ubuntu-arm64.tar.gz',
        'https://github.com/ggml-org/llama.cpp/releases/download/b8744/llama-b8744-bin-ubuntu-arm64.tar.gz',
        'a4d50fe88828beba8ffc57b19fc747e82ed3b4a58f1c973dc60f1637a376e4d3',
        28165115,
    ),
    TrustedLlamaCppAsset(
        'b8744',
        'windows',
        'x64',
        'cpu',
        None,
        'llama-b8744-bin-win-cpu-x64.zip',
        'https://github.com/ggml-org/llama.cpp/releases/download/b8744/llama-b8744-bin-win-cpu-x64.zip',
        '631ba5afa627de5de8ce3dcfd03153cf974fb2dc31ba459076cf0948fdfb39f5',
        39669314,
    ),
    TrustedLlamaCppAsset(
        'b8744',
        'windows',
        'arm64',
        'cpu',
        None,
        'llama-b8744-bin-win-cpu-arm64.zip',
        'https://github.com/ggml-org/llama.cpp/releases/download/b8744/llama-b8744-bin-win-cpu-arm64.zip',
        'f2c09ab011133f9187eddfc45f6ed43a8bc49f9213a0381ae5c3a3bc43b3e7ca',
        32452420,
    ),
    TrustedLlamaCppAsset(
        'b8744',
        'windows',
        'x64',
        'cuda',
        '12.4',
        'llama-b8744-bin-win-cuda-12.4-x64.zip',
        'https://github.com/ggml-org/llama.cpp/releases/download/b8744/llama-b8744-bin-win-cuda-12.4-x64.zip',
        'fcca7c42a8b5a3a820c9dc5638223ffcfea9a1ca8ebe5d7e166da6af8126b033',
        252783509,
    ),
    TrustedLlamaCppAsset(
        'b8744',
        'windows',
        'x64',
        'cuda',
        '13.1',
        'llama-b8744-bin-win-cuda-13.1-x64.zip',
        'https://github.com/ggml-org/llama.cpp/releases/download/b8744/llama-b8744-bin-win-cuda-13.1-x64.zip',
        '631b308eeb4bca28c86a64efaa87dff36d36161ae8306d3dc3bc4e9533100d39',
        168897611,
    ),
)
