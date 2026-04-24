import copy
import hashlib
import json
import threading
import time
from pathlib import Path

import keyboard
import pyautogui
import pygetwindow as gw
import pyperclip

# =========================
# CONFIG GENERAL
# =========================
CONFIG_PATH = Path("bridge_config.json")
LOG_PATH = Path("bridge_log.txt")
SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    "cursor_window_title_hint": "Cursor",
    "cursor_window_title_hints": ["Cursor"],
    # ChatGPT: solo píxeles de pantalla (F4/F6). No se busca ni activa ventana del navegador.
    "chatgpt_screen_coordinates_only": True,
    # Antes de Copy: clic en la caja F6 + teclas End (baja el hilo; el Copy queda estable respecto al input).
    "chatgpt_scroll_before_copy_screen_mode": True,
    # F4 guarda offset (dx,dy) desde la caja F6 hasta el Copy (recomendado si el botón se mueve).
    "chatgpt_copy_use_offset_from_input": True,
    # Plantillas PNG en la carpeta del script (chatgpt_copy.png / cursor_copy.png).
    "chatgpt_copy_image_search": True,
    "chatgpt_copy_image_path": "chatgpt_copy.png",
    "chatgpt_copy_image_confidence": 0.75,
    "cursor_copy_image_search": True,
    "cursor_copy_image_path": "cursor_copy.png",
    "cursor_copy_image_confidence": 0.75,
    "chatgpt_select_rightmost_browser": True,
    "chatgpt_rightmost_browser_fallback": True,
    "chatgpt_window_title_hint": "ChatGPT",
    "chatgpt_window_title_hints": [],
    "chatgpt_browser_title_markers": [
        "google chrome",
        "microsoft edge",
        "brave",
        "opera",
        "chromium",
        "firefox",
        "waterfox",
        "vivaldi",
    ],
    "chatgpt_fallback_min_width": 400,
    "chatgpt_fallback_min_height": 350,
    "poll_interval_sec": 2.0,
    "settle_interval_sec": 1.2,
    "stable_reads_required": 3,
    "clipboard_wait_sec": 2.5,
    "max_round_trips": 50,
    "post_send_pause_sec": 1.5,
    "use_window_activation": True,
    "scroll_before_copy": True,
    "scroll_focus_click_enabled": True,
    "scroll_focus_click_pause_sec": 0.12,
    "scroll_focus_relative_default": [0.5, 0.42],
    "scroll_focus_relative_overrides": {
        "Cursor": [0.14, 0.42],
        "ChatGPT": [0.52, 0.42],
        "Google Chrome": [0.52, 0.42],
        "Microsoft Edge": [0.52, 0.42],
        "Brave": [0.52, 0.42],
        "Firefox": [0.52, 0.42],
    },
    "scroll_end_key_repeat": 12,
    "scroll_between_key_pause_sec": 0.05,
    "scroll_use_ctrl_end": True,
    "scroll_after_bottom_pause_sec": 0.45,
    "positions": {
        "cursor_copy": None,
        "cursor_input": None,
        "chatgpt_copy": None,
        "chatgpt_input": None,
    },
}

paused = True
running = True
config_lock = threading.Lock()

last_cursor_forwarded_hash = None
last_chatgpt_forwarded_hash = None
round_trips = 0

_chatgpt_rightmost_logged_title: str | None = None


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str):
    line = f"[{now()}] {msg}"
    print(line)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def merge_config_defaults(user: dict, default: dict) -> dict:
    result = copy.deepcopy(default)
    if not user:
        return result
    for key, u_val in user.items():
        if key not in result:
            result[key] = copy.deepcopy(u_val) if isinstance(u_val, dict) else u_val
            continue
        d_val = result[key]
        if isinstance(d_val, dict) and isinstance(u_val, dict):
            result[key] = merge_config_defaults(u_val, d_val)
        else:
            result[key] = u_val
    return result


