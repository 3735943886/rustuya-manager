#!/usr/bin/env python3
"""
Rustuya Manager Tool
Synchronizes devices between Tuya Cloud (tuyadevices.json) and rustuya-bridge.
"""

import json
import argparse
import os
import sys
import threading
import time
import paho.mqtt.client as mqtt
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich.prompt import Prompt, Confirm
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    class Console:
        def print(self, *args, **kwargs):
            if args and hasattr(args[0], 'plain'):
                print(args[0].plain, **kwargs)
            else:
                print(*args, **kwargs)
    
    class Table:
        def __init__(self, **kwargs): 
            self.title = kwargs.get('title', '')
            self.columns = []
            self.rows = []
        def add_column(self, name, **kwargs): self.columns.append(name)
        def add_row(self, *args): self.rows.append(args)

    class Panel:
        def __init__(self, text, **kwargs): self.plain = f"--- {text} ---"

    class Text:
        @staticmethod
        def assemble(*args):
            return type('Plain', (), {'plain': "".join([str(a[0]) if isinstance(a, tuple) else str(a) for a in args])})

    class Prompt:
        @staticmethod
        def ask(msg, choices=None, default=None):
            prompt_str = f"{msg} ({'/'.join(choices)})" if choices else msg
            if default: prompt_str += f" [{default}]"
            res = input(f"{prompt_str}: ").strip()
            return res if res else default

    class Confirm:
        @staticmethod
        def ask(msg):
            return input(f"{msg} (y/n): ").lower().startswith('y')

    class box:
        ROUNDED = None

# Default Constants
DEFAULT_ROOT = 'rustuya'
DEFAULT_CONFIG = 'config.json'
DEFAULT_CLOUD = 'tuyadevices.json'
TIMEOUT_SEC = 5

console = Console()

