"""Vendor the minimal Qt subset native/src/qt_dialogs.cpp needs.

The bridge links against the Qt DLLs Max already loads, so nothing from Qt is
shipped. Building needs only the headers qt_dialogs.cpp includes and import
libraries for the symbols it calls. This script downloads the official Qt
builds (the oldest Qt of each major version Max ships), compiles
qt_dialogs.cpp against them, copies exactly the included headers into
native/third_party/qt/<version>/include, and writes one .def file per Qt DLL.
CMake turns the .def files into import libraries.

Run again after changing qt_dialogs.cpp's Qt includes or Qt calls:
    .venv\\Scripts\\python.exe scripts\\vendor_qt.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native" / "src" / "qt_dialogs.cpp"
CACHE = ROOT / "local" / "qt-sdk"
OUT = ROOT / "native" / "third_party" / "qt"
VSWHERE = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")

# 3ds Max 2021 使用 Qt 5.12.5，2023/2024 使用 Qt 5.15.1，
# 2025/2026 使用 Qt 6.5.3，2027 使用 Qt 6.8.3。
# Qt 在同一大版本内保持二进制兼容，因此 6.5.3 可覆盖 2025 及以后版本。
# 3ds Max 2027 使用 C++20，2021-2026 使用 C++17。
VERSIONS = {
    "5.12.5": {
        "major": 5,
        "package": "win64_msvc2017_64",
        "install_dir": "msvc2017_64",
        "standards": ["/std:c++17"],
    },
    "5.15.1": {
        "major": 5,
        "package": "win64_msvc2019_64",
        "install_dir": "msvc2019_64",
        "standards": ["/std:c++17"],
    },
    "6.5.3": {
        "major": 6,
        "package": "win64_msvc2019_64",
        "install_dir": "msvc2019_64",
        "standards": ["/std:c++17", "/std:c++20"],
    },
}
MODULES = ("Core", "Gui", "Widgets")
# Mirrors native/CMakeLists.txt for qt_dialogs.cpp. Optimized and unoptimized
# builds reference different symbols, so both are collected.
FLAGS = ["/nologo", "/c", "/EHa", "/GR", "/MD", "/Zc:__cplusplus", "/permissive-", "/showIncludes",
         "/DUNICODE", "/D_UNICODE", "/DNOMINMAX", "/DWIN32", "/D_WIN32", "/DNDEBUG", "/DQT_NO_VERSION_TAGGING"]
OPTIMIZATION = ["/O2", "/Od"]


def find_vcvars() -> Path:
    configured = os.environ.get("VCVARS", "").strip()
    if configured:
        path = Path(configured)
        if path.is_file():
            return path
        raise RuntimeError(f"VCVARS points to a missing file: {path}")

    if VSWHERE.is_file():
        result = subprocess.run(
            [str(VSWHERE), "-latest", "-products", "*", "-requires",
             "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        installation = result.stdout.strip()
        if installation:
            path = Path(installation) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
            if path.is_file():
                return path

    raise RuntimeError("Cannot find Visual Studio C++ tools; set VCVARS to vcvars64.bat")


VCVARS = find_vcvars()


def sdk_dir(version: str, config: dict) -> Path:
    path = CACHE / version / config["install_dir"]
    if not (path / "include" / "QtWidgets").is_dir():
        CACHE.mkdir(parents=True, exist_ok=True)
        subprocess.run(["uvx", "--from", "aqtinstall", "aqt", "install-qt", "windows", "desktop", version,
                        config["package"], "--archives", "qtbase", "-O", str(CACHE)], check=True)
    return path


def run_vs(command: list[str], cwd: Path) -> str:
    line = subprocess.list2cmdline(command)
    # 中文版 MSVC 会把 /showIncludes 翻译成中文，强制英文输出才能稳定解析依赖头文件。
    result = subprocess.run(f'set "VSLANG=1033" && call "{VCVARS}" >nul && {line}', shell=True, cwd=cwd,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed:\n{result.stdout}\n{result.stderr}")
    # MSVC 可能把 /showIncludes 备注写到 stderr，依赖头文件必须同时解析两路输出。
    return result.stdout + "\n" + result.stderr


def exported(lib: Path, work: Path) -> dict[str, str]:
    """Map each symbol of an import library to its import type: code or data."""
    names: dict[str, str] = {}
    symbol = None
    for line in run_vs(["dumpbin", "/headers", str(lib)], work).splitlines():
        line = line.strip()
        if line.startswith("Symbol name"):
            symbol = line.split(":", 1)[1].split()[0]
        elif line.startswith("Type") and symbol:
            names[symbol] = line.split(":", 1)[1].strip()
            symbol = None
    return names


def vendor(version: str, config: dict) -> None:
    sdk = sdk_dir(version, config)
    include_root = (sdk / "include").resolve()
    headers: set[Path] = set()
    imported: set[str] = set()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for standard, optimization in [(s, o) for s in config["standards"] for o in OPTIMIZATION]:
            obj = work / "qt_dialogs.obj"
            output = run_vs(["cl", *FLAGS, standard, optimization, f"/I{ROOT / 'native' / 'include'}",
                             f"/I{ROOT / 'native' / 'third_party'}", f"/I{include_root}",
                             f"/Fo{obj}", str(SOURCE)], work)
            for line in output.splitlines():
                for marker in ("Note: including file:", "注意: 包含文件:", "注意：包含文件："):
                    if marker not in line:
                        continue
                    path = Path(line.split(marker, 1)[1].strip()).resolve()
                    if path.is_relative_to(include_root):
                        headers.add(path)
                    break
            for line in run_vs(["dumpbin", "/symbols", str(obj)], work).splitlines():
                if "UNDEF" in line and "External" in line and "| " in line:
                    imported.add(line.split("| ", 1)[1].split()[0].removeprefix("__imp_"))

        target = OUT / version
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        for header in sorted(headers):
            dest = target / "include" / header.relative_to(include_root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(header, dest)

        assigned: set[str] = set()
        for module in MODULES:
            dll = f"Qt{config['major']}{module}"
            types = exported(sdk / "lib" / f"{dll}.lib", work)
            symbols = sorted(imported & types.keys())
            assigned.update(symbols)
            if symbols:
                body = "".join(f"    {name}{' DATA' if types[name] == 'data' else ''}\n" for name in symbols)
                (target / f"{dll}.def").write_text(f"LIBRARY {dll}.dll\nEXPORTS\n{body}", encoding="ascii")
    unknown = sorted(name for name in imported - assigned if "Q" in name and "std@@" not in name)
    if unknown:
        raise RuntimeError(f"Qt {version}: imports not found in Qt{MODULES}: {unknown[:10]}")
    (target / "README.md").write_text(
        f"Minimal Qt {version} subset for `native/src/qt_dialogs.cpp`, generated by\n"
        "`scripts/vendor_qt.py` from the official Qt build. The bridge links against the\n"
        "Qt DLLs that 3ds Max already loads; no Qt binary is shipped.\n\n"
        "Qt is available under the GNU LGPL v3 (https://www.gnu.org/licenses/lgpl-3.0.html).\n",
        encoding="ascii")
    print(f"Qt {version}: {len(headers)} headers, {len(assigned)} imported symbols")


def main() -> int:
    for version, config in VERSIONS.items():
        vendor(version, config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