def load_config():
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("La config debe ser un objeto JSON (dict).")
            return merge_config_defaults(raw, DEFAULT_CONFIG)
        except Exception as e:
            log(f"Error leyendo config, se usará la config por defecto: {e}")
    return copy.deepcopy(DEFAULT_CONFIG)


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Config guardada en {CONFIG_PATH.resolve()}")


config = load_config()


def get_cfg(key, default=None):
    with config_lock:
        return config.get(key, default)


def chatgpt_screen_pixels_only() -> bool:
    return bool(get_cfg("chatgpt_screen_coordinates_only", True))


def get_position(name: str):
    with config_lock:
        return config["positions"].get(name)


def _position_entry_ready(val) -> bool:
    if val is None:
        return False
    if isinstance(val, dict) and val.get("mode") == "relative":
        try:
            float(val["rx"])
            float(val["ry"])
            return True
        except (KeyError, TypeError, ValueError):
            return False
    if isinstance(val, dict) and val.get("mode") == "offset_screen":
        try:
            int(val["dx"])
            int(val["dy"])
            return True
        except (KeyError, TypeError, ValueError):
            return False
    if isinstance(val, (list, tuple)) and len(val) == 2:
        try:
            int(val[0])
            int(val[1])
            return True
        except (TypeError, ValueError):
            return False
    return False


def _window_hints_from_cfg(list_key: str, single_key: str, default_single: str) -> list[str]:
    lst = get_cfg(list_key)
    if isinstance(lst, list) and len(lst) > 0:
        out = [str(x).strip() for x in lst if str(x).strip()]
        if out:
            return out
    s = get_cfg(single_key, default_single)
    return [str(s).strip()] if s else []


def cursor_window_hints() -> list[str]:
    return _window_hints_from_cfg("cursor_window_title_hints", "cursor_window_title_hint", "Cursor")


def chatgpt_window_hints() -> list[str]:
    return _window_hints_from_cfg("chatgpt_window_title_hints", "chatgpt_window_title_hint", "ChatGPT")


def slot_window_hints(slot: str) -> list[str]:
    if slot.startswith("cursor"):
        return cursor_window_hints()
    if slot.startswith("chatgpt"):
        return chatgpt_window_hints()
    raise ValueError(f"Slot de posición desconocido: {slot}")


def window_role_for_slot(slot: str) -> str:
    return "chatgpt" if slot.startswith("chatgpt") else "cursor"


def find_window_first_match(hints: list[str]):
    if not hints:
        return None
    for hint in hints:
        try:
            wins = gw.getWindowsWithTitle(hint)
            if wins:
                return wins[0]
        except Exception:
            continue
    try:
        for w in gw.getAllWindows():
            title = (getattr(w, "title", None) or "").strip()
            if len(title) < 2:
                continue
            tl = title.lower()
            for hint in hints:
                hl = str(hint).lower()
                if hl and hl in tl:
                    return w
    except Exception:
        pass
    return None


def _browser_title_markers_lower() -> list[str]:
    raw = get_cfg("chatgpt_browser_title_markers")
    if isinstance(raw, list) and raw:
        return [str(x).lower().strip() for x in raw if str(x).strip()]
    return [
        "google chrome",
        "microsoft edge",
        "brave",
        "opera",
        "chromium",
        "firefox",
        "waterfox",
        "vivaldi",
    ]


def chatgpt_use_rightmost_browser_only() -> bool:
    v = get_cfg("chatgpt_select_rightmost_browser", None)
    if v is not None:
        return bool(v)
    return bool(get_cfg("chatgpt_rightmost_browser_fallback", True))


