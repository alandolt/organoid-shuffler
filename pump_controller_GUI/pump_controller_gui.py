#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pyserial>=3.5",
# ]
# ///
"""
Pump Controller GUI
Controls pump operation via Arduino UNO.

Run:
    uv run pump_controller_gui.py
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import json
from pathlib import Path
import serial
import serial.tools.list_ports
import threading
import time

class StepperControllerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("CNC Shield Stepper Motor Controller")
        self.root.geometry("760x620")
        self.root.resizable(True, True)
        self.settings_path = Path(__file__).with_name("pump_settings.json")

        # Serial port variables
        self.ser = None
        self.connected = False
        self.serial_lock = threading.Lock()
        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")

        # Motor control variables
        self.axis_var = tk.StringVar(value="X")
        self.linked_axis_var = tk.StringVar(value="None")
        self.move_value_var = tk.StringVar(value="1000")
        self.move_unit_var = tk.StringVar(value="steps")
        self.steps_per_mm_var = tk.StringVar(value="80")
        self.mm_per_ml_var = tk.StringVar(value="10")
        self.speed_var = tk.StringVar(value="5000")
        self.speed_unit_var = tk.StringVar(value="steps/s")
        self.pump_cycle_volume_ml_var = tk.StringVar(value="0.5")
        self.pump_pause_seconds_var = tk.StringVar(value="2")
        self.pump_speed_ml_s_var = tk.StringVar(value="0.2")
        self.pump_cycle_count_var = tk.StringVar(value="0")
        self.pump_running = False
        self.pump_stop_event = threading.Event()
        self.pump_thread = None

        # Restore saved scale factors from previous run.
        self.load_scale_settings()

        # Create GUI elements
        self.create_widgets()
        self.setup_scale_autosave()
        self.refresh_ports()

    def load_scale_settings(self):
        """Load saved scale factor settings from disk."""
        try:
            if not self.settings_path.exists():
                return
            with self.settings_path.open("r", encoding="utf-8") as file:
                data = json.load(file)

            steps_per_mm = data.get("steps_per_mm")
            mm_per_ml = data.get("mm_per_ml")

            if steps_per_mm is not None:
                self.steps_per_mm_var.set(str(steps_per_mm))
            if mm_per_ml is not None:
                self.mm_per_ml_var.set(str(mm_per_ml))
        except Exception:
            # If settings are malformed/unreadable, keep defaults.
            pass

    def save_scale_settings(self):
        """Save current scale factor settings to disk."""
        try:
            data = {
                "steps_per_mm": self.steps_per_mm_var.get().strip(),
                "mm_per_ml": self.mm_per_ml_var.get().strip(),
            }
            with self.settings_path.open("w", encoding="utf-8") as file:
                json.dump(data, file, indent=2)
        except Exception as e:
            self.log_message(f"[Warning] Could not save settings: {e}")

    def setup_scale_autosave(self):
        """Autosave scale factors when edited."""
        self.steps_per_mm_var.trace_add("write", lambda *args: self.save_scale_settings())
        self.mm_per_ml_var.trace_add("write", lambda *args: self.save_scale_settings())

    def open_calibration_dialog(self):
        """Open a compact dialog for calibration settings."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Calibration Settings")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        content = ttk.Frame(dialog, padding=12)
        content.pack(fill=tk.BOTH, expand=True)

        ttk.Label(content, text="Steps / mm:").grid(row=0, column=0, sticky=tk.W, pady=6)
        steps_entry = ttk.Entry(content, textvariable=self.steps_per_mm_var, width=18)
        steps_entry.grid(row=0, column=1, padx=8, pady=6)

        ttk.Label(content, text="mm / ml:").grid(row=1, column=0, sticky=tk.W, pady=6)
        mm_entry = ttk.Entry(content, textvariable=self.mm_per_ml_var, width=18)
        mm_entry.grid(row=1, column=1, padx=8, pady=6)

        ttk.Label(content, text="These values are saved automatically.", font=("Arial", 8)).grid(
            row=2, column=0, columnspan=2, sticky=tk.W, pady=(4, 10)
        )

        button_row = ttk.Frame(content)
        button_row.grid(row=3, column=0, columnspan=2, sticky=tk.E)

        def close_dialog():
            self.save_scale_settings()
            dialog.destroy()

        ttk.Button(button_row, text="Close", command=close_dialog).pack(side=tk.RIGHT)
        steps_entry.focus_set()
        dialog.protocol("WM_DELETE_WINDOW", close_dialog)

    def open_axis_tie_dialog(self):
        """Open a compact dialog to tie the primary axis to another axis."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Tie Axes")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        current_axis = self.axis_var.get()
        allowed_axes = ["None"] + [axis for axis in ["X", "Y", "Z"] if axis != current_axis]

        content = ttk.Frame(dialog, padding=12)
        content.pack(fill=tk.BOTH, expand=True)

        ttk.Label(content, text=f"Primary axis: {current_axis}").grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))
        ttk.Label(content, text="Tie with:").grid(row=1, column=0, sticky=tk.W, pady=6)

        tie_combo = ttk.Combobox(content, textvariable=self.linked_axis_var, values=allowed_axes, width=12, state="readonly")
        tie_combo.grid(row=1, column=1, padx=8, pady=6)

        ttk.Label(content, text="Both axes will receive the same move and stop commands.", font=("Arial", 8)).grid(
            row=2, column=0, columnspan=2, sticky=tk.W, pady=(4, 10)
        )

        button_row = ttk.Frame(content)
        button_row.grid(row=3, column=0, columnspan=2, sticky=tk.E)

        def apply_tie():
            chosen_axis = self.linked_axis_var.get()
            if chosen_axis == current_axis:
                messagebox.showerror("Error", "You cannot tie an axis to itself")
                return
            if chosen_axis not in allowed_axes:
                self.linked_axis_var.set("None")
            self.update_linked_axis_label()
            dialog.destroy()

        def clear_tie():
            self.linked_axis_var.set("None")
            self.update_linked_axis_label()
            dialog.destroy()

        ttk.Button(button_row, text="Apply", command=apply_tie).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(button_row, text="Clear", command=clear_tie).pack(side=tk.RIGHT)
        tie_combo.focus_set()
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

    def update_linked_axis_label(self):
        """Refresh the linked-axis status on the main screen."""
        self.linked_axis_label.config(text=f"Linked axis: {self.linked_axis_var.get()}")

    def create_widgets(self):
        """Create all GUI elements"""

        # ===== Serial Connection Frame =====
        conn_frame = ttk.LabelFrame(self.root, text="Serial Connection", padding=10)
        conn_frame.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(conn_frame, text="COM Port:").grid(row=0, column=0, sticky=tk.W)
        self.port_combo = ttk.Combobox(conn_frame, textvariable=self.port_var, width=20, state="readonly")
        self.port_combo.grid(row=0, column=1, padx=5)

        ttk.Button(conn_frame, text="Refresh Ports", command=self.refresh_ports).grid(row=0, column=2, padx=5)

        ttk.Label(conn_frame, text="Baud Rate:").grid(row=0, column=3, sticky=tk.W, padx=(20, 0))
        baud_combo = ttk.Combobox(conn_frame, textvariable=self.baud_var,
                                   values=["9600", "115200", "250000"], width=10, state="readonly")
        baud_combo.grid(row=0, column=4, padx=5)

        self.connect_btn = ttk.Button(conn_frame, text="Connect", command=self.connect_serial)
        self.connect_btn.grid(row=0, column=5, padx=5)

        self.status_label = ttk.Label(conn_frame, text="Disconnected", foreground="red")
        self.status_label.grid(row=0, column=6, padx=10)

        # ===== Motor Control Frame =====
        motor_frame = ttk.LabelFrame(self.root, text="Motor Control", padding=15)
        motor_frame.pack(fill=tk.BOTH, padx=10, pady=10, expand=False)

        # Axis selection
        ttk.Label(motor_frame, text="Select Axis:").grid(row=0, column=0, sticky=tk.W, pady=10)
        axis_frame = ttk.Frame(motor_frame)
        axis_frame.grid(row=0, column=1, columnspan=3, sticky=tk.W, pady=10)

        ttk.Radiobutton(axis_frame, text="X", variable=self.axis_var, value="X").pack(side=tk.LEFT, padx=10)
        ttk.Radiobutton(axis_frame, text="Y", variable=self.axis_var, value="Y").pack(side=tk.LEFT, padx=10)
        ttk.Radiobutton(axis_frame, text="Z", variable=self.axis_var, value="Z").pack(side=tk.LEFT, padx=10)

        self.linked_axis_label = ttk.Label(motor_frame, text="Linked axis: None", font=("Arial", 8))
        self.linked_axis_label.grid(row=0, column=3, sticky=tk.W, padx=5)

        ttk.Button(motor_frame, text="Tie Axes", command=self.open_axis_tie_dialog).grid(
            row=0, column=2, sticky=tk.E, padx=5
        )

        # Move amount input
        ttk.Label(motor_frame, text="Move:").grid(row=1, column=0, sticky=tk.W, pady=10)
        move_entry = ttk.Entry(motor_frame, textvariable=self.move_value_var, width=15)
        move_entry.grid(row=1, column=1, sticky=tk.EW, padx=5, pady=10)

        unit_combo = ttk.Combobox(
            motor_frame,
            textvariable=self.move_unit_var,
            values=["steps", "mm", "ml"],
            width=12,
            state="readonly",
        )
        unit_combo.grid(row=1, column=2, sticky=tk.W, padx=5, pady=10)
        ttk.Label(motor_frame, text="(positive = forward, negative = backward)", font=("Arial", 8)).grid(row=1, column=3, sticky=tk.W, padx=5)

        # Speed input
        ttk.Label(motor_frame, text="Speed:").grid(row=2, column=0, sticky=tk.W, pady=10)
        speed_entry = ttk.Entry(motor_frame, textvariable=self.speed_var, width=15)
        speed_entry.grid(row=2, column=1, sticky=tk.EW, padx=5, pady=10)
        speed_unit_combo = ttk.Combobox(
            motor_frame,
            textvariable=self.speed_unit_var,
            values=["steps/s", "mm/s", "ml/s"],
            width=12,
            state="readonly",
        )
        speed_unit_combo.grid(row=2, column=2, sticky=tk.W, padx=5, pady=10)
        ttk.Label(motor_frame, text="(converted to steps/s)", font=("Arial", 8)).grid(row=2, column=3, sticky=tk.W, padx=5)

        # Move and Stop buttons
        button_frame = ttk.Frame(motor_frame)
        button_frame.grid(row=3, column=0, columnspan=4, pady=20, sticky=tk.EW)

        self.move_btn = ttk.Button(button_frame, text="Move Motor", command=self.move_motor)
        self.move_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)
        self.move_btn.config(state=tk.DISABLED)

        self.stop_btn = ttk.Button(button_frame, text="Stop Motor", command=self.stop_motor)
        self.stop_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)
        self.stop_btn.config(state=tk.DISABLED)

        ttk.Button(self.root, text="Configure Calibration", command=self.open_calibration_dialog).pack(
            fill=tk.X, padx=10, pady=(0, 10)
        )

        # ===== Syringe Pump Frame =====
        pump_frame = ttk.LabelFrame(self.root, text="Syringe Pump (Stop/Go)", padding=10)
        pump_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

        ttk.Label(pump_frame, text="Cycle Volume (ml):").grid(row=0, column=0, sticky=tk.W, pady=5)
        ttk.Entry(pump_frame, textvariable=self.pump_cycle_volume_ml_var, width=12).grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)

        ttk.Label(pump_frame, text="Pause (s):").grid(row=0, column=2, sticky=tk.W, pady=5)
        ttk.Entry(pump_frame, textvariable=self.pump_pause_seconds_var, width=12).grid(row=0, column=3, sticky=tk.W, padx=5, pady=5)

        ttk.Label(pump_frame, text="Flow (ml/s):").grid(row=1, column=0, sticky=tk.W, pady=5)
        ttk.Entry(pump_frame, textvariable=self.pump_speed_ml_s_var, width=12).grid(row=1, column=1, sticky=tk.W, padx=5, pady=5)

        ttk.Label(pump_frame, text="Cycles (0 = continuous):").grid(row=1, column=2, sticky=tk.W, pady=5)
        ttk.Entry(pump_frame, textvariable=self.pump_cycle_count_var, width=12).grid(row=1, column=3, sticky=tk.W, padx=5, pady=5)

        pump_btn_frame = ttk.Frame(pump_frame)
        pump_btn_frame.grid(row=2, column=0, columnspan=4, sticky=tk.EW, pady=(8, 2))

        self.pump_go_btn = ttk.Button(pump_btn_frame, text="Pump Go", command=self.start_syringe_pump)
        self.pump_go_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)
        self.pump_go_btn.config(state=tk.DISABLED)

        self.pump_stop_btn = ttk.Button(pump_btn_frame, text="Pump Stop", command=self.stop_syringe_pump)
        self.pump_stop_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)
        self.pump_stop_btn.config(state=tk.DISABLED)

        ttk.Label(pump_frame, text="Pumps cycle volume, then pauses for the set time.", font=("Arial", 8)).grid(
            row=3, column=0, columnspan=4, sticky=tk.W, pady=(4, 0)
        )

        # ===== Output Console Frame =====
        console_frame = ttk.LabelFrame(self.root, text="Serial Output", padding=10)
        console_frame.pack(fill=tk.BOTH, padx=10, pady=10, expand=True)

        self.console = scrolledtext.ScrolledText(console_frame, height=12, width=80, state=tk.DISABLED)
        self.console.pack(fill=tk.BOTH, expand=True)

    def refresh_ports(self):
        """Refresh available COM ports"""
        ports = [port.device for port in serial.tools.list_ports.comports()]
        self.port_combo['values'] = ports

        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def connect_serial(self):
        """Connect or disconnect from serial port"""
        if self.connected:
            self.disconnect_serial()
        else:
            port = self.port_var.get()
            if not port:
                messagebox.showerror("Error", "Please select a COM port")
                return

            try:
                baud = int(self.baud_var.get())
                self.ser = serial.Serial(port, baud, timeout=1)
                self.connected = True
                self.connect_btn.config(text="Disconnect")
                self.status_label.config(text=f"Connected to {port}", foreground="green")
                self.move_btn.config(state=tk.NORMAL)
                self.stop_btn.config(state=tk.NORMAL)
                self.pump_go_btn.config(state=tk.NORMAL)
                self.pump_stop_btn.config(state=tk.NORMAL)

                # Start reading thread
                self.read_thread = threading.Thread(target=self.read_serial, daemon=True)
                self.read_thread.start()

                self.log_message(f"Connected to {port} at {baud} baud")
                time.sleep(0.5)  # Wait for Arduino startup message

            except Exception as e:
                messagebox.showerror("Connection Error", f"Failed to connect: {e}")
                self.connected = False

    def disconnect_serial(self):
        """Disconnect from serial port"""
        if self.ser:
            self.stop_syringe_pump(silent=True)
            self.ser.close()
            self.connected = False
            self.connect_btn.config(text="Connect")
            self.status_label.config(text="Disconnected", foreground="red")
            self.move_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.DISABLED)
            self.pump_go_btn.config(state=tk.DISABLED)
            self.pump_stop_btn.config(state=tk.DISABLED)
            self.log_message("Disconnected from serial port")

    def read_serial(self):
        """Read from serial port in background thread"""
        while self.connected and self.ser:
            try:
                if self.ser.in_waiting > 0:
                    data = self.ser.readline().decode('utf-8', errors='ignore').strip()
                    if data:
                        self.log_message(f"[Arduino] {data}")
            except Exception as e:
                if self.connected:
                    self.log_message(f"[Error] {e}")
                break

    def move_motor(self):
        """Send move command to Arduino"""
        if not self.connected:
            messagebox.showerror("Error", "Not connected to Arduino")
            return

        axis = self.axis_var.get()
        try:
            move_value = float(self.move_value_var.get())
            speed_value = float(self.speed_var.get())
            steps_per_mm = float(self.steps_per_mm_var.get())
            mm_per_ml = float(self.mm_per_ml_var.get())

            if speed_value <= 0:
                messagebox.showerror("Error", "Speed must be greater than 0")
                return

            if steps_per_mm <= 0:
                messagebox.showerror("Error", "Steps / mm must be greater than 0")
                return

            if mm_per_ml <= 0:
                messagebox.showerror("Error", "mm / ml must be greater than 0")
                return

            move_steps = self.convert_distance_to_steps(
                move_value, self.move_unit_var.get(), steps_per_mm, mm_per_ml
            )
            speed_steps = self.convert_speed_to_steps_per_sec(
                speed_value, self.speed_unit_var.get(), steps_per_mm, mm_per_ml
            )

            if speed_steps <= 0:
                messagebox.showerror("Error", "Converted speed must be greater than 0 steps/s")
                return

            self.send_motion_command(
                axis,
                move_steps,
                speed_steps,
                move_value,
                is_stop=False,
                move_unit=self.move_unit_var.get(),
                speed_value=speed_value,
                speed_unit=self.speed_unit_var.get(),
            )

        except ValueError:
            messagebox.showerror("Error", "Please enter valid numeric values")

    def stop_motor(self):
        """Send stop command to Arduino"""
        if not self.connected:
            messagebox.showerror("Error", "Not connected to Arduino")
            return

        axis = self.axis_var.get()
        try:
            speed_value = float(self.speed_var.get())
            steps_per_mm = float(self.steps_per_mm_var.get())
            mm_per_ml = float(self.mm_per_ml_var.get())
            speed_steps = self.convert_speed_to_steps_per_sec(
                speed_value, self.speed_unit_var.get(), steps_per_mm, mm_per_ml
            )
            if speed_steps <= 0:
                speed_steps = 1
            self.send_motion_command(axis, 0, speed_steps, 0, is_stop=True)
        except Exception as e:
            self.log_message(f"[Error] Failed to send stop command: {e}")

    def start_syringe_pump(self):
        """Start stop/go syringe pump cycle in a background thread."""
        if not self.connected:
            messagebox.showerror("Error", "Not connected to Arduino")
            return

        if self.pump_running:
            messagebox.showinfo("Pump Running", "Syringe pump is already running")
            return

        try:
            cycle_volume_ml = float(self.pump_cycle_volume_ml_var.get())
            pause_seconds = float(self.pump_pause_seconds_var.get())
            flow_ml_s = float(self.pump_speed_ml_s_var.get())
            cycle_count = int(self.pump_cycle_count_var.get())

            if cycle_volume_ml == 0:
                messagebox.showerror("Error", "Cycle volume must be non-zero")
                return
            if pause_seconds < 0:
                messagebox.showerror("Error", "Pause time cannot be negative")
                return
            if flow_ml_s <= 0:
                messagebox.showerror("Error", "Flow must be greater than 0")
                return
            if cycle_count < 0:
                messagebox.showerror("Error", "Cycles must be 0 or greater")
                return

            self.pump_stop_event.clear()
            self.pump_running = True
            self.pump_go_btn.config(state=tk.DISABLED)
            self.log_message(
                f"[Pump] Started: {cycle_volume_ml} ml per cycle, {pause_seconds} s pause, {flow_ml_s} ml/s, cycles={cycle_count}"
            )

            self.pump_thread = threading.Thread(
                target=self._run_syringe_pump,
                args=(cycle_volume_ml, pause_seconds, flow_ml_s, cycle_count),
                daemon=True,
            )
            self.pump_thread.start()

        except ValueError:
            messagebox.showerror("Error", "Please enter valid syringe pump numeric values")

    def stop_syringe_pump(self, silent=False):
        """Stop the syringe pump cycle and issue a stop command."""
        self.pump_stop_event.set()

        if self.connected:
            try:
                speed_steps = self.convert_speed_to_steps_per_sec(
                    float(self.pump_speed_ml_s_var.get()),
                    "ml/s",
                    float(self.steps_per_mm_var.get()),
                    float(self.mm_per_ml_var.get()),
                )
                if speed_steps <= 0:
                    speed_steps = 1
                self.send_motion_command(
                    self.axis_var.get(),
                    0,
                    speed_steps,
                    0,
                    is_stop=True,
                    speed_value=float(self.pump_speed_ml_s_var.get()),
                    speed_unit="ml/s",
                )
            except Exception:
                pass

        if self.pump_running and not silent:
            self.log_message("[Pump] Stop requested")

        self.pump_running = False
        if self.connected:
            self.pump_go_btn.config(state=tk.NORMAL)

    def _run_syringe_pump(self, cycle_volume_ml, pause_seconds, flow_ml_s, cycle_count):
        """Worker loop for stop/go syringe pumping."""
        try:
            steps_per_mm = float(self.steps_per_mm_var.get())
            mm_per_ml = float(self.mm_per_ml_var.get())
            move_steps = self.convert_distance_to_steps(cycle_volume_ml, "ml", steps_per_mm, mm_per_ml)
            speed_steps = self.convert_speed_to_steps_per_sec(flow_ml_s, "ml/s", steps_per_mm, mm_per_ml)

            if speed_steps <= 0:
                self.log_message("[Pump] ERROR: Converted speed must be > 0")
                return

            cycle_index = 0
            infinite_mode = cycle_count == 0
            while not self.pump_stop_event.is_set() and (infinite_mode or cycle_index < cycle_count):
                cycle_index += 1
                self.send_motion_command(
                    self.axis_var.get(),
                    move_steps,
                    speed_steps,
                    cycle_volume_ml,
                    is_stop=False,
                    move_unit="ml",
                    speed_value=flow_ml_s,
                    speed_unit="ml/s",
                )

                # Estimate movement duration from commanded flow and volume.
                move_seconds = abs(cycle_volume_ml) / flow_ml_s
                if self._wait_with_stop(move_seconds):
                    break

                if pause_seconds > 0 and (infinite_mode or cycle_index < cycle_count):
                    self.log_message(f"[Pump] Pausing for {pause_seconds} s")
                    if self._wait_with_stop(pause_seconds):
                        break

            if self.pump_stop_event.is_set():
                self.log_message("[Pump] Stopped")
            else:
                self.log_message("[Pump] Completed scheduled cycles")
        except Exception as e:
            self.log_message(f"[Pump] ERROR: {e}")
        finally:
            self.pump_running = False
            if self.connected:
                self.root.after(0, lambda: self.pump_go_btn.config(state=tk.NORMAL))

    def _wait_with_stop(self, seconds):
        """Wait for a duration while remaining interruptible by pump stop."""
        end_time = time.time() + seconds
        while time.time() < end_time:
            if self.pump_stop_event.is_set() or not self.connected:
                return True
            time.sleep(0.05)
        return False

    def send_motion_command(self, primary_axis, steps, speed_steps, original_value, is_stop=False, move_unit=None, speed_value=None, speed_unit=None):
        """Send a command to the selected axis and any tied axis."""
        axes = [primary_axis]
        linked_axis = self.linked_axis_var.get()
        if linked_axis != "None" and linked_axis != primary_axis:
            axes.append(linked_axis)

        display_move_unit = move_unit if move_unit is not None else self.move_unit_var.get()
        display_speed_value = speed_value if speed_value is not None else self.speed_var.get()
        display_speed_unit = speed_unit if speed_unit is not None else self.speed_unit_var.get()

        for axis in axes:
            command = f"{axis}{steps},{speed_steps}\n"
            with self.serial_lock:
                self.ser.write(command.encode())
            if is_stop:
                self.log_message(f"[Command] {axis}0,{speed_steps} (STOP)")
            else:
                self.log_message(
                    f"[Command] {axis}{steps},{speed_steps} "
                    f"(move: {original_value} {display_move_unit}, speed: {display_speed_value} {display_speed_unit})"
                )

    def convert_distance_to_steps(self, value, unit, steps_per_mm, mm_per_ml):
        """Convert displacement value from selected unit to motor steps."""
        if unit == "steps":
            return int(round(value))
        if unit == "mm":
            return int(round(value * steps_per_mm))
        if unit == "ml":
            mm_value = value * mm_per_ml
            return int(round(mm_value * steps_per_mm))
        raise ValueError(f"Unsupported move unit: {unit}")

    def convert_speed_to_steps_per_sec(self, value, unit, steps_per_mm, mm_per_ml):
        """Convert speed value from selected unit to steps per second."""
        if unit == "steps/s":
            return int(round(value))
        if unit == "mm/s":
            return int(round(value * steps_per_mm))
        if unit == "ml/s":
            mm_per_sec = value * mm_per_ml
            return int(round(mm_per_sec * steps_per_mm))
        raise ValueError(f"Unsupported speed unit: {unit}")

    def log_message(self, message):
        """Log message to console"""
        self.console.config(state=tk.NORMAL)
        self.console.insert(tk.END, message + "\n")
        self.console.see(tk.END)
        self.console.config(state=tk.DISABLED)

    def on_closing(self):
        """Handle window closing"""
        self.save_scale_settings()
        self.stop_syringe_pump(silent=True)
        if self.connected:
            self.disconnect_serial()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = StepperControllerGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()