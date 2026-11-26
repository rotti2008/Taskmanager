import os
import select
import socket
import sys
import termios
import time
import tty
from collections import deque

from rich.align import Align
from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from Taskmanager import get_all, get_disks

HISTORY = 60  # so viele Messpunkte zeigt der Verlauf
SPARK = "▁▂▃▄▅▆▇█"

cpu_history: deque = deque(maxlen=HISTORY)
up_history: deque = deque(maxlen=HISTORY)
down_history: deque = deque(maxlen=HISTORY)


def load_color(percent: float) -> str:
    """Farbe nach Auslastung: grün, gelb, rot."""
    if percent < 50:
        return "green"
    if percent < 80:
        return "yellow"
    return "red"


def temp_color(temp: float) -> str:
    if temp < 65:
        return "green"
    if temp < 85:
        return "yellow"
    return "red"


def format_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


def format_uptime(seconds: float) -> str:
    d, rest = divmod(int(seconds), 86400)
    h, rest = divmod(rest, 3600)
    return (f"{d} d " if d else "") + f"{h} h {rest // 60} min"


def format_rate(kb: float) -> str:
    """KB/s, ab 1024 als MB/s."""
    return f"{kb / 1024:.1f} MB/s" if kb >= 1024 else f"{kb:.1f} KB/s"


def sparkline(values, maximum: float | None = None, width: int = HISTORY) -> str:
    """Verlauf als Block-Zeichen; ohne `maximum` wird auf den größten Wert skaliert."""
    values = list(values)
    top = maximum if maximum is not None else max(values, default=0)
    top = top or 1
    chars = "".join(SPARK[min(int(v / top * (len(SPARK) - 1)), len(SPARK) - 1)] for v in values)
    return chars.rjust(width, " ")[-width:]


def bar(percent: float) -> ProgressBar:
    return ProgressBar(
        total=100, completed=percent, complete_style=load_color(percent), finished_style="red"
    )


def bar_row(label: str, percent: float, label_width: int = 8) -> Table:
    """Eine Zeile: Label, Balken, Prozentwert."""
    row = Table.grid(expand=True, padding=(0, 1))
    row.add_column(width=label_width)
    row.add_column(ratio=1)
    row.add_column(width=5, justify="right")
    row.add_row(label, bar(percent), Text(f"{percent:.0f}%", style=load_color(percent)))
    return row


def cpu_panel(cpu: dict, spark_width: int = 40) -> Panel:
    cores = cpu["cores"]
    half = (len(cores) + 1) // 2
    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    for i in range(half):
        left = bar_row(f"K{i}", cores[i], label_width=3)
        j = i + half
        right = bar_row(f"K{j}", cores[j], label_width=3) if j < len(cores) else ""
        grid.add_row(left, right)

    spark = Text(sparkline(cpu_history, maximum=100, width=spark_width), style=load_color(cpu["total"]))
    body = Group(
        bar_row("Gesamt", cpu["total"]),
        Text.assemble(("Verlauf ", "dim"), spark),
        Text(""),
        grid,
    )
    return Panel(body, title=f"[bold]CPU[/] · {len(cores)} Kerne", border_style="cyan")


def memory_panel(mem: dict) -> Panel:
    grid = Table.grid(expand=True)
    grid.add_row(bar_row("RAM", mem["percent"], label_width=5))
    grid.add_row(Text(f"{mem['used']:.1f} / {mem['total']:.1f} GB", style="dim"))
    if mem["swap_total"]:
        grid.add_row(bar_row("Swap", mem["swap_percent"], label_width=5))
        grid.add_row(Text(f"{mem['swap_used']:.1f} / {mem['swap_total']:.1f} GB", style="dim"))
    return Panel(grid, title="[bold]Arbeitsspeicher[/]", border_style="green")


def disk_panel(disks: list[dict]) -> Panel:
    table = Table(expand=True, box=None, padding=(0, 1))
    table.add_column("Mount", width=8, style="bold", no_wrap=True, overflow="ellipsis")
    table.add_column("", ratio=1, min_width=6)
    table.add_column("", width=5, justify="right")
    table.add_column("Frei", justify="right", width=9, style="dim")
    for d in disks:
        table.add_row(
            d["mount"],
            bar(d["percent"]),
            Text(f"{d['percent']:.0f}%", style=load_color(d["percent"])),
            f"{d['free']:.1f} GB",
        )
    return Panel(table, title="[bold]Datenträger[/]", border_style="yellow")


def network_panel(net: dict, spark_width: int = 24) -> Panel:
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(width=2)
    table.add_column(width=11, justify="right")
    table.add_column(ratio=1)
    table.add_row(
        Text("↑", style="bold magenta"),
        format_rate(net["up"]),
        Text(sparkline(up_history, width=spark_width), style="magenta"),
    )
    table.add_row(
        Text("↓", style="bold cyan"),
        format_rate(net["down"]),
        Text(sparkline(down_history, width=spark_width), style="cyan"),
    )
    return Panel(table, title="[bold]Netzwerk[/]", border_style="magenta")


SORT_NAMES = {"cpu": "CPU", "ram": "RAM", "time": "CPU-Zeit"}


