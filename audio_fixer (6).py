import os
import sys
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-INSTALACJA ZALEŻNOŚCI
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_deps():
    """Instaluje pycaw, comtypes i pystray jeśli nie są dostępne."""
    deps = {"pycaw": "pycaw", "comtypes": "comtypes", "pystray": "pystray", "PIL": "pillow"}
    for module, package in deps.items():
        try:
            __import__(module)
        except ImportError:
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", package, "--quiet"],
                    check=True, capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception:
                pass


_ensure_deps()

# ─────────────────────────────────────────────────────────────────────────────
# KONFIGURACJA
# ─────────────────────────────────────────────────────────────────────────────
POLLING_INTERVAL_SEC  = 2      # Jak często sprawdzać rejestr (sekundy)
AUTO_RESET_COOLDOWN   = 10     # Minimalna przerwa między resetami (antyflood)


# ─────────────────────────────────────────────────────────────────────────────
# UPRAWNIENIA ADMINA
# ─────────────────────────────────────────────────────────────────────────────
def is_admin():
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# SKRYPT POWERSHELL – NIE MODYFIKOWAĆ
# ─────────────────────────────────────────────────────────────────────────────
PS_SCRIPT = """
    # 1. Restart kontrolera Intel Smart Sound (kluczowe w Dellu bez Waves)
    Get-PnpDevice -FriendlyName "*Smart Sound*" -Status OK | ForEach-Object { 
        Disable-PnpDevice -InstanceId $_.InstanceId -Confirm:$false
        Start-Sleep -Seconds 1
        Enable-PnpDevice -InstanceId $_.InstanceId -Confirm:$false 
    }
    
    # 2. Restart samej karty Realtek Audio
    Get-PnpDevice -FriendlyName "*Realtek*" -Status OK | ForEach-Object { 
        Disable-PnpDevice -InstanceId $_.InstanceId -Confirm:$false
        Start-Sleep -Seconds 1
        Enable-PnpDevice -InstanceId $_.InstanceId -Confirm:$false 
    }
    
    # 3. Restart głównej usługi audio Windowsa
    Restart-Service -Name "Audiosrv" -Force

    # 4. Opcjonalnie: ustaw słuchawki jako domyślne (cicho, błąd nie przerywa)
    try {
        Start-Sleep -Seconds 2
        $hasModule = Get-Module -ListAvailable -Name AudioDeviceCmdlets -ErrorAction SilentlyContinue
        if ($hasModule) {
            Import-Module AudioDeviceCmdlets -ErrorAction SilentlyContinue
            $hp = Get-AudioDevice -List -ErrorAction SilentlyContinue | Where-Object {
                $_.Type -eq 'Playback' -and $_.Name -match 'S.uchawk|Headphone|Headset|Jack'
            } | Select-Object -First 1
            if ($hp) { Set-AudioDevice -ID $hp.ID | Out-Null }
        }
    } catch {}
    """




# ─────────────────────────────────────────────────────────────────────────────
# RDZEŃ: RESET AUDIO (wywołanie PS w osobnym wątku)
# ─────────────────────────────────────────────────────────────────────────────
_reset_lock = threading.Lock()          # blokuje równoczesne resety
_last_reset_time = 0.0                  # znacznik czasu ostatniego resetu


