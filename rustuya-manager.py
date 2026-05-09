#!/usr/bin/env python3
"""
Rustuya Manager Tool
Synchronizes devices between Tuya Cloud (tuyadevices.json) and rustuya-bridge.
"""

import json
import argparse
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List, Any, Union
import paho.mqtt.client as mqtt

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# --- UI Abstraction Layer ---
class UIManager:
    """Provides a consistent UI interface regardless of whether 'rich' is installed."""
    def __init__(self):
        self.console = Console() if HAS_RICH else self._fallback_console()

    def _fallback_console(self):
        class FallbackConsole:
            def print(self, *args, **kwargs):
                # Simple fallback for rich tags
                msg = str(args[0]) if args else ""
                import re
                msg = re.sub(r'\[.*?\]', '', msg) 
                print(msg, **kwargs)
        return FallbackConsole()

    def print(self, *args, **kwargs):
        self.console.print(*args, **kwargs)

    def panel(self, text: str, title: str = "", style: str = "cyan"):
        if HAS_RICH:
            self.console.print(Panel(text, title=title, border_style=style, expand=False))
        else:
            self.print(f"\n[{title}]\n{text}\n" + "-"*len(text))

    def confirm(self, msg: str) -> bool:
        if HAS_RICH:
            return Confirm.ask(msg)
        return input(f"{msg} (y/n): ").lower().startswith('y')

    def ask(self, msg: str, choices: List[str] = None, default: str = None) -> str:
        if HAS_RICH:
            return Prompt.ask(msg, choices=choices, default=default)
        prompt_str = f"{msg}"
        if choices: prompt_str += f" ({'/'.join(choices)})"
        if default: prompt_str += f" [{default}]"
        res = input(f"{prompt_str}: ").strip()
        return res if res else default

    def table(self, title: str, columns: List[Dict[str, Any]], rows: List[List[Any]]):
        if HAS_RICH:
            table = Table(title=title, box=box.ROUNDED, header_style="bold magenta")
            for col in columns:
                table.add_column(col["name"], style=col.get("style"), justify=col.get("justify", "left"), no_wrap=col.get("no_wrap", False))
            for row in rows:
                table.add_row(*[str(item) for item in row])
            self.console.print(table)
        else:
            self.print(f"\n=== {title} ===")
            for row in rows:
                self.print(" | ".join(str(r) for r in row))

ui = UIManager()

# --- Data Models ---
@dataclass
class Device:
    id: str
    name: str = "N/A"
    type: str = "WiFi"
    cid: Optional[str] = None
    parent_id: Optional[str] = None
    key: Optional[str] = None
    ip: str = "Auto"
    version: str = "Auto"
    status: str = "offline"
    raw_data: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Device':
        did = data.get('id')
        name = data.get('name', 'N/A')
        cid = data.get('node_id') or data.get('cid')
        parent_id = data.get('parent') or data.get('parent_id')
        key = data.get('local_key') or data.get('key')
        ip = data.get('ip') or "Auto"
        version = data.get('version') or data.get('ver') or "Auto"
        status = data.get('status', 'offline')
        
        is_sub = (data.get('sub') is True or cid is not None)
        if ip != "Auto" and not parent_id:
            is_sub = False
            
        return cls(
            id=did, name=name, type="SubDevice" if is_sub else "WiFi",
            cid=cid, parent_id=parent_id, key=key, ip=ip, version=version,
            status=str(status), raw_data=data
        )

    def get_routing_info(self) -> str:
        if self.type == "SubDevice":
            return f"P:{self.shorten(self.parent_id)} C:{self.cid}"
        return ""

    @staticmethod
    def shorten(val: str, length: int = 12) -> str:
        if not val or len(val) <= length: return str(val)
        return f"{val[:4]}...{val[-4:]}"

    def compare(self, other: 'Device') -> List[str]:
        mismatches = []
        if self.type == "WiFi":
            if self.key and other.key and self.key != other.key:
                mismatches.append(f"KEY: [yellow]{self.shorten(other.key)}[/yellow] -> [bold cyan]{self.shorten(self.key)}[/bold cyan]")
            if other.ip != "Auto" and self.ip != other.ip:
                mismatches.append(f"IP: [yellow]{other.ip}[/yellow] -> [bold cyan]{self.ip}[/bold cyan]")
            if other.version != "Auto" and self.version != other.version:
                mismatches.append(f"VER: [yellow]{other.version}[/yellow] -> [bold cyan]{self.version}[/bold cyan]")
        else:
            if self.cid != other.cid:
                mismatches.append(f"CID: [yellow]{other.cid}[/yellow] -> [bold cyan]{self.cid}[/bold cyan]")
            if self.parent_id != other.parent_id:
                mismatches.append(f"PARENT: [yellow]{self.shorten(other.parent_id)}[/yellow] -> [bold cyan]{self.shorten(self.parent_id)}[/bold cyan]")
        return mismatches