def find_chatgpt_window_rightmost_browser():
    if not chatgpt_use_rightmost_browser_only():
        return None
    markers = _browser_title_markers_lower()
    min_w = int(get_cfg("chatgpt_fallback_min_width", 400))
    min_h = int(get_cfg("chatgpt_fallback_min_height", 350))
    candidates = []
    try:
        for w in gw.getAllWindows():
            title = (getattr(w, "title", None) or "").strip()
            if len(title) < 8:
                continue
            tl = title.lower()
            if "cursor" in tl:
                continue
            if not any(m in tl for m in markers):
                continue
            try:
                left = int(w.left)
                ww = int(w.width)
                wh = int(w.height)
            except Exception:
                continue
            if ww < min_w or wh < min_h:
                continue
            cx = left + ww / 2
            candidates.append((cx, w))
    except Exception:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def resolve_target_window(hints: list[str], role: str):
    global _chatgpt_rightmost_logged_title
    if role == "chatgpt":
        if chatgpt_use_rightmost_browser_only():
            win = find_chatgpt_window_rightmost_browser()
            if win:
                t = getattr(win, "title", "") or ""
                if t != _chatgpt_rightmost_logged_title:
                    _chatgpt_rightmost_logged_title = t
                    log(f"ChatGPT: ventana = navegador más a la derecha: {t!r}")
                return win
            return None
        return find_window_first_match(hints)

    return find_window_first_match(hints)


def resolve_position_to_pixels(name: str) -> tuple[int, int]:
    raw = get_position(name)
    if not _position_entry_ready(raw):
        raise RuntimeError(f"No hay coordenadas calibradas válidas para '{name}'")

    if name.startswith("chatgpt") and chatgpt_screen_pixels_only():
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            return int(raw[0]), int(raw[1])
        if isinstance(raw, dict) and raw.get("mode") == "offset_screen":
            if name != "chatgpt_copy":
                raise RuntimeError("offset_screen solo aplica a chatgpt_copy.")
            ix, iy = resolve_position_to_pixels("chatgpt_input")
            return int(ix + int(raw["dx"])), int(iy + int(raw["dy"]))
        if isinstance(raw, dict) and raw.get("mode") == "relative":
            raise RuntimeError(
                f"{name}: calibración antigua (relativa a ventana). Pulsa F4 o F6 de nuevo para guardar "
                "píxeles de pantalla fijos (modo chatgpt_screen_coordinates_only)."
            )
        raise RuntimeError(f"Formato de posición no válido para '{name}'.")

    if isinstance(raw, dict) and raw.get("mode") == "relative":
        hints = slot_window_hints(name)
        role = window_role_for_slot(name)
        win = resolve_target_window(hints, role)
        if not win:
            if role == "chatgpt":
                raise RuntimeError(
                    f"No hay ventana de navegador reconocida para '{name}'. "
                    "Pon chatgpt_screen_coordinates_only en true y recalibra F4/F6, "
                    "o revisa chatgpt_browser_title_markers / ventana a la derecha."
                )
            raise RuntimeError(
                f"No hay ventana visible que coincida con hints={hints!r} para '{name}'. "
                "Ajusta cursor_window_title_hint o cursor_window_title_hints en bridge_config.json."
            )
        w = max(int(win.width), 1)
        h = max(int(win.height), 1)
        rx = float(raw["rx"])
        ry = float(raw["ry"])
        x = int(win.left + rx * w)
        y = int(win.top + ry * h)
        return x, y

    x = int(raw[0])
    y = int(raw[1])
    return x, y


def set_position(name: str, stored):
    with config_lock:
        if isinstance(stored, dict):
            config["positions"][name] = stored
        else:
            config["positions"][name] = [int(stored[0]), int(stored[1])]
        save_config(config)
        val = config["positions"][name]
        if isinstance(val, dict) and val.get("mode") == "relative":
            log(f"Posición guardada: {name} (relativo rx={float(val['rx']):.4f}, ry={float(val['ry']):.4f})")
        elif isinstance(val, dict) and val.get("mode") == "offset_screen":
            log(f"Posición guardada: {name} (offset desde chatgpt_input dx={int(val['dx'])}, dy={int(val['dy'])})")
        else:
            log(f"Posición guardada: {name} -> píxeles absolutos {val}")


def all_positions_ready() -> bool:
    with config_lock:
        pos = config["positions"]
        return all(_position_entry_ready(pos.get(k)) for k in ("cursor_copy", "cursor_input", "chatgpt_copy", "chatgpt_input"))


def click_pos(name: str):
    x, y = resolve_position_to_pixels(name)
    pyautogui.click(x, y)