class RustuyaManager:
    def __init__(self, config_path, cloud_path):
        self.config_path = config_path
        self.cloud_path = cloud_path
        self.config = {}
        self.cloud_devices = {}
        self.bridge_devices = {}
        self.mqtt_client = None
        self.response_received = threading.Event()
        
        # Derived settings
        self.broker = 'localhost'
        self.port = 1883
        self.root_topic = DEFAULT_ROOT
        
        # Topic Templates (matches rustuya-bridge config names)
        # Defaults if not provided in config.json
        self.mqtt_command_topic = "{root}/command"
        self.mqtt_message_topic = None # Will be derived if missing
        self.mqtt_event_topic = "{root}/event/{type}"
        
        # Status categorizations
        self.mismatched = []
        self.missing = []
        self.orphaned = []
        self.synced = []

    def ensure_file_exists(self, initial_path, description):
        """Checks if a file exists; if not, scans directory and asks user to select."""
        if initial_path and os.path.exists(initial_path):
            return initial_path

        console.print(f"\n[bold yellow]⚠ {description} not found at:[/bold yellow] [dim]{initial_path}[/dim]")
        
        # Scan for candidate JSON files in current directory
        json_files = [f for f in os.listdir('.') if f.endswith('.json')]
        
        if not json_files:
            return Prompt.ask(f"[bold cyan]Please enter the path to your {description} manualy[/bold cyan]")

        console.print(f"[bold]Found these JSON files in the current directory:[/bold]")
        for i, f in enumerate(json_files, 1):
            console.print(f"  {i}. [cyan]{f}[/cyan]")
        console.print(f"  m. [dim]Enter path manually[/dim]")
        console.print(f"  q. [red]Quit[/red]")

        choice = Prompt.ask("\nSelect a file", choices=[str(i) for i in range(1, len(json_files)+1)] + ["m", "q"])

        if choice == "q":
            sys.exit(0)
        elif choice == "m":
            return Prompt.ask(f"[bold cyan]Enter path to {description}[/bold cyan]")
        else:
            return json_files[int(choice) - 1]

    def load_configs(self):
        # 0. Interactively ensure cloud data exists (required)
        self.cloud_path = self.ensure_file_exists(self.cloud_path, "Cloud Data (tuyadevices.json)")

        # 1. Load Bridge Config (config.json) - Optional
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self.config = json.load(f)
                
                # Extract MQTT info
                self.broker = self.config.get('mqtt_broker', self.broker)
                if '://' in self.broker:
                    # Very basic parsing of mqtt://host:port
                    parts = self.broker.split('://')[-1].split(':')
                    self.broker = parts[0]
                    if len(parts) > 1:
                        try:
                            self.port = int(parts[1])
                        except: pass
                
                self.root_topic = self.config.get('mqtt_root_topic', self.root_topic)
                
                # Load templates from config
                if 'mqtt_command_topic' in self.config:
                    self.mqtt_command_topic = self.config['mqtt_command_topic']
                if 'mqtt_message_topic' in self.config:
                    self.mqtt_message_topic = self.config['mqtt_message_topic']
                if 'mqtt_event_topic' in self.config:
                    self.mqtt_event_topic = self.config['mqtt_event_topic']

                console.print(f"[green]✔[/green] Loaded config from [bold]{self.config_path}[/bold]")
            except Exception as e:
                console.print(f"[red]✘[/red] Error: Failed to parse {self.config_path}: {e}")
                sys.exit(1)
        else:
            console.print(f"[yellow]![/yellow] Config [bold]{self.config_path}[/bold] not found. Using default MQTT broker: [cyan]{self.broker}:{self.port}[/cyan]")

        # 2. Load Cloud Data (tuyadevices.json)
        try:
            with open(self.cloud_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    self.cloud_devices = {d['id']: d for d in data if 'id' in d}
                else:
                    self.cloud_devices = data
            console.print(f"[green]✔[/green] Loaded cloud data from [bold]{self.cloud_path}[/bold] ({len(self.cloud_devices)} devices)")
        except Exception as e:
            console.print(f"[red]✘[/red] Error: Failed to load {self.cloud_path}: {e}")
            sys.exit(1)

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
            # Rust bridge returns status response with 'devices' map
            devices = payload.get('devices', {})
            # If it's the standard API response format
            if not devices and 'data' in payload and isinstance(payload['data'], dict):
                devices = payload['data'].get('devices', {})
            
            if devices:
                self.bridge_devices = devices
                self.response_received.set()
        except Exception as e:
            pass

    def resolve_topic(self, template, **kwargs):
        """Resolves template variables like {root}, {id}, {action}, {level}"""
        if template is None:
            # Fallback logic from bridge.rs
            root_topic = self.mqtt_event_topic \
                .replace("/{type}", "") \
                .replace("/event", "") \
                .rstrip('/')
            
            level = kwargs.get('level', 'response')
            device_id = kwargs.get('id', 'bridge')
            template = f"{root_topic}/{level}/{device_id}"
            
        res = template.replace("{root}", self.root_topic)
        for k, v in kwargs.items():
            res = res.replace("{" + k + "}", str(v))
        return res

    def fetch_bridge_status(self):
        console.print("[cyan]Connecting to MQTT broker...[/cyan]")
        try:
            # Handle paho-mqtt v2 vs v1
            try:
                self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            except AttributeError:
                self.mqtt_client = mqtt.Client()

            self.mqtt_client.on_message = self.on_message
            self.mqtt_client.connect(self.broker, self.port)
            
            # 1. Resolve response topic for bridge
            # Usually {root}/response/bridge or {root}/extra/device/response/bridge
            resp_topic = self.resolve_topic(self.mqtt_message_topic, id="bridge", level="response")
            self.mqtt_client.subscribe(resp_topic)
            console.print(f"[dim]Subscribed to response: {resp_topic}[/dim]")
            
            self.mqtt_client.loop_start()

            console.print("[cyan]Requesting bridge status...[/cyan]")
            
            # 2. Resolve command topic for status
            cmd_topic = self.resolve_topic(self.mqtt_command_topic, action="status", id="bridge")
            status_payload = json.dumps({"action": "status"})
            
            # Publish status request
            self.mqtt_client.publish(cmd_topic, status_payload)
            # Some bridges might expect just None or empty on a specific status topic
            # We'll try just the resolved command topic first as it's the standard
            
            if not self.response_received.wait(TIMEOUT_SEC):
                console.print("[yellow]⚠ Timeout: No response from bridge. Is it running?[/yellow]")
            
            self.mqtt_client.loop_stop()
        except Exception as e:
            console.print(f"[red]✘ MQTT Error: {e}[/red]")

    def compare(self):
        self.mismatched = []
        self.missing = []
        self.orphaned = []
        self.synced = []

        cloud_ids = set(self.cloud_devices.keys())
        bridge_ids = set(self.bridge_devices.keys())

        # Check for Missing and Mismatched
        for cid in cloud_ids:
            cdev = self.cloud_devices[cid]
            if cid not in bridge_ids:
                self.missing.append(cdev)
            else:
                bdev = self.bridge_devices[cid]
                # Compare local keys
                c_key = cdev.get('local_key') or cdev.get('key')
                b_key = bdev.get('key') or bdev.get('local_key')
                
                if c_key and b_key and c_key != b_key:
                    self.mismatched.append({**cdev, 'old_key': b_key, 'new_key': c_key})
                else:
                    self.synced.append(cdev)

        # Check for Orphaned
        for bid in bridge_ids:
            if bid not in cloud_ids:
                self.orphaned.append(self.bridge_devices[bid])

    def show_dashboard(self):
        self.compare()
        
        if HAS_RICH:
            table = Table(title="Rustuya Device Sync Dashboard", box=box.ROUNDED, header_style="bold magenta")
            table.add_column("ID", style="cyan", no_wrap=True)
            table.add_column("Name", style="white")
            table.add_column("Status", justify="center")
            table.add_column("Local Key (Old -> New)", style="dim")

            for dev in self.mismatched:
                table.add_row(dev['id'], dev.get('name', 'N/A'), "[bold yellow]KEY MISMATCH[/bold yellow]", f"{dev['old_key']} -> {dev['new_key']}")
            for dev in self.missing:
                table.add_row(dev['id'], dev.get('name', 'N/A'), "[bold green]MISSING (NEW)[/bold green]", f"-> {dev.get('local_key', '???')}")
            for dev in self.orphaned:
                table.add_row(dev['id'], dev.get('name', 'N/A'), "[bold red]ORPHANED[/bold red]", "")
            for dev in self.synced:
                table.add_row(dev['id'], dev.get('name', 'N/A'), "[blue]SYNCED[/blue]", "")
            
            console.print(table)
        else:
            # Plain Text Fallback
            console.print(f"\n=== Rustuya Device Sync Dashboard ===")
            for dev in self.mismatched:
                console.print(f"[MISMATCH] {dev['id']} ({dev.get('name')}) | {dev['old_key']} -> {dev['new_key']}")
            for dev in self.missing:
                console.print(f"[MISSING]  {dev['id']} ({dev.get('name')})")
            for dev in self.orphaned:
                console.print(f"[ORPHANED] {dev['id']} ({dev.get('name')})")
            # Skip synced in plain text to keep it clean, or just list count
        
        summary_text = f"Summary: {len(self.synced)} Synced, {len(self.mismatched)} Mismatch, {len(self.missing)} Missing, {len(self.orphaned)} Orphaned"
        if HAS_RICH:
            summary = Text.assemble(
                ("Summary: ", "bold"),
                (f"{len(self.synced)} Synced", "blue"), ", ",
                (f"{len(self.mismatched)} Mismatch", "yellow"), ", ",
                (f"{len(self.missing)} Missing", "green"), ", ",
                (f"{len(self.orphaned)} Orphaned", "red")
            )
            console.print(Panel(summary))
        else:
            console.print(f"\n{summary_text}\n" + "-"*len(summary_text))

    def publish_action(self, topic_suffix, payload):
        """Helper to publish action to bridge"""
        if not self.mqtt_client:
            try:
                try:
                    self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                except AttributeError:
                    self.mqtt_client = mqtt.Client()
                self.mqtt_client.connect(self.broker, self.port)
            except Exception as e:
                console.print(f"[red]Error connecting for action: {e}[/red]")
                return

        # Resolve command topic for this specific action
        action_name = topic_suffix.split('/')[-1] # e.g. "add", "remove"
        topic = self.resolve_topic(self.mqtt_command_topic, action=action_name)
        
        self.mqtt_client.publish(topic, json.dumps(payload))
        console.print(f"[dim]Published to: {topic}[/dim]")
            
    def run_sync_keys(self):
        if not self.mismatched:
            console.print("[dim]No keys to update.[/dim]")
            return
        
        console.print(f"\n[bold yellow]Updating {len(self.mismatched)} keys...[/bold yellow]")
        for dev in self.mismatched:
            console.print(f"  → Updating [cyan]{dev['id']}[/cyan] ({dev.get('name')})")
            # Bridge update is usually just 'add' with existing ID
            payload = {**dev} # Contain all cloud info
            payload['key'] = dev['new_key']
            self.publish_action("command", {"action": "add", **payload})
            # Also try the specific add topic
            self.publish_action("add", payload)
            
        console.print("[bold green]Done![/bold green]")

    def run_add_missing(self):
        if not self.missing:
            console.print("[dim]No missing devices to add.[/dim]")
            return
        
        console.print(f"\n[bold green]Adding {len(self.missing)} devices...[/bold green]")
        for dev in self.missing:
            if Confirm.ask(f"  Add [cyan]{dev['id']}[/cyan] ({dev.get('name')})?"):
                self.publish_action("command", {"action": "add", **dev})
                self.publish_action("add", dev)
        console.print("[bold green]Done![/bold green]")

    def run_remove_orphans(self):
        if not self.orphaned:
            console.print("[dim]No orphaned devices to remove.[/dim]")
            return
        
        console.print(f"\n[bold red]Removing {len(self.orphaned)} orphaned devices...[/bold red]")
        for dev in self.orphaned:
            did = dev.get('id')
            if Confirm.ask(f"  Remove [cyan]{did}[/cyan] ({dev.get('name')})?"):
                self.publish_action("command", {"action": "remove", "id": did})
                self.publish_action("remove", {"id": did})
        console.print("[bold green]Done![/bold green]")

    def interactive_menu(self):
        while True:
            self.show_dashboard()
            
            if not self.mismatched and not self.missing and not self.orphaned:
                console.print("\n[bold green]✨ System is fully synchronized. No actions required.[/bold green]")
                break

            console.print("\n[bold]Select an action:[/bold]")
            console.print("1. [yellow]Update Mismatched Keys[/yellow]")
            console.print("2. [green]Add Missing Devices[/green]")
            console.print("3. [red]Remove Orphaned Devices[/red]")
            console.print("4. [bold cyan]Sync All[/bold cyan]")
            console.print("5. Exit")
            
            choice = Prompt.ask("Choice", choices=["1", "2", "3", "4", "5"], default="5")
            
            if choice == "1":
                self.run_sync_keys()
            elif choice == "2":
                self.run_add_missing()
            elif choice == "3":
                self.run_remove_orphans()
            elif choice == "4":
                if Confirm.ask("Are you sure you want to perform all sync actions?"):
                    self.run_sync_keys()
                    for dev in self.missing:
                        self.publish_action("add", dev)
                    for dev in self.orphaned:
                        self.publish_action("remove", {"id": dev.get('id')})
                    console.print("[bold green]Full Sync Completed![/bold green]")
            elif choice == "5":
                break
            
            # Re-fetch status to see updates
            console.print("\n[dim]Refreshing status...[/dim]")
            self.response_received.clear()
            self.fetch_bridge_status()

def main():
    parser = argparse.ArgumentParser(description="Rustuya Bridge Management Tool")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help=f"Path to config.json (default: {DEFAULT_CONFIG})")
    parser.add_argument("-l", "--cloud", default=DEFAULT_CLOUD, help=f"Path to tuyadevices.json (default: {DEFAULT_CLOUD})")
    parser.add_argument("--broker", help="Override MQTT broker address")
    parser.add_argument("--root", help="Override MQTT root topic")
    
    args = parser.parse_args()
    
    manager = RustuyaManager(args.config, args.cloud)
    if args.broker: manager.broker = args.broker
    if args.root: manager.root_topic = args.root
    
    console.print(Panel("[bold cyan]Rustuya Bridge Manager[/bold cyan]\n[dim]State-of-the-art device synchronization[/dim]", expand=False))
    if not HAS_RICH:
        console.print("[yellow]Note: 'rich' library not found. Falling back to plain text mode.[/yellow]\n")
    
    manager.load_configs()
    manager.fetch_bridge_status()
    manager.interactive_menu()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Exiting...[/yellow]")
        sys.exit(0)
