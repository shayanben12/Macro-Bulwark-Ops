import cv2
import numpy as np
import subprocess
import time
import json
import sys
import os
import threading
import ctypes
from functools import lru_cache

# Path to the ADB executable. Ensure this folder exists in the same directory as the script.
ADB_CMD = ".\\adb_tools\\adb.exe"

# Global shared dictionaries and synchronization locks
emulator_statuses = {} 
emulator_cycles = {}   
global_cycles_lock = threading.Lock() # Protects file writes to cycles.json
ui_state_lock = threading.Lock()      # Protects shared dictionary access across threads

# =======================================================
# IN-MEMORY TEMPLATE CACHE
# =======================================================
@lru_cache(maxsize=128)
def get_cached_template(template_path):
    """Loads an image template into RAM once and caches it for all future scans."""
    if not os.path.exists(template_path):
        return None
    return cv2.imread(template_path)

# =======================================================
# AUTO-DETECTION: GET EMULATOR NAME AUTOMATICALLY
# =======================================================
def get_emulator_name_from_port(port):
    """
    Finds the actual Windows Window Title (e.g., 'Account 1') using the ADB port. 
    If it fails, it calculates the MuMu Instance number mathematically.
    """
    port_num = int(port)
    calculated_instance = int(((port_num - 16384) / 32) + 1)
    fallback_name = f"Instance {calculated_instance}" if port_num >= 16384 else f"Port {port}"
    
    if os.name != 'nt': 
        return fallback_name
        
    try:
        netstat = subprocess.run(f"netstat -ano | findstr LISTENING | findstr :{port}", shell=True, capture_output=True, text=True)
        if not netstat.stdout:
            return fallback_name
            
        first_line = netstat.stdout.strip().split('\n')[0]
        pid = int(first_line.split()[-1])
        
        EnumWindows = ctypes.windll.user32.EnumWindows
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
        GetWindowThreadProcessId = ctypes.windll.user32.GetWindowThreadProcessId
        GetWindowText = ctypes.windll.user32.GetWindowTextW
        GetWindowTextLength = ctypes.windll.user32.GetWindowTextLengthW
        IsWindowVisible = ctypes.windll.user32.IsWindowVisible

        titles = []
        def foreach_window(hwnd, lParam):
            if IsWindowVisible(hwnd):
                window_pid = ctypes.c_uint(0)
                GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
                if window_pid.value == pid:
                    length = GetWindowTextLength(hwnd)
                    if length > 0:
                        buff = ctypes.create_unicode_buffer(length + 1)
                        GetWindowText(hwnd, buff, length + 1)
                        titles.append(buff.value)
            return True
        
        EnumWindows(EnumWindowsProc(foreach_window), 0)
        
        if titles:
            raw_title = titles[0]
            clean_title = raw_title.split(" - MuMu")[0]
            return clean_title
            
    except Exception:
        pass
        
    return fallback_name

