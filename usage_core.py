"""Versioned, in-process bridge to the required Rust Claude batch reducer."""

from __future__ import annotations

import ctypes
import json
import platform
import sys
import sysconfig
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
ABI_VERSION = 1


class NativeCoreError(RuntimeError):
    """The native collector is unavailable or its private protocol is invalid."""


def platform_key() -> str:
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64", "i386": "x86", "i686": "x86"}.get(machine, machine)
    if sys.platform == "win32":
        # Python's build platform identifies emulated x64/32-bit interpreters;
        # platform.machine() can instead report the underlying ARM64 OS.
        machine = {"win-amd64": "x86_64", "win-arm64": "aarch64", "win32": "x86"}.get(
            sysconfig.get_platform(), machine
        )
    # Some Windows Python builds report the OS architecture, even under WOW64.
    bits = ctypes.sizeof(ctypes.c_void_p) * 8
    if machine == "x86_64" and bits == 32:
        machine = "x86"
    return f"{sys.platform}-{machine}-{bits}"


def library_name() -> str:
    if sys.platform == "win32":
        return "ai_usage_core.dll"
    if sys.platform == "darwin":
        return "libai_usage_core.dylib"
    if sys.platform.startswith("linux"):
        return "libai_usage_core.so"
    raise NativeCoreError(f"Unsupported native collector platform: {sys.platform}")


def library_path() -> Path:
    # Absolute, app-local lookup only. Never search PATH or the working directory.
    return HERE / "native" / "lib" / platform_key() / library_name()


@lru_cache(maxsize=1)
def _load_library():
    path = library_path()
    try:
        lib = ctypes.CDLL(str(path))
        lib.ai_usage_core_version.argtypes = []
        lib.ai_usage_core_version.restype = ctypes.c_uint32
        if lib.ai_usage_core_version() != ABI_VERSION:
            raise NativeCoreError("Native collector ABI mismatch; rebuild with python build_native.py")
        lib.ai_usage_core_reduce.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        lib.ai_usage_core_reduce.restype = ctypes.c_void_p
        lib.ai_usage_core_free.argtypes = [ctypes.c_void_p]
        lib.ai_usage_core_free.restype = None
        return lib
    except (OSError, AttributeError) as error:
        raise NativeCoreError(
            f"Native collector unavailable for {platform_key()}; run python build_native.py "
            "with Rust and Python for the same architecture"
        ) from error


def _native_integer(value: str) -> int:
    # Native BigInt sums can outgrow the decimal conversion limit even when
    # every JSONL input was accepted. Decode this private wire in bounded
    # chunks without changing the process-wide limit or the JSONL reader.
    if len(value) <= 640:
        return int(value)
    negative = value.startswith("-")
    digits = value[1:] if negative else value
    result = 0
    for offset in range(0, len(digits), 640):
        chunk = digits[offset:offset + 640]
        result = result * 10**len(chunk) + int(chunk)
    return -result if negative else result


def _request(payload: bytes) -> dict:
    lib = _load_library()
    pointer = lib.ai_usage_core_reduce(payload, len(payload))
    if not pointer:
        raise NativeCoreError("Native collector returned no response")
    try:
        raw = ctypes.string_at(pointer)
        response = json.loads(raw, parse_int=_native_integer)
    except (UnicodeError, ValueError) as error:
        raise NativeCoreError("Native collector returned invalid JSON") from error
    finally:
        # The library that allocated the response always frees it, even if decode fails.
        lib.ai_usage_core_free(pointer)
    if not isinstance(response, dict) or response.get("version") != ABI_VERSION:
        raise NativeCoreError("Native collector response version mismatch")
    if "error" in response:
        if response["error"] == "integer overflow":
            raise OverflowError("int too large to convert to float")
        if isinstance(response["error"], str) and response["error"].startswith("time error:"):
            kind, _, message = response["error"][len("time error:"):].partition(":")
            exception = {"ValueError": ValueError, "OverflowError": OverflowError, "OSError": OSError}.get(kind)
            if exception:
                raise exception(message)
        raise NativeCoreError(f"Native collector rejected batch: {response['error']}")
    result = response.get("ok")
    if not isinstance(result, dict):
        raise NativeCoreError("Native collector returned no result")
    return result


def aggregate_claude(records: list[dict], prices: dict, window_count: int, *, summary: bool = True) -> dict:
    result = _request(json.dumps({
        "version": ABI_VERSION, "summary": summary, "records": records,
        "prices": prices, "window_count": window_count,
    }, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    try:
        data = result["summary"]
        # Bucket pairs preserve encounter order, including float reduction order
        # in build_view and stable ordering of model values in the dashboard.
        for key in ("by_day", "by_model"):
            data[key] = dict(data[key])
            for bucket in data[key].values():
                bucket["cost"] = float(bucket["cost"])
                if "by_model" in bucket:
                    bucket["by_model"] = dict(bucket["by_model"])
        for row in [*data["projects"], *data["tools"]]:
            row["name"] = bytes.fromhex(row["name"]).decode("utf-8", errors="surrogatepass")
        for bucket in [data["totals"], *data["projects"]]:
            bucket["cost"] = float(bucket["cost"])
        data["burn"]["cost_last_60min"] = float(data["burn"]["cost_last_60min"])
        if len(result["windows"]) != window_count:
            raise ValueError("window count mismatch")
    except (KeyError, TypeError, ValueError) as error:
        raise NativeCoreError("Native collector returned an invalid batch result") from error
    return result
