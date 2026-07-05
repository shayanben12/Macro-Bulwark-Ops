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

# Caminho para o executável ADB. Certifique-se de que esta pasta existe no mesmo diretório do script.
ADB_CMD = ".\\adb_tools\\adb.exe"

# Dicionários globais compartilhados e travas (locks) de sincronização
emulator_statuses = {} 
emulator_cycles = {}   
global_cycles_lock = threading.Lock() # Protege gravações de arquivo no cycles.json
ui_state_lock = threading.Lock()      # Protege o acesso ao dicionário compartilhado entre threads

# =======================================================
# CACHE DE MODELOS EM MEMÓRIA (RAM)
# =======================================================
@lru_cache(maxsize=128)
def get_cached_template(template_path):
    """Carrega um modelo de imagem na RAM uma única vez e armazena em cache para todas as varreduras futuras."""
    if not os.path.exists(template_path):
        return None
    return cv2.imread(template_path)

# =======================================================
# AUTO-DETECÇÃO: OBTER NOME DO EMULADOR AUTOMATICAMENTE
# =======================================================
def get_emulator_name_from_port(port):
    """
    Encontra o título real da janela do Windows (ex: 'Account 1') usando a porta ADB. 
    Se falhar, calcula o número da instância do MuMu matematicamente.
    """
    port_num = int(port)
    calculated_instance = int(((port_num - 16384) / 32) + 1)
    fallback_name = f"Instância {calculated_instance}" if port_num >= 16384 else f"Porta {port}"
    
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
# SISTEMA DE MEMÓRIA (Salvar/Carregar Ciclos por Nome Personalizado)
# =======================================================
def load_saved_cycles():
    """Carrega a contagem de ciclos de sessões anteriores para que o bot retome corretamente."""
    if os.path.exists('cycles.json'):
        try:
            with open('cycles.json', 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {}

def save_cycle_count(name, count):
    """Salva com segurança a contagem de ciclos em um arquivo JSON usando o nome da instância detectado automaticamente."""
    with global_cycles_lock:
        cycles = load_saved_cycles()
        cycles[name] = count
        with open('cycles.json', 'w') as f:
            json.dump(cycles, f, indent=4)

# =======================================================
# CONFIGURAÇÕES BÁSICAS E CONEXÃO
# =======================================================
def load_settings():
    """Carrega ou cria o arquivo settings.json contendo as configurações básicas."""
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
    Encerra servidores ADB travados (fantasmas), inicia um novo e verifica as portas padrão de emuladores.
    Retorna uma lista de IPs para emuladores que estão realmente ativos e respondendo.
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
    """Atualiza constantemente a interface do terminal de forma segura usando travas (thread locks)."""
    if os.name == 'nt': os.system('') 
    os.system('cls' if os.name == 'nt' else 'clear')
    
    while True:
        sys.stdout.write('\033[H') 
        sys.stdout.write("=== Status do Macro ===\033[K\n")
        sys.stdout.write("Pressione Ctrl+C para encerrar todos os bots com segurança.\033[K\n\n")
        
        with ui_state_lock:
            names = list(emulator_statuses.keys())
            for name in names:
                status = emulator_statuses.get(name, "")
                cycles = emulator_cycles.get(name, (1, 1))
                
                if isinstance(cycles, int):
                    session_cycle, total_cycle = 1, cycles
                else:
                    session_cycle, total_cycle = cycles
                    
                sys.stdout.write(f"[{name}] Sessão: {session_cycle} | Total: {total_cycle} | {status}\033[K\n")
            
        sys.stdout.flush()
        time.sleep(0.1)

# =======================================================
# CLASSE DA INSTÂNCIA DO BOT (Um objeto por Emulador)
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
        """Atualiza com segurança o dicionário de status global com travamento mutex."""
        with ui_state_lock:
            emulator_statuses[self.name] = message

    def capture_screen_adb(self):
        """Tira uma captura de tela instantânea por fluxo de bytes direto da memória via ADB (Sem I/O em disco)."""
        cmd = [ADB_CMD, "-s", self.target_device, "exec-out", "screencap", "-p"]
        proc = subprocess.run(cmd, capture_output=True)
        
        if proc.returncode != 0 or not proc.stdout:
            self.log("[!] Conexão perdida ou falha ao capturar a tela.")
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
        """Simula arrastar diretamente via array de argumentos."""
        duration_ms = str(int(duration_seconds * 1000))
        cmd = [ADB_CMD, "-s", self.target_device, "shell", "input", "swipe", 
               str(int(start_x)), str(int(start_y)), str(int(end_x)), str(int(end_y)), duration_ms]
        subprocess.run(cmd)
        time.sleep(duration_seconds)

    def adb_click(self, x, y):
        """Simula um toque rápido diretamente via array de argumentos."""
        cmd = [ADB_CMD, "-s", self.target_device, "shell", "input", "tap", str(int(x)), str(int(y))]
        subprocess.run(cmd)

    def adb_hold(self, x, y, duration_seconds):
        """Simula manter um dedo pressionado em um ponto específico."""
        duration_ms = str(int(duration_seconds * 1000))
        cmd = [ADB_CMD, "-s", self.target_device, "shell", "input", "swipe", 
               str(int(x)), str(int(y)), str(int(x)), str(int(y)), duration_ms]
        subprocess.run(cmd)
        time.sleep(duration_seconds)

    def scale_coords(self, x, y, current_w, current_h):
        """Redimensiona coordenadas fixas do JSON para corresponder à resolução atual do emulador."""
        scale_x = current_w / self.original_res[0]
        scale_y = current_h / self.original_res[1]
        return int(x * scale_x), int(y * scale_y)

    def find_image_on_screen(self, screen, template_name, threshold=0.75):
        """Usa correspondência de modelo do OpenCV suportada pelo cache em memória RAM."""
        if screen is None:
            return False, None
            
        current_h, current_w = screen.shape[:2]
        template_path = os.path.join(f"templates_{current_h}", template_name)
            
        template = get_cached_template(template_path)
        if template is None:
            self.log(f"[!] CORROMPIDO OU AUSENTE: '{template_path}'")
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
        """Rotina de reinicialização de emergência legada."""
        on_fail = step.get('on_fail', 'continue')
        
        if on_fail == 'restart':
            wait_mins = step.get('fail_wait_minutes', 1)
            fail_images = step.get('fail_image', []) 
            
            if isinstance(fail_images, str):
                fail_images = [fail_images]
                
            fail_retries = step.get('fail_max_retries', 5) 
            fail_interval = step.get('fail_retry_interval', 1) 
            fail_skip = step.get('fail_skip', True) 
            
            self.log(f"[FALHA] Limite atingido. Pausando por {wait_mins} min...")
            
            for _ in range(wait_mins * 60):
                if not self.is_running: return False
                time.sleep(1)
                
            if fail_images:
                self.log("[REINICIAR] Tocando no centro da tela para forçar a Interface Móvel...")
                screen = self.capture_screen_adb()
                if screen is not None:
                    current_h, current_w = screen.shape[:2]
                    center_x, center_y = current_w // 2, current_h // 2
                    self.adb_click(center_x, center_y)
                    time.sleep(2)
                    
                self.log(f"[REINICIAR] Procurando por {len(fail_images)} imagens de emergência...")
                attempts = 0
                found_and_clicked = False 
                
                while self.is_running:
                    attempts += 1
                    if fail_skip and attempts > fail_retries:
                        break
                        
                    self.log(f"[REINICIAR] Tentativa {attempts} -> Procurando...")
                    screen = self.capture_screen_adb()
                    current_attempt_found = False
                    
                    for img_name in fail_images:
                        found, coords = self.find_image_on_screen(screen, img_name)
                        if found:
                            self.log(f"[REINICIAR] '{img_name}' encontrado! Clicando em {coords[0]}, {coords[1]}")
                            self.adb_click(coords[0], coords[1])
                            time.sleep(3)
                            current_attempt_found = True
                            found_and_clicked = True
                            break
                    
                    if current_attempt_found:
                        continue
                        
                    if not current_attempt_found:
                        if found_and_clicked:
                            self.log("[REINICIAR] Recuperação bem-sucedida!")
                            return True
                        else:
                            time.sleep(fail_interval)
                
            self.log("[REINICIAR] Reiniciando macro a partir da Etapa 1...")
            return True
            
        return False

    def execute_step(self, step, label="Etapa"):
        """
        Executa uma única instrução (etapa) recursivamente. 
        Executa arrays de fallback dentro de 'on_fail' caso os limites de execução sejam excedidos.
        """
        if not self.is_running:
            return False, False

        action = step.get('action')
        skip = step.get('skip', False)
        delay_before = step.get('delay_before', 0)

        if delay_before > 0:
            self.log(f"{label}: Aguardando {delay_before}s...")
            time.sleep(delay_before)

        success = False
        restart_triggered = False

        # =======================================================
        # MANIPULADORES DE AÇÕES DE ETAPA
        # =======================================================
        if action == "restart_macro":
            self.log(f"{label}: [REINICIAR] Reinício explícito solicitado pela sequência de recuperação.")
            return True, True  # (success=True, restart_triggered=True)

        elif action == "find_and_click":
            target_image = step['image']
            interval = step.get('retry_interval', 1)
            max_retries = step.get('max_retries', 1)
            attempts = 0

            while not success and self.is_running:
                attempts += 1
                self.log(f"{label}: Tentativa {attempts}/{max_retries} -> {target_image}")
                screen = self.capture_screen_adb()
                found, coords = self.find_image_on_screen(screen, target_image)

                if found:
                    self.log(f"{label}: [SUCESSO] Clicando em {coords[0]}, {coords[1]}")
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
                self.log(f"{label}: Tentativa {attempts}/{max_retries} -> {target_image}")
                screen = self.capture_screen_adb()
                found, _ = self.find_image_on_screen(screen, target_image)

                if found:
                    current_h, current_w = screen.shape[:2]
                    scaled_x, scaled_y = self.scale_coords(t_x, t_y, current_w, current_h)
                    self.log(f"{label}: [SUCESSO] Pressionando {scaled_x}, {scaled_y} por {hold_time}s")
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
                self.log(f"{label}: Tentativa {attempts}/{max_retries} -> {target_image}")
                screen = self.capture_screen_adb()
                found, _ = self.find_image_on_screen(screen, target_image)

                if found:
                    current_h, current_w = screen.shape[:2]
                    scaled_sx, scaled_sy = self.scale_coords(s_x, s_y, current_w, current_h)
                    scaled_ex, scaled_ey = self.scale_coords(e_x, e_y, current_w, current_h)
                    self.log(f"{label}: [SUCESSO] Arrastando por {hold_time}s")
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
                self.log(f"{label}: Aguardando {elapsed}s/{timeout}s -> {target_image}")
                screen = self.capture_screen_adb()
                found, _ = self.find_image_on_screen(screen, target_image)
                if found:
                    success = True
                    self.log(f"{label}: [SUCESSO] Apareceu!")
                else:
                    time.sleep(1)

        elif action == "stop_script":
            target_image = step['image']
            self.log(f"{label}: Verificação de segurança para {target_image}...")
            screen = self.capture_screen_adb()
            found, _ = self.find_image_on_screen(screen, target_image)
            if found:
                self.log(f"{label}: [!!!] FATAL: {target_image} encontrado. Parando.")
                self.is_running = False
            else:
                success = True
                self.log(f"{label}: [SEGURO] Verificação de segurança aprovada.")

        # =======================================================
        # TRATAMENTO DE FALHAS E EXECUÇÃO DE SUBETAPAS
        # =======================================================
        if not success and self.is_running:
            on_fail = step.get('on_fail')

            if on_fail == 'restart':
                restart_triggered = self.handle_step_failure(step)

            elif isinstance(on_fail, list):
                self.log(f"{label}: [FALHA] Executando {len(on_fail)} subetapa(s) de recuperação...")
                for sub_idx, substep in enumerate(on_fail):
                    if not self.is_running:
                        break
                    sub_label = f"{label}.FALHA[{sub_idx + 1}]"
                    sub_success, sub_restart = self.execute_step(substep, label=sub_label)
                    
                    if sub_restart:
                        return False, True
                        
                self.log(f"{label}: Subetapas de recuperação concluídas. Retomando o fluxo.")

            elif skip:
                self.log(f"{label}: [PULAR] Avançando para a próxima etapa.")

        return success, restart_triggered

    def run(self):
        """O loop principal para esta instância específica do bot."""
        self.log(f"Iniciando Macro: {self.flux.get('flux_name', 'Sem Nome')}")
        
        while self.is_running: 
            self.log("Iniciando execução da etapa...")
            restart_cycle = False
            
            for step_index, step in enumerate(self.flux['steps']):
                if not self.is_running: break
                
                step_label = f"Etapa {step_index + 1}/{len(self.flux['steps'])}"
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
                self.log("Ciclo concluído com sucesso!")

# =======================================================
# EXECUÇÃO PRINCIPAL
# =======================================================
if __name__ == "__main__":
    settings = load_settings()
    
    try:
        with open('flux.json', 'r') as f:
            flux_data = json.load(f)
    except FileNotFoundError:
        print("[!] Erro: Não foi possível encontrar 'flux.json'.")
        sys.exit(1)

    print("\n[*] Reiniciando o motor ADB e procurando por emuladores ativos...")
    active_devices = get_connected_devices()
    
    if not active_devices:
        print("\n[!!!] ERRO CRÍTICO: Não foi possível encontrar nenhuma instância ativa de emulador.")
        print("Certifique-se de que o MuMu Player está aberto e a permissão 'ADB Debug/Root' está habilitada nas configurações.")
        input("\nPressione Enter para fechar...")
        sys.exit(1)
    
    bots = []
    threads = []
    
    for device_ip in active_devices:
        port = device_ip.split(':')[-1]
        auto_name = get_emulator_name_from_port(port)
        
        with ui_state_lock:
            emulator_statuses[auto_name] = "Aguardando inicialização..."
        
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
        print("\n[!] Script interrompido manualmente pelo usuário (Ctrl+C). Encerrando todas as instâncias...")
        for bot in bots:
            bot.is_running = False
        sys.exit(0)