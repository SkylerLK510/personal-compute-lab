"""Read-only, opt-in hardware diagnostic. Prints one JSON document to stdout.

With no flags it runs NO external commands: OS, architecture, CPU count, RAM and
which relevant executables are on PATH all come from the standard library.

Opt-in sections each run a small fixed set of commands, with a timeout, no shell,
and no stdin:  --nvidia (nvidia-smi), --wsl (wsl.exe), --macos (sysctl,
system_profiler), or --all.

It never installs anything, never opens a network connection, never starts a GPU
workload and never writes a file. It does not report serial numbers, hardware UUIDs,
the hostname, the user name, environment variables or raw command output: every
optional section copies an allowlist of fields, and the finished report is scrubbed
for the user name, home directory and hostname before printing.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import getpass
import io
import json
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import sys
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_S = 10.0
MAX_TIMEOUT_S = 60.0
REDACTED = '[redacted]'

# Presence on PATH only. Nothing here is executed unless its section is opted into.
EXECUTABLES = (
    'python', 'python3', 'py', 'pip', 'uv', 'conda', 'git', 'cmake', 'ninja',
    'nvidia-smi', 'nvcc', 'wsl', 'docker', 'ollama',
    'llama-cli', 'llama-server', 'rpc-server', 'ggml-rpc-server',
    'sysctl', 'system_profiler',
)

MAC_HARDWARE_FIELDS = ('chip_type', 'machine_model', 'machine_name', 'number_processors', 'physical_memory')
MAC_GPU_FIELDS = ('sppci_model', 'sppci_cores', 'sppci_device_type', 'spdisplays_mtlgpufamilysupport')
NVIDIA_FIELDS = ('name', 'memory.total', 'driver_version', 'compute_cap')


# --- bounded command runner ---------------------------------------------------------

def run_bounded(command, timeout_s=DEFAULT_TIMEOUT_S, encoding='utf-8'):
    """Run a fixed argv with a timeout. Never raises; never uses a shell.

    Returns {'status': 'ok'|'missing'|'timeout'|'failed'|'error', ...}. stdout is
    returned to the caller for parsing and must never be copied into the report.
    """
    exe = shutil.which(command[0])
    if exe is None:
        return {'status': 'missing', 'stdout': ''}
    try:
        done = subprocess.run(
            [exe, *command[1:]], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=timeout_s, shell=False, check=False)
    except subprocess.TimeoutExpired:
        return {'status': 'timeout', 'stdout': ''}
    except OSError as exc:  # permission denied, bad format, vanished binary
        return {'status': 'error', 'error': type(exc).__name__, 'stdout': ''}
    text = decode_output(done.stdout, encoding)
    status = 'ok' if done.returncode == 0 else 'failed'
    return {'status': status, 'returncode': done.returncode, 'stdout': text}


def decode_output(raw, encoding='utf-8'):
    """wsl.exe writes UTF-16LE, sometimes with a BOM; everything else is UTF-8-ish."""
    if raw.startswith(b'\xff\xfe') or (len(raw) >= 4 and raw[1:2] == b'\x00' and raw[3:4] == b'\x00'):
        return raw.decode('utf-16-le', errors='replace').lstrip('﻿')
    return raw.decode(encoding, errors='replace').replace('\x00', '')


# --- privacy ------------------------------------------------------------------------

_HOME_PATTERNS = (
    re.compile(r'^(/Users/|/home/)[^/]+'),
    re.compile(r'^[A-Za-z]:\\Users\\[^\\]+', re.IGNORECASE),
)


def redact_path(path, home=None):
    """Replace the home-directory prefix (which contains the user name) with '~'."""
    if not path:
        return path
    text = str(path)
    home = str(home if home is not None else Path.home())
    if home and home not in ('/', '\\') and text.lower().startswith(home.lower()):
        return '~' + text[len(home):]
    for pattern in _HOME_PATTERNS:
        if pattern.match(text):
            return pattern.sub('~', text, count=1)
    return text


def sensitive_needles():
    """Strings that must not appear in a report: user name, home path, hostname."""
    found = set()
    for getter in (getpass.getuser, lambda: str(Path.home()), lambda: Path.home().name,
                   socket.gethostname, lambda: os.environ.get('COMPUTERNAME', '')):
        try:
            value = getter()
        except Exception:  # no passwd entry, no HOME, no resolver: nothing to leak
            continue
        if value:
            found.add(value)
            found.add(value.split('.')[0])
    # Very short needles ("me", "pc") would shred ordinary words.
    return sorted((n for n in found if len(n) >= 3), key=len, reverse=True)


def scrub(value, needles):
    """Recursively replace any needle (case-insensitive) in every string and key."""
    if isinstance(value, str):
        for needle in needles:
            value = re.sub(re.escape(needle), REDACTED, value, flags=re.IGNORECASE)
        return value
    if isinstance(value, dict):
        return {scrub(k, needles): scrub(v, needles) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, needles) for v in value]
    return value


# --- base section: standard library only, no subprocess -----------------------------
# platform.architecture(), platform.processor() and, on some Windows builds,
# platform.uname() quietly spawn `file`, `uname -p` or `cmd /c ver`. The default run
# promises to start no process, so OS facts come from sys / os / struct directly.

def system_name():
    if sys.platform == 'win32':
        return 'Windows'
    if sys.platform == 'darwin':
        return 'Darwin'
    return 'Linux' if sys.platform.startswith('linux') else sys.platform


def os_facts():
    if sys.platform == 'win32':
        win = sys.getwindowsversion()
        return {'release': f'{win.major}.{win.minor}', 'version': f'build {win.build}',
                'machine': os.environ.get('PROCESSOR_ARCHITECTURE')}
    uname = os.uname()
    return {'release': uname.release, 'version': uname.version, 'machine': uname.machine}


def total_ram_bytes():
    """Physical RAM, or None. sysconf on POSIX, GlobalMemoryStatusEx on Windows."""
    try:
        if sys.platform == 'win32':
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong)] + [
                    (name, ctypes.c_ulonglong) for name in (
                        'ullTotalPhys', 'ullAvailPhys', 'ullTotalPageFile', 'ullAvailPageFile',
                        'ullTotalVirtual', 'ullAvailVirtual', 'ullAvailExtendedVirtual')]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
            return None
        return int(os.sysconf('SC_PHYS_PAGES')) * int(os.sysconf('SC_PAGE_SIZE'))
    except (AttributeError, OSError, ValueError):
        return None


def inside_wsl(proc_version='/proc/version'):
    try:
        return 'microsoft' in Path(proc_version).read_text(errors='replace').lower()
    except OSError:
        return False


def probe_executables(names=EXECUTABLES, which=shutil.which):
    found = {}
    for name in names:
        path = which(name)
        found[name] = {'found': path is not None, 'path': redact_path(path) if path else None}
    return found


def probe_base():
    ram = total_ram_bytes()
    facts = os_facts()
    return {
        'os': {
            'system': system_name(), 'release': facts['release'],
            'version': facts['version'], 'inside_wsl': inside_wsl(),
        },
        'architecture': {'machine': facts['machine'], 'pointer_bits': struct.calcsize('P') * 8},
        'cpu': {'logical_cores': os.cpu_count()},
        'ram': {'total_bytes': ram, 'total_gib': round(ram / 2**30, 2) if ram else None},
        'python': {'version': platform.python_version(), 'implementation': platform.python_implementation()},
        'executables': probe_executables(),
    }


# --- opt-in: NVIDIA -----------------------------------------------------------------

def parse_nvidia_csv(text, fields):
    gpus = []
    for row in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in row]
        if len(cells) != len(fields) or not any(cells):
            continue
        gpu = dict(zip(fields, cells))
        if 'memory.total' in gpu:
            gpu['memory_total_mib'] = int(gpu.pop('memory.total')) if gpu['memory.total'].isdigit() else None
        gpus.append(gpu)
    return gpus


def probe_nvidia(timeout_s=DEFAULT_TIMEOUT_S, run=run_bounded):
    # Older drivers reject compute_cap; retry without it rather than report nothing.
    for fields in (NVIDIA_FIELDS, NVIDIA_FIELDS[:-1]):
        result = run(['nvidia-smi', f'--query-gpu={",".join(fields)}', '--format=csv,noheader,nounits'], timeout_s)
        if result['status'] != 'failed':
            break
    section = {'status': result['status']}
    if result['status'] != 'ok':
        return section
    section['gpus'] = parse_nvidia_csv(result['stdout'], fields)
    # The plain banner is the only place the driver's CUDA version appears. It also
    # lists running process names, so keep the one regex match and drop the rest.
    # Newer drivers (seen on 616.56) label it "CUDA UMD Version:" instead.
    banner = run(['nvidia-smi'], timeout_s)
    match = re.search(r'CUDA(?: UMD)? Version:\s*([0-9.]+)', banner.get('stdout', ''))
    section['cuda_version'] = match.group(1) if match else None
    return section


# --- opt-in: WSL (from Windows) -----------------------------------------------------

def parse_wsl_list(text):
    """Parse `wsl --list --verbose`: NAME STATE VERSION, '*' marks the default."""
    distros = []
    for line in text.splitlines()[1:]:
        default = line.lstrip().startswith('*')
        parts = line.replace('*', ' ', 1).split()
        if len(parts) >= 3 and parts[-1].isdigit():
            distros.append({'name': ' '.join(parts[:-2]), 'state': parts[-2],
                            'wsl_version': int(parts[-1]), 'default': default})
    return distros


def probe_wsl(timeout_s=DEFAULT_TIMEOUT_S, run=run_bounded):
    if system_name() != 'Windows':
        return {'status': 'not_applicable', 'inside_wsl': inside_wsl()}
    listing = run(['wsl', '--list', '--verbose'], timeout_s)
    section = {'status': listing['status']}
    if listing['status'] == 'ok':
        section['distros'] = parse_wsl_list(listing['stdout'])
    status = run(['wsl', '--status'], timeout_s)
    section['status_command'] = status['status']
    # Output is localised; the default version is the one number worth extracting.
    match = re.search(r'[Vv]ersion\D{0,20}([12])\b', status.get('stdout', ''))
    section['default_wsl_version'] = int(match.group(1)) if match else None
    return section


# --- opt-in: macOS ------------------------------------------------------------------

def _walk_values(node, key):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, str):
                yield v
            else:
                yield from _walk_values(v, key)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_values(item, key)


def _profiler_json(data_type, timeout_s, run):
    result = run(['system_profiler', data_type, '-json'], timeout_s)
    if result['status'] != 'ok':
        return result['status'], []
    try:
        entries = json.loads(result['stdout']).get(data_type, [])
    except (json.JSONDecodeError, AttributeError):
        return 'unparseable', []
    return 'ok', entries if isinstance(entries, list) else []


def probe_macos(timeout_s=DEFAULT_TIMEOUT_S, run=run_bounded):
    if system_name() != 'Darwin':
        return {'status': 'not_applicable'}
    section = {'status': 'ok', 'commands': {}}

    # system_profiler also returns serial_number, platform_UUID and provisioning_UDID:
    # copy an allowlist, never the entry.
    status, entries = _profiler_json('SPHardwareDataType', timeout_s, run)
    section['commands']['SPHardwareDataType'] = status
    section['hardware'] = {f: entries[0].get(f) for f in MAC_HARDWARE_FIELDS} if entries else None

    status, entries = _profiler_json('SPDisplaysDataType', timeout_s, run)
    section['commands']['SPDisplaysDataType'] = status
    section['gpus'] = [{f: e.get(f) for f in MAC_GPU_FIELDS} for e in entries if isinstance(e, dict)]

    status, entries = _profiler_json('SPThunderboltDataType', timeout_s, run)
    section['commands']['SPThunderboltDataType'] = status
    section['thunderbolt_link_speeds'] = sorted(set(_walk_values(entries, 'current_speed_key')))

    sysctl = {}
    for name in ('machdep.cpu.brand_string', 'hw.memsize', 'hw.perflevel0.physicalcpu',
                 'hw.perflevel1.physicalcpu', 'iogpu.wired_limit_mb'):
        result = run(['sysctl', '-n', name], timeout_s)
        value = result['stdout'].strip() if result['status'] == 'ok' else None
        sysctl[name] = int(value) if value and value.isdigit() else value
    section['sysctl'] = sysctl
    if any(s != 'ok' for s in section['commands'].values()):
        section['status'] = 'partial'
    return section


# --- report and CLI -----------------------------------------------------------------

def build_report(nvidia=False, wsl=False, macos=False, timeout_s=DEFAULT_TIMEOUT_S, run=run_bounded):
    report = {
        'schema_version': SCHEMA_VERSION,
        'generated_at_utc': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'sections_run': ['base'] + [n for n, on in (('nvidia', nvidia), ('wsl', wsl), ('macos', macos)) if on],
        'timeout_seconds': timeout_s,
        **probe_base(),
    }
    if nvidia:
        report['nvidia'] = probe_nvidia(timeout_s, run)
    if wsl:
        report['wsl'] = probe_wsl(timeout_s, run)
    if macos:
        report['macos'] = probe_macos(timeout_s, run)
    return scrub(report, sensitive_needles())


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Read-only hardware probe. Runs no external commands unless a section is opted into.')
    parser.add_argument('--nvidia', action='store_true', help='run nvidia-smi (name, VRAM, driver, compute capability)')
    parser.add_argument('--wsl', action='store_true', help='on Windows, run wsl --list --verbose and wsl --status')
    parser.add_argument('--macos', action='store_true', help='on macOS, run sysctl and system_profiler (allowlisted fields)')
    parser.add_argument('--all', action='store_true', help='all optional sections; ones that do not apply report so')
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT_S,
                        help=f'per-command timeout in seconds (default {DEFAULT_TIMEOUT_S:g}, max {MAX_TIMEOUT_S:g})')
    parser.add_argument('--pretty', action='store_true', help='indent the JSON')
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= MAX_TIMEOUT_S:
        parser.error(f'--timeout must be in (0, {MAX_TIMEOUT_S:g}]')
    return args


def main(argv=None, out=None):
    args = parse_args(argv)
    report = build_report(nvidia=args.nvidia or args.all, wsl=args.wsl or args.all,
                          macos=args.macos or args.all, timeout_s=args.timeout)
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True), file=out or sys.stdout)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