def _force_headphones_pycaw() -> bool:
    """
    Metoda A – pycaw + comtypes: bezpośredni dostęp do IMMDeviceEnumerator / IPolicyConfig.
    Wymaga: pip install pycaw comtypes
    Zwraca True jeśli udało się ustawić słuchawki jako domyślne.
    """
    try:
        import comtypes                              # type: ignore
        from comtypes import CLSCTX_ALL             # type: ignore
        from pycaw.pycaw import AudioUtilities, IMMDeviceEnumerator, EDataFlow, ERole  # type: ignore
        from pycaw.constants import CLSID_MMDeviceEnumerator                           # type: ignore
        from ctypes import HRESULT, cast, POINTER
        import comtypes.client

        # Pobierz enumerator
        enumerator = comtypes.CoCreateInstance(
            CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL
        )

        # Pobierz kolekcję wszystkich aktywnych urządzeń odtwarzania
        collection = enumerator.EnumAudioEndpoints(EDataFlow.eRender.value, 0x1)  # DEVICE_STATE_ACTIVE
        count = collection.GetCount()

        HEADPHONE_PATTERNS = [
            "headphone", "headset", "słuchawk", "sluchawk",
            "jack", "line out", "analog"
        ]

        candidates = []
        for i in range(count):
            device = collection.Item(i)
            dev_id = device.GetId()
            # Pobierz nazwę przyjazną przez PropertyStore
            try:
                store = device.OpenPropertyStore(0)  # STGM_READ
                # PKEY_Device_FriendlyName = {a45c254e-df1c-4efd-8020-67d146a850e0}, 14
                import comtypes.gen  # noqa
                from comtypes import GUID
                key_guid = GUID("{a45c254e-df1c-4efd-8020-67d146a850e0}")
                # Użyj surowego GetValue
                from ctypes import c_ulong
                from comtypes.automation import VARIANT
                propkey = (key_guid, 14)
                try:
                    val = store.GetValue(propkey)
                    name = str(val).lower()
                except Exception:
                    name = dev_id.lower()
            except Exception:
                name = dev_id.lower()

            score = sum(1 for p in HEADPHONE_PATTERNS if p in name)
            candidates.append((score, i, dev_id, name))

        # Sortuj – największy score = najbardziej prawdopodobne słuchawki
        candidates.sort(key=lambda x: -x[0])

        if not candidates:
            return False

        # Weź kandydata z najwyższym score; jeśli remis (score=0) – weź ostatnie urządzenie
        best_score, best_idx, best_id, best_name = candidates[0]
        if best_score == 0:
            # Żadna nazwa nie pasuje – weź ostatnie wyliczone (głośniki zwykle są pierwsze)
            candidates_by_idx = sorted(candidates, key=lambda x: -x[1])
            _, best_idx, best_id, best_name = candidates_by_idx[0]

        _set_status(f"pycaw: ustawiam '{best_name[:30]}'…", color="#FFD60A")

        # IPolicyConfig – nieudokumentowany interfejs Windows Audio
        # CLSID: {870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}
        # IID:   {F294ACFC-3146-4483-A7BF-ADDCA7C260E2}
        from comtypes import GUID, IUnknown, COMMETHOD
        from ctypes import c_int, c_wchar_p, c_void_p

        class IPolicyConfig(IUnknown):
            _iid_ = GUID("{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}")
            _methods_ = [
                COMMETHOD([], HRESULT, "GetMixFormat",      ["in", c_wchar_p], ["in", c_void_p]),
                COMMETHOD([], HRESULT, "GetDeviceFormat",   ["in", c_wchar_p], ["in", c_int], ["in", c_void_p]),
                COMMETHOD([], HRESULT, "ResetDeviceFormat",  ["in", c_wchar_p]),
                COMMETHOD([], HRESULT, "SetDeviceFormat",   ["in", c_wchar_p], ["in", c_void_p], ["in", c_void_p]),
                COMMETHOD([], HRESULT, "GetProcessingPeriod", ["in", c_wchar_p], ["in", c_int], ["in", c_void_p], ["in", c_void_p]),
                COMMETHOD([], HRESULT, "SetProcessingPeriod", ["in", c_wchar_p], ["in", c_void_p]),
                COMMETHOD([], HRESULT, "GetShareMode",      ["in", c_wchar_p], ["in", c_void_p]),
                COMMETHOD([], HRESULT, "SetShareMode",      ["in", c_wchar_p], ["in", c_void_p]),
                COMMETHOD([], HRESULT, "GetPropertyValue",  ["in", c_wchar_p], ["in", c_int], ["in", c_void_p], ["in", c_void_p]),
                COMMETHOD([], HRESULT, "SetPropertyValue",  ["in", c_wchar_p], ["in", c_int], ["in", c_void_p], ["in", c_void_p]),
                COMMETHOD([], HRESULT, "SetDefaultEndpoint", ["in", c_wchar_p], ["in", c_int]),
                COMMETHOD([], HRESULT, "SetEndpointVisibility", ["in", c_wchar_p], ["in", c_int]),
            ]

        CLSID_PolicyConfig = GUID("{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}")
        policy = comtypes.CoCreateInstance(CLSID_PolicyConfig, IPolicyConfig, CLSCTX_ALL)

        # Ustaw dla wszystkich 3 ról: eConsole=0, eMultimedia=1, eCommunications=2
        for role in range(3):
            policy.SetDefaultEndpoint(best_id, role)

        return True

    except ImportError:
        return False   # pycaw/comtypes niedostępne
    except Exception:
        return False