def activate_window_hints(hints: list[str], *, role: str) -> bool:
    with config_lock:
        use_activation = bool(config.get("use_window_activation", True))

    if not use_activation:
        return True

    win = resolve_target_window(hints, role)
    if not win:
        log(f"No encontré ventana (hints={hints!r}, role={role})")
        return False

    try:
        if win.isMinimized:
            win.restore()
            time.sleep(0.5)
        win.activate()
        time.sleep(0.8)
        return True
    except Exception as e:
        log(f"No pude activar ventana (título ~{getattr(win, 'title', '?')!r}): {e}")
        return False


def _scroll_focus_relative_for_hint(win_title: str) -> tuple[float, float]:
    overrides = get_cfg("scroll_focus_relative_overrides") or {}
    if isinstance(overrides, dict):
        for needle, pair in overrides.items():
            if (
                isinstance(pair, (list, tuple))
                and len(pair) == 2
                and str(needle).lower() in str(win_title).lower()
            ):
                return float(pair[0]), float(pair[1])
    d = get_cfg("scroll_focus_relative_default", [0.5, 0.42])
    return float(d[0]), float(d[1])


def scroll_chat_to_bottom_hints(hints: list[str], *, role: str) -> None:
    if not bool(get_cfg("scroll_before_copy", True)):
        return

    win = resolve_target_window(hints, role)
    if not win:
        return

    w = max(int(win.width), 1)
    h = max(int(win.height), 1)
    win_title = getattr(win, "title", "") or ""
    rx, ry = _scroll_focus_relative_for_hint(win_title)

    if bool(get_cfg("scroll_focus_click_enabled", True)):
        fx = int(win.left + rx * w)
        fy = int(win.top + ry * h)
        pyautogui.click(fx, fy)
        time.sleep(float(get_cfg("scroll_focus_click_pause_sec", 0.12)))

    presses = max(0, int(get_cfg("scroll_end_key_repeat", 12)))
    pause = float(get_cfg("scroll_between_key_pause_sec", 0.05))
    for _ in range(presses):
        pyautogui.press("end")
        time.sleep(pause)

    if bool(get_cfg("scroll_use_ctrl_end", True)):
        pyautogui.hotkey("ctrl", "end")
        time.sleep(pause)

    time.sleep(float(get_cfg("scroll_after_bottom_pause_sec", 0.45)))


def clear_clipboard():
    pyperclip.copy("")
    time.sleep(0.05)


def read_clipboard() -> str:
    try:
        return pyperclip.paste() or ""
    except Exception:
        return ""


def _template_image_path(cfg_key: str, default_name: str) -> Path:
    name = str(get_cfg(cfg_key, default_name))
    p = Path(name)
    if p.is_absolute():
        return p
    for base in (Path.cwd(), SCRIPT_DIR):
        cand = base / p
        if cand.is_file():
            return cand
    return SCRIPT_DIR / p


def _try_click_copy_icon(template_path: Path, confidence: float) -> bool:
    if not template_path.is_file():
        return False
    try:
        loc = pyautogui.locateCenterOnScreen(str(template_path), confidence=confidence)
        if loc is not None:
            x = int(getattr(loc, "x", loc[0]))
            y = int(getattr(loc, "y", loc[1]))
            pyautogui.click(x, y)
            return True
    except Exception:
        try:
            loc = pyautogui.locateCenterOnScreen(str(template_path))
            if loc is not None:
                x = int(getattr(loc, "x", loc[0]))
                y = int(getattr(loc, "y", loc[1]))
                pyautogui.click(x, y)
                return True
        except Exception:
            pass
    return False


def _try_click_chatgpt_copy_from_image() -> bool:
    if not bool(get_cfg("chatgpt_copy_image_search", True)):
        return False
    path = _template_image_path("chatgpt_copy_image_path", "chatgpt_copy.png")
    conf = float(get_cfg("chatgpt_copy_image_confidence", 0.75))
    return _try_click_copy_icon(path, conf)