# Default Constants
DEFAULT_ROOT = 'rustuya'
DEFAULT_CONFIG = 'config.json'
DEFAULT_CLOUD = 'tuyadevices.json'
TIMEOUT_SEC = 5

class RustuyaManager:
    def __init__(self, config_path: str, cloud_path: str):
        self.config_path = Path(config_path)
        self.cloud_path = Path(cloud_path)
        self.config = {}
        self.cloud_devices: Dict[str, Device] = {}
        self.bridge_devices: Dict[str, Device] = {}
        self.mqtt_client = None
        self.response_received = threading.Event()
        
        self.broker, self.port = 'localhost', 1883
        self.root_topic = DEFAULT_ROOT
        self.mqtt_command_topic = "{root}/command"
        self.mqtt_message_topic = None 
        self.mqtt_event_topic = "{root}/event/{type}"
        
        self.mismatched, self.missing, self.orphaned, self.synced = [], [], [], []

    def ensure_file(self, path: Path, desc: str) -> Path:
        if path.exists(): return path
        ui.print(f"\n[yellow]⚠ {desc} not found at:[/yellow] {path}")
        files = list(Path('.').glob('*.json'))
        if not files: return Path(ui.ask(f"Enter path to {desc}"))
        ui.print("Available JSON files:")
        for i, f in enumerate(files, 1): ui.print(f"  {i}. [cyan]{f}[/cyan]")
        choice = ui.ask("Select file", choices=[str(i) for i in range(1, len(files)+1)] + ["m", "q"], default="q")
        if choice == "q": sys.exit(0)
        if choice == "m": return Path(ui.ask(f"Enter path to {desc}"))
        return files[int(choice) - 1]

    def load_configs(self):
        self.cloud_path = self.ensure_file(self.cloud_path, "Cloud Data")
        if self.config_path.exists():
            try:
                with self.config_path.open() as f: self.config = json.load(f)
                self.broker = self.config.get('mqtt_broker', self.broker)
                if '://' in self.broker:
                    addr = self.broker.split('://')[-1].split(':')
                    self.broker = addr[0]
                    if len(addr) > 1: self.port = int(addr[1])
                self.root_topic = self.config.get('mqtt_root_topic', self.root_topic)
                for key in ['command', 'message', 'event']:
                    topic_key = f'mqtt_{key}_topic'
                    if topic_key in self.config: setattr(self, topic_key, self.config[topic_key])
                ui.print(f"[green]✔[/green] Loaded config from {self.config_path}")
            except Exception as e: ui.print(f"[red]✘[/red] Config error: {e}"); sys.exit(1)
        else: ui.print(f"[yellow]![/yellow] Using default MQTT: {self.broker}:{self.port}")

        try:
            with self.cloud_path.open() as f:
                data = json.load(f)
                dev_list = data if isinstance(data, list) else data.values()
                self.cloud_devices = {d['id']: Device.from_dict(d) for d in dev_list if 'id' in d}
            ui.print(f"[green]✔[/green] Loaded {len(self.cloud_devices)} cloud devices")
        except Exception as e: ui.print(f"[red]✘[/red] Cloud data error: {e}"); sys.exit(1)

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            devices = payload.get('devices') or payload.get('data', {}).get('devices')
            if devices is not None:
                self.bridge_devices = {did: Device.from_dict(d) for did, d in devices.items()}
                self.response_received.set()
        except: pass

    def resolve_topic(self, template: str, **kwargs) -> str:
        if template is None:
            base = self.mqtt_event_topic.replace("/{type}", "").replace("/event", "").rstrip('/')
            template = f"{base}/{{level}}/{{id}}"
        kwargs.update({'root': self.root_topic, 'level': kwargs.get('level', 'response'), 'id': kwargs.get('id', 'bridge')})
        res = template
        for k, v in kwargs.items(): res = res.replace("{" + k + "}", str(v))
        return res

    def fetch_bridge_status(self):
        ui.print("[cyan]Connecting to MQTT...[/cyan]")
        try:
            try: self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            except: self.mqtt_client = mqtt.Client()
            self.mqtt_client.on_message = self.on_message
            self.mqtt_client.connect(self.broker, self.port)
            resp_topic = self.resolve_topic(self.mqtt_message_topic)
            self.mqtt_client.subscribe(resp_topic)
            self.mqtt_client.loop_start()
            cmd_topic = self.resolve_topic(self.mqtt_command_topic, action="status")
            self.mqtt_client.publish(cmd_topic, json.dumps({"action": "status"}))
            if not self.response_received.wait(TIMEOUT_SEC): ui.print("[yellow]⚠ Timeout: No response from bridge.[/yellow]")
            self.mqtt_client.loop_stop()
        except Exception as e: ui.print(f"[red]✘ MQTT Error: {e}[/red]")

    def compare(self):
        self.mismatched, self.missing, self.orphaned, self.synced = [], [], [], []
        cloud_ids, bridge_ids = set(self.cloud_devices.keys()), set(self.bridge_devices.keys())
        for cid in cloud_ids:
            cdev = self.cloud_devices[cid]
            if cid not in bridge_ids: self.missing.append(cdev)
            else:
                m = cdev.compare(self.bridge_devices[cid])
                if m: self.mismatched.append({'dev': cdev, 'reason': "\n".join(m)})
                else: self.synced.append(cdev)
        for bid in bridge_ids:
            if bid not in cloud_ids: self.orphaned.append(self.bridge_devices[bid])

    def show_dashboard(self):
        self.compare()
        cols = [{"name": "Type", "style": "dim"}, {"name": "ID / Routing", "style": "cyan", "no_wrap": True}, 
                {"name": "Name", "style": "white"}, {"name": "Status", "justify": "center"}, {"name": "Details", "style": "dim"}]
        rows = []
        for d in self.mismatched:
            status = self._format_status(d['dev'])
            rows.append([d['dev'].type, d['dev'].id, d['dev'].name, f"[yellow]MISMATCH[/yellow] {status}", d['reason']])
        for d in self.missing:
            rows.append([d.type, f"{d.id}\n[dim]{d.get_routing_info()}[/dim]", d.name, "[green]MISSING[/green]", f"Key: {Device.shorten(d.key)}"])
        for d in self.orphaned:
            status = self._format_status(d)
            rows.append([d.type, d.id, d.name, f"[red]ORPHANE[/red] {status}", ""])
        for d in self.synced:
            status = self._format_status(d)
            rows.append([d.type, d.id, d.name, f"[blue]SYNCED[/blue] {status}", ""])
        ui.table("Rustuya Device Dashboard", cols, rows)
        ui.panel(f"Summary: {len(self.synced)} Synced, {len(self.mismatched)} Mismatch, {len(self.missing)} Missing, {len(self.orphaned)} Orphaned", title="Sync Status")

    def _format_status(self, dev: Device) -> str:
        s = dev.status
        if s in ('online', '0', 'true'): return "[green]●[/green]"
        if s in ('subdevice', 'no parent', 'invalid subdevice'): return "[blue]○[/blue]"
        if s.isdigit(): return f"[red]ERR:{s}[/red]"
        return "[dim]●[/dim]"

    def publish_action(self, action: str, dev: Device):
        if not self.mqtt_client:
            try:
                try: self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                except: self.mqtt_client = mqtt.Client()
                self.mqtt_client.connect(self.broker, self.port)
            except Exception as e: ui.print(f"[red]✘ Connection Error: {e}[/red]"); return
        topic = self.resolve_topic(self.mqtt_command_topic, action=action)
        payload = {"action": action, "id": dev.id, "name": dev.name}
        if action == "add":
            if dev.type == "WiFi":
                for k in ['key', 'ip', 'version']:
                    v = getattr(dev, k)
                    if v and v != "Auto": payload[k] = v
            else:
                for k in ['cid', 'parent_id']:
                    v = getattr(dev, k)
                    if v: payload[k] = v
        self.mqtt_client.publish(topic, json.dumps(payload))
        ui.print(f"[dim]Published {action} to: {topic}[/dim]")

    def batch_process(self, items: list, name: str, func, auto: bool = False):
        if not items: ui.print(f"[dim]No items to {name}.[/dim]"); return
        ui.print(f"\n[bold]{name.capitalize()} {len(items)} items...[/bold]")
        all_mode = auto
        for item in items:
            dev = item['dev'] if isinstance(item, dict) else item
            if not all_mode:
                res = ui.ask(f"  {name.capitalize()} {dev.id} ({dev.name})?", choices=["y", "n", "a", "q"], default="y").lower()
                if res == "q": break
                if res == "n": continue
                if res == "a": all_mode = True
            func(dev)
        ui.print(f"[green]{name.capitalize()} done.[/green]")

    def interactive_menu(self):
        while True:
            self.show_dashboard()
            if not any([self.mismatched, self.missing, self.orphaned]):
                ui.print("\n[bold green]✨ System synchronized.[/bold green]"); break
            ui.print("\n1. Update Mismatches\n2. Add Missing\n3. Remove Orphans\n4. Sync All\nq. Exit")
            choice = ui.ask("Select", choices=["1", "2", "3", "4", "q"], default="q")
            if choice == "1": self.batch_process(self.mismatched, "update", lambda d: self.publish_action("add", d))
            elif choice == "2": self.batch_process(self.missing, "add", lambda d: self.publish_action("add", d))
            elif choice == "3": self.batch_process(self.orphaned, "remove", lambda d: self.publish_action("remove", d))
            elif choice == "4":
                if ui.confirm("Sync all?"):
                    self.batch_process(self.mismatched, "update", lambda d: self.publish_action("add", d), auto=True)
                    self.batch_process(self.missing, "add", lambda d: self.publish_action("add", d), auto=True)
                    self.batch_process(self.orphaned, "remove", lambda d: self.publish_action("remove", d), auto=True)
            elif choice == "q": break
            ui.print("\n[dim]Refreshing...[/dim]"); self.response_received.clear(); self.fetch_bridge_status()

def main():
    parser = argparse.ArgumentParser(description="Rustuya Bridge Management Tool")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    parser.add_argument("-l", "--cloud", default=DEFAULT_CLOUD)
    parser.add_argument("--broker")
    parser.add_argument("--root")
    args = parser.parse_args()
    mgr = RustuyaManager(args.config, args.cloud)
    if args.broker: mgr.broker = args.broker
    if args.root: mgr.root_topic = args.root
    ui.panel("Rustuya Bridge Manager", title="Welcome")
    mgr.load_configs(); mgr.fetch_bridge_status(); mgr.interactive_menu()

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: ui.print("\nExiting..."); sys.exit(0)