def _force_headphones_ps_cmdlets() -> bool:
    """
    Metoda B – AudioDeviceCmdlets przez PowerShell.
    Wymaga: Install-Module -Name AudioDeviceCmdlets (PS Gallery)
    """
    ps = """
    $m = Get-Module -ListAvailable AudioDeviceCmdlets -EA SilentlyContinue
    if (-not $m) { exit 1 }
    Import-Module AudioDeviceCmdlets -EA SilentlyContinue
    $patterns = 'S.uchawk','Headphone','Headset','Jack','Line Out','Analog'
    $hp = $null
    foreach ($p in $patterns) {
        $hp = Get-AudioDevice -List -EA SilentlyContinue |
              Where-Object { $_.Type -eq 'Playback' -and $_.Name -match $p } |
              Select-Object -First 1
        if ($hp) { break }
    }
    if (-not $hp) {
        # Fallback: weź ostatnie urządzenie Playback (głośniki są pierwsze)
        $all = Get-AudioDevice -List -EA SilentlyContinue | Where-Object { $_.Type -eq 'Playback' }
        if ($all.Count -ge 2) { $hp = $all[-1] }
    }
    if ($hp) { Set-AudioDevice -ID $hp.ID | Out-Null; exit 0 } else { exit 2 }
    """
    try:
        r = subprocess.run(
            ["powershell", "-Command", ps],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=20
        )
        return r.returncode == 0
    except Exception:
        return False


def _force_headphones_nircmd() -> bool:
    """
    Metoda C – NirCmd (nircmd.exe setdefaultsounddevice).
    Pobiera nircmd.exe do katalogu skryptu jeśli go nie ma.
    """
    import urllib.request
    import zipfile
    import io

    script_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    nircmd_path = os.path.join(script_dir, "nircmd.exe")

    # Pobierz nircmd jeśli nie ma
    if not os.path.exists(nircmd_path):
        try:
            _set_status("Pobieranie nircmd.exe…", color="#FFD60A")
            url = "https://www.nirsoft.net/utils/nircmd.zip"
            with urllib.request.urlopen(url, timeout=15) as resp:
                zdata = resp.read()
            with zipfile.ZipFile(io.BytesIO(zdata)) as zf:
                for name in zf.namelist():
                    if name.lower() == "nircmd.exe":
                        with zf.open(name) as src, open(nircmd_path, "wb") as dst:
                            dst.write(src.read())
                        break
        except Exception:
            return False

    if not os.path.exists(nircmd_path):
        return False

    # Spróbuj ustawić znane wzorce nazw urządzeń jako domyślne
    patterns = ["Headphones", "Słuchawki", "Headset", "Realtek HD Audio 2nd output"]
    for pattern in patterns:
        try:
            r = subprocess.run(
                [nircmd_path, "setdefaultsounddevice", pattern, "1"],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=5
            )
            if r.returncode == 0:
                return True
        except Exception:
            continue
    return False


