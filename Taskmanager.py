import psutil as ps
import time as t

# Zustand für die Netzwerk-Rate: Zähler und Zeitpunkt der letzten Messung
_last_net = {"bytes": ps.net_io_counters(), "time": t.time()}

# Mountpoints, die nicht angezeigt werden (Unterordner-Mounts, die nur verwirren)
HIDDEN_MOUNTS = {"/root"}


def get_cpu(): # -> dict
    """Gesamt-Auslastung und Auslastung pro Kern in Prozent."""
    # interval=None: liefert die Auslastung seit dem letzten Aufruf, blockiert nicht
    return {
        "total": ps.cpu_percent(interval=None),
        "cores": ps.cpu_percent(interval=None, percpu=True),
    }


def get_memory(): # -> dict
    """RAM und Swap in Prozent und in GB."""
    m = ps.virtual_memory()
    s = ps.swap_memory()
    return {
        "percent": m.percent,
        "used": m.used / 1024**3,
        "total": m.total / 1024**3,
        "swap_percent": s.percent,
        "swap_used": s.used / 1024**3,
        "swap_total": s.total / 1024**3,
    }


def get_disks(): #  -> list[dict]
    """Belegung und freier Platz jedes eingehängten Datenträgers."""
    disks = []
    for part in ps.disk_partitions():  # all=False: nur echte Datenträger
        if part.mountpoint in HIDDEN_MOUNTS:
            continue
        try:
            usage = ps.disk_usage(part.mountpoint)
        except PermissionError:
            continue
        disks.append({
            "mount": part.mountpoint,
            "percent": usage.percent,
            "free": usage.free / 1024**3,
        })
    return disks


def get_network(): # -> dict
    """Upload/Download in KB/s seit dem letzten Aufruf."""
    now = t.time()
    counters = ps.net_io_counters()
    dt = max(now - _last_net["time"], 0.001)
    up = (counters.bytes_sent - _last_net["bytes"].bytes_sent) / dt / 1024
    down = (counters.bytes_recv - _last_net["bytes"].bytes_recv) / dt / 1024
    _last_net["bytes"], _last_net["time"] = counters, now
    return {"up": up, "down": down}


def get_processes(count: int = 10, sort_by: str = "cpu"): # -> list[dict]
    """Die `count` Prozesse mit dem höchsten Wert für `sort_by` ("cpu", "ram" oder "time")."""
    cores = ps.cpu_count()  # logische Kerne
    procs = []
    # process_iter merkt sich die Prozesse, daher hat cpu_percent ab dem 2. Aufruf Werte
    attrs = ["pid", "name", "cpu_percent", "memory_info", "cpu_times", "num_threads"]
    for p in ps.process_iter(attrs):
        info = p.info
        if info["memory_info"] is None or info["cpu_times"] is None:  # Zugriff verweigert
            continue
        procs.append({
            "pid": info["pid"],
            "name": info["name"],
            # psutil: 100 % = ein Kern. Durch Kernzahl teilen, dann ist 100 % = ganze CPU
            "cpu": (info["cpu_percent"] or 0.0) / cores,
            "ram": info["memory_info"].rss / 1024**2,
            "time": info["cpu_times"].user + info["cpu_times"].system,  # CPU-Rechenzeit in s
            "threads": info["num_threads"],
        })
    procs.sort(key=lambda p: p[sort_by], reverse=True)
    return procs[:count]


def get_extra(): # -> dict
    """Zusatzinfos: Uptime in Sekunden, CPU-Takt in MHz, höchste Temperatur in °C."""
    uptime = t.time() - ps.boot_time()  # Jetztzeit - BootTime = Uptime
    freq = ps.cpu_freq()
    temps = ps.sensors_temperatures()
    hottest = max((s.current for sensor in temps.values() for s in sensor), default=None)
    return {
        "uptime": uptime,
        "freq": freq.current if freq else None,
        "temp": hottest,
    }


def get_all(count: int = 10, sort_by: str = "cpu"): # -> dict
    """Alle Daten in der Struktur, die skript.py erwartet."""
    return {
        "cpu": get_cpu(),
        "mem": get_memory(),
        "net": get_network(),
        "disks": get_disks(),
        "procs": get_processes(count, sort_by),
        "extra": get_extra(),
    }