def _try_click_cursor_copy_from_image() -> bool:
    if not bool(get_cfg("cursor_copy_image_search", True)):
        return False
    path = _template_image_path("cursor_copy_image_path", "cursor_copy.png")
    conf = float(get_cfg("cursor_copy_image_confidence", 0.75))
    return _try_click_copy_icon(path, conf)


def _chatgpt_screen_scroll_to_bottom():
    """Enfoca la caja de ChatGPT (F6) y baja el hilo con teclas, sin usar pygetwindow."""
    if not bool(get_cfg("chatgpt_scroll_before_copy_screen_mode", True)):
        return
    try:
        ix, iy = resolve_position_to_pixels("chatgpt_input")
    except Exception as e:
        log(f"ChatGPT: no pude resolver chatgpt_input para scroll antes de Copy ({e})")
        return
    pyautogui.click(ix, iy)
    time.sleep(float(get_cfg("scroll_focus_click_pause_sec", 0.12)))
    presses = max(0, int(get_cfg("scroll_end_key_repeat", 12)))
    pause = float(get_cfg("scroll_between_key_pause_sec", 0.05))
    for _ in range(presses):
        pyautogui.press("end")
        time.sleep(pause)
    if bool(get_cfg("scroll_use_ctrl_end", True)):
        pyautogui.hotkey("ctrl", "end")
        time.sleep(pause)
    time.sleep(float(get_cfg("scroll_after_bottom_pause_sec", 0.45)))


def click_chatgpt_copy_screen_mode():
    _chatgpt_screen_scroll_to_bottom()
    if _try_click_chatgpt_copy_from_image():
        return
    click_pos("chatgpt_copy")


def copy_via_button(window_hints: list[str], copy_pos_name: str) -> str:
    role = window_role_for_slot(copy_pos_name)
    chatgpt_pixels = role == "chatgpt" and chatgpt_screen_pixels_only()
    if not chatgpt_pixels:
        if not activate_window_hints(window_hints, role=role):
            return ""
        scroll_chat_to_bottom_hints(window_hints, role=role)

    before = read_clipboard()
    clear_clipboard()

    try:
        if chatgpt_pixels and copy_pos_name == "chatgpt_copy":
            click_chatgpt_copy_screen_mode()
        elif copy_pos_name == "cursor_copy" and _try_click_cursor_copy_from_image():
            pass
        else:
            click_pos(copy_pos_name)
    except Exception as e:
        log(f"Error haciendo click en {copy_pos_name}: {e}")
        return ""

    wait_until = time.time() + float(get_cfg("clipboard_wait_sec", 2.5))
    while time.time() < wait_until:
        current = read_clipboard()
        if current.strip() and current != before:
            return current.strip()
        time.sleep(0.1)

    current = read_clipboard()
    if current.strip():
        return current.strip()
    return ""


def send_text(window_hints: list[str], input_pos_name: str, text: str) -> bool:
    if not text.strip():
        log(f"No se enviará texto vacío a hints={window_hints!r}")
        return False

    role = window_role_for_slot(input_pos_name)
    if not (role == "chatgpt" and chatgpt_screen_pixels_only()):
        if not activate_window_hints(window_hints, role=role):
            return False

    try:
        click_pos(input_pos_name)
    except Exception as e:
        log(f"Error haciendo click en {input_pos_name}: {e}")
        return False

    pyperclip.copy(text)
    time.sleep(0.1)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.15)
    pyautogui.press("enter")

    pause = float(get_cfg("post_send_pause_sec", 1.5))
    time.sleep(pause)
    return True


