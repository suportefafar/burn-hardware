#!/usr/bin/env python3
"""Controlador de estresse de CPU, RAM, disco e GPU (Linux, Python 3.10+).

O valor de --percent é usado como intensidade: CPU/GPU alternam períodos
ativo/ocioso e a RAM ocupa uma fração com reserva configurável. Ctrl-C
encerra os workers e remove o arquivo temporário do teste de disco.
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Optional


RESOURCES = ("cpu", "memory", "disk", "gpu")


def reset_worker_signals() -> None:
    """Workers devem ser terminados pelo pai com SIGTERM padrão."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def percentage(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("a porcentagem deve ser um inteiro") from exc
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("a porcentagem deve estar entre 1 e 100")
    return number


def duration(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("a duração deve ser um número em segundos") from exc
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("a duração deve ser um número finito não negativo")
    return number


def positive_interval(value: str) -> float:
    number = duration(value)
    if number == 0:
        raise argparse.ArgumentTypeError("o intervalo deve ser maior que zero")
    return number


def resources(value: str) -> set[str]:
    selected = {item.strip().lower() for item in value.split(",") if item.strip()}
    invalid = selected.difference(RESOURCES)
    if invalid or not selected:
        names = ", ".join(RESOURCES)
        raise argparse.ArgumentTypeError(f"recursos inválidos: {', '.join(sorted(invalid))}; use {names}")
    return selected


def meminfo() -> tuple[int, int]:
    """Retorna (memória total, memória disponível), em bytes."""
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            fields = rest.split()
            if fields:
                values[key] = int(fields[0]) * (1024 if len(fields) > 1 and fields[1].lower() == "kb" else 1)
    except (OSError, ValueError):
        return 0, 0
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    # /proc/meminfo mostra a RAM do host, mesmo sob um limite de contêiner.
    for limit_path, current_path in (
        (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory.current")),
        (Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"), Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")),
    ):
        try:
            limit = int(limit_path.read_text().strip())
            current = int(current_path.read_text().strip())
        except (OSError, ValueError):
            continue
        if 0 < limit < total:
            total = limit
            available = min(available, max(0, limit - current))
            break
    return total, available


def memory_target(percent: int, reserve_mib: int) -> int:
    total, available = meminfo()
    reserve = max(0, reserve_mib) * 1024 * 1024
    # A porcentagem representa a utilização total desejada, descontando o que
    # já está em uso. Mantemos uma reserva para o sistema não entrar em OOM.
    desired_extra = max(0, int(total * percent / 100) - (total - available))
    return min(desired_extra, max(0, available - reserve))


def cpu_temperature() -> Optional[float]:
    readings: list[float] = []
    for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            name = (hwmon / "name").read_text().strip().lower()
        except OSError:
            continue
        cpu_sensor = any(token in name for token in ("k10temp", "zenpower", "coretemp", "cpu"))
        for input_path in hwmon.glob("temp*_input"):
            try:
                temp = int(input_path.read_text().strip()) / 1000.0
            except (OSError, ValueError):
                continue
            label = ""
            try:
                label = (input_path.with_name(input_path.name.replace("_input", "_label"))).read_text().lower()
            except OSError:
                pass
            if cpu_sensor or any(token in label for token in ("tctl", "tdie", "package", "cpu")):
                if 0 < temp < 130:
                    readings.append(temp)
    for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            zone_type = (zone / "type").read_text().lower()
            temp = int((zone / "temp").read_text().strip()) / 1000.0
        except (OSError, ValueError):
            continue
        if any(token in zone_type for token in ("cpu", "x86_pkg", "k10", "zen")) and 0 < temp < 130:
            readings.append(temp)
    return max(readings) if readings else None


def gpu_status() -> tuple[Optional[float], Optional[float]]:
    """Retorna (temperatura, utilização) via nvidia-smi, se disponível."""
    command = shutil.which("nvidia-smi")
    if not command:
        return None, None
    try:
        result = subprocess.run(
            [command, "--query-gpu=temperature.gpu,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2, check=True,
        )
        temps: list[float] = []
        utils: list[float] = []
        for line in result.stdout.splitlines():
            fields = [part.strip() for part in line.split(",")]
            if len(fields) == 2:
                temps.append(float(fields[0]))
                utils.append(float(fields[1]))
        return (max(temps) if temps else None, max(utils) if utils else None)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, None


def cpu_worker(stop: mp.synchronize.Event, intensity: int, cpu_id: Optional[int]) -> None:
    reset_worker_signals()
    if cpu_id is not None:
        try:
            os.sched_setaffinity(0, {cpu_id})
        except (AttributeError, OSError):
            pass
    period = 0.1
    active_fraction = intensity / 100.0
    value = 0x12345678
    while not stop.is_set():
        started = time.perf_counter()
        active_until = started + period * active_fraction
        while time.perf_counter() < active_until:
            for _ in range(20_000):
                value = (value * 1_664_525 + 1_013_904_223) & 0xFFFFFFFF
            if stop.is_set():
                return
        remaining = period - (time.perf_counter() - started)
        if remaining > 0:
            stop.wait(remaining)


def memory_worker(stop: mp.synchronize.Event, target: int, intensity: int) -> None:
    reset_worker_signals()
    chunk_size = 32 * 1024 * 1024
    page = 4096
    blocks: list[bytearray] = []
    allocated = 0
    while allocated < target and not stop.is_set():
        size = min(chunk_size, target - allocated)
        try:
            block = bytearray(size)
            for offset in range(0, size, page):
                block[offset] = (offset // page) & 0xFF
            blocks.append(block)
            allocated += size
        except MemoryError:
            break
        # Faz a ocupação crescer gradualmente em intensidades menores.
        if intensity < 100:
            stop.wait(min(0.5, 0.5 * (100 - intensity) / max(1, intensity)))
    if not blocks:
        stop.wait()
        return
    period = 0.25
    index = 0
    while not stop.is_set():
        started = time.perf_counter()
        active_until = started + period * intensity / 100.0
        while time.perf_counter() < active_until and not stop.is_set():
            block = blocks[index % len(blocks)]
            index += 1
            for offset in range(0, len(block), page):
                block[offset] = (block[offset] + 1) & 0xFF
                if time.perf_counter() >= active_until:
                    break
        stop.wait(max(0, period - (time.perf_counter() - started)))


def disk_worker(stop: mp.synchronize.Event, path: str, size: int, intensity: int) -> None:
    reset_worker_signals()
    block_size = 1024 * 1024
    data = b"HARDWARE-STRESS" * (block_size // 15)
    offset = 0
    try:
        with open(path, "r+b", buffering=0) as handle:
            period = 0.5
            while not stop.is_set():
                started = time.perf_counter()
                active_until = started + period * intensity / 100.0
                writes = 0
                while time.perf_counter() < active_until and not stop.is_set():
                    handle.seek(offset)
                    block = data[: min(len(data), size - offset)]
                    handle.write(block)
                    offset = (offset + len(block)) % size
                    writes += 1
                    if writes % 8 == 0:
                        handle.flush()
                        os.fsync(handle.fileno())
                stop.wait(max(0, period - (time.perf_counter() - started)))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"falha no teste de disco: {exc}") from exc


def torch_gpu_worker(stop: mp.synchronize.Event, intensity: int) -> None:
    reset_worker_signals()
    import torch  # type: ignore
    torch.cuda.set_device(0)
    # 4096² FP16 ocupa ~32 MiB por matriz e gera carga de cálculo na GPU.
    a = torch.randn((4096, 4096), device="cuda", dtype=torch.float16)
    b = torch.randn((4096, 4096), device="cuda", dtype=torch.float16)
    period = 0.5
    while not stop.is_set():
        started = time.perf_counter()
        active_until = started + period * intensity / 100.0
        while time.perf_counter() < active_until and not stop.is_set():
            torch.mm(a, b)
        torch.cuda.synchronize()
        stop.wait(max(0, period - (time.perf_counter() - started)))


def cupy_gpu_worker(stop: mp.synchronize.Event, intensity: int) -> None:
    reset_worker_signals()
    import cupy as cp  # type: ignore
    cp.cuda.Device(0).use()
    a = cp.random.random((4096, 4096), dtype=cp.float32)
    b = cp.random.random((4096, 4096), dtype=cp.float32)
    period = 0.5
    while not stop.is_set():
        started = time.perf_counter()
        active_until = started + period * intensity / 100.0
        while time.perf_counter() < active_until and not stop.is_set():
            cp.matmul(a, b)
        cp.cuda.Stream.null.synchronize()
        stop.wait(max(0, period - (time.perf_counter() - started)))


def gpu_burn_worker(stop: mp.synchronize.Event, intensity: int, executable: str) -> None:
    reset_worker_signals()
    process: Optional[subprocess.Popen[bytes]] = None
    try:
        process = subprocess.Popen([executable, "1000000000"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        period = 1.0
        while not stop.is_set() and process.poll() is None:
            if intensity < 100:
                os.kill(process.pid, signal.SIGCONT)
            active = period * intensity / 100.0
            if stop.wait(active):
                break
            if intensity < 100:
                os.kill(process.pid, signal.SIGSTOP)
            stop.wait(period - active)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"falha no gpu-burn: {exc}") from exc
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGCONT)
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()


def find_gpu_backend(requested: str) -> tuple[str, Optional[str]]:
    if requested in ("auto", "gpu-burn"):
        for name in ("gpu-burn", "gpu_burn"):
            executable = shutil.which(name)
            if executable:
                return "gpu-burn", executable
        if requested == "gpu-burn":
            raise RuntimeError("--gpu-backend gpu-burn foi solicitado, mas gpu-burn não está no PATH")
    if requested in ("auto", "torch") and importlib.util.find_spec("torch"):
        probe = subprocess.run([sys.executable, "-c", "import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)"], timeout=20)
        if probe.returncode == 0:
            return "torch", None
        if requested == "torch":
            raise RuntimeError("PyTorch encontrado, mas CUDA não está disponível")
    if requested in ("auto", "cupy") and importlib.util.find_spec("cupy"):
        probe = subprocess.run([sys.executable, "-c", "import cupy; cupy.cuda.Device(0).compute_capability"], timeout=20)
        if probe.returncode == 0:
            return "cupy", None
        if requested == "cupy":
            raise RuntimeError("CuPy encontrado, mas não conseguiu acessar a GPU CUDA")
    raise RuntimeError("nenhum backend de GPU disponível; instale gpu-burn, PyTorch CUDA ou CuPy CUDA")


def cpu_percent(previous: Optional[tuple[int, int]]) -> tuple[Optional[float], tuple[int, int]]:
    try:
        fields = Path("/proc/stat").read_text().splitlines()[0].split()
        values = [int(value) for value in fields[1:]]
    except (OSError, ValueError, IndexError):
        return None, (0, 0)
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    if previous is None:
        return None, (total, idle)
    total_delta = total - previous[0]
    idle_delta = idle - previous[1]
    return (100.0 * (1 - idle_delta / total_delta) if total_delta else 0.0), (total, idle)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Estressa hardware com intensidade controlável.",
        epilog="Ex.: %(prog)s -p 75 --duration 300 --resources cpu,memory,gpu; modo infinito: %(prog)s -p 60 --forever",
    )
    parser.add_argument("-p", "--percent", required=True, type=percentage, help="intensidade de 1 a 100%%")
    parser.add_argument("--duration", type=duration, default=60.0, help="duração em segundos; 0 significa infinito (padrão: 60)")
    parser.add_argument("--forever", action="store_true", help="executa indefinidamente, até Ctrl-C")
    parser.add_argument("--resources", type=resources, default={"cpu", "memory"}, help="lista separada por vírgulas (padrão: cpu,memory)")
    parser.add_argument("--all", action="store_true", help="inclui cpu, memory, disk e gpu")
    parser.add_argument("--workers", type=int, default=0, help="workers de CPU; 0 usa todos os CPUs disponíveis")
    parser.add_argument("--memory-reserve-mib", type=int, default=1024, help="RAM deixada livre para o sistema (padrão: 1024)")
    parser.add_argument("--disk-size-mib", type=int, default=1024, help="tamanho do arquivo temporário de disco (padrão: 1024)")
    parser.add_argument("--disk-dir", default=tempfile.gettempdir(), help="diretório do arquivo temporário de disco")
    parser.add_argument("--gpu-backend", choices=("auto", "gpu-burn", "torch", "cupy"), default="auto")
    parser.add_argument("--report-interval", type=positive_interval, default=5.0, help="intervalo do relatório em segundos")
    args = parser.parse_args()
    if args.forever:
        args.duration = 0.0
    if args.workers < 0 or args.memory_reserve_mib < 0 or args.disk_size_mib <= 0:
        parser.error("workers/reserva devem ser >= 0; disk-size deve ser > 0")
    if args.all:
        args.resources = set(RESOURCES)
    return args


def main() -> int:
    args = parse_args()
    selected = args.resources
    if "disk" in selected:
        try:
            free = shutil.disk_usage(args.disk_dir).free
        except OSError as exc:
            raise SystemExit(f"diretório de disco indisponível: {exc}") from exc
        if args.disk_size_mib * 1024 * 1024 > free - 2 * 1024**3:
            raise SystemExit("espaço insuficiente: o teste de disco preserva 2 GiB livres")
    gpu_backend: Optional[tuple[str, Optional[str]]] = None
    if "gpu" in selected:
        try:
            gpu_backend = find_gpu_backend(args.gpu_backend)
        except (RuntimeError, subprocess.SubprocessError) as exc:
            raise SystemExit(f"GPU: {exc}") from exc
    context = mp.get_context("fork")
    stop = context.Event()
    interrupted = {"signal": None}
    worker_failed = False

    def request_stop(signum: int, _frame: object) -> None:
        # Não chame métodos de multiprocessing dentro de um tratador de sinal:
        # o sinal pode interromper uma operação interna de semáforo e causar
        # deadlock. O loop principal observa esta flag em seguida.
        interrupted["signal"] = signum

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    workers: list[mp.Process] = []
    disk_path: Optional[Path] = None
    try:
        cpu_ids = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else list(range(os.cpu_count() or 1))
        if "cpu" in selected:
            count = args.workers or len(cpu_ids)
            if count < 1:
                raise SystemExit("nenhum CPU disponível")
            if count > len(cpu_ids):
                raise SystemExit(f"--workers excede as {len(cpu_ids)} CPUs lógicas disponíveis")
            for index in range(count):
                worker = context.Process(target=cpu_worker, args=(stop, args.percent, cpu_ids[index % len(cpu_ids)]), name=f"cpu-{index}")
                worker.start()
                workers.append(worker)
        if "memory" in selected:
            target = memory_target(args.percent, args.memory_reserve_mib)
            worker = context.Process(target=memory_worker, args=(stop, target, args.percent), name="memory")
            worker.start()
            workers.append(worker)
            print(f"RAM alvo: {target / 1024**3:.2f} GiB (reserva: {args.memory_reserve_mib} MiB)")
        if "disk" in selected:
            try:
                fd, raw_path = tempfile.mkstemp(prefix="hardware-stress-", suffix=".bin", dir=args.disk_dir)
            except OSError as exc:
                raise SystemExit(f"não foi possível criar o arquivo de disco: {exc}") from exc
            os.close(fd)
            disk_path = Path(raw_path)
            worker = context.Process(target=disk_worker, args=(stop, raw_path, args.disk_size_mib * 1024 * 1024, args.percent), name="disk")
            worker.start()
            workers.append(worker)
            print(f"Arquivo temporário de disco: {raw_path} ({args.disk_size_mib} MiB)")
        if gpu_backend:
            backend, executable = gpu_backend
            target = gpu_burn_worker if backend == "gpu-burn" else torch_gpu_worker if backend == "torch" else cupy_gpu_worker
            gpu_args = (stop, args.percent, executable) if backend == "gpu-burn" else (stop, args.percent)
            worker = context.Process(target=target, args=gpu_args, name=f"gpu-{backend}")
            worker.start()
            workers.append(worker)
            print(f"GPU: backend {backend}")
        print(f"Estresse {args.percent}% em {', '.join(sorted(selected))}; Ctrl-C para encerrar.", flush=True)
        previous_cpu: Optional[tuple[int, int]] = None
        started = time.monotonic()
        while not stop.is_set() and interrupted["signal"] is None:
            elapsed = time.monotonic() - started
            if args.duration and elapsed >= args.duration:
                break
            temp = cpu_temperature()
            gpu_temp, gpu_util = gpu_status()
            usage, previous_cpu = cpu_percent(previous_cpu)
            details = []
            if usage is not None:
                details.append(f"CPU host {usage:.0f}%")
            if temp is not None:
                details.append(f"CPU {temp:.1f}°C")
            if gpu_temp is not None:
                details.append(f"GPU {gpu_temp:.1f}°C/{gpu_util:.0f}%")
            print(f"[{elapsed:8.1f}s] " + (" | ".join(details) or "monitoramento indisponível"), flush=True)
            for worker in workers:
                if not worker.is_alive():
                    print(f"Worker {worker.name} terminou com código {worker.exitcode}; encerrando.", flush=True)
                    worker_failed = True
                    stop.set()
                    break
            stop.wait(args.report_interval)
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
        for worker in workers:
            worker.join(timeout=5)
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=2)
        if disk_path:
            try:
                disk_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                print(f"Aviso: não foi possível remover {disk_path}: {exc}", file=sys.stderr)
    print("Estresse encerrado.", flush=True)
    if interrupted["signal"] == signal.SIGINT:
        return 130
    if interrupted["signal"] == signal.SIGTERM:
        return 143
    return 3 if worker_failed else 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