def _run_reset_thread(triggered_by_auto: bool = False):
    """Właściwa logika resetu – uruchamiana w wątku roboczym."""
    global _last_reset_time

    with _reset_lock:
        try:
            source = "auto" if triggered_by_auto else "ręcznie"
            _set_status(f"Status: Resetowanie sprzętu ({source})…", color="#FFD60A")
            _set_button_state("disabled")

            # ── Kroki 1-3: sprzętowy reset (niezmienione PS_SCRIPT) ────────────
            result = subprocess.run(
                ["powershell", "-Command", PS_SCRIPT],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            _last_reset_time = time.monotonic()

            if result.returncode != 0:
                _set_status("Status: Błąd sprzętowy.", color="#FF453A")
                if not triggered_by_auto:
                    root.after(0, lambda: messagebox.showerror(
                        "Błąd", f"PowerShell zgłosił problem:\n{result.stderr}"))
                return

            # ── Krok 4: poczekaj aż Audiosrv wyliczy endpointy ───────────────
            _set_status("Czekam na endpointy audio…", color="#FFD60A")
            time.sleep(3)

            # ── Krok 5: wymuś słuchawki – 3 metody po kolei ──────────────────
            forced = False

            _set_status("Metoda A: pycaw/COM…", color="#FFD60A")
            forced = _force_headphones_pycaw()

            if not forced:
                _set_status("Metoda B: AudioDeviceCmdlets…", color="#FFD60A")
                forced = _force_headphones_ps_cmdlets()

            if not forced:
                _set_status("Metoda C: nircmd…", color="#FFD60A")
                forced = _force_headphones_nircmd()

            if forced:
                _set_status("Status: Słuchawki ustawione! ✓", color="#30D158")
            else:
                # Wszystkie metody zawiodły, ale reset sprzętowy i tak przywrócił dźwięk
                _set_status("Status: Reset OK (ręcz. wybierz słuchawki)", color="#FF9F0A")

            if not triggered_by_auto:
                msg = ("Kontroler zresetowany i słuchawki ustawione jako domyślne!"
                       if forced else
                       "Kontroler zresetowany.\nJeśli dźwięk jest w głośnikach, kliknij ikonę\n"
                       "głośnika → wybierz słuchawki ręcznie.")
                root.after(0, lambda m=msg: messagebox.showinfo("Sukces", m))

        except Exception as e:
            _set_status("Status: Wyjątek!", color="#FF453A")
            root.after(0, lambda: messagebox.showerror("Błąd", str(e)))
        finally:
            _set_button_state("normal")


def restart_audio(triggered_by_auto: bool = False):
    """Uruchamia reset w osobnym wątku – GUI pozostaje responsywne."""
    if _reset_lock.locked():
        return  # reset już w toku, ignoruj
    t = threading.Thread(target=_run_reset_thread, args=(triggered_by_auto,), daemon=True)
    t.start()


# ─────────────────────────────────────────────────────────────────────────────
# POMOCNICZE: aktualizacja GUI z wątku roboczego (thread-safe)
# ─────────────────────────────────────────────────────────────────────────────
def _set_status(text: str, color: str = "#AEAEB2"):
    root.after(0, lambda: lbl_status.configure(text=text, text_color=color))


def _set_button_state(state: str):
    root.after(0, lambda: btn_repair.configure(state=state))


def _set_auto_badge(active: bool, method: str = ""):
    color  = "#30D158" if active else "#636366"
    symbol = "●" if active else "○"
    label  = f"{symbol} Auto ({method})" if active else f"{symbol} Auto (wył.)"
    root.after(0, lambda: lbl_auto_badge.configure(text=label, text_color=color))


# ─────────────────────────────────────────────────────────────────────────────
# DETEKCJA JACK – rdzeń: odczyt stanu złącza z rejestru Realteka
#
# Na Dell Latitude 5400 bez Waves endpoint "Głośniki/Słuchawki (Realtek(R) Audio)"
# jest JEDEN dla obu urządzeń – Status nigdy się nie zmienia przy wepnięciu.
# Jedyne co się zmienia to klucze w rejestrze sterownika Realteka:
#   HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e96c...}\<numer>\
#     DriverDesc      = "Realtek High Definition Audio"
#     Wartości zmieniające się przy jack-detect:
#       - "JackCtrl"        (DWORD) – maska bitowa stanu złącz
#       - "DevProperties"  (BINARY) – blob konfiguracji
#       - "Settings"       (BINARY) – ustawienia endpointów
#     Bierzemy hash całego podklucza sterownika – każda zmiana = jack event.
# ─────────────────────────────────────────────────────────────────────────────

import winreg
import hashlib

_REALTEK_CLASS_PATH = (
    r"SYSTEM\CurrentControlSet\Control\Class"
    r"\{4d36e96c-e325-11ce-bfc1-08002be10318}"
)

# InstanceId głównego endpointu z wyniku użytkownika
_TARGET_ENDPOINT_ID = "{CD8BD9D5-3EC9-4BC6-82E6-0962BAB2C2A8}"


def _find_realtek_driver_subkey() -> str | None:
    """
    Zwraca ścieżkę do podklucza sterownika Realteka w Class\{4d36e96c...}.
    Szuka podklucza gdzie DriverDesc zawiera 'Realtek'.
    """
    try:
        base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _REALTEK_CLASS_PATH)
        idx = 0
        while True:
            try:
                sub_name = winreg.EnumKey(base, idx)
                sub_path = _REALTEK_CLASS_PATH + "\\" + sub_name
                sub_key  = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub_path)
                try:
                    desc, _ = winreg.QueryValueEx(sub_key, "DriverDesc")
                    if "realtek" in str(desc).lower():
                        winreg.CloseKey(sub_key)
                        winreg.CloseKey(base)
                        return sub_path
                except FileNotFoundError:
                    pass
                winreg.CloseKey(sub_key)
                idx += 1
            except OSError:
                break
        winreg.CloseKey(base)
    except Exception:
        pass
    return None


