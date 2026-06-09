#!/usr/bin/env python3
"""
flight_logger_app.py

Tkinter ground station for the provided MicroPython logger.
- Adds a Plotter window that can load data from the MCU or local CSV files.
- Terminal always visible
- Robust parsing and plotting
"""

import os
import re
import time
import threading
import csv
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import serial
import serial.tools.list_ports
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

SYNC_DIR = "synced_flights"
os.makedirs(SYNC_DIR, exist_ok=True)

FLIGHT_RE = re.compile(r"^flight_\d{3}\.csv$", re.IGNORECASE)

class PlotterWindow:
    """A separate Toplevel window with an embedded Matplotlib canvas.
    Can load data from the MCU (via the main app) or from a local CSV file.
    """
    def __init__(self, parent, read_remote_callback):
        """
        parent: root or main app window
        read_remote_callback: function(fname, timeout) -> raw_text
        """
        self.parent = parent
        self.read_remote = read_remote_callback
        self.win = tk.Toplevel(parent)
        self.win.title("Flight Plotter")
        self.win.geometry("900x600")

        # Top controls
        ctrl = ttk.Frame(self.win)
        ctrl.pack(side="top", fill="x", padx=6, pady=6)

        ttk.Label(ctrl, text="Remote filename:").grid(row=0, column=0, sticky="w")
        self.remote_entry = ttk.Entry(ctrl, width=30)
        self.remote_entry.grid(row=0, column=1, sticky="w", padx=(4,8))
        ttk.Button(ctrl, text="Load from MCU", command=self.load_from_mcu).grid(row=0, column=2, padx=4)
        ttk.Button(ctrl, text="Open Local", command=self.open_local).grid(row=0, column=3, padx=4)
        ttk.Button(ctrl, text="Clear", command=self.clear_plot).grid(row=0, column=4, padx=4)
        ttk.Button(ctrl, text="Save PNG", command=self.save_png).grid(row=0, column=5, padx=4)

        # Stats labels
        stats = ttk.Frame(self.win)
        stats.pack(side="top", fill="x", padx=6)
        ttk.Label(stats, text="File:").grid(row=0, column=0, sticky="w")
        self.lbl_file = ttk.Label(stats, text="—")
        self.lbl_file.grid(row=0, column=1, sticky="w", padx=(4,20))
        ttk.Label(stats, text="Max Altitude:").grid(row=0, column=2, sticky="w")
        self.lbl_max_alt = ttk.Label(stats, text="—")
        self.lbl_max_alt.grid(row=0, column=3, sticky="w", padx=(4,20))
        ttk.Label(stats, text="Max Speed:").grid(row=0, column=4, sticky="w")
        self.lbl_max_speed = ttk.Label(stats, text="—")
        self.lbl_max_speed.grid(row=0, column=5, sticky="w", padx=(4,20))
        ttk.Label(stats, text="Duration (s):").grid(row=0, column=6, sticky="w")
        self.lbl_duration = ttk.Label(stats, text="—")
        self.lbl_duration.grid(row=0, column=7, sticky="w", padx=(4,4))

        # Matplotlib figure
        self.fig, (self.ax_alt, self.ax_spd) = plt.subplots(2,1, figsize=(8,6), sharex=True)
        self.fig.tight_layout(pad=3.0)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.win)
        self.canvas.get_tk_widget().pack(side="top", fill="both", expand=True)

        # Internal data
        self.times_s = []
        self.alts = []
        self.speeds = []
        self.current_fname = None

    def clear_plot(self):
        self.times_s = []
        self.alts = []
        self.speeds = []
        self.current_fname = None
        self.lbl_file.config(text="—")
        self.lbl_max_alt.config(text="—")
        self.lbl_max_speed.config(text="—")
        self.lbl_duration.config(text="—")
        self.ax_alt.clear()
        self.ax_spd.clear()
        self.canvas.draw()

    def save_png(self):
        if not (self.times_s and self.alts):
            messagebox.showinfo("Save PNG", "No plot to save.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG image","*.png")])
        if not path:
            return
        try:
            self.fig.savefig(path)
            messagebox.showinfo("Save PNG", f"Saved to {path}")
        except Exception as e:
            messagebox.showerror("Save PNG", f"Error saving PNG: {e}")

    def open_local(self):
        path = filedialog.askopenfilename(title="Open CSV file", filetypes=[("CSV files","*.csv"),("All files","*.*")])
        if not path:
            return
        try:
            with open(path, "r", newline="") as f:
                raw = f.read()
        except Exception as e:
            messagebox.showerror("Open Local", f"Failed to open file: {e}")
            return
        self.current_fname = os.path.basename(path)
        self._parse_and_plot_raw(raw, self.current_fname)

    def load_from_mcu(self):
        fname = self.remote_entry.get().strip()
        if not fname:
            messagebox.showerror("Load from MCU", "Enter a remote filename (e.g., flight_001.csv).")
            return
        # Fetch in background to avoid blocking UI
        threading.Thread(target=self._fetch_and_plot_remote, args=(fname,), daemon=True).start()

    def _fetch_and_plot_remote(self, fname):
        try:
            raw = self.read_remote(fname, timeout=8.0)
        except Exception as e:
            self.parent.after(0, lambda: messagebox.showerror("Load from MCU", f"Error reading {fname}: {e}"))
            return
        if not raw:
            self.parent.after(0, lambda: messagebox.showerror("Load from MCU", f"No data received for {fname}"))
            return
        self.current_fname = fname
        self.parent.after(0, lambda: self._parse_and_plot_raw(raw, fname))

    def _parse_and_plot_raw(self, raw, fname):
        # tolerant parsing similar to main app
        lines = [ln.strip().replace('\r','') for ln in raw.splitlines() if ln.strip()]
        lines = [ln for ln in lines if not ln.startswith('---BEGIN') and not ln.startswith('---END')]
        if lines and any(c.isalpha() for c in lines[0].split(',')[0]):
            lines = lines[1:]

        times, alts, speeds = [], [], []
        for ln in lines:
            if ',' not in ln:
                continue
            parts = [p.strip() for p in ln.split(',') if p.strip() != ""]
            if not parts:
                continue
            try:
                t = float(parts[0])
            except Exception:
                continue
            try:
                alt = float(parts[1]) if len(parts) > 1 else 0.0
            except Exception:
                continue
            try:
                speed = float(parts[2]) if len(parts) > 2 else 0.0
            except Exception:
                speed = 0.0
            times.append(t)
            alts.append(alt)
            speeds.append(speed)

        if not times or not alts:
            self.show_raw_preview(raw, title=f"Raw preview: {fname}")
            messagebox.showerror("Plotter", "No numeric CSV data found. Preview opened.")
            return

        t0 = times[0]
        times_s = [(tt - t0) / 1000.0 if max(times) > 1e4 else (tt - t0) for tt in times]

        # store
        self.times_s = times_s
        self.alts = alts
        self.speeds = speeds

        # stats
        max_alt = max(alts)
        max_speed = max(speeds) if speeds else 0.0
        duration = times_s[-1] - times_s[0] if len(times_s) > 1 else 0.0

        # update UI
        self.lbl_file.config(text=fname)
        self.lbl_max_alt.config(text=f"{max_alt:.2f}")
        self.lbl_max_speed.config(text=f"{max_speed:.2f}")
        self.lbl_duration.config(text=f"{duration:.2f}")

        # plot
        self.ax_alt.clear()
        self.ax_spd.clear()
        self.ax_alt.plot(times_s, alts, label="Altitude")
        self.ax_alt.set_ylabel("Altitude")
        self.ax_alt.grid(True)
        self.ax_alt.legend()
        self.ax_spd.plot(times_s, speeds, color="orange", label="Speed")
        self.ax_spd.set_xlabel("Time (s)")
        self.ax_spd.set_ylabel("Speed")
        self.ax_spd.grid(True)
        self.ax_spd.legend()
        self.fig.tight_layout()
        self.canvas.draw()

    def show_raw_preview(self, raw, title="Raw preview"):
        preview = "\n".join(raw.splitlines()[:200])
        dlg = tk.Toplevel(self.win)
        dlg.title(title)
        txt = tk.Text(dlg, height=30, width=120)
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", preview)
        btn = ttk.Button(dlg, text="Close", command=dlg.destroy)
        btn.pack(pady=4)


