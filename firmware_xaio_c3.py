
### Completed Unified Firmware (`main.py` or `firmware.py`)

import machine
import time
import struct
import os
import sys
import esp32

# --- Hardware Setup ---
# Standard Blue LED on GPIO 2
led = machine.Pin(2, machine.Pin.OUT)
p_sw = 0
#power switch count
# Button on GP5 with internal pull-up
# (Note: An external 10k pull-up resistor from GP5 to 3.3V is highly recommended 
# to keep the pin from floating and causing accidental wake-ups during deep sleep)
btn_pin = machine.Pin(5, machine.Pin.IN, machine.Pin.PULL_UP)

# --- DEEP SLEEP WAKE DEBOUNCE ---
# If we just woke up from sleep, the user might still be holding the button down.
# We wait for them to release it so it doesn't instantly trigger a "Power Off".
if machine.reset_cause() == machine.DEEPSLEEP_RESET:
    print("Woken up by Power Button. Waiting for release...")
    while btn_pin.value() == 0:
        time.sleep_ms(20)
    time.sleep_ms(200) # Short debounce delay after release

# I2C for BMP180 on XIAO ESP32-C3 (SDA=GP6, SCL=GP7)
try:
    i2c = machine.I2C(0, sda=machine.Pin(6), scl=machine.Pin(7), freq=400000)
    BMP180_ADDR = 0x77
    cal_bytes = i2c.readfrom_mem(BMP180_ADDR, 0xAA, 22)
    AC1, AC2, AC3, AC4, AC5, AC6, B1, B2, MB, MC, MD = struct.unpack(">hhhHHHhhhhh", cal_bytes)
except Exception as e:
    print("Hardware Init Error:", e)
    sys.exit()

def read_raw_data():
    try:
        i2c.writeto_mem(BMP180_ADDR, 0xF4, b'\x2E')
        time.sleep_ms(5)
        UT = struct.unpack(">H", i2c.readfrom_mem(BMP180_ADDR, 0xF6, 2))[0]
        i2c.writeto_mem(BMP180_ADDR, 0xF4, b'\x34')
        time.sleep_ms(5)
        UP = struct.unpack(">H", i2c.readfrom_mem(BMP180_ADDR, 0xF6, 2))[0]
        return UT, UP
    except:
        return None, None

def calculate_altitude(UT, UP):
    if UT is None or UP is None: 
        return 0.0
    X1 = (UT - AC6) * AC5 >> 15
    X2 = (MC << 11) // (X1 + MD)
    B5 = X1 + X2
    B6 = B5 - 4000
    X1 = (B2 * (B6 * B6 >> 12)) >> 11
    X2 = AC2 * B6 >> 11
    X3 = X1 + X2
    B3 = (((AC1 * 4 + X3) + 2) >> 2)
    X1 = AC3 * B6 >> 13
    X2 = (B1 * (B6 * B6 >> 12)) >> 16
    X3 = ((X1 + X2) + 2) >> 2
    B4 = (AC4 * (X3 + 32768)) >> 15
    B7 = (UP - B3) * 50000
    p = (B7 * 2) // B4 if B7 < 0x80000000 else (B7 // B4) * 2
    X1 = (p >> 8) * (p >> 8)
    X1 = (X1 * 3038) >> 16
    X2 = (-7357 * p) >> 16
    pressure = p + ((X1 + X2 + 3791) >> 4)
    return 44330.0 * (1.0 - pow(pressure / 101325.0, 0.1903))

# --- File Management ---
def get_next_flight_name():
    n = 1
    files = os.listdir()
    while f"flight_{n:03d}.csv" in files: 
        n += 1
    return f"flight_{n:03d}.csv"

def open_log_file(name):
    try:
        f = open(name, "a")
        if os.stat(name)[6] == 0:
            f.write("time_ms,raw_alt,smooth_alt,speed_mph\n")
        return f
    except:
        return None

# --- Serial Input Setup (Non-blocking WebREPL/USB Poller) ---
use_usb_input = False
poller = None
uart = None

try:
    import select
    poller = select.poll()
    poller.register(sys.stdin, select.POLLIN)
    use_usb_input = True
except:
    try:
        # Fallback to UART connection on GP21/GP20 if terminal environment demands it
        uart = machine.UART(1, baudrate=115200, tx=machine.Pin(21), rx=machine.Pin(20))
    except:
        pass

def read_command():
    if use_usb_input and poller:
        if poller.poll(0):
            line = sys.stdin.readline()
            if line: 
                return line.strip()
    elif uart:
        if uart.any():
            line = uart.readline()
            if line:
                try: return line.decode().strip()
                except: return line.strip()
    return None