def process_panel(procs: list[dict], sort_by: str = "cpu", narrow: bool = False) -> Panel:
    def head(key: str, label: str) -> str:
        return (f"{label}▼" if narrow else f"{label} ▼") if key == sort_by else label

    table = Table(expand=True, header_style="bold", border_style="dim")
    table.add_column("PID", justify="right", style="dim", min_width=6, no_wrap=True)
    table.add_column("Name", ratio=1, min_width=12, no_wrap=True, overflow="ellipsis")
    table.add_column(head("cpu", "CPU" if narrow else "CPU %"), justify="right")
    table.add_column(head("ram", "MB" if narrow else "RAM MB"), justify="right")
    if not narrow:  # bei schmalem Fenster nur PID, Name, CPU, RAM
        table.add_column(head("time", "Zeit"), justify="right", style="dim")
        table.add_column("Threads", justify="right", style="dim")
    for p in procs:
        row = [
            str(p["pid"]),
            p["name"],
            Text(f"{p['cpu']:.1f}", style=load_color(p["cpu"])),
            f"{p['ram']:.0f}",
        ]
        if not narrow:
            row += [format_time(p["time"]), str(p["threads"])]
        table.add_row(*row)
    return Panel(
        table, title=f"[bold]Top Prozesse[/] · nach {SORT_NAMES[sort_by]}", border_style="red"
    )


def header_panel(extra: dict, narrow: bool = False) -> Panel:
    text = Text(justify="center")
    text.append("System-Monitor", style="bold white")
    if not narrow:
        text.append(f"   {socket.gethostname()}", style="dim")
    text.append("   │   ", style="dim")
    text.append("Uptime ", style="dim")
    text.append(format_uptime(extra["uptime"]), style="bold")
    if extra["freq"]:
        text.append("   │   ", style="dim")
        text.append(f"{extra['freq']:.0f} MHz", style="bold")
    if extra["temp"] is not None:
        text.append("   │   ", style="dim")
        text.append(f"{extra['temp']:.0f} °C", style=f"bold {temp_color(extra['temp'])}")
    text.append("   │   ", style="dim")
    text.append(time.strftime("%H:%M:%S"), style="bold")
    return Panel(text, style="blue")


def footer(sort_by: str, narrow: bool = False) -> Align:
    keys = Text(justify="center", style="dim")
    keys.append("c", style="bold")
    keys.append(" CPU  ")
    keys.append("m", style="bold")
    keys.append(" RAM  ")
    keys.append("t", style="bold")
    keys.append(" Zeit   │   ")
    keys.append("q", style="bold")
    keys.append(" Beenden")
    if not narrow:
        keys.append("   │   CPU % = Anteil an der ganzen CPU")
    return Align.center(keys)


def build_layout(data: dict, sort_by: str = "cpu", width: int = 120) -> Layout:
    left_inner = width * 5 // 11 - 4  # Innenbreite der linken Spalte (ratio 5 von 11)
    narrow = width < 100
    cpu_history.append(data["cpu"]["total"])
    up_history.append(data["net"]["up"])
    down_history.append(data["net"]["down"])

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=1),
    )
    layout["body"].split_row(
        Layout(name="left", ratio=5),
        Layout(name="right", ratio=6),
    )
    layout["left"].split_column(
        Layout(name="cpu"),
        Layout(name="mem", size=8),
        Layout(name="net", size=4),
    )
    layout["right"].split_column(
        Layout(name="procs", ratio=2),
        Layout(name="disks", size=len(data["disks"]) + 3),
    )

    layout["header"].update(header_panel(data["extra"], narrow))
    layout["cpu"].update(cpu_panel(data["cpu"], spark_width=max(8, min(40, left_inner - 9))))
    layout["mem"].update(memory_panel(data["mem"]))
    layout["net"].update(network_panel(data["net"], spark_width=max(8, min(24, left_inner - 16))))
    layout["procs"].update(process_panel(data["procs"], sort_by, narrow))
    layout["disks"].update(disk_panel(data["disks"]))
    layout["footer"].update(footer(sort_by, narrow))
    return layout


def process_rows(height: int, disk_count: int) -> int:
    """Wie viele Prozesszeilen ins Fenster passen (Header, Fußzeile, Datenträger, Rahmen abgezogen)."""
    return max(5, height - 4 - (disk_count + 3) - 7)


def wait_for_keys(seconds: float) -> str:
    """Wartet bis zu `seconds` auf Eingaben und gibt alles Eingetippte (klein) zurück."""
    ready, _, _ = select.select([sys.stdin], [], [], max(0.0, seconds))
    if not ready:
        return ""
    # os.read statt sys.stdin.read: sonst bleiben Zeichen im Puffer hängen, die select nicht sieht
    return os.read(sys.stdin.fileno(), 1024).decode(errors="ignore").lower()


if __name__ == "__main__":
    sort_by = "cpu"
    get_all()  # Vorlauf: ab dem 2. Aufruf haben CPU, Prozesse und Netzwerk echte Werte
    time.sleep(1)
    old_settings = termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin)  # Tasten sofort lesen, ohne Enter und ohne Anzeige
    try:
        with Live(screen=True, auto_refresh=False) as live:
            next_update = 0.0
            while True:
                if time.time() >= next_update:  # höchstens einmal pro Sekunde messen und zeichnen
                    rows = process_rows(live.console.size.height, len(get_disks()))
                    live.update(
                        build_layout(get_all(rows, sort_by), sort_by, live.console.size.width),
                        refresh=True,
                    )
                    next_update = time.time() + 1
                keys = wait_for_keys(next_update - time.time())
                if "q" in keys:
                    break
                for key, name in (("c", "cpu"), ("m", "ram"), ("t", "time")):
                    if key in keys:  # gilt ab der nächsten Aktualisierung (spätestens 1 s)
                        sort_by = name
                # alle anderen Eingaben (Mausrad = Pfeiltasten, ...) werden ignoriert
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)  # Terminal zurücksetzen