class FlightLoggerApp:
    def __init__(self, root):
        self.root = root
        root.title("Flight Logger - Ground Station")
        self.ser = None
        self.running = False
        self.read_lock = threading.Lock()
        self.recv_buffer = ""
        # terminal is always visible
        self.terminal_visible = True

        # --- Top: Serial controls ---
        frame_top = ttk.Frame(root)
        frame_top.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        ttk.Label(frame_top, text="Serial Port:").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar()
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if ports:
            self.port_var.set(ports[0])
        self.port_menu = ttk.OptionMenu(frame_top, self.port_var, self.port_var.get(), *ports)
        self.port_menu.grid(row=0, column=1, sticky="ew")
        ttk.Button(frame_top, text="Refresh Ports", command=self.refresh_ports).grid(row=0, column=2, padx=4)
        ttk.Button(frame_top, text="Connect", command=self.connect).grid(row=0, column=3, padx=4)
        ttk.Button(frame_top, text="Disconnect", command=self.disconnect).grid(row=0, column=4, padx=4)

        # --- Middle: Terminal (always visible) and file list ---
        frame_mid = ttk.Frame(root)
        frame_mid.grid(row=1, column=0, sticky="nsew", padx=6)
        root.rowconfigure(1, weight=1)
        root.columnconfigure(0, weight=1)

        # Terminal output (always visible)
        self.output = tk.Text(frame_mid, height=12, bg="black", fg="lime", wrap="none")
        self.output.grid(row=0, column=0, columnspan=3, sticky="nsew", pady=(0,6))
        frame_mid.rowconfigure(0, weight=0)

        # File list
        ttk.Label(frame_mid, text="Filesystem:").grid(row=1, column=0, sticky="w", pady=(0,4))
        self.file_list = tk.Listbox(frame_mid, height=12)
        self.file_list.grid(row=2, column=0, sticky="nsew", pady=(0,6))
        frame_mid.rowconfigure(2, weight=1)
        frame_mid.columnconfigure(0, weight=1)

        # Bind double-click to graph the selected file
        self.file_list.bind("<Double-1>", lambda e: self.graph_selected())

        file_btns = ttk.Frame(frame_mid)
        file_btns.grid(row=2, column=1, sticky="n", padx=6)
        ttk.Button(file_btns, text="Refresh Files", command=self.refresh_files).grid(row=0, column=0, pady=2)
        ttk.Button(file_btns, text="Open Flight", command=self.open_flight).grid(row=1, column=0, pady=2)
        ttk.Button(file_btns, text="Graph Flight", command=self.graph_selected).grid(row=2, column=0, pady=2)
        ttk.Button(file_btns, text="Open Local", command=self.open_local_file).grid(row=3, column=0, pady=2)
        ttk.Button(file_btns, text="Open Plotter", command=self.open_plotter).grid(row=4, column=0, pady=2)
        ttk.Button(file_btns, text="Delete File", command=self.delete_file).grid(row=5, column=0, pady=2)
        ttk.Button(file_btns, text="Export Local", command=self.export_local).grid(row=6, column=0, pady=2)

        # Flight info panel (shows stats for selected/last-graph)
        info_frame = ttk.LabelFrame(frame_mid, text="Flight Info")
        info_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6,0))
        info_frame.columnconfigure(1, weight=1)
        ttk.Label(info_frame, text="File:").grid(row=0, column=0, sticky="w")
        self.info_file = ttk.Label(info_frame, text="—")
        self.info_file.grid(row=0, column=1, sticky="w")
        ttk.Label(info_frame, text="Max Altitude:").grid(row=1, column=0, sticky="w")
        self.info_max_alt = ttk.Label(info_frame, text="—")
        self.info_max_alt.grid(row=1, column=1, sticky="w")
        ttk.Label(info_frame, text="Max Speed:").grid(row=2, column=0, sticky="w")
        self.info_max_speed = ttk.Label(info_frame, text="—")
        self.info_max_speed.grid(row=2, column=1, sticky="w")
        ttk.Label(info_frame, text="Duration (s):").grid(row=3, column=0, sticky="w")
        self.info_duration = ttk.Label(info_frame, text="—")
        self.info_duration.grid(row=3, column=1, sticky="w")

        # --- Bottom: Command entry and quick buttons ---
        frame_bot = ttk.Frame(root)
        frame_bot.grid(row=2, column=0, sticky="ew", padx=6, pady=6)

        ttk.Label(frame_bot, text="Command:").grid(row=0, column=0, sticky="w")
        self.cmd_entry = ttk.Entry(frame_bot, width=60)
        self.cmd_entry.grid(row=0, column=1, sticky="ew")
        ttk.Button(frame_bot, text="Send", command=self.send_cmd).grid(row=0, column=2, padx=4)

        quick = ttk.Frame(frame_bot)
        quick.grid(row=1, column=0, columnspan=3, pady=6, sticky="w")
        ttk.Button(quick, text="start", command=lambda: self.send("start")).grid(row=0, column=0, padx=2)
        ttk.Button(quick, text="stop", command=lambda: self.send("stop")).grid(row=0, column=1, padx=2)
        ttk.Button(quick, text="status", command=lambda: self.send("status")).grid(row=0, column=2, padx=2)
        ttk.Button(quick, text="ls", command=self.refresh_files).grid(row=0, column=3, padx=2)

        # initial UI state
        self._append_local("Ready. Select a serial port and Connect.\n")
        self.plotter = None

    # --- Serial helpers ---
    def refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        menu = self.port_menu["menu"]
        menu.delete(0, "end")
        for p in ports:
            menu.add_command(label=p, command=lambda v=p: self.port_var.set(v))
        if ports:
            self.port_var.set(ports[0])

    def connect(self):
        if self.ser:
            self._append_local("Already connected.\n")
            return
        port = self.port_var.get()
        if not port:
            self._append_local("No serial port selected.\n")
            return
        try:
            self.ser = serial.Serial(port, 115200, timeout=0.1)
            self.running = True
            threading.Thread(target=self.reader_thread, daemon=True).start()
            self._append_local(f"Connected to {port}\n")
            # Sync on connect in background
            threading.Thread(target=self.sync_on_connect, daemon=True).start()
            # ensure device is not logging when we connect
            self.send("stop")
        except Exception as e:
            self._append_local(f"Connect error: {e}\n")
            self.ser = None

    def disconnect(self):
        self.running = False
        if self.ser:
            try:
                self.ser.close()
            except:
                pass
            self.ser = None
        self._append_local("Disconnected.\n")

    def reader_thread(self):
        while self.running:
            try:
                if self.ser and self.ser.in_waiting:
                    data = self.ser.read(self.ser.in_waiting)
                    text = data.decode("utf-8", "replace")
                    with self.read_lock:
                        self.recv_buffer += text
                    self._append_local(text)
                else:
                    time.sleep(0.02)
            except Exception as e:
                self._append_local(f"Read error: {e}\n")
                time.sleep(0.2)

    def send(self, text):
        if not self.ser:
            self._append_local("Not connected.\n")
            return
        try:
            self.ser.write((text + "\n").encode())
            self._append_local(f"> {text}\n")
        except Exception as e:
            self._append_local(f"Send error: {e}\n")

    def send_cmd(self):
        cmd = self.cmd_entry.get().strip()
        if cmd:
            self.send(cmd)
            self.cmd_entry.delete(0, tk.END)

    def _append_local(self, text):
        def _insert():
            # terminal always visible, so insert directly
            self.output.insert(tk.END, text)
            self.output.see(tk.END)
        self.root.after(0, _insert)

    # --- Sync on connect ---
    def sync_on_connect(self):
        time.sleep(0.15)
        self.refresh_files()
        time.sleep(0.2)
        files = list(self.file_list.get(0, tk.END))
        if not files:
            self._append_local("No flight files found to sync.\n")
            return
        self._append_local(f"Syncing {len(files)} files to '{SYNC_DIR}'...\n")
        for fname in files:
            try:
                raw = self.read_remote_file_text(fname, timeout=6.0)
                if raw:
                    path = os.path.join(SYNC_DIR, fname)
                    with open(path, "w", newline="") as f:
                        f.write(raw)
                    self._append_local(f"Saved {fname}\n")
                else:
                    self._append_local(f"No data for {fname}\n")
            except Exception as e:
                self._append_local(f"Sync error {fname}: {e}\n")
        self._append_local("Sync complete.\n")

    # --- File list and parsing (strict) ---
    def refresh_files(self):
        """Send ls, wait briefly, parse only flight_###.csv filenames."""
        if not self.ser:
            self._append_local("Not connected.\n")
            return
        self.file_list.delete(0, tk.END)
        with self.read_lock:
            self.recv_buffer = ""
        self.send("ls")
        time.sleep(0.35)
        with self.read_lock:
            data = self.recv_buffer
        lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
        files = []
        for ln in lines:
            # ignore echoed commands and errors
            if ln.startswith(">") or ln.lower().startswith("ls error") or ln.lower().startswith("error"):
                continue
            # accept only strict flight filenames
            if FLIGHT_RE.match(ln):
                files.append(ln)
        # fallback: if none found, try looser match (numbers)
        if not files:
            for ln in lines:
                if FLIGHT_RE.match(ln):
                    files.append(ln)
        for f in files:
            self.file_list.insert(tk.END, f)
        self._append_local(f"Found {len(files)} flight files.\n")

    def read_remote_file_text(self, fname, timeout=4.0):
        """
        Try 'export fname' first (framed). If no framed content, fall back to 'cat fname'.
        Return the file content as a text string.
        """
        if not self.ser:
            raise RuntimeError("Not connected")
        # Try export (framed)
        with self.read_lock:
            self.recv_buffer = ""
        self.send(f"export {fname}")
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(0.05)
            with self.read_lock:
                buf = self.recv_buffer
            if '---BEGIN FILE:' in buf and '---END FILE---' in buf:
                # extract content between first begin newline and end marker
                start = buf.find('---BEGIN FILE:')
                begin_line_end = buf.find('\n', start)
                if begin_line_end == -1:
                    begin_line_end = start
                end = buf.find('---END FILE---', begin_line_end)
                if end != -1:
                    content = buf[begin_line_end+1:end]
                    return content
        # Fallback to cat
        with self.read_lock:
            self.recv_buffer = ""
        self.send(f"cat {fname}")
        t0 = time.time()
        last_len = 0
        while time.time() - t0 < timeout:
            time.sleep(0.05)
            with self.read_lock:
                cur = len(self.recv_buffer)
            if cur > 0 and cur == last_len:
                break
            last_len = cur
        with self.read_lock:
            data = self.recv_buffer
        return data

    # --- Local open and parsing helpers ---
    def open_local_file(self):
        """Open a CSV from local disk, parse, update Flight Info, and graph."""
        path = filedialog.askopenfilename(title="Open CSV file", filetypes=[("CSV files","*.csv"),("All files","*.*")])
        if not path:
            return
        try:
            with open(path, "r", newline="") as f:
                raw = f.read()
        except Exception as e:
            messagebox.showerror("Open Local", f"Failed to open file: {e}")
            return

        # Save a copy into synced_flights for convenience
        try:
            dst = os.path.join(SYNC_DIR, os.path.basename(path))
            with open(dst, "w", newline="") as out:
                out.write(raw)
        except Exception:
            pass

        self._parse_and_update_info(raw, os.path.basename(path))

    def _parse_and_update_info(self, raw, fname):
        """Parse raw CSV text and update the Flight Info panel (no plotting)."""
        lines = [ln.strip().replace('\r','') for ln in raw.splitlines() if ln.strip()]
        lines = [ln for ln in lines if not ln.startswith('---BEGIN') and not ln.startswith('---END')]
        if lines and any(c.isalpha() for c in lines[0].split(',')[0]):
            lines = lines[1:]

        times, alts, speeds = [], [], []
        for ln in lines:
            if ',' not in ln:
                continue
            parts = [p.strip() for p in ln.split(',') if p.strip() != ""]
            if not parts:
                continue
            try:
                t = float(parts[0])
            except Exception:
                continue
            try:
                alt = float(parts[1]) if len(parts) > 1 else 0.0
            except Exception:
                continue
            try:
                speed = (float(parts[2]) if len(parts) > 2 else 0.0)/10000
            except Exception:
                speed = 0.0
            times.append(t)
            alts.append(alt)
            speeds.append(speed)

        if not times or not alts:
            self.show_raw_preview(raw, title=f"Raw preview: {fname}")
            messagebox.showerror("Open Local", "No numeric CSV data found. Preview opened.")
            return

        t0 = times[0]
        times_s = [(tt - t0) / 1000.0 if max(times) > 1e4 else (tt - t0) for tt in times]
        max_alt = max(alts)
        max_speed = max(speeds) if speeds else 0.0
        duration = times_s[-1] - times_s[0] if len(times_s) > 1 else 0.0

        # Update Flight Info panel
        try:
            self.info_file.config(text=fname)
            self.info_max_alt.config(text=f"{max_alt:.2f}")
            self.info_max_speed.config(text=f"{max_speed:.2f}")
            self.info_duration.config(text=f"{duration:.2f}")
        except Exception:
            pass

    def show_raw_preview(self, raw, title="Raw transfer preview"):
        preview = "\n".join(raw.splitlines()[:200])
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        txt = tk.Text(dlg, height=30, width=120)
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", preview)
        btn = ttk.Button(dlg, text="Close", command=dlg.destroy)
        btn.pack(pady=4)

    # --- Existing remote file functions (open_flight, graph_selected, delete, export) ---
    def open_flight(self):
        sel = self.file_list.curselection()
        if not sel:
            messagebox.showerror("Open Flight", "No file selected.")
            return
        fname = self.file_list.get(sel[0])
        try:
            raw = self.read_remote_file_text(fname, timeout=6.0)
        except Exception as e:
            messagebox.showerror("Open Flight", f"Error reading file: {e}")
            return
        self._parse_and_update_info(raw, fname)

    def graph_selected(self):
        sel = self.file_list.curselection()
        if not sel:
            messagebox.showerror("Graph Flight", "No file selected.")
            return
        fname = self.file_list.get(sel[0])
        try:
            raw = self.read_remote_file_text(fname, timeout=6.0)
        except Exception as e:
            messagebox.showerror("Graph Flight", f"Error reading file: {e}")
            return
        # If plotter is open, use it; otherwise open a new one
        if not self.plotter or not tk.Toplevel.winfo_exists(self.plotter.win):
            self.open_plotter()
        # set remote filename in plotter and plot
        self.plotter.remote_entry.delete(0, tk.END)
        self.plotter.remote_entry.insert(0, fname)
        self.plotter._fetch_and_plot_remote(fname)

    def delete_file(self):
        sel = self.file_list.curselection()
        if not sel:
            messagebox.showerror("Delete", "No file selected.")
            return
        fname = self.file_list.get(sel[0])
        if not messagebox.askyesno("Delete", f"Delete {fname}?"):
            return
        self.send(f"delete {fname}")
        time.sleep(0.2)
        self.refresh_files()

    def export_local(self):
        sel = self.file_list.curselection()
        if not sel:
            messagebox.showerror("Export", "No file selected.")
            return
        fname = self.file_list.get(sel[0])
        try:
            raw = self.read_remote_file_text(fname, timeout=8.0)
        except Exception as e:
            messagebox.showerror("Export", f"Error reading file: {e}")
            return
        save_path = filedialog.asksaveasfilename(title="Save file as", initialfile=fname)
        if not save_path:
            return
        try:
            with open(save_path, "w", newline="") as f:
                f.write(raw)
            messagebox.showinfo("Export", f"Saved to {save_path}")
        except Exception as e:
            messagebox.showerror("Export", f"Save error: {e}")

    def open_plotter(self):
        if self.plotter and tk.Toplevel.winfo_exists(self.plotter.win):
            self.plotter.win.lift()
            return
        # pass the read_remote_file_text method as callback
        self.plotter = PlotterWindow(self.root, self.read_remote_file_text)

if __name__ == "__main__":
    root = tk.Tk()
    app = FlightLoggerApp(root)
    root.geometry("1100x800")
    root.mainloop()