# --- Baseline Calibration Routine ---
def run_calibration(samples=20):
    prev_led = led.value()
    led.value(1) # Force LED on during calculation work
    total_val, got_val = 0.0, 0
    for _ in range(samples * 2):
        ut, up = read_raw_data()
        if ut is not None:
            total_val += calculate_altitude(ut, up)
            got_val += 1
            if got_val >= samples:
                break
        time.sleep_ms(20)
    led.value(prev_led)
    if got_val > 0:
        return total_val / got_val
    return 0.0

# --- Main Initialization ---
flight_name = get_next_flight_name()
print("Logging to", flight_name)

baseline = run_calibration(20)
print("Calibration complete. Baseline:", baseline)

smooth_alt = 0.0
prev_smooth_alt = 0.0
smooth_speed_mph = 0.0
ALT_ALPHA = 0.15
SPD_ALPHA = 0.10
last_time = time.ticks_ms()

log = open_log_file(flight_name)
logging_enabled = True if log else False
if logging_enabled: 
    led.value(1) # Solid LED means actively running/logging

# --- Interactive Command Handling Console Engine ---
def handle_command(cmd):
    global logging_enabled, log, flight_name, baseline, ALT_ALPHA, SPD_ALPHA
    parts = cmd.split()
    if not parts:
        return
    c = parts[0].lower()
    if c == "start":
        if logging_enabled:
            print("Already logging to", flight_name)
        else:
            log = open_log_file(flight_name)
            logging_enabled = bool(log)
            if logging_enabled:
                led.value(1)
            print("Logging started:", flight_name)
            
    elif c == "stop":
        if log:
            try:
                log.close()
            except:
                pass
        logging_enabled = False
        log = None
        led.value(0)
        print("Logging stopped.")
        
        
    elif c == "calibrate":
        print("Re-calibrating baseline...")
        b = calibrate_baseline()
        if b is not None:
            baseline = b
    elif c == "set_alpha" and len(parts) >= 2:
        try:
            ALT_ALPHA = float(parts[1])
            print("ALT_ALPHA set to", ALT_ALPHA)
        except:
            print("Invalid value for set_alpha")
    elif c == "set_spd_alpha" and len(parts) >= 2:
        try:
            SPD_ALPHA = float(parts[1])
            print("SPD_ALPHA set to", SPD_ALPHA)
        except:
            print("Invalid value for set_spd_alpha")
    elif c == "set_baseline" and len(parts) >= 2:
        try:
            baseline = float(parts[1])
            print("Baseline set to", baseline)
        except:
            print("Invalid baseline value")
    elif c == "filename" and len(parts) >= 2:
        newname = parts[1]
        if log:
            try:
                log.close()
            except:
                pass
        flight_name = newname
        log = open_log_file(flight_name)
        logging_enabled = bool(log)
        print("Switched filename to", flight_name)
    elif c == "status":
        print("Status:")
        print("  logging:", logging_enabled)
        print("  file:", flight_name)
        print("  baseline:", baseline)
        print("  ALT_ALPHA:", ALT_ALPHA)
        print("  SPD_ALPHA:", SPD_ALPHA)
    elif c == "led":
            
        print("Neopixel not avaliable on hardware.")
       
    elif c == "flush":
        if log:
            try:
                log.flush()
                print("Flushed log.")
            except Exception as e:
                print("Flush error:", e)
        else:
            print("No open log to flush.")
            
    elif c == "delete" and len(parts) >= 2:
        fname = parts[1]
        try:
            import os
            os.remove(fname)
            print("Deleted", fname)
        except Exception as e:
            print("delete error:", e)

    elif c == "cat" and len(parts) >= 2:
        fname = parts[1]
        try:
            with open(fname, "r") as f:
                for line in f:
                    print(line, end="")
        except Exception as e:
            print("cat error:", e)
    elif c == "prgms":
        try:
            print("!DO NOT MODIFY MAIN.PY!")
            import os
            for name in os.listdir():
                if name.endswith(".py"):
                   print(name)
        except Exception as e:
            print("ls error:", e)
    elif c == "ls":
        try:
            import os
            for name in os.listdir():
                print(name)
        except Exception as e:
            print("ls error:", e)
        
    
    elif c == "clear":
        try:
            import os
            count = 0
            for fname in os.listdir():
                if fname.startswith("flight_") and fname.endswith(".csv"):
                    # Don't delete the file we are actively logging to
                    if fname == flight_name and logging_enabled:
                        continue
                    os.remove(fname)
                    count += 1
            print(f"Deleted {count} flight log files.")
        except Exception as e:
            print("Clear error:", e)
    
    elif c =="help":
        
        print("clear - deletes all flights stored on device\nprgms - lists all programs installed \nrun - execute script files\nstart - begin logging\nstop - stop logging\nstatus - show logger state\ncalibrate - recalibrate baseline\nset_alpha <val> - set altitude filter alpha\nset_spd_alpha <val> - set speed filter alpha\nset_baseline <m> - manually set baseline\nfilename <name> - switch log file\nflush - flush file to storage\nexport [file] - send file to terminal \nled <r> <g> <b> - set LED color\nexit - stop program")
    elif c == "export":
        # export [filename] -> stream file to console (UART or USB REPL)
        fname = parts[1] if len(parts) >= 2 else flight_name
        try:
            # open in binary to preserve exact bytes
            with open(fname, "rb") as f:
                if uart:
                    # send simple markers and stream bytes
                    try:
                        uart.write(b"---BEGIN FILE: " + fname.encode() + b"---\n")
                    except:
                        pass
                    while True:
                        chunk = f.read(256)
                        if not chunk:
                            break
                        try:
                            uart.write(chunk)
                        except Exception:
                            # if write fails, stop to avoid lockup
                            break
                    try:
                        uart.write(b"\n---END FILE---\n")
                    except:
                        pass
                else:
                    # USB REPL: print text, decode bytes with replacement for non-UTF8
                    print("---BEGIN FILE:", fname, "---")
                    while True:
                        chunk = f.read(1024)
                        if not chunk:
                            break
                        try:
                            sys.stdout.write(chunk.decode("utf-8", "replace"))
                        except Exception:
                            sys.stdout.write(chunk.decode("latin-1", "replace"))
                    print("\n---END FILE---")
            print("Export complete:", fname)
        except Exception as e:
            print("Export error:", e)
    elif c == "exit":
        raise KeyboardInterrupt
    elif c == "run" and len(parts) >= 2:
        fname = parts[1]
        try:
            with open(fname, "r") as f:
                    exec(f.read())
        except Exception as e:
            print("prgm error:", e)
            
    elif c == "analyze":
        try:
            with open("logviewer.py", "r") as f:
                code = f.read()
            exec(code, {})
        except Exception as e:
            print("analyze error:", e)
    else:
        print("Unknown command:", cmd)