def wait_for_stable_message(source_name: str, window_hints: list[str], copy_pos_name: str, last_forwarded_hash: str | None):
    poll_interval = float(get_cfg("poll_interval_sec", 2.0))
    settle_interval = float(get_cfg("settle_interval_sec", 1.2))
    stable_needed = int(get_cfg("stable_reads_required", 3))

    stable_count = 0
    candidate_text = ""
    candidate_hash = None

    while running and not paused:
        text = copy_via_button(window_hints, copy_pos_name)

        if not text.strip():
            stable_count = 0
            candidate_text = ""
            candidate_hash = None
            time.sleep(poll_interval)
            continue

        current_hash = sha(text)

        if current_hash == last_forwarded_hash:
            stable_count = 0
            candidate_text = ""
            candidate_hash = None
            time.sleep(poll_interval)
            continue

        if candidate_hash == current_hash:
            stable_count += 1
            log(f"{source_name}: lectura estable {stable_count}/{stable_needed}")
        else:
            candidate_text = text
            candidate_hash = current_hash
            stable_count = 1
            log(f"{source_name}: nuevo candidato detectado, esperando estabilidad...")

        if stable_count >= stable_needed:
            log(f"{source_name}: mensaje estable confirmado.")
            return candidate_text, candidate_hash

        time.sleep(settle_interval)

    return None, None


def capture_pos(label: str):
    mx, my = pyautogui.position()
    role = window_role_for_slot(label)

    if label.startswith("chatgpt") and chatgpt_screen_pixels_only():
        if label == "chatgpt_input":
            set_position(label, (int(mx), int(my)))
            return
        if label == "chatgpt_copy":
            if bool(get_cfg("chatgpt_copy_use_offset_from_input", True)):
                if not _position_entry_ready(get_position("chatgpt_input")):
                    log("Primero F6 (chatgpt_input). Guardando Copy como píxeles absolutos de pantalla.")
                    set_position(label, (int(mx), int(my)))
                else:
                    ix, iy = resolve_position_to_pixels("chatgpt_input")
                    set_position(label, {"mode": "offset_screen", "dx": int(mx - ix), "dy": int(my - iy)})
            else:
                set_position(label, (int(mx), int(my)))
            return
        set_position(label, (int(mx), int(my)))
        return

    hints = slot_window_hints(label)
    win = resolve_target_window(hints, role)
    if not win:
        log(
            f"No encontré ventana que coincida con hints={hints!r} para calibrar '{label}'. "
            "Ajusta cursor_window_title_hint o cursor_window_title_hints en bridge_config.json."
        )
        return

    w = max(int(win.width), 1)
    h = max(int(win.height), 1)
    rx = (mx - win.left) / w
    ry = (my - win.top) / h

    if not (win.left <= mx <= win.left + w and win.top <= my <= win.top + h):
        log(
            f"Aviso: el cursor no estaba dentro del rectángulo de la ventana encontrada "
            f"({getattr(win, 'title', '')!r}) al calibrar {label}. "
            f"Relativo guardado rx={rx:.4f}, ry={ry:.4f} (puede fallar si era otra ventana)."
        )

    set_position(label, {"mode": "relative", "rx": rx, "ry": ry})


def hotkey_set_cursor_copy():
    log("Capturando posición actual del ratón para cursor_copy")
    capture_pos("cursor_copy")


def hotkey_set_cursor_input():
    log("Capturando posición actual del ratón para cursor_input")
    capture_pos("cursor_input")


def hotkey_set_chatgpt_copy():
    log("Capturando chatgpt_copy (recomendado: F6 primero; F4 guarda offset desde la caja al icono Copy)")
    capture_pos("chatgpt_copy")


def hotkey_set_chatgpt_input():
    log("Capturando posición actual del ratón para chatgpt_input")
    capture_pos("chatgpt_input")


def toggle_pause():
    global paused
    paused = not paused
    log("PUENTE REANUDADO" if not paused else "PUENTE PAUSADO")


def stop_script():
    global running
    running = False
    log("DETENCIÓN solicitada por hotkey.")