# =======================================================
# MEMORY SYSTEM (Save/Load Cycles by Custom Name)
# =======================================================
def load_saved_cycles():
    """Loads cycle counts from previous sessions so the bot resumes correctly."""
    if os.path.exists('cycles.json'):
        try:
            with open('cycles.json', 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {}

def save_cycle_count(name, count):
    """Safely saves the cycle count to a JSON file using the auto-detected instance name."""
    with global_cycles_lock:
        cycles = load_saved_cycles()
        cycles[name] = count
        with open('cycles.json', 'w') as f:
            json.dump(cycles, f, indent=4)

# =======================================================
# BASIC SETTINGS & CONNECTION
# =======================================================
def load_settings():
    """Loads or creates settings.json containing base configurations."""
    settings_file = 'settings.json'
    settings_data = {
        "debug_mode": True,
        "original_resolution": [1920, 1080]
    }
    
    if os.path.exists(settings_file):
        try:
            with open(settings_file, 'r') as f:
                settings = json.load(f)
                settings_data["debug_mode"] = settings.get('debug_mode', True)
                settings_data["original_resolution"] = settings.get('original_resolution', [1920, 1080])
        except json.JSONDecodeError:
            pass
    else:
        with open(settings_file, 'w') as f:
            json.dump(settings_data, f, indent=4)
            
    if settings_data["debug_mode"]:
        os.makedirs('debug', exist_ok=True)
        
    return settings_data

def get_connected_devices():
    """
    Kills any ghost ADB servers, starts a fresh one, and scans default emulator ports.
    Returns a list of IPs for emulators that are actually alive and responding.
    """
    common_ports = ["16384", "16416", "16448", "16480", "16512", "5555", "7555"]
    connected_devices = []
    seen_boot_ids = set()
    
    subprocess.run([ADB_CMD, "kill-server"], capture_output=True)
    subprocess.run([ADB_CMD, "start-server"], capture_output=True)
    
    for port in common_ports:
        device_ip = f"127.0.0.1:{port}"
        subprocess.run([ADB_CMD, "connect", device_ip], capture_output=True)
        
    check = subprocess.run([ADB_CMD, "devices"], capture_output=True, text=True)
    
    for line in check.stdout.splitlines():
        if "127.0.0.1" in line and "device" in line and "offline" not in line:
            ip = line.split()[0]
            try:
                test = subprocess.run([ADB_CMD, "-s", ip, "shell", "echo", "alive"], capture_output=True, text=True, timeout=3)
                if "alive" in test.stdout:
                    boot_id_req = subprocess.run([ADB_CMD, "-s", ip, "shell", "cat", "/proc/sys/kernel/random/boot_id"], capture_output=True, text=True)
                    boot_id = boot_id_req.stdout.strip()
                    
                    if boot_id and boot_id not in seen_boot_ids:
                        seen_boot_ids.add(boot_id)
                        connected_devices.append(ip)
                    else:
                        subprocess.run([ADB_CMD, "disconnect", ip], capture_output=True)
                else:
                    subprocess.run([ADB_CMD, "disconnect", ip], capture_output=True)
            except subprocess.TimeoutExpired:
                subprocess.run([ADB_CMD, "disconnect", ip], capture_output=True)
                
    return connected_devices

def ui_loop():
    """Constantly updates the terminal UI safely using thread locks."""
    if os.name == 'nt': os.system('') 
    os.system('cls' if os.name == 'nt' else 'clear')
    
    while True:
        sys.stdout.write('\033[H') 
        sys.stdout.write("=== Macro Status ===\033[K\n")
        sys.stdout.write("Press Ctrl+C to shut down all bots safely.\033[K\n\n")
        
        with ui_state_lock:
            names = list(emulator_statuses.keys())
            for name in names:
                status = emulator_statuses.get(name, "")
                cycles = emulator_cycles.get(name, (1, 1))
                
                if isinstance(cycles, int):
                    session_cycle, total_cycle = 1, cycles
                else:
                    session_cycle, total_cycle = cycles
                    
                sys.stdout.write(f"[{name}] Session: {session_cycle} | Total: {total_cycle} | {status}\033[K\n")
            
        sys.stdout.flush()
        time.sleep(0.1)

# =======================================================
# BOT INSTANCE CLASS (One object per Emulator)
# =======================================================
class MacroBot:
    def __init__(self, device_ip, settings, flux_data, auto_name):
        self.target_device = device_ip
        self.port = device_ip.split(':')[-1]
        self.name = auto_name
        
        self.debug_mode = settings["debug_mode"]
        self.original_res = settings["original_resolution"]
        self.flux = flux_data
        self.is_running = True
        
        self.total_cycles = load_saved_cycles().get(self.name, 1)
        self.session_cycles = 1
        with ui_state_lock:
            emulator_cycles[self.name] = (self.session_cycles, self.total_cycles)

    def log(self, message):
        """Safely updates global status dictionary with mutex locking."""
        with ui_state_lock:
            emulator_statuses[self.name] = message

    def capture_screen_adb(self):
        """Takes an instant screenshot via direct ADB memory byte stream (No Disk I/O)."""
        cmd = [ADB_CMD, "-s", self.target_device, "exec-out", "screencap", "-p"]
        proc = subprocess.run(cmd, capture_output=True)
        
        if proc.returncode != 0 or not proc.stdout:
            self.log("[!] Connection lost or screencap failed.")
            self.is_running = False
            return None
            
        if self.debug_mode:
            local_path = f"debug/latest_adb_screen_{self.port}.png"
            with open(local_path, "wb") as f:
                f.write(proc.stdout)
                
        image_array = np.frombuffer(proc.stdout, np.uint8)
        img_bgr = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
        return img_bgr

    def adb_drag_and_hold(self, start_x, start_y, end_x, end_y, duration_seconds):
        """Simulates dragging directly via argument array."""
        duration_ms = str(int(duration_seconds * 1000))
        cmd = [ADB_CMD, "-s", self.target_device, "shell", "input", "swipe", 
               str(int(start_x)), str(int(start_y)), str(int(end_x)), str(int(end_y)), duration_ms]
        subprocess.run(cmd)
        time.sleep(duration_seconds)

    def adb_click(self, x, y):
        """Simulates a quick tap directly via argument array."""
        cmd = [ADB_CMD, "-s", self.target_device, "shell", "input", "tap", str(int(x)), str(int(y))]
        subprocess.run(cmd)

    def adb_hold(self, x, y, duration_seconds):
        """Simulates holding a finger in one spot."""
        duration_ms = str(int(duration_seconds * 1000))
        cmd = [ADB_CMD, "-s", self.target_device, "shell", "input", "swipe", 
               str(int(x)), str(int(y)), str(int(x)), str(int(y)), duration_ms]
        subprocess.run(cmd)
        time.sleep(duration_seconds)

    def scale_coords(self, x, y, current_w, current_h):
        """Scales hardcoded JSON coordinates to match the emulator resolution."""
        scale_x = current_w / self.original_res[0]
        scale_y = current_h / self.original_res[1]
        return int(x * scale_x), int(y * scale_y)

    def find_image_on_screen(self, screen, template_name, threshold=0.75):
        """Uses OpenCV template matching backed by an in-memory RAM cache."""
        if screen is None:
            return False, None
            
        current_h, current_w = screen.shape[:2]
        template_path = os.path.join(f"templates_{current_h}", template_name)
            
        template = get_cached_template(template_path)
        if template is None:
            self.log(f"[!] CORRUPT OR MISSING: '{template_path}'")
            return False, None
            
        result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
        loc = np.where(result >= threshold)
        
        if len(loc[0]) > 0:
            h, w = template.shape[:2]
            center_x = loc[1][0] + w // 2
            center_y = loc[0][0] + h // 2
            return True, (center_x, center_y)
            
        return False, None

    def handle_step_failure(self, step):
        """Emergency legacy restart routine."""
        on_fail = step.get('on_fail', 'continue')
        
        if on_fail == 'restart':
            wait_mins = step.get('fail_wait_minutes', 1)
            fail_images = step.get('fail_image', []) 
            
            if isinstance(fail_images, str):
                fail_images = [fail_images]
                
            fail_retries = step.get('fail_max_retries', 5) 
            fail_interval = step.get('fail_retry_interval', 1) 
            fail_skip = step.get('fail_skip', True) 
            
            self.log(f"[FAIL] Limit reached. Pausing for {wait_mins} min...")
            
            for _ in range(wait_mins * 60):
                if not self.is_running: return False
                time.sleep(1)
                
            if fail_images:
                self.log("[RESTART] Tapping screen center to force Mobile UI...")
                screen = self.capture_screen_adb()
                if screen is not None:
                    current_h, current_w = screen.shape[:2]
                    center_x, center_y = current_w // 2, current_h // 2
                    self.adb_click(center_x, center_y)
                    time.sleep(2)
                    
                self.log(f"[RESTART] Scanning for {len(fail_images)} emergency images...")
                attempts = 0
                found_and_clicked = False 
                
                while self.is_running:
                    attempts += 1
                    if fail_skip and attempts > fail_retries:
                        break
                        
                    self.log(f"[RESTART] Attempt {attempts} -> Scanning...")
                    screen = self.capture_screen_adb()
                    current_attempt_found = False
                    
                    for img_name in fail_images:
                        found, coords = self.find_image_on_screen(screen, img_name)
                        if found:
                            self.log(f"[RESTART] Found '{img_name}'! Clicking {coords[0]}, {coords[1]}")
                            self.adb_click(coords[0], coords[1])
                            time.sleep(3)
                            current_attempt_found = True
                            found_and_clicked = True
                            break
                    
                    if current_attempt_found:
                        continue
                        
                    if not current_attempt_found:
                        if found_and_clicked:
                            self.log("[RESTART] Recovery successful!")
                            return True
                        else:
                            time.sleep(fail_interval)
                
            self.log("[RESTART] Restarting macro from Step 1...")
            return True
            
        return False

    def execute_step(self, step, label="Step"):
        """
        Executes a single instruction step recursively. 
        Runs fallback arrays inside 'on_fail' if execution limits are exceeded.
        """
        if not self.is_running:
            return False, False

        action = step.get('action')
        skip = step.get('skip', False)
        delay_before = step.get('delay_before', 0)

        if delay_before > 0:
            self.log(f"{label}: Waiting {delay_before}s...")
            time.sleep(delay_before)

        success = False
        restart_triggered = False

        # =======================================================
        # STEP ACTION HANDLERS
        # =======================================================
        if action == "restart_macro":
            self.log(f"{label}: [RESTART] Explicit restart requested by recovery sequence.")
            return True, True  # (success=True, restart_triggered=True)

        elif action == "find_and_click":
            target_image = step['image']
            interval = step.get('retry_interval', 1)
            max_retries = step.get('max_retries', 1)
            attempts = 0

            while not success and self.is_running:
                attempts += 1
                self.log(f"{label}: Attempt {attempts}/{max_retries} -> {target_image}")
                screen = self.capture_screen_adb()
                found, coords = self.find_image_on_screen(screen, target_image)

                if found:
                    self.log(f"{label}: [SUCCESS] Clicking {coords[0]}, {coords[1]}")
                    self.adb_click(coords[0], coords[1])
                    success = True
                else:
                    if attempts >= max_retries:
                        break
                    time.sleep(interval)

        elif action == "find_and_hold_touch":
            target_image = step['image']
            t_x, t_y = step['touch_x'], step['touch_y']
            hold_time = step.get('hold_duration', 1)
            interval = step.get('retry_interval', 1)
            max_retries = step.get('max_retries', 1)
            attempts = 0

            while not success and self.is_running:
                attempts += 1
                self.log(f"{label}: Attempt {attempts}/{max_retries} -> {target_image}")
                screen = self.capture_screen_adb()
                found, _ = self.find_image_on_screen(screen, target_image)

                if found:
                    current_h, current_w = screen.shape[:2]
                    scaled_x, scaled_y = self.scale_coords(t_x, t_y, current_w, current_h)
                    self.log(f"{label}: [SUCCESS] Holding {scaled_x}, {scaled_y} for {hold_time}s")
                    self.adb_hold(scaled_x, scaled_y, hold_time)
                    success = True
                else:
                    if attempts >= max_retries:
                        break
                    time.sleep(interval)

        elif action == "joystick_drag":
            target_image = step['image']
            s_x, s_y = step['start_x'], step['start_y']
            e_x, e_y = step['end_x'], step['end_y']
            hold_time = step.get('hold_duration', 1)
            interval = step.get('retry_interval', 1)
            max_retries = step.get('max_retries', 1)
            attempts = 0

            while not success and self.is_running:
                attempts += 1
                self.log(f"{label}: Attempt {attempts}/{max_retries} -> {target_image}")
                screen = self.capture_screen_adb()
                found, _ = self.find_image_on_screen(screen, target_image)

                if found:
                    current_h, current_w = screen.shape[:2]
                    scaled_sx, scaled_sy = self.scale_coords(s_x, s_y, current_w, current_h)
                    scaled_ex, scaled_ey = self.scale_coords(e_x, e_y, current_w, current_h)
                    self.log(f"{label}: [SUCCESS] Dragging for {hold_time}s")
                    self.adb_drag_and_hold(scaled_sx, scaled_sy, scaled_ex, scaled_ey, hold_time)
                    success = True
                else:
                    if attempts >= max_retries:
                        break
                    time.sleep(interval)

        elif action == "wait_for_image":
            target_image = step['image']
            timeout = step.get('timeout', 60)
            start_wait = time.time()

            while not success and (time.time() - start_wait) < timeout and self.is_running:
                elapsed = int(time.time() - start_wait)
                self.log(f"{label}: Waiting {elapsed}s/{timeout}s -> {target_image}")
                screen = self.capture_screen_adb()
                found, _ = self.find_image_on_screen(screen, target_image)
                if found:
                    success = True
                    self.log(f"{label}: [SUCCESS] Appeared!")
                else:
                    time.sleep(1)

        elif action == "stop_script":
            target_image = step['image']
            self.log(f"{label}: Safety check for {target_image}...")
            screen = self.capture_screen_adb()
            found, _ = self.find_image_on_screen(screen, target_image)
            if found:
                self.log(f"{label}: [!!!] FATAL: Found {target_image}. Stopping.")
                self.is_running = False
            else:
                success = True
                self.log(f"{label}: [CLEAR] Safety passed.")

        # =======================================================
        # FAILURE HANDLING & SUBSTEP EXECUTION
        # =======================================================
        if not success and self.is_running:
            on_fail = step.get('on_fail')

            if on_fail == 'restart':
                restart_triggered = self.handle_step_failure(step)

            elif isinstance(on_fail, list):
                self.log(f"{label}: [FAIL] Running {len(on_fail)} recovery substep(s)...")
                for sub_idx, substep in enumerate(on_fail):
                    if not self.is_running:
                        break
                    sub_label = f"{label}.FAIL[{sub_idx + 1}]"
                    sub_success, sub_restart = self.execute_step(substep, label=sub_label)
                    
                    if sub_restart:
                        return False, True
                        
                self.log(f"{label}: Recovery substeps finished. Resuming flow.")

            elif skip:
                self.log(f"{label}: [SKIP] Moving to next step.")

        return success, restart_triggered

    def run(self):
        """The main loop for this specific bot instance."""
        self.log(f"Starting Macro: {self.flux.get('flux_name', 'Unnamed')}")
        
        while self.is_running: 
            self.log("Starting step execution...")
            restart_cycle = False
            
            for step_index, step in enumerate(self.flux['steps']):
                if not self.is_running: break
                
                step_label = f"Step {step_index + 1}/{len(self.flux['steps'])}"
                _, restart_cycle = self.execute_step(step, label=step_label)
                
                if restart_cycle:
                    break
            
            if restart_cycle:
                continue 
            
            if self.is_running:
                self.session_cycles += 1
                self.total_cycles += 1
                with ui_state_lock:
                    emulator_cycles[self.name] = (self.session_cycles, self.total_cycles)
                save_cycle_count(self.name, self.total_cycles)
                self.log("Cycle completed successfully!")

# =======================================================
# MAIN EXECUTION
# =======================================================
if __name__ == "__main__":
    settings = load_settings()
    
    try:
        with open('flux.json', 'r') as f:
            flux_data = json.load(f)
    except FileNotFoundError:
        print("[!] Error: Could not find 'flux.json'.")
        sys.exit(1)

    print("\n[*] Resetting ADB Engine & Scanning for active emulators...")
    active_devices = get_connected_devices()
    
    if not active_devices:
        print("\n[!!!] CRITICAL ERROR: Could not find any active emulator instances.")
        print("Ensure MuMu Player is open and 'ADB Debug/Root Permission' is enabled in settings.")
        input("\nPress Enter to close...")
        sys.exit(1)
    
    bots = []
    threads = []
    
    for device_ip in active_devices:
        port = device_ip.split(':')[-1]
        auto_name = get_emulator_name_from_port(port)
        
        with ui_state_lock:
            emulator_statuses[auto_name] = "Waiting for initialization..."
        
        bot_instance = MacroBot(device_ip, settings, flux_data, auto_name)
        bots.append(bot_instance)
        
        t = threading.Thread(target=bot_instance.run)
        t.daemon = True
        t.start()
        threads.append(t)

    t_ui = threading.Thread(target=ui_loop)
    t_ui.daemon = True
    t_ui.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        os.system('cls' if os.name == 'nt' else 'clear')
        print("\n[!] Script stopped manually by user (Ctrl+C). Shutting down all instances...")
        for bot in bots:
            bot.is_running = False
        sys.exit(0)