def _hash_realtek_key(path: str) -> str:
    """
    Zwraca hash wszystkich wartości w podkluczu sterownika Realteka.
    Każda zmiana przy wepnięciu jacka = inny hash.
    """
    h = hashlib.md5()
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
        idx = 0
        while True:
            try:
                name, data, _ = winreg.EnumValue(key, idx)
                h.update(name.encode("utf-8", errors="replace"))
                if isinstance(data, bytes):
                    h.update(data)
                else:
                    h.update(str(data).encode("utf-8", errors="replace"))
                idx += 1
            except OSError:
                break
        winreg.CloseKey(key)
    except Exception:
        pass
    return h.hexdigest()


def _get_jack_state_via_pycaw() -> str | None:
    """
    Używa pycaw/comtypes do odczytania aktualnego domyślnego urządzenia
    odtwarzania. Zmiana ID = ktoś przepiął wyjście (np. Windows wykrył jack).
    Fallback gdy rejestr nie wykrywa zmian.
    """
    try:
        import comtypes
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import IMMDeviceEnumerator, EDataFlow, ERole  # type: ignore
        from pycaw.constants import CLSID_MMDeviceEnumerator            # type: ignore

        enumerator = comtypes.CoCreateInstance(
            CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL
        )
        device = enumerator.GetDefaultAudioEndpoint(
            EDataFlow.eRender.value, ERole.eMultimedia.value
        )
        return device.GetId()
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# COOLDOWN: antyflood
# ─────────────────────────────────────────────────────────────────────────────
def _cooldown_ok() -> bool:
    return (time.monotonic() - _last_reset_time) >= AUTO_RESET_COOLDOWN


# ─────────────────────────────────────────────────────────────────────────────
# GŁÓWNY WATCHER – jeden precyzyjny wątek
#
# Strategia (od najlepszej):
#   1. Hash rejestru sterownika Realteka  – wykrywa zmianę konfiguracji jacka
#   2. pycaw default-device ID            – fallback gdy rejestr nie reaguje
#   Oba sprawdzane co POLLING_INTERVAL_SEC (domyślnie 2s).
#   WMI celowo usunięte – strzelało na każde zdarzenie systemowe, nie tylko jack.
# ─────────────────────────────────────────────────────────────────────────────
_auto_running = False
_auto_thread: threading.Thread | None = None


def _watcher_target():
    realtek_path = _find_realtek_driver_subkey()

    if realtek_path:
        _set_auto_badge(True, "rejestr Realtek")
        method_label = "rejestr"
    else:
        _set_auto_badge(True, "pycaw")
        method_label = "pycaw"

    _set_status(f"Auto: nasłuchuję ({method_label})…", color="#30D158")

    prev_reg_hash   = _hash_realtek_key(realtek_path) if realtek_path else None
    prev_default_id = _get_jack_state_via_pycaw()

    while _auto_running:
        time.sleep(POLLING_INTERVAL_SEC)

        jack_detected = False

        # ── Metoda 1: hash rejestru ──────────────────────────────────────────
        if realtek_path:
            curr_reg_hash = _hash_realtek_key(realtek_path)
            if curr_reg_hash != prev_reg_hash:
                prev_reg_hash = curr_reg_hash
                jack_detected = True

        # ── Metoda 2: zmiana domyślnego urządzenia (pycaw) ──────────────────
        if not jack_detected:
            curr_default_id = _get_jack_state_via_pycaw()
            if curr_default_id and curr_default_id != prev_default_id:
                prev_default_id = curr_default_id
                jack_detected = True

        if jack_detected and _cooldown_ok():
            _set_status("Auto: wykryto zmianę złącza jack! ●", color="#FFD60A")
            restart_audio(triggered_by_auto=True)

    _set_auto_badge(False)
    _set_status("Auto zatrzymane.", color="#636366")


def toggle_auto():
    global _auto_running, _auto_thread

    if not _auto_running:
        _auto_running = True
        _auto_thread = threading.Thread(target=_watcher_target, daemon=True)
        _auto_thread.start()
        btn_auto.configure(text="◼ Zatrzymaj auto", fg_color="#3A3A3C", hover_color="#48484A")
    else:
        _auto_running = False
        _set_auto_badge(False)
        _set_status("Auto zatrzymane.", color="#AEAEB2")
        btn_auto.configure(text="▶ Włącz auto-reset", fg_color="#1C1C1E", hover_color="#2C2C2E",
                           border_color="#0A84FF", border_width=1)