def bridge_loop():
    global last_cursor_forwarded_hash
    global last_chatgpt_forwarded_hash
    global round_trips
    global paused

    log("Bridge loop iniciado.")
    while running:
        if paused:
            time.sleep(0.25)
            continue

        if not all_positions_ready():
            if not paused:
                paused = True
                log(
                    "Faltan coordenadas calibradas. Puente pausado automáticamente; "
                    "calibra con F2-F4 y F6, luego F8 para reanudar."
                )
            time.sleep(1.0)
            continue

        max_rts = int(get_cfg("max_round_trips", 50))
        if round_trips >= max_rts:
            if not paused:
                paused = True
                log(
                    f"Se alcanzó el máximo de intercambios ({max_rts}). Puente pausado; "
                    "F8 para reanudar o sube max_round_trips en bridge_config.json."
                )
            time.sleep(1.0)
            continue

        cursor_text, cursor_hash = wait_for_stable_message(
            source_name="Cursor",
            window_hints=cursor_window_hints(),
            copy_pos_name="cursor_copy",
            last_forwarded_hash=last_cursor_forwarded_hash,
        )

        if not running or paused:
            continue

        if cursor_text and cursor_hash and cursor_hash != last_cursor_forwarded_hash:
            log("Reenviando Cursor -> ChatGPT")
            ok = send_text(
                window_hints=chatgpt_window_hints(),
                input_pos_name="chatgpt_input",
                text=cursor_text,
            )
            if ok:
                last_cursor_forwarded_hash = cursor_hash
                log("Cursor -> ChatGPT completado")
            else:
                log("Falló Cursor -> ChatGPT")
                time.sleep(1.0)
                continue

        if not running or paused:
            continue

        chatgpt_text, chatgpt_hash = wait_for_stable_message(
            source_name="ChatGPT",
            window_hints=chatgpt_window_hints(),
            copy_pos_name="chatgpt_copy",
            last_forwarded_hash=last_chatgpt_forwarded_hash,
        )

        if not running or paused:
            continue

        if chatgpt_text and chatgpt_hash and chatgpt_hash != last_chatgpt_forwarded_hash:
            log("Reenviando ChatGPT -> Cursor")
            ok = send_text(
                window_hints=cursor_window_hints(),
                input_pos_name="cursor_input",
                text=chatgpt_text,
            )
            if ok:
                last_chatgpt_forwarded_hash = chatgpt_hash
                round_trips += 1
                log(f"ChatGPT -> Cursor completado | round_trip={round_trips}")
            else:
                log("Falló ChatGPT -> Cursor")
                time.sleep(1.0)

        time.sleep(0.4)

    log("Bridge loop finalizado.")


def print_banner():
    print("\n" + "=" * 70)
    print(" CURSOR <-> CHATGPT BRIDGE ")
    print("=" * 70)
    print("Cursor: F2/F3 relativos a la ventana Cursor.")
    print("ChatGPT pantalla: F6 caja de texto; F4 offset del botón Copy respecto a F6 (se recalcula tras scroll).")
    print("  Copy por imagen: chatgpt_copy.png (ChatGPT) y cursor_copy.png (Cursor) junto a orquestador.py.")
    print("Hotkeys:")
    print("  F2  = cursor_copy   |  F3  = cursor_input")
    print("  F4  = chatgpt_copy (tras F6) |  F6  = chatgpt_input")
    print("  F8  = pausar / reanudar   |  F12 = detener")
    print("=" * 70 + "\n")


def main():
    pyautogui.FAILSAFE = True

    print_banner()
    log("Script iniciado.")
    log(f"Archivo de log: {LOG_PATH.resolve()}")
    log(f"Archivo de config: {CONFIG_PATH.resolve()}")

    keyboard.add_hotkey("F2", hotkey_set_cursor_copy)
    keyboard.add_hotkey("F3", hotkey_set_cursor_input)
    keyboard.add_hotkey("F4", hotkey_set_chatgpt_copy)
    keyboard.add_hotkey("F6", hotkey_set_chatgpt_input)
    keyboard.add_hotkey("F8", toggle_pause)
    keyboard.add_hotkey("F12", stop_script)

    worker = threading.Thread(target=bridge_loop, daemon=True)
    worker.start()

    try:
        while running:
            time.sleep(0.2)
    except KeyboardInterrupt:
        stop_script()

    log("Esperando salida limpia...")
    worker.join(timeout=3.0)
    log("Programa terminado.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"Error fatal: {e}")
        raise