# --- Main Runtime Loop ---
try:
    print("System active. Press GP5 button at any time to Power Off.")
    while True:
        # 1. CHECK POWER BUTTON (Turn Off Request)
        if btn_pin.value() == 0:
            time.sleep_ms(50) # Hardware debounce gate
            if btn_pin.value() == 0:
                print("Power button detected. Shutting down cleanly...")
                
                # Visual feedback flash: blink LED 3 times before sleeping
                for _ in range(3):
                    led.value(0); time.sleep_ms(100)
                    led.value(1); time.sleep_ms(100)
                led.value(0)
                
                if log:
                    try: log.close()
                    except: pass
                
                # Setup GP5 to wake the device when pressed (pulled LOW)
                esp32.wake_on_gpio(pins=(btn_pin,), level=esp32.WAKEUP_ALL_LOW)
                
                print("Entering deep sleep mode. Press GP5 again to turn ON.")
                machine.deepsleep() # Execution processing drops offline here

        # 2. CHECK SERIAL COMMANDS
        cmd = read_command()
        if cmd:
            print("CMD>", cmd)
            # Short LED pulse acknowledging command line reception receipt
            current_led_state = led.value()
            led.value(not current_led_state); time.sleep_ms(40); led.value(current_led_state)
            
            if cmd.lower() == "exit": 
                raise KeyboardInterrupt
            try:
                handle_command(cmd)
            except Exception as cmd_err:
                print("Console operational crash handler captured:", cmd_err)

        # 3. SENSOR & LOGGING LOGIC
        current_time = time.ticks_ms()
        dt = time.ticks_diff(current_time, last_time) / 1000.0

        ut, up = read_raw_data()
        if ut is not None:
            raw_alt = calculate_altitude(ut, up) - baseline
            smooth_alt = (ALT_ALPHA * raw_alt) + (1.0 - ALT_ALPHA) * smooth_alt

            if dt > 0:
                inst_speed_ms = (smooth_alt - prev_smooth_alt) / dt
                inst_speed_mph = inst_speed_ms * 2.23694
                smooth_speed_mph = (SPD_ALPHA * inst_speed_mph) + (1.0 - SPD_ALPHA) * smooth_speed_mph

            prev_smooth_alt = smooth_alt
            last_time = current_time

            if logging_enabled and log:
                try:
                    log.write(f"{current_time},{raw_alt:.2f},{smooth_alt:.2f},{smooth_speed_mph:.2f}\n")
                except:
                    logging_enabled = False

            # Intermittent disk write synchronization check window
            if (current_time & 0x3FF) < 20 and logging_enabled and log:
                try: log.flush()
                except: pass

        time.sleep_ms(20)

except KeyboardInterrupt:
    print("Stopping loop via REPL.")

finally:
    if log:
        try: 
            log.close()
            print("Altimeter flight logs unmounted cleanly.")
        except: pass
    led.value(0)