# ─────────────────────────────────────────────────────────────────────────────
# AUTOSTART – wpis w rejestrze HKCU (nie wymaga UAC przy każdym starcie)
# ─────────────────────────────────────────────────────────────────────────────
_AUTOSTART_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_NAME = "AudioFixer"


def _get_autostart_cmd() -> str:
    """Zwraca komendę startową — pythonw.exe żeby nie było okna konsoli."""
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    script = os.path.abspath(sys.argv[0])
    return f'"{pythonw}" "{script}"'


def is_autostart_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY)
        val, _ = winreg.QueryValueEx(key, _AUTOSTART_NAME)
        winreg.CloseKey(key)
        return val == _get_autostart_cmd()
    except Exception:
        return False


def set_autostart(enable: bool):
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY,
            0, winreg.KEY_SET_VALUE
        )
        if enable:
            winreg.SetValueEx(key, _AUTOSTART_NAME, 0, winreg.REG_SZ, _get_autostart_cmd())
        else:
            try:
                winreg.DeleteValue(key, _AUTOSTART_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        messagebox.showerror("Błąd autostartu", str(e))


# ─────────────────────────────────────────────────────────────────────────────
# IKONA TRAY – rysowana programowo (nie potrzeba pliku .ico)
# ─────────────────────────────────────────────────────────────────────────────
def _make_tray_icon(active: bool = False) -> "PIL.Image.Image":
    """
    Rysuje ikonę 64×64: ciemne koło z falą dźwiękową.
    active=True → niebieska (auto włączone), False → szara.
    """
    from PIL import Image, ImageDraw
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d    = ImageDraw.Draw(img)

    # Tło – koło
    bg = (10, 132, 255, 255) if active else (60, 60, 67, 255)
    d.ellipse([2, 2, size - 2, size - 2], fill=bg)

    # Głośnik – prostokąt + trójkąt
    w = "#FFFFFF"
    d.rectangle([14, 24, 22, 40], fill=w)
    d.polygon([(22, 24), (34, 14), (34, 50), (22, 40)], fill=w)

    # Fale dźwiękowe
    for r, a in [(8, 200), (14, 140), (20, 80)]:
        d.arc([34 - r, 32 - r, 34 + r, 32 + r], -60, 60,
              fill=(255, 255, 255, a), width=3)

    return img


# ─────────────────────────────────────────────────────────────────────────────
# GUI
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        if not is_admin():
            import ctypes as _ct
            script_path = os.path.abspath(sys.argv[0])
            script_dir  = os.path.dirname(script_path)
            params      = f'"{script_path}"'
            _ct.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, script_dir, 1)
            sys.exit()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # ── Okno główne (pojawia się od razu, minimalizacja → tray) ─────────────
        root = ctk.CTk()
        root.title("Audio Fixer")
        root.geometry("380x300")
        root.resizable(False, False)
        root.eval("tk::PlaceWindow . center")

        frame = ctk.CTkFrame(root, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=30, pady=22)

        lbl_title = ctk.CTkLabel(
            frame, text="Sprzętowy Reset Audio",
            font=("Segoe UI", 16, "bold"), text_color="#FFFFFF")
        lbl_title.pack(pady=(0, 2))

        lbl_sub = ctk.CTkLabel(
            frame, text="Intel Smart Sound & Realtek Controller",
            font=("Segoe UI", 11), text_color="#8E8E93")
        lbl_sub.pack(pady=(0, 16))

        btn_repair = ctk.CTkButton(
            frame, text="Restartuj kontroler",
            command=lambda: restart_audio(triggered_by_auto=False),
            font=("Segoe UI", 12, "bold"),
            fg_color="#0A84FF", hover_color="#0066CC",
            corner_radius=10, height=38)
        btn_repair.pack(fill="x", pady=(0, 8))

        btn_auto = ctk.CTkButton(
            frame, text="▶ Włącz auto-reset",
            command=toggle_auto,
            font=("Segoe UI", 11),
            fg_color="#1C1C1E", hover_color="#2C2C2E",
            border_color="#0A84FF", border_width=1,
            corner_radius=10, height=34)
        btn_auto.pack(fill="x", pady=(0, 8))

        # ── Przycisk autostartu ───────────────────────────────────────────────
        _autostart_var = tk.BooleanVar(value=is_autostart_enabled())

        def _on_autostart_toggle():
            set_autostart(_autostart_var.get())

        btn_autostart = ctk.CTkCheckBox(
            frame, text="Uruchamiaj z Windows",
            variable=_autostart_var, command=_on_autostart_toggle,
            font=("Segoe UI", 11), text_color="#AEAEB2",
            fg_color="#0A84FF", hover_color="#0066CC",
            checkmark_color="#FFFFFF", border_color="#636366",
            corner_radius=4)
        btn_autostart.pack(anchor="w", pady=(0, 10))

        lbl_auto_badge = ctk.CTkLabel(
            frame, text="○ Auto (wył.)",
            font=("Segoe UI", 10), text_color="#636366")
        lbl_auto_badge.pack(pady=(0, 4))

        lbl_status = ctk.CTkLabel(
            frame, text="Wepnij słuchawki i kliknij przycisk.",
            font=("Segoe UI", 11), text_color="#AEAEB2")
        lbl_status.pack()

        lbl_status.config = lbl_status.configure

        # ── Tray: pokaż / ukryj okno ─────────────────────────────────────────
        _window_visible = True

        def show_window():
            global _window_visible
            _window_visible = True
            root.after(0, lambda: (root.deiconify(), root.lift(), root.focus_force()))

        def hide_window():
            global _window_visible
            _window_visible = False
            root.after(0, root.withdraw)

        def toggle_window():
            if _window_visible:
                hide_window()
            else:
                show_window()

        def on_close():
            """X w oknie → schowaj do traya (nie zamykaj)."""
            hide_window()

        def on_minimize(event):
            """Minimalizacja → schowaj do traya zamiast paska zadań."""
            if root.state() == "iconic":
                hide_window()

        root.protocol("WM_DELETE_WINDOW", on_close)
        root.bind("<Unmap>", on_minimize)

        # ── Tray: aktualizacja ikony przy toggle auto ─────────────────────────
        _tray_icon_ref = None   # wypełnione poniżej

        _orig_toggle_auto = toggle_auto

        def toggle_auto_with_tray():
            _orig_toggle_auto()
            # Odśwież ikonę traya
            if _tray_icon_ref is not None:
                try:
                    _tray_icon_ref.icon = _make_tray_icon(active=_auto_running)
                except Exception:
                    pass

        btn_auto.configure(command=toggle_auto_with_tray)

        # ── Tray: buduj i uruchom w osobnym wątku ────────────────────────────
        def _build_tray():
            global _tray_icon_ref
            try:
                import pystray  # type: ignore

                def _tray_show(icon, item):
                    show_window()

                def _tray_reset(icon, item):
                    restart_audio(triggered_by_auto=False)

                def _tray_toggle_auto(icon, item):
                    toggle_auto_with_tray()

                def _tray_autostart(icon, item):
                    new_val = not is_autostart_enabled()
                    set_autostart(new_val)
                    root.after(0, lambda: _autostart_var.set(new_val))

                def _tray_quit(icon, item):
                    global _auto_running
                    _auto_running = False
                    icon.stop()
                    root.after(0, root.destroy)

                def _autostart_checked(item):
                    return is_autostart_enabled()

                def _auto_checked(item):
                    return _auto_running

                menu = pystray.Menu(
                    pystray.MenuItem("Pokaż okno",        _tray_show, default=True),
                    pystray.MenuItem("Resetuj teraz",     _tray_reset),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Auto-reset",        _tray_toggle_auto,
                                     checked=_auto_checked),
                    pystray.MenuItem("Start z Windows",   _tray_autostart,
                                     checked=_autostart_checked),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Zamknij",           _tray_quit),
                )

                icon = pystray.Icon(
                    "AudioFixer",
                    icon=_make_tray_icon(active=False),
                    title="Audio Fixer",
                    menu=menu,
                )
                _tray_icon_ref = icon
                icon.run()          # blokuje ten wątek
            except ImportError:
                # pystray niedostępne – pokaż okno normalnie
                root.after(0, show_window)
            except Exception:
                root.after(0, show_window)

        tray_thread = threading.Thread(target=_build_tray, daemon=True)
        tray_thread.start()

        root.mainloop()

    except Exception as global_error:
        root_err = tk.Tk()
        root_err.withdraw()
        messagebox.showerror("Błąd krytyczny", f"Problem:\n{global_error}")